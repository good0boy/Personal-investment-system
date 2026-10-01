"""同花顺 fuyao REST 客户端 —— 本项目的【特色数据源】。

来源：`https://fuyao.aicubes.cn`（同花顺官方金融数据 REST API，需 `X-api-key`）

★ 为什么会有这个源（2026-10-01 实测）
------------------------------------
东财 `push2.eastmoney.com` / `push2his.eastmoney.com` 在本机被**网络掐断**
（`RemoteDisconnected`）—— 概念/行业板块、板块成分、全市场快照、个股日线
全在这两个域名上，等于整块不可用。而 akshare 自带的同花顺接口（`*_ths`）
与同花顺自己的 REST 都通。于是：

    板块列表/历史  → akshare 内置 `*_ths`（免费，见 catalog 的 ths 条目）
    板块成分股     → 本客户端（akshare **没有** `*_ths` 成分股接口）
    涨跌停/连板池  → 本客户端（可按**历史交易日**取，补东财只留最近 1~2 日的短板）
    全市场快照     → 本客户端（`/api/a-share/prices/snapshot`）
    全市场代码表   → 本客户端（`/api/meta/tickers/list`）

★★ 三条必须记住的契约
--------------------
1. **业务错误也返回 HTTP 200**，靠信封里的 `code` 判断（0 = 成功）。
   `resp.raise_for_status()` 对业务错误**无效**，别依赖它。
2. **时间戳一律毫秒级 Unix**（Asia/Shanghai 零点）。
3. 代码用 **thscode**（`600519.SH` / 板块 `886042.TI`），**不接受纯 ticker**。

★ 错误码映射（决定"什么该熔断"）
------------------------------
================ ============================================ ==========
code             映射                                          是否熔断
================ ============================================ ==========
0                成功                                          -
2001 / 2003      未认证 / 无权限 → `NotConfiguredError`         **否**
4001 / HTTP 429  频率超限 → `DataSourceRateLimited(definite)`   **是**
其它非 0（3002/  一律 `DataSourceError`（`detail.fuyao_code`   否
3004/5003…）     留痕），不做归因猜测
================ ============================================ ==========

⚠️ **只有 4001 / 429 才算限频**。本项目刚被"把连接被掐断误判成限频 →
   熔断器把整个数据源封掉"坑过一次（详见 `line_groups.py` 的说明），
   这里绝不重蹈：拿不准的错误码一律当普通错误，不触发熔断。

★ 密钥纪律
----------
Key 只从 `.env` 的 `FUYAO_API_KEY` 读（经 `EnvSettings`），**只放进 `X-api-key`
请求头**，绝不进 `params`（那会随 `datasource_log` 与 `/system/config` 泄露）。
"""
from __future__ import annotations

import time
from typing import Any

import httpx
import pandas as pd

from app.core.config import settings
from app.core.errors import (
    AppError,
    DataSourceError,
    DataSourceRateLimited,
    DataSourceTimeout,
    InvalidParamError,
    NotConfiguredError,
    NotFoundError,
)
from app.services.data_source.base import (
    INDEX_COLUMNS,
    QUOTE_COLUMNS,
    BaseDataSource,
    DataSourceStatus,
    coerce_numeric,
    ensure_columns,
    normalize_date,
    raise_if_limited,
)
from app.services.data_source.symbols import normalize, to_thscode
from app.services.data_source.throttle import TIER_DEFINITE

#: 板块"业务分类" → fuyao `tag` 取值。
#: akshare 的 `stock_board_*_name_ths` 只分"行业/概念"两类，而 fuyao 有四个 tag。
#: 这张表是两边唯一的桥（代码体系不通：akshare 给 881121/308614，
#: fuyao 给 886042.TI），所以必须显式写死并加测试。
BOARD_TAG_MAP: dict[str, str] = {
    "industry": "industry",
    "concept": "cn_concept",
    "region": "region",
    "special": "tszs",
}

