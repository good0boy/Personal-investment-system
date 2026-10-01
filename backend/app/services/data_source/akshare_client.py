"""akshare 客户端 —— 本项目的【广度补充源】。

用途：
1. **模块 1 数据分类**：接口目录 + 在线试跑（akshare 有 1083 个函数，广度是其核心价值）
2. **实时行情**：`stock_zh_a_spot_em` 一次拿全市场快照（baostock 没有实时）
3. **基本面 / 板块 / 龙虎榜 / 资金流** 等扩展数据

⚠️ 与 baostock 的分工（见 技术架构.md 5.9）：
   模块 3 回测、模块 4 模拟盘 **不用它** —— 那两块要的是 ST/停牌/退市股的精度，
   只有 baostock 能保证。这里只做广度补充。

⚠️ akshare 版本间常有 breaking change，必须锁版本 + 启动自检。
   实测版本：1.18.94

★★ 2026-10-01 数据源现状（**取数前先看这段**）
   · 东财 `push2` / `push2his` 域名在本机被网络**掐断** → 走这两个域名的
     方法（`fetch_spot` / `fetch_daily` / `fetch_index_daily`）**目前不可用**，
     已标记 deprecated。相关接口条目也已从 `config/catalog/` 下架。
   · **不再考虑东财 push2**：能替代的已改用同花顺 —— akshare 内置的
     `*_ths`（板块列表/历史）与 `fuyao_client`（板块成分/快照/涨跌停/热榜/龙虎榜）。
   · 东财 `push2ex`（涨停板系列）与 `datacenter-web`（龙虎榜）**仍可用**。
   · **优先选 `*_ths` 后缀的接口**：它们走 10jqka 域名，实测全通。

★★ 频率控制（比 baostock 严格得多）

   akshare 的数据源（东财、新浪）对高频访问很敏感，因此配置独立节流：

   ========================== ==========================================
   约束                        值
   ========================== ==========================================
   最小调用间隔                1 秒（1 秒最多 1 次）
   60 秒滑动窗口上限           30 次
   触发限频后熔断              5 分钟（期间一个请求都不发）
   ========================== ==========================================

   安全上限：30 次/分钟 = 1800 次/小时。按此速率拉 5000 只股票日线约需 2.8 小时。
   **这个代价是必要的** —— 被东财封 IP 后恢复时间不可控。

   所有请求都经 `BaseDataSource._invoke()`，因此限流、熔断、缓存、重试、
   调用日志**自动生效**。新增方法时不要绕开它。
"""
from __future__ import annotations

import inspect
import json
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pandas as pd

from app.core.config import settings
from app.core.errors import (
    DataSourceError,
    DataSourceTimeout,
    InvalidParamError,
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
    to_compact_date,
)
from app.services.data_source.symbols import normalize, to_akshare

#: akshare 日线中文字段 -> 内部字段
_HIST_COLUMN_MAP = {
    "日期": "date",
    "股票代码": "code",
    "开盘": "open",
    "收盘": "close",
    "最高": "high",
    "最低": "low",
    "成交量": "volume",
    "成交额": "amount",
    "振幅": "amplitude",
    "涨跌幅": "pct_chg",
    "涨跌额": "change",
    "换手率": "turn",
}

_NUMERIC_COLS = (
    "open", "high", "low", "close", "volume", "amount",
    "amplitude", "pct_chg", "change", "turn",
)

#: 全市场快照中文字段 -> 内部字段
_SPOT_COLUMN_MAP = {
    "代码": "code",
    "名称": "name",
    "最新价": "close",
    "涨跌幅": "pct_chg",
    "涨跌额": "change",
    "成交量": "volume",
    "成交额": "amount",
    "振幅": "amplitude",
    "最高": "high",
    "最低": "low",
    "今开": "open",
    "昨收": "preclose",
    "换手率": "turn",
    "总市值": "total_mv",
    "流通市值": "float_mv",
    "市盈率-动态": "pe_ttm",
    "市净率": "pb",
}

#: 交易日历缓存 TTL（秒）—— 日历一年才变一次，可以缓存久些
_CALENDAR_CACHE_TTL = 3600.0

_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="akshare")


