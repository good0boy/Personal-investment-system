"""baostock 客户端 —— 本项目的【行情主源】。

为什么是主源（而不是 akshare）？实测对比见 技术架构.md 5.9 节：

| 能力             | akshare        | baostock                       |
|------------------|----------------|--------------------------------|
| 逐日历史 ST 状态 | ❌ 仅当前列表  | ✅ `isST` 字段逐日时变         |
| 停牌标记         | ⚠️ 靠成交量推断 | ✅ `tradestatus` (1=交易 0=停牌)|
| 退市股历史数据   | ⚠️ 不稳定      | ✅ 完整到退市日                |
| 历史指数成分股   | ❌ 不接受日期  | ✅ `query_*_stocks(date)`      |
| 日线含涨跌幅     | 需自行计算     | ✅ `pctChg` 直接给             |
| 接口稳定性       | ❌ 频繁变更    | ✅ 长期稳定                    |
| 实时行情         | ✅             | ❌                             |

代码格式：`sh.600519` / `sz.000001` / `sh.000001`（指数）

⚠️ baostock 使用**模块级全局会话**，且非线程安全，因此所有调用都在同一把锁内串行。
"""
from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable
from datetime import date, datetime
from typing import Any, TypeVar

import pandas as pd

from app.core.config import settings
from app.core.errors import DataSourceError, SymbolFormatError
from app.core.logging import logger
from app.core.paths import BAOSTOCK_QUOTA_FILE
from app.services.data_source.base import (
    INDEX_COLUMNS,
    QUOTE_COLUMNS,
    BaseDataSource,
    DataSourceStatus,
    coerce_numeric,
    ensure_columns,
    normalize_date,
)
from app.services.data_source.symbols import normalize, split_exchange, to_baostock

T = TypeVar("T")

#: 个股日线字段（★ isST 与 tradestatus 是选 baostock 的核心理由）
_DAILY_FIELDS = "date,code,open,high,low,close,preclose,volume,amount,turn,pctChg,tradestatus,isST"

#: 指数日线字段（指数无换手率/停牌/ST 概念）
_INDEX_FIELDS = "date,code,open,high,low,close,preclose,volume,amount,pctChg"

_NUMERIC_COLS = (
    "open", "high", "low", "close", "preclose",
    "volume", "amount", "turn", "pct_chg", "trade_status", "is_st",
)

#: baostock adjustflag
_ADJUST_MAP = {"hfq": "1", "qfq": "2", "": "3", "none": "3"}

#: 缓存 TTL（秒）
_CALENDAR_CACHE_TTL = 3600.0     # 交易日历一年才变一次
_NAME_LIST_CACHE_TTL = 300.0     # 证券名单/成分股/行业：会变，缓存短些

_LOCK = threading.RLock()
_QUOTA_LOCK = threading.RLock()