#: 复权口径：内部（baostock 风格）→ fuyao `adjust`
_ADJUST_MAP: dict[str, str] = {
    "hfq": "backward",
    "qfq": "forward",
    "none": "none",
    "": "none",
}

#: fuyao 日线字段 → 内部统一列名
_BAR_COLUMN_MAP: dict[str, str] = {
    "date_ms": "date",
    "open_price": "open",
    "high_price": "high",
    "low_price": "low",
    "close_price": "close",
    "volume": "volume",
    "turnover": "amount",
}
_BAR_NUMERIC = ("open", "high", "low", "close", "volume", "amount")


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def date_to_ms(value: str | int | None) -> int | None:
    """把 `YYYYMMDD` / `YYYY-MM-DD` / 毫秒戳 统一成**毫秒级 Unix 时间戳**。

    空值 / None / 空串 → None（**表示不传该参数**，而不是传 0）——
    传 0 会被服务端理解成 1970-01-01，静默返回空表，是很难查的那类 bug。
    """
    if value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        if len(text) == 8:                                  # YYYYMMDD
            return int(pd.Timestamp(text, tz="Asia/Shanghai").value // 1_000_000)
        return int(text)                                    # 已是毫秒戳
    try:
        return int(pd.Timestamp(normalize_date(text), tz="Asia/Shanghai").value // 1_000_000)
    except Exception as exc:
        raise InvalidParamError(
            f"日期参数无法解析：{value!r}（支持 YYYYMMDD / YYYY-MM-DD / 毫秒时间戳）"
        ) from exc


def _clean_params(params: dict[str, Any] | None) -> dict[str, Any]:
    """丢掉空值 —— 空串/None 一律不发给服务端（见 `date_to_ms` 的说明）。"""
    if not params:
        return {}
    return {
        k: v
        for k, v in params.items()
        if v is not None and not (isinstance(v, str) and not v.strip())
    }


def _raise_for_business_code(code: int, message: str, *, path: str) -> None:
    """把 fuyao 的业务错误码映射成项目异常。

    ⚠️ 只有 4001 映射为限频（会触发熔断）。其余一律普通错误 ——
       宁可少熔断一次，也不要重演"误判成限频 → 整个源被短路"。
    """
    if code in (2001, 2003):
        raise NotConfiguredError(
            f"同花顺 fuyao 未授权（code={code}）：{message or 'API Key 缺失或无效'}。"
            f"请在 .env 配置 FUYAO_API_KEY（申请入口 https://fuyao.aicubes.cn/admin/）",
            detail={"fuyao_code": code, "path": path, "key_env": "FUYAO_API_KEY"},
        )
    if code == 4001:
        raise DataSourceRateLimited(
            f"同花顺 fuyao 触发频率限制（code=4001）：{message or '超过约定 QPS'}",
            detail={"tier": TIER_DEFINITE, "fuyao_code": code, "path": path},
        )
    raise DataSourceError(
        f"同花顺 fuyao 返回业务错误 code={code}：{message or '未提供原因'}",
        detail={"fuyao_code": code, "path": path},
    )


class FuyaoClient(BaseDataSource):
    """同花顺 fuyao REST 封装。

    ★ 所有请求都经 `BaseDataSource._invoke()`（缓存 → 熔断 → 限流 → 重试 →
      落缓存 → 记日志），不绕开它 —— 这是项目的硬纪律。
    """

    name = "fuyao"

    def __init__(self) -> None:
        # ★ 必须在 super().__init__() 之前设置 —— 基类构造器会读取这些属性
        cfg = settings.datasource.fuyao
        self._min_interval = cfg.min_interval
        self._max_calls_per_minute = cfg.max_calls_per_minute
        self._circuit_break_seconds = float(cfg.circuit_break_seconds)
        self._cache_ttl = float(cfg.cache_ttl)
        super().__init__()

        self._base_url = cfg.base_url.rstrip("/")
        self._timeout = float(cfg.timeout)
        self._key_env = cfg.api_key_env
        self._http: httpx.Client | None = None

    # ------------------------------------------------------------------
    # 会话 / 元信息
    # ------------------------------------------------------------------
    @property
    def version(self) -> str:
        """REST 接口无客户端版本；返回口径标识（供健康检查展示）。"""
        return "fuyao-rest-v1"

    @property
    def configured(self) -> bool:
        return settings.env.fuyao_configured

    def _headers(self) -> dict[str, str]:
        """认证头。**Key 只在这里出现**，绝不进 params / 日志。"""
        key = settings.env.fuyao_api_key.strip()
        if not key:
            raise NotConfiguredError(
                "同花顺 fuyao 未配置：请在 .env 里设置 FUYAO_API_KEY"
                "（申请入口 https://fuyao.aicubes.cn/admin/）",
                detail={"key_env": self._key_env},
            )
        return {"X-api-key": key, "Accept": "application/json"}

    @property
    def http(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(
                base_url=self._base_url,
                timeout=self._timeout,
                # trust_env=False：本机开着代理，而东财 push2 正是被代理/网络掐断的。
                # 走系统代理会让 fuyao 也变成"看运气"，显式直连更可控。
                trust_env=False,
                headers={"User-Agent": "stock-service/1.0"},
            )
        return self._http

    def login(self) -> None:
        _ = self.http  # 建连接池即视为就绪

    def logout(self) -> None:
        if self._http is not None:
            try:
                self._http.close()
            finally:
                self._http = None

    def _line_group(self, func_name: str) -> str:
        """熔断分组：fuyao 单一主机，全部归一组即可。"""
        return "fuyao"

    # ------------------------------------------------------------------
    # 健康检查
    # ------------------------------------------------------------------
    def health(self, *, deep: bool = False) -> DataSourceStatus:
        """健康检查。

        `deep=False` → 零网络请求（走基类：版本号 + 熔断状态）。
        `deep=True`  → 真实打一次最轻的接口（A 股代码表取 1 条）。
        """
        if not deep:
            status = super().health(deep=False)
            if not self.configured:
                status.available = False
                status.message = f"未配置（.env 缺少 {self._key_env}）"
            return status

        t0 = time.perf_counter()
        try:
            df = self.tickers_list("a-share", limit=1, force_refresh=True)
            rows = len(df)
            return DataSourceStatus(
                name=self.name,
                available=rows > 0,
                version=self.version,
                message=f"ok，真实请求成功（A股代码表 {rows} 行）",
                deep=True,
                latency_ms=round((time.perf_counter() - t0) * 1000, 2),
            )
        except Exception as exc:
            return DataSourceStatus(
                name=self.name,
                available=False,
                version=self.version,
                message=f"{type(exc).__name__}: {exc}",
                deep=True,
                latency_ms=round((time.perf_counter() - t0) * 1000, 2),
            )

    # ==================================================================
    # 底层：统一请求 + 信封解析
    # ==================================================================
    def _get(
        self,
        path: str,
        params: dict[str, Any] | None,
        *,
        func_name: str,
        cache_key: str | None = None,
        cache_ttl: float | None = None,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        """发一次 GET，返回信封里的 `data` 字典。所有错误在此收敛。"""
        clean = _clean_params(params)

        def _call() -> dict[str, Any]:
            # ★ 认证头在 try 之外取：`_headers()` 会抛 NotConfiguredError
            #   （Key 未配置）。若把它放进下面的 `except Exception` 里，
            #   配置类错误会被吞成"网络错误" —— 用户就看不到真正的病因了。
            headers = self._headers()
            try:
                resp = self.http.get(path, params=clean, headers=headers)
            except httpx.TimeoutException as exc:
                raise DataSourceTimeout(
                    f"同花顺 fuyao 请求超时（{self._timeout:.0f}s）：{path}"
                ) from exc
            except AppError:
                raise
            except Exception as exc:
                # 连接被掐断 → 与 akshare 走同一套档位识别（soft）
                raise_if_limited(exc)
                raise DataSourceError(
                    f"同花顺 fuyao 网络错误：{type(exc).__name__}: {exc}"
                ) from exc

            if resp.status_code == 429:
                raise DataSourceRateLimited(
                    "同花顺 fuyao 返回 HTTP 429（频率超限）",
                    detail={"tier": TIER_DEFINITE, "raw": "HTTP 429", "path": path},
                )
            if resp.status_code >= 400:
                raise DataSourceError(
                    f"同花顺 fuyao HTTP {resp.status_code}：{path}",
                    detail={"http_status": resp.status_code, "path": path},
                )

            try:
                body = resp.json()
            except Exception as exc:
                raise DataSourceError(
                    f"同花顺 fuyao 响应不是合法 JSON：{path}",
                    detail={"path": path, "raw": resp.text[:200]},
                ) from exc

            code = _as_int(body.get("code"))
            if code is None:
                raise DataSourceError(
                    f"同花顺 fuyao 响应缺少 code 字段：{path}",
                    detail={"path": path},
                )
            if code != 0:
                # ★ 业务错误也是 HTTP 200，只能靠这里拦
                _raise_for_business_code(code, str(body.get("message") or ""), path=path)
            return body.get("data") or {}

        return self._invoke(
            func_name,
            _call,
            params={"path": path, **clean},
            cache_key=cache_key,
            cache_ttl=cache_ttl,
            force_refresh=force_refresh,
        )

    def _frame(
        self,
        path: str,
        params: dict[str, Any] | None,
        *,
        func_name: str,
        cache_key: str | None = None,
        cache_ttl: float | None = None,
        force_refresh: bool = False,
    ) -> pd.DataFrame:
        """取 `data.item`（扁平对象数组）→ DataFrame。"""
        data = self._get(
            path,
            params,
            func_name=func_name,
            cache_key=cache_key,
            cache_ttl=cache_ttl,
            force_refresh=force_refresh,
        )
        items = data.get("item") or []
        if isinstance(items, dict):
            items = [items]
        return pd.DataFrame(items)

    # ==================================================================
    # 特色数据：涨跌停 / 连板
    # ==================================================================
    def limit_up_pool(
        self,
        date_ms: str | int | None = None,
        page: int = 1,
        size: int = 200,
        sort_field: str = "",
        sort_dir: str = "desc",
    ) -> pd.DataFrame:
        """涨停股票池（含封单额、连板数、涨停原因）。

        ★ 与东财 `stock_zt_pool_em` 的关键差别：本接口的 `date_ms` 可以指定
          **任意历史交易日**，而东财只保留最近 1~2 日（见开发进度 §5 Q1）。
        """
        return self._frame(
            "/api/a-share/special-data/limit-up-pool",
            {
                "date_ms": date_to_ms(date_ms),
                "page": page,
                "size": size,
                "sort_field": sort_field,
                "sort_dir": sort_dir,
            },
            func_name="limit_up_pool",
            cache_key=f"fuyao:limit_up:{date_ms}:{page}:{size}:{sort_field}:{sort_dir}",
        )

    def limit_down_pool(
        self,
        date_ms: str | int | None = None,
        page: int = 1,
        size: int = 200,
        sort_field: str = "",
        sort_dir: str = "desc",
    ) -> pd.DataFrame:
        """跌停股票池（首次/最后跌停时间、换手率）。"""
        return self._frame(
            "/api/a-share/special-data/limit-down-pool",
            {
                "date_ms": date_to_ms(date_ms),
                "page": page,
                "size": size,
                "sort_field": sort_field,
                "sort_dir": sort_dir,
            },
            func_name="limit_down_pool",
            cache_key=f"fuyao:limit_down:{date_ms}:{page}:{size}:{sort_field}:{sort_dir}",
        )

    def limit_break_pool(
        self,
        date_ms: str | int | None = None,
        page: int = 1,
        size: int = 200,
        sort_field: str = "",
        sort_dir: str = "desc",
    ) -> pd.DataFrame:
        """炸板股票池（开板次数、换手率、成交额）。"""
        return self._frame(
            "/api/a-share/special-data/limit-break-pool",
            {
                "date_ms": date_to_ms(date_ms),
                "page": page,
                "size": size,
                "sort_field": sort_field,
                "sort_dir": sort_dir,
            },
            func_name="limit_break_pool",
            cache_key=f"fuyao:limit_break:{date_ms}:{page}:{size}:{sort_field}:{sort_dir}",
        )

    def limit_up_ladder(self) -> pd.DataFrame:
        """连板天梯（近 30 个交易日，按日期 → 板位 → 股票列表）。

        ⚠️ 返回结构是**嵌套**的：`date` + `boards`（dict，6 个板位各一个 list）。
           本方法把 6 个板位**展平**成行，便于表格展示与落盘 ——
           保留 `date` / `board`（板位名）/ `board_num` 三个维度列。
        """
        data = self._get(
            "/api/a-share/special-data/limit-up-ladder",
            None,
            func_name="limit_up_ladder",
            cache_key="fuyao:limit_up_ladder",
        )
        rows: list[dict[str, Any]] = []
        for day in data.get("item") or []:
            date = day.get("date")
            for board, stocks in (day.get("boards") or {}).items():
                for stk in stocks or []:
                    rows.append({"date": date, "board": board, **stk})
        return pd.DataFrame(rows)

    # ==================================================================
    # 板块（同花顺指数）
    # ==================================================================
    def index_list(self, tag: str = "industry") -> pd.DataFrame:
        """同花顺指数清单（`thscode` + `name`）。

        Args:
            tag: `industry` 行业 / `cn_concept` 概念 / `region` 区域 / `tszs` 特色。
                 也接受 akshare 风格的中文业务名（见 `BOARD_TAG_MAP`）。
        """
        tag_value = BOARD_TAG_MAP.get(tag, tag)
        return self._frame(
            "/api/a-share-index/catalog/ths-index-list",
            {"tag": tag_value},
            func_name="index_list",
            cache_key=f"fuyao:index_list:{tag_value}",
            # 板块表一天变不了几次，缓存久点
            cache_ttl=max(self._cache_ttl, 3600.0),
        )

    def index_constituents(self, thscode: str) -> pd.DataFrame:
        """板块成分股（**akshare 的 `*_ths` 没有这个能力**，是本源的独占项）。"""
        return self._frame(
            "/api/a-share-index/constituents/ths-stock-list",
            {"thscode": thscode},
            func_name="index_constituents",
            cache_key=f"fuyao:cons:{thscode}",
        )

    def index_snapshot(self, thscodes: str) -> pd.DataFrame:
        """指数/板块快照（`thscodes` 逗号分隔，**必须传**，不支持枚举全市场）。"""
        return self._frame(
            "/api/a-share-index/prices/snapshot",
            {"thscodes": thscodes},
            func_name="index_snapshot",
            cache_key=f"fuyao:index_spot:{thscodes}",
            cache_ttl=min(self._cache_ttl, 60.0),
        )

    def index_historical(
        self,
        thscode: str,
        start: str | int,
        end: str | int,
        interval: str = "1d",
    ) -> pd.DataFrame:
        """指数/板块历史 K 线（单只、窗口 ≤ 10 年、无复权语义）。"""
        return self._frame(
            "/api/a-share-index/prices/historical",
            {
                "thscode": thscode,
                "interval": interval,
                "start": date_to_ms(start),
                "end": date_to_ms(end),
            },
            func_name="index_historical",
            cache_key=f"fuyao:index_hist:{thscode}:{interval}:{start}:{end}",
        )

    def resolve_board_thscode(self, board_name: str, tag: str = "industry") -> str:
        """板块**中文名** → fuyao 的板块 thscode（如 `886042.TI`）。

        ★ 为什么以中文名做桥：akshare 给的是同花顺内部板块码（`881121` /
          `308614`），fuyao 要的是同花顺**指数**码（`886042.TI`），两套码
          互不相通；唯一稳定的公共键是板块名。

        ⚠️ 匹配不上**如实报错**，不返回空 —— 静默返回空会让下游"以为这个板块
           没有成分股"，属于本项目最忌讳的静默污染。
        """
        df = self.index_list(tag)
        if df.empty or "name" not in df.columns:
            raise NotFoundError(
                f"同花顺板块清单为空，无法解析「{board_name}」（tag={tag}）"
            )
        hit = df[df["name"].astype(str).str.strip() == board_name.strip()]
        if hit.empty:
            names = ", ".join(df["name"].astype(str).head(8))
            raise NotFoundError(
                f"同花顺板块表中找不到「{board_name}」（tag={tag}）。"
                f"样例名称：{names} … 共 {len(df)} 个",
                detail={"board": board_name, "tag": tag},
            )
        return str(hit.iloc[0]["thscode"])

    def board_constituents(self, board_name: str, tag: str = "industry") -> pd.DataFrame:
        """按板块中文名直接取成分股（内部先做名称 → thscode 解析）。"""
        return self.index_constituents(self.resolve_board_thscode(board_name, tag))

    # ==================================================================
    # 行情 / 元信息
    # ==================================================================
    def snapshot(
        self,
        thscodes: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> pd.DataFrame:
        """A 股行情快照。

        `thscodes` 逗号分隔批量取；省略则**遍历全市场**并按 `limit`/`offset` 分页。
        """
        return self._frame(
            "/api/a-share/prices/snapshot",
            {"thscodes": thscodes, "limit": limit, "offset": offset},
            func_name="snapshot",
            cache_key=f"fuyao:spot:{thscodes or 'ALL'}:{limit}:{offset}",
            cache_ttl=min(self._cache_ttl, 10.0),   # 快照不能缓存太久
        )

    def prices_historical(
        self,
        thscode: str,
        start: str | int,
        end: str | int,
        interval: str = "1d",
        adjust: str = "backward",
    ) -> pd.DataFrame:
        """个股历史 K 线（**每次仅一只**、窗口 ≤ 10 年）。"""
        return self._frame(
            "/api/a-share/prices/historical",
            {
                "thscode": thscode,
                "interval": interval,
                "start": date_to_ms(start),
                "end": date_to_ms(end),
                "adjust": adjust,
            },
            func_name="prices_historical",
            cache_key=f"fuyao:hist:{thscode}:{interval}:{adjust}:{start}:{end}",
        )

    def tickers_list(
        self,
        asset_type: str = "a-share",
        limit: int = 1000,
        offset: int = 0,
        *,
        force_refresh: bool = False,
    ) -> pd.DataFrame:
        """标的代码表（默认全 A 股）。"""
        return self._frame(
            "/api/meta/tickers/list",
            {"asset_type": asset_type, "limit": limit, "offset": offset},
            func_name="tickers_list",
            cache_key=f"fuyao:tickers:{asset_type}:{limit}:{offset}",
            cache_ttl=max(self._cache_ttl, 3600.0),
            force_refresh=force_refresh,
        )

    def dragon_tiger_list(self, date_ms: str | int | None = None, kind: str = "") -> pd.DataFrame:
        """龙虎榜榜单（全部 / 机构榜 / 游资榜）。"""
        return self._frame(
            "/api/a-share/special-data/dragon-tiger-list",
            {"date_ms": date_to_ms(date_ms), "type": kind},
            func_name="dragon_tiger_list",
            cache_key=f"fuyao:lhb:{date_ms}:{kind}",
        )

    # ==================================================================
    # BaseDataSource 抽象方法
    #
    # ⚠️ fuyao 的日线**不能**用于回测/模拟盘：它没有逐日 isST 与停牌标记，
    #    而这两个字段是本项目的"数据正确性红线"（只有 baostock 提供）。
    #    这里的实现是为了满足契约、并在需要时做交叉校验，不是要让主源换人。
    # ==================================================================
    def fetch_daily(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
        *,
        adjust: str = "hfq",
    ) -> pd.DataFrame:
        """个股日线。

        ⚠️ 返回的 `trade_status` 恒为 1、`is_st` 为 NA —— 与 akshare 同样的问题：
           **禁止用本方法的数据判断 ST / 停牌**。
        """
        canonical = normalize(symbol)
        adjust_key = (adjust or "none").lower()
        adjust_value = _ADJUST_MAP.get(adjust_key)
        if adjust_value is None:
            raise DataSourceError(f"不支持的复权参数：{adjust!r}（可选 hfq/qfq/none）")

        raw = self.prices_historical(
            to_thscode(canonical),
            start_date,
            end_date,
            interval="1d",
            adjust=adjust_value,
        )
        if raw is None or raw.empty:
            return pd.DataFrame(columns=list(QUOTE_COLUMNS))

        df = raw.rename(columns=_BAR_COLUMN_MAP)
        df["symbol"] = canonical
        df = coerce_numeric(df, _BAR_NUMERIC)
        df["date"] = pd.to_datetime(df["date"], unit="ms", errors="coerce").dt.normalize()
        df = df.dropna(subset=["date"])
        df["preclose"] = df["close"].shift(1)
        df["pct_chg"] = (df["close"] / df["preclose"] - 1.0) * 100.0
        df["turn"] = pd.NA
        df["trade_status"] = 1
        df["is_st"] = pd.NA          # ⚠️ 未知，禁止用于 ST 判定
        df = ensure_columns(df, QUOTE_COLUMNS)
        return df.sort_values("date").reset_index(drop=True)

    def fetch_index_daily(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
    ) -> pd.DataFrame:
        """指数日线（symbol 传内部格式，如 `sh.000001`）。"""
        canonical = normalize(symbol, is_index=True)
        raw = self.index_historical(
            to_thscode(canonical), start_date, end_date, interval="1d"
        )
        if raw is None or raw.empty:
            return pd.DataFrame(columns=list(INDEX_COLUMNS))

        df = raw.rename(columns=_BAR_COLUMN_MAP)
        df["symbol"] = canonical
        df = coerce_numeric(df, _BAR_NUMERIC)
        df["date"] = pd.to_datetime(df["date"], unit="ms", errors="coerce").dt.normalize()
        df = df.dropna(subset=["date"])
        df["preclose"] = df["close"].shift(1)
        df["pct_chg"] = (df["close"] / df["preclose"] - 1.0) * 100.0
        df = ensure_columns(df, INDEX_COLUMNS)
        return df.sort_values("date").reset_index(drop=True)

    def fetch_trade_dates(
        self, start_date: str, end_date: str, *, force_refresh: bool = False
    ) -> list[str]:
        """交易日历 —— **委托 baostock**。

        ★ fuyao 没有日历端点。这里刻意不"顺手用别的接口凑一个"：
          本项目的日历唯一真相是 baostock（含历史全量与年度更新），
          多造一个来源就多一种"两边对不上"的可能。
        """
        from app.services.data_source.baostock_client import (  # noqa: PLC0415
            baostock_client,
        )

        return baostock_client.fetch_trade_dates(
            start_date, end_date, force_refresh=force_refresh
        )


#: 模块级单例（与 baostock_client / akshare_client 一致的用法）
fuyao_client = FuyaoClient()

__all__ = [
    "BOARD_TAG_MAP",
    "FuyaoClient",
    "date_to_ms",
    "fuyao_client",
]