def _stable_params_key(params: dict[str, Any] | None) -> str:
    """把参数字典转成稳定的缓存键片段（处理 list/dict 等不可哈希值）。"""
    if not params:
        return ""
    try:
        return json.dumps(params, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        return repr(sorted(params.items(), key=lambda kv: str(kv[0])))


class AkshareClient(BaseDataSource):
    """akshare 封装（广度源）。"""

    name = "akshare"

    def __init__(self) -> None:
        # ★ 必须在 super().__init__() 之前设置 —— 基类构造器会读取这些属性
        ak_cfg = settings.datasource.akshare
        self._min_interval = ak_cfg.min_interval
        self._max_calls_per_minute = ak_cfg.max_calls_per_minute
        self._circuit_break_seconds = ak_cfg.circuit_break_seconds
        self._cache_ttl = float(ak_cfg.cache_ttl)
        super().__init__()

        self._ak: Any = None
        self._spot_cache_ttl = float(ak_cfg.spot_cache_ttl)

    # ------------------------------------------------------------------
    # 会话（akshare 无会话概念，这里只做懒加载）
    # ------------------------------------------------------------------
    @property
    def ak(self) -> Any:
        if self._ak is None:
            try:
                import akshare as ak  # noqa: PLC0415
            except ImportError as exc:
                raise DataSourceError("akshare 未安装，请执行 pip install akshare") from exc
            self._ak = ak
        return self._ak

    @property
    def version(self) -> str:
        return str(getattr(self.ak, "__version__", "unknown"))

    def login(self) -> None:
        _ = self.ak  # 触发导入即视为就绪

    def logout(self) -> None:
        return None

    # ------------------------------------------------------------------
    # 健康检查
    #   deep=False → 零网络请求（走基类）
    #   deep=True  → 真实调一次轻量接口（交易日历，新浪源）
    # ------------------------------------------------------------------
    def health(self, *, deep: bool = False) -> DataSourceStatus:
        """健康检查。

        ⚠️ **为什么必须自己实现 deep**：基类只读版本号 + 熔断状态，**零网络请求**。
           若 akshare 沿用基类实现，`/api/v1/datasources/health?deep=true`
           对它就等于什么都没做 —— 页面上显示"正常"，但那只说明**库装上了**，
           不代表**能连上东财/新浪**。这两个结论完全不是一回事，
           而"库能导入但连不上网"恰恰是最需要被发现的故障。
        """
        if not deep:
            return super().health(deep=False)

        t0 = time.perf_counter()
        try:
            v = self.version
            # ★ force_refresh：探测必须真的发请求，不能被缓存挡住
            raw = self._invoke(
                "tool_trade_date_hist_sina",
                lambda: self._run_with_timeout(self.ak.tool_trade_date_hist_sina),
                params={"kind": "probe"},
                cache_key="calendar:probe",
                force_refresh=True,
            )
            rows = 0 if raw is None else len(raw)
            return DataSourceStatus(
                name=self.name,
                available=rows > 0,
                version=v,
                message=f"ok，真实请求成功（交易日历 {rows} 行）",
                deep=True,
                latency_ms=round((time.perf_counter() - t0) * 1000, 2),
            )
        except Exception as exc:
            return DataSourceStatus(
                name=self.name,
                available=False,
                version="",
                message=f"{type(exc).__name__}: {exc}",
                deep=True,
                latency_ms=round((time.perf_counter() - t0) * 1000, 2),
            )

    # ------------------------------------------------------------------
    # 个股日线
    # ------------------------------------------------------------------
    def fetch_daily(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
        *,
        adjust: str = "hfq",
    ) -> pd.DataFrame:
        """个股日线。akshare 入参为 6 位纯数字，日期为 YYYYMMDD。"""
        canonical = normalize(symbol)
        code = to_akshare(canonical, "plain")
        s, e = to_compact_date(start_date), to_compact_date(end_date)

        adjust_key = (adjust or "").lower()
        adjust_arg = {"hfq": "hfq", "qfq": "qfq", "": "", "none": ""}.get(adjust_key)
        if adjust_arg is None:
            raise DataSourceError(f"不支持的复权参数：{adjust!r}")

        def _call() -> pd.DataFrame:
            return self.ak.stock_zh_a_hist(
                symbol=code,
                period="daily",
                start_date=s,
                end_date=e,
                adjust=adjust_arg,
                timeout=settings.datasource.timeout,
            )

        raw = self._invoke(
            "stock_zh_a_hist",
            lambda: self._run_with_timeout(_call),
            params={"symbol": code, "start": s, "end": e, "adjust": adjust_arg or "none"},
            cache_key=f"daily:{canonical}:{s}:{e}:{adjust_key}",
        )

        if raw is None or raw.empty:
            return pd.DataFrame(columns=list(QUOTE_COLUMNS))

        df = raw.rename(columns=_HIST_COLUMN_MAP)
        df["symbol"] = canonical
        df = coerce_numeric(df, _NUMERIC_COLS)
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.dropna(subset=["date"])

        # akshare 日线不含停牌/ST 标记 —— 这是我们不用它做回测的原因
        df["preclose"] = df["close"].shift(1)
        df["trade_status"] = 1
        df["is_st"] = pd.NA          # ⚠️ 未知，禁止用 akshare 数据判断 ST

        df = ensure_columns(df, QUOTE_COLUMNS)
        return df.sort_values("date").reset_index(drop=True)

    # ------------------------------------------------------------------
    # 指数日线
    # ------------------------------------------------------------------
    def fetch_index_daily(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
    ) -> pd.DataFrame:
        """指数日线。akshare `index_zh_a_hist` 入参是 6 位纯数字。"""
        canonical = normalize(symbol, is_index=True)
        code = to_akshare(canonical, "plain")
        s, e = to_compact_date(start_date), to_compact_date(end_date)

        def _call() -> pd.DataFrame:
            return self.ak.index_zh_a_hist(
                symbol=code, period="daily", start_date=s, end_date=e
            )

        raw = self._invoke(
            "index_zh_a_hist",
            lambda: self._run_with_timeout(_call),
            params={"symbol": code, "start": s, "end": e},
            cache_key=f"index:{canonical}:{s}:{e}",
        )

        if raw is None or raw.empty:
            return pd.DataFrame(columns=list(INDEX_COLUMNS))

        df = raw.rename(columns=_HIST_COLUMN_MAP)
        df["symbol"] = canonical
        df = coerce_numeric(df, _NUMERIC_COLS)
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.dropna(subset=["date"])
        df["preclose"] = df["close"].shift(1)
        df = ensure_columns(df, INDEX_COLUMNS)
        return df.sort_values("date").reset_index(drop=True)

    # ------------------------------------------------------------------
    # 交易日历
    # ------------------------------------------------------------------
    def fetch_trade_dates(
        self, start_date: str, end_date: str, *, force_refresh: bool = False
    ) -> list[str]:
        """交易日历（新浪源，返回 1990-12-19 ~ 次年年底）。

        Args:
            force_refresh: 跳过缓存，强制真实请求（用于"探测/刷新"场景）
        """
        s, e = normalize_date(start_date), normalize_date(end_date)

        cal = self._invoke(
            "tool_trade_date_hist_sina",
            lambda: self._run_with_timeout(self.ak.tool_trade_date_hist_sina),
            params={"start": s, "end": e},
            cache_key=f"calendar:{s}:{e}",
            cache_ttl=_CALENDAR_CACHE_TTL,
            force_refresh=force_refresh,
        )

        if cal is None or cal.empty:
            return []
        col = "trade_date" if "trade_date" in cal.columns else cal.columns[0]
        ds = pd.to_datetime(cal[col], errors="coerce").dropna()
        mask = (ds >= pd.Timestamp(s)) & (ds <= pd.Timestamp(e))
        return sorted(d.strftime("%Y-%m-%d") for d in ds[mask])

    # ------------------------------------------------------------------
    # 实时行情（baostock 没有，这是 akshare 的不可替代项）
    # ------------------------------------------------------------------
    def fetch_spot(self) -> pd.DataFrame:
        """全市场实时快照（一次拿 5000+ 只）。

        ⚠️ **deprecated（2026-10-01）**：本方法走东财 `stock_zh_a_spot_em`，
           而 `push2.eastmoney.com` 在本机被网络**掐断** → 目前必然失败。
           要全市场快照请用 `fuyao_client.snapshot()`（同花顺官方 REST）。
           项目已定"不再考虑东财 push2"，本方法保留只为让失败点集中可见。

        ⚠️ 实时数据的缓存 TTL 很短（默认 10 秒），否则"实时"就没有意义了。
        """
        raw = self._invoke(
            "stock_zh_a_spot_em",
            lambda: self._run_with_timeout(self.ak.stock_zh_a_spot_em),
            params={"scope": "全市场"},
            cache_key="spot:all",
            cache_ttl=self._spot_cache_ttl,
        )

        if raw is None or raw.empty:
            return pd.DataFrame()
        df = raw.rename(columns=_SPOT_COLUMN_MAP)
        df["symbol"] = df["code"].map(lambda c: normalize(str(c)))
        return df

    # ------------------------------------------------------------------
    # 模块 1：通用试跑（接口目录页的"在线试跑"）
    # ------------------------------------------------------------------
    def call(self, func_name: str, params: dict[str, Any] | None = None) -> pd.DataFrame:
        """按函数名与参数真实调用 akshare。

        ⚠️ 这是全系统唯一接受任意参数的入口，因此在这里**强制**做两道校验：

        1. `func_name` 必须是 akshare 的公开可调用函数（不允许私有函数/属性）
        2. `params` 的键必须是该函数签名里真实存在的参数名（除非它接受 **kwargs）

        为什么参数白名单是必须的：`fn(**kwargs)` 会把任意键透传下去，
        拼错的参数名在部分接口上会静默失效或被忽略，让用户以为"试跑成功了
        但结果不对"。显式报错比静默错好。

        频率控制、缓存、重试由 `_invoke` 统一负责，此处不重复实现。
        """
        self._validate_func(func_name)
        if params is not None and not isinstance(params, dict):
            raise InvalidParamError("params 必须是对象（key-value）")

        fn = getattr(self.ak, func_name)
        kwargs = dict(params or {})
        self._validate_params(func_name, fn, kwargs)

        def _call() -> pd.DataFrame:
            return self._run_with_timeout(lambda: fn(**kwargs))

        try:
            return self._invoke(
                func_name,
                _call,
                params=kwargs,
                cache_key=f"call:{func_name}:{_stable_params_key(kwargs)}",
            )
        except InvalidParamError:
            raise
        except TypeError as exc:
            raise InvalidParamError(f"参数不匹配：{exc}") from exc

    def _validate_func(self, func_name: str) -> None:
        """校验函数名存在于 akshare 且为公开可调用对象。"""
        if not func_name or not isinstance(func_name, str):
            raise InvalidParamError("func_name 必须是非空字符串")
        if func_name.startswith("_"):
            raise InvalidParamError("不允许调用私有函数")
        if not hasattr(self.ak, func_name):
            raise InvalidParamError(f"akshare 中不存在函数：{func_name}")
        if not callable(getattr(self.ak, func_name)):
            raise InvalidParamError(f"{func_name} 不是可调用对象")

    def _validate_params(self, func_name: str, fn: Any, kwargs: dict[str, Any]) -> None:
        """校验参数名属于函数签名（有 **kwargs 时放行）。"""
        if not kwargs:
            return
        try:
            sig = inspect.signature(fn)
        except (TypeError, ValueError):  # pragma: no cover —— 少数内置函数无签名
            return

        accepted: set[str] = set()
        for name, p in sig.parameters.items():
            if p.kind is inspect.Parameter.VAR_KEYWORD:
                return  # 接受任意关键字参数，无需校验
            if p.kind in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            ):
                accepted.add(name)

        unknown = sorted(set(kwargs) - accepted)
        if unknown:
            raise InvalidParamError(
                f"{func_name} 不接受参数 {unknown}。该接口可用参数：{sorted(accepted) or '（无）'}",
                detail={"func_name": func_name, "unknown": unknown, "accepted": sorted(accepted)},
            )

    def function_names(self, prefixes: tuple[str, ...] = ("stock_", "index_", "fund_")) -> list[str]:
        """列出 akshare 可用函数名（供模块 1 生成接口目录骨架）。"""
        return sorted(
            n for n, o in inspect.getmembers(self.ak, inspect.isfunction)
            if n.startswith(prefixes)
        )

    # ------------------------------------------------------------------
    # 内部：超时保护
    # ------------------------------------------------------------------
    def _run_with_timeout(self, fn: Any, timeout: float | None = None) -> pd.DataFrame:
        """akshare 是同步阻塞的，用线程池 + 超时包一层，避免拖死事件循环。"""
        limit = timeout or settings.datasource.timeout
        future = _EXECUTOR.submit(fn)
        try:
            return future.result(timeout=limit)
        except TimeoutError as exc:
            raise DataSourceTimeout(
                f"akshare 调用超时（>{limit}s）"
            ) from exc
        except Exception as exc:
            # 在这一层统一识别限频，转成可重试/可熔断的异常类型
            raise_if_limited(exc)
            raise


#: 全局单例
akshare_client = AkshareClient()