def _quota_check_and_count() -> None:
    """baostock 每日 API 请求计数 +1；达到当日上限抛 DataSourceError。

    背景（2026-10-10 主人给定）：baostock 每日 API 请求超过 5 万次会触发
    **6 小时封禁**（10001011 黑名单用户）。计数器落
    `data/meta/baostock_quota.json`（按天自动重置，跨进程持久），
    达到上限当天拒绝再发任何请求 —— job 记 failed + 企微告警，次日自动恢复。
    计数点 = login 与每一次真实数据请求（缓存命中不计数）。
    """
    limit = settings.datasource.baostock_daily_api_limit
    today = datetime.now().strftime("%Y-%m-%d")
    count = 0
    with _QUOTA_LOCK:
        try:
            if BAOSTOCK_QUOTA_FILE.exists():
                raw = json.loads(BAOSTOCK_QUOTA_FILE.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and raw.get("date") == today:
                    count = int(raw.get("count") or 0)
        except Exception as exc:  # 计数文件损坏：告警并从 0 继续（罕见态）
            logger.warning(f"[baostock] 配额计数文件损坏（从 0 重计）：{exc!r}")
        if count >= limit:
            raise DataSourceError(
                f"今日 baostock API 请求已达上限：{count} / {limit}。"
                f"为避免触发 6 小时封禁（10001011 黑名单），今天不再发起任何 baostock 请求；"
                f"明日 0 点计数自动重置。基本面回补走 akshare 不受影响。"
            )
        count += 1
        tmp = BAOSTOCK_QUOTA_FILE.with_suffix(f".tmp{os.getpid()}")
        tmp.write_text(json.dumps({"date": today, "count": count}), encoding="utf-8")
        os.replace(tmp, BAOSTOCK_QUOTA_FILE)


class BaostockClient(BaseDataSource):
    """baostock 封装。全局单例使用（模块级会话 + 互斥锁）。"""

    name = "baostock"

    def __init__(self) -> None:
        super().__init__()
        self._logged_in = False

    # ------------------------------------------------------------------
    # 会话
    # ------------------------------------------------------------------
    @property
    def version(self) -> str:
        """pip 可见的版本号（如 0.9.3）。

        ⚠️ 注意：`baostock.__version__` 返回的是它自己格式化的 "00.9.30"，
        与 pip 版本号不是同一个字符串，别搞混（实测确认）。
        这里统一上报 pip 版本，便于与 requirements.txt 的 pin 对齐。
        """
        try:
            from importlib.metadata import PackageNotFoundError  # noqa: PLC0415
            from importlib.metadata import version as dist_version  # noqa: PLC0415

            try:
                return dist_version("baostock")
            except PackageNotFoundError:
                pass
            import baostock as bs  # noqa: PLC0415

            return str(getattr(bs, "__version__", "unknown"))
        except ImportError as exc:
            raise DataSourceError("baostock 未安装，请执行 pip install baostock") from exc

    @property
    def version_raw(self) -> str:
        """baostock 自己上报的版本串（如 "00.9.30"），仅用于诊断展示。"""
        try:
            import baostock as bs  # noqa: PLC0415

            return str(getattr(bs, "__version__", ""))
        except Exception:
            return ""

    def login(self) -> None:
        with _LOCK:
            if self._logged_in:
                return
            import baostock as bs  # noqa: PLC0415

            _quota_check_and_count()   # 登录也是一次服务端请求
            res = bs.login()
            if res.error_code != "0":
                raise DataSourceError(
                    f"baostock 登录失败：{res.error_code} {res.error_msg}"
                )
            self._logged_in = True
            logger.debug(f"baostock 登录成功（版本 {self.version}）")

    def logout(self) -> None:
        with _LOCK:
            if not self._logged_in:
                return
            import baostock as bs  # noqa: PLC0415

            bs.logout()
            self._logged_in = False

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    @staticmethod
    def _fetch_all(rs: Any, fields: list[str]) -> pd.DataFrame:
        """把 baostock 的 ResultData 游标读成 DataFrame。"""
        if rs.error_code != "0":
            raise DataSourceError(f"baostock 查询失败：{rs.error_code} {rs.error_msg}")

        rows: list[list[str]] = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())

        if not rows:
            return pd.DataFrame(columns=rs.fields or fields)
        return pd.DataFrame(rows, columns=rs.fields)

    def _kdata(
        self,
        code: str,
        fields: str,
        start_date: str,
        end_date: str,
        frequency: str = "d",
        adjustflag: str = "1",
    ) -> pd.DataFrame:
        """裸查询（不含节流/缓存）。

        ⚠️ 不要直接调用它 —— 必须经 `self._query()`，
           否则限流、重试、缓存、调用日志全部失效。
        """
        import baostock as bs  # noqa: PLC0415

        rs = bs.query_history_k_data_plus(
            code,
            fields,
            start_date=start_date,
            end_date=end_date,
            frequency=frequency,
            adjustflag=adjustflag,
        )
        return self._fetch_all(rs, fields.split(","))

    def _with_session(self, call: Callable[[], T]) -> T:
        """在会话锁内执行。

        baostock 用的是**模块级全局会话**且非线程安全，因此所有查询必须串行。
        """
        with _LOCK:
            self.login()
            return call()

    def _query(
        self,
        func_name: str,
        call: Callable[[], T],
        *,
        params: dict[str, Any] | None = None,
        cache_key: str | None = None,
        cache_ttl: float | None = None,
        force_refresh: bool = False,
    ) -> T:
        """★ baostock 的统一切口：限流 → 重试 → 缓存 → 调用日志 → 会话锁。

        baostock 免费且接口稳定，无需像 akshare 那样严格节流，
        但**同样需要**重试与调用日志 —— 出问题时没有日志就无法排查。

        Args:
            force_refresh: 跳过缓存读取。语义上是"刷新/探测"的方法必须传 True。
        """
        def _metered() -> T:
            _quota_check_and_count()   # 每次真实数据请求计数（缓存命中不走这里）
            return self._with_session(call)

        return self._invoke(
            func_name,
            _metered,
            params=params,
            cache_key=cache_key,
            cache_ttl=cache_ttl,
            force_refresh=force_refresh,
        )

    # ------------------------------------------------------------------
    # 日线
    # ------------------------------------------------------------------
    def fetch_daily(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
        *,
        adjust: str = "hfq",
    ) -> pd.DataFrame:
        """个股日线。返回统一列名（QUOTE_COLUMNS），含 is_st / trade_status。"""
        try:
            canonical = normalize(symbol)
            ex, _code6 = split_exchange(canonical)
        except SymbolFormatError as exc:
            raise SymbolFormatError(f"个股代码非法：{symbol!r}") from exc

        if ex == "bj":
            logger.debug(f"baostock 对北交所支持有限，跳过 {canonical}")
            return pd.DataFrame(columns=list(QUOTE_COLUMNS))

        bs_code = to_baostock(canonical)
        adjustflag = _ADJUST_MAP.get((adjust or "").lower())
        if adjustflag is None:
            raise DataSourceError(f"不支持的复权参数：{adjust!r}（可选 hfq/qfq/空）")

        s, e = normalize_date(start_date), normalize_date(end_date)
        if s > e:
            raise DataSourceError(f"起始日期晚于结束日期：{s} > {e}")

        try:
            raw = self._query(
                "query_history_k_data_plus",
                lambda: self._kdata(bs_code, _DAILY_FIELDS, s, e, "d", adjustflag),
                params={"code": bs_code, "start": s, "end": e, "adjust": adjustflag},
                cache_key=f"bs:daily:{canonical}:{s}:{e}:{adjustflag}",
            )
        except DataSourceError:
            raise
        except Exception as exc:
            raise DataSourceError(f"baostock 拉取 {canonical} 日线失败：{exc}") from exc

        if raw.empty:
            return pd.DataFrame(columns=list(QUOTE_COLUMNS))

        df = raw.rename(
            columns={
                "date": "date",
                "code": "code",
                "pctChg": "pct_chg",
                "tradestatus": "trade_status",
                "isST": "is_st",
            }
        )
        df["symbol"] = canonical
        # baostock 缺失值用空字符串表示
        df = df.replace({"": pd.NA})
        df = coerce_numeric(df, _NUMERIC_COLS)
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.dropna(subset=["date"])

        for c in ("trade_status", "is_st"):
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).astype("int8")

        df = ensure_columns(df, QUOTE_COLUMNS)
        df = df.sort_values("date").reset_index(drop=True)
        return df

    # ------------------------------------------------------------------
    # 指数日线
    # ------------------------------------------------------------------
    def fetch_index_daily(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
    ) -> pd.DataFrame:
        """指数日线（指数必须显式带交易所前缀，如 sh.000001）。"""
        canonical = normalize(symbol, is_index=True)
        bs_code = to_baostock(canonical)
        s, e = normalize_date(start_date), normalize_date(end_date)

        try:
            raw = self._query(
                "query_history_k_data_plus",
                lambda: self._kdata(bs_code, _INDEX_FIELDS, s, e, "d", "3"),
                params={"code": bs_code, "start": s, "end": e, "kind": "index"},
                cache_key=f"bs:index:{canonical}:{s}:{e}",
            )
        except DataSourceError:
            raise
        except Exception as exc:
            raise DataSourceError(f"baostock 拉取指数 {canonical} 失败：{exc}") from exc

        if raw.empty:
            return pd.DataFrame(columns=list(INDEX_COLUMNS))

        df = raw.rename(columns={"pctChg": "pct_chg"})
        df["symbol"] = canonical
        df = df.replace({"": pd.NA})
        df = coerce_numeric(df, ("open", "high", "low", "close", "preclose", "volume", "amount", "pct_chg"))
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.dropna(subset=["date"])
        df = ensure_columns(df, INDEX_COLUMNS)
        return df.sort_values("date").reset_index(drop=True)

    # ------------------------------------------------------------------
    # 交易日历
    # ------------------------------------------------------------------
    def fetch_trade_dates(
        self, start_date: str, end_date: str, *, force_refresh: bool = False
    ) -> list[str]:
        """区间内的交易日（YYYY-MM-DD）。

        Args:
            force_refresh: 跳过缓存，强制真实请求（用于"探测/刷新"场景）
        """
        import baostock as bs  # noqa: PLC0415

        s, e = normalize_date(start_date), normalize_date(end_date)

        df = self._query(
            "query_trade_dates",
            lambda: self._fetch_all(
                bs.query_trade_dates(start_date=s, end_date=e),
                ["calendar_date", "is_trading_day"],
            ),
            params={"start": s, "end": e},
            cache_key=f"bs:cal:{s}:{e}",
            cache_ttl=_CALENDAR_CACHE_TTL,
            force_refresh=force_refresh,
        )

        if df.empty:
            return []
        df = df[df["is_trading_day"] == "1"]
        return sorted(df["calendar_date"].tolist())

    def fetch_all_trade_dates(
        self,
        start_date: str = "1990-01-01",
        end_date: str | None = None,
        *,
        force_refresh: bool = False,
    ) -> pd.DataFrame:
        """全部日历（含非交易日），返回列 [date, is_trading_day]。

        ⚠️ `force_refresh` 对"刷新交易日历"是**必须**的 —— 不加就是假刷新：
           接口报"已同步"，实际读的是 1 小时内的缓存。
        """
        import baostock as bs  # noqa: PLC0415

        e = normalize_date(end_date) if end_date else date.today().strftime("%Y-%m-%d")
        s = normalize_date(start_date)

        df = self._query(
            "query_trade_dates",
            lambda: self._fetch_all(
                bs.query_trade_dates(start_date=s, end_date=e),
                ["calendar_date", "is_trading_day"],
            ),
            params={"start": s, "end": e, "kind": "full"},
            cache_key=f"bs:calall:{s}:{e}",
            cache_ttl=_CALENDAR_CACHE_TTL,
            force_refresh=force_refresh,
        )

        if df.empty:
            return pd.DataFrame(columns=["date", "is_trading_day"])
        df = df.rename(columns={"calendar_date": "date"})
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df["is_trading_day"] = pd.to_numeric(df["is_trading_day"], errors="coerce").fillna(0).astype("int8")
        return df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)

    # ------------------------------------------------------------------
    # 股票名单 / 成分股 / 行业
    # ------------------------------------------------------------------
    def fetch_all_stock(self, day: str | None = None) -> pd.DataFrame:
        """某日全部证券列表。

        返回列：code(内部格式) / name / trade_status

        ⚠️ 实测：`code_name` 是【当前名称】而非历史名称
           （4220 只共同股票在 2020-06-01 与 2024-12-31 的名称差异为 0 处）。
        因此**不能用它重建历史 ST 状态**，历史 ST 必须走日线的 `isST` 字段。
        """
        import baostock as bs  # noqa: PLC0415

        day_s = normalize_date(day) if day else date.today().strftime("%Y-%m-%d")

        df = self._query(
            "query_all_stock",
            lambda: self._fetch_all(
                bs.query_all_stock(day=day_s), ["code", "tradeStatus", "code_name"]
            ),
            params={"day": day_s},
            cache_key=f"bs:allstock:{day_s}",
            # 证券名单当日会变（新股上市/退市），缓存不能太久
            cache_ttl=_NAME_LIST_CACHE_TTL,
        )

        if df.empty:
            return pd.DataFrame(columns=["code", "name", "trade_status"])

        df = df.rename(columns={"code_name": "name", "tradeStatus": "trade_status"})
        df["trade_status"] = pd.to_numeric(df["trade_status"], errors="coerce").fillna(0).astype("int8")
        return df[["code", "name", "trade_status"]]

    def fetch_index_constituents(self, index: str, day: str | None = None) -> pd.DataFrame:
        """★ 历史指数成分股（baostock 的独有能力）。

        Args:
            index: "hs300" | "zz500" | "sz50" | "zz1000"
            day: 交易日（YYYY-MM-DD）。baostock 支持历史日期查询。
        """
        import baostock as bs  # noqa: PLC0415

        fn_map = {
            "hs300": "query_hs300_stocks",
            "zz500": "query_zz500_stocks",
            "sz50": "query_sz50_stocks",
            "zz1000": "query_zz1000_stocks",
        }
        key = index.lower().strip()
        if key not in fn_map or not hasattr(bs, fn_map[key]):
            raise DataSourceError(f"不支持的指数：{index!r}（可选 {list(fn_map)}）")

        fn = getattr(bs, fn_map[key])
        day_s = normalize_date(day) if day else None

        df = self._query(
            fn_map[key],
            lambda: self._fetch_all(
                fn(day_s) if day_s else fn(), ["updateDate", "code", "code_name"]
            ),
            params={"index": key, "day": day_s or "latest"},
            cache_key=f"bs:cons:{key}:{day_s or 'latest'}",
            cache_ttl=_NAME_LIST_CACHE_TTL,
        )

        if df.empty:
            return pd.DataFrame(columns=["update_date", "code", "name"])
        return df.rename(columns={"updateDate": "update_date", "code_name": "name"})[
            ["update_date", "code", "name"]
        ]

    def fetch_stock_industry(self) -> pd.DataFrame:
        """证监会行业分类。返回列：code / name / industry。"""
        import baostock as bs  # noqa: PLC0415

        df = self._query(
            "query_stock_industry",
            lambda: self._fetch_all(
                bs.query_stock_industry(),
                ["updateDate", "code", "code_name", "industry", "industryClassification"],
            ),
            params={"kind": "industry"},
            cache_key="bs:industry:all",
            cache_ttl=_NAME_LIST_CACHE_TTL,
        )

        if df.empty:
            return pd.DataFrame(columns=["code", "name", "industry"])
        return df.rename(columns={"code_name": "name"})[["code", "name", "industry"]]

    # ------------------------------------------------------------------
    # 健康检查
    #   deep=False → 零网络请求（读版本号 + 熔断状态）
    #   deep=True  → 真实拉一次交易日历（force_refresh，绕过缓存）
    # ------------------------------------------------------------------
    def health(self, *, deep: bool = False) -> DataSourceStatus:
        if not deep:
            return super().health(deep=False)

        import time  # noqa: PLC0415

        t0 = time.perf_counter()
        try:
            v = self.version
            self.login()
            today = date.today().strftime("%Y-%m-%d")
            # ★ force_refresh 必须为 True：探测要是真请求，不能被缓存挡住
            dates = self.fetch_trade_dates("2024-01-01", today, force_refresh=True)
            return DataSourceStatus(
                name=self.name,
                available=bool(dates),
                version=v,
                message=f"ok，真实请求成功（交易日历 2024 至今 {len(dates)} 个交易日）",
                deep=True,
                latency_ms=round((time.perf_counter() - t0) * 1000, 2),
            )
        except Exception as exc:
            return DataSourceStatus(
                name=self.name,
                available=False,
                version=getattr(self, "_version_cache", ""),
                message=f"{type(exc).__name__}: {exc}",
                deep=True,
                latency_ms=round((time.perf_counter() - t0) * 1000, 2),
            )


#: 全局单例（baostock 会话是模块级全局的，必须复用）
baostock_client = BaostockClient()
