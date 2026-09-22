"""数据源抽象基类与公共设施。

设计目标：
1. **统一列名与类型** —— 上层业务（回测、因子、模拟盘）不感知数据源差异
2. **统一节流与熔断** —— baostock/akshare 都是免费源，必须自己保护
3. **统一缓存** —— 避免重复请求同一份数据
4. **统一异常** —— 抛出 AppError 子类，接口层可直接转成中文提示
5. **统一调用日志** —— 每次请求落 `datasource_log`，否则出问题无从排查

★ 关键纪律：**所有对外请求方法都必须经由 `BaseDataSource._invoke()`**。
   它是一条流水线：缓存 → 熔断 → 限流 → 重试 → 落缓存 → 记日志。
   把这几件事写在方法里重复一遍，就一定会漏（历史教训：`_rate_limit()`
   在 akshare 客户端的调用次数是 0，`with_retry` / 缓存 / 调用日志全是 0）。
"""
from __future__ import annotations

import functools
import threading
import time
from abc import ABC, abstractmethod
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, TypeVar

import pandas as pd

from app.core.config import settings
from app.core.errors import (
    AppError,
    ConfigError,
    DataSourceError,
    DataSourceRateLimited,
    InvalidParamError,
    NotConfiguredError,
    SymbolFormatError,
)
from app.core.logging import logger
from app.repos import meta_repo
from app.services.data_source.throttle import (
    TIER_DEFINITE,
    TIER_SOFT,
    CircuitBreaker,
    SlidingWindowRateLimiter,
    classify_limit_signal,
)

T = TypeVar("T")

# ============================================================
# 统一列名（行情日线）
# ============================================================
#: 内部统一行情表列（回测/因子/模拟盘的唯一口径）
QUOTE_COLUMNS: tuple[str, ...] = (
    "date",         # date32，交易日
    "symbol",       # string，内部格式 sh.600519
    "open",         # float64
    "high",         # float64
    "low",          # float64
    "close",        # float64
    "preclose",     # float64，前收盘（复权后）
    "volume",       # float64，成交量（股）
    "amount",       # float64，成交额（元）
    "turn",         # float64，换手率 %
    "pct_chg",      # float64，涨跌幅 %
    "trade_status", # int8，1=正常交易 0=停牌
    "is_st",        # int8，1=ST 0=非ST
)

#: 内部统一指数日线列
INDEX_COLUMNS: tuple[str, ...] = (
    "date", "symbol", "open", "high", "low", "close", "preclose",
    "volume", "amount", "pct_chg",
)

#: 永不重试的异常 —— 重试多少次都是同样的结果
#: · DataSourceRateLimited：越重试撞得越狠，交给熔断器静默
#: · 参数/代码格式/配置错误：输入问题，不是偶发问题
_NON_RETRYABLE_EXCEPTIONS = (
    DataSourceRateLimited,
    InvalidParamError,
    SymbolFormatError,
    ConfigError,
    NotConfiguredError,
)


def is_retryable(exc: BaseException) -> bool:
    """判断异常是否值得重试。

    ★ 采用【黑名单】而非白名单。原因：数据源抛出的异常类型来自第三方库
      （requests / urllib3 / pandas / 裸 OSError / socket），枚举不可能穷尽。
      历史 bug 就出在这里 —— 白名单里写了内置 `ConnectionError`，
      而 akshare 抛的是 `requests.exceptions.ConnectionError`，
      **两者没有继承关系**，导致所有网络错误都静默地不重试。
    """
    if isinstance(exc, _NON_RETRYABLE_EXCEPTIONS):
        return False
    if isinstance(exc, DataSourceError):
        return True          # 数据源类错误（含超时）：值得重试
    # 其余业务异常（存储/数据库/回测）：重试无意义；
    # 第三方库异常（requests/urllib3/OSError…）：默认值得重试
    return not isinstance(exc, AppError)


def call_with_retry(fn: Callable[[], T], *, max_attempts: int | None = None) -> T:
    """按统一策略重试执行一次调用：指数退避，黑名单过滤。

    ★ 两种限频信号的处理**不同**，这是一次实测事故的教训：

    - **明确限频**（429 / "频率超限"）：服务端已经说了超速，再试就是主动
      加重封禁 → **不重试**，直接抛给熔断器。
    - **疑似限频**（连接被掐断 RemoteDisconnected / Connection aborted）：
      单次被掐断**不足以证明被限频**，更可能是网络抖动 → **先重试**；
      重试用尽仍失败才升级为限频异常，由 `_invoke` 侧按"连续次数"决定熔断。

    为什么必须这样分：曾经把两者都当"立即限频"，结果批量探测 44 个接口时
    第一个请求遇到一次瞬时断连就熔断 60 秒，后面 43 个接口全被短路，
    一个真请求都没发出去。
    """
    attempts = max(1, max_attempts or settings.datasource.max_retries)

    last_exc: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except BaseException as exc:
            if isinstance(exc, DataSourceRateLimited):
                raise

            tier = classify_limit_signal(exc)

            # 明确限频：不重试
            if tier == TIER_DEFINITE:
                raise DataSourceRateLimited(
                    f"数据源明确限频：{str(exc)[:200]}",
                    detail={"tier": TIER_DEFINITE, "raw": str(exc)[:300]},
                ) from exc

            # 疑似限频与普通网络错误一样参与重试
            retryable = tier == TIER_SOFT or is_retryable(exc)
            if not retryable or attempt >= attempts:
                if tier == TIER_SOFT:
                    raise DataSourceRateLimited(
                        f"数据源疑似限频（连续 {attempts} 次连接被掐断）：{str(exc)[:200]}",
                        detail={"tier": TIER_SOFT, "raw": str(exc)[:300], "attempts": attempts},
                    ) from exc
                raise

            last_exc = exc
            delay = min(0.5 * 2 ** (attempt - 1), 8.0)
            logger.warning(
                f"[retry] {getattr(fn, '__name__', 'call')} "
                f"第 {attempt}/{attempts} 次失败（{tier or '网络'}），"
                f"{delay:.1f}s 后重试：{exc!r}"
            )
            time.sleep(delay)

    if last_exc is not None:  # pragma: no cover —— 循环内已 raise
        raise last_exc
    raise AssertionError("unreachable")  # pragma: no cover


# ============================================================
# TTL 缓存
# ============================================================
class _MissType:
    """「缓存未命中」哨兵（单例，用 `is` 比较）。

    ⚠️ **为什么不能用 `None` 表示未命中**：
       `None` 是一个**合法的缓存值**。`AkshareClient.call()` 可以调用任意 akshare
       函数，其中存在返回 `None` 的接口 —— 用 `None` 表示未命中，这类结果就永远
       无法命中缓存，静默退化成"不缓存"（实测：连续 3 次调用 = 3 次真实请求）。

    `__bool__` 刻意返回 False：这样写成 `if cached:` 的旧代码行为不变
    （仍按"未命中"处理），而严格判断用 `is MISS` / `is not MISS`。
    """

    __slots__ = ()

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return "<CACHE_MISS>"


MISS = _MissType()


@dataclass
class _CacheEntry:
    value: Any
    expire_at: float


class TTLCache:
    """进程内 TTL 缓存（LRU + 过期淘汰，线程安全）。

    用途：模块 1 的"试跑"结果缓存、交易日历缓存、股票列表缓存。

    ⚠️ `get()` 未命中时返回 `MISS` 哨兵，**不是 `None`**。
    """

    def __init__(self, ttl: float = 300.0, maxsize: int = 512) -> None:
        self.ttl = ttl
        self.maxsize = maxsize
        self._lock = threading.Lock()
        self._data: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._hits = 0
        self._misses = 0

    def get(self, key: str) -> Any:
        """取值；未命中（不存在或已过期）返回 `MISS`。"""
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                self._misses += 1
                return MISS
            if entry.expire_at < time.time():
                self._data.pop(key, None)
                self._misses += 1
                return MISS
            self._data.move_to_end(key)
            self._hits += 1
            return entry.value

    def set(self, key: str, value: Any, ttl: float | None = None) -> None:
        with self._lock:
            self._data[key] = _CacheEntry(
                value=value, expire_at=time.time() + (ttl if ttl is not None else self.ttl)
            )
            self._data.move_to_end(key)
            while len(self._data) > self.maxsize:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def stats(self) -> dict[str, int]:
        with self._lock:
            now = time.time()
            alive = sum(1 for e in self._data.values() if e.expire_at >= now)
            return {
                "total": len(self._data),
                "alive": alive,
                "maxsize": self.maxsize,
                "hits": self._hits,
                "misses": self._misses,
            }


# ============================================================
# 重试（装饰器形式）
# ============================================================
def with_retry(
    max_attempts: int | None = None,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """把 `call_with_retry` 包装成装饰器形式。

    策略本体在 `call_with_retry`，这里只是语法糖，避免两处维护重试策略。
    """

    def decorator(fn: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            return call_with_retry(lambda: fn(*args, **kwargs), max_attempts=max_attempts)

        return wrapper

    return decorator


# ============================================================
# 数据源抽象基类
# ============================================================
@dataclass
class DataSourceStatus:
    """数据源健康状态，供 /api/health 与数据源能力矩阵页展示。"""

    name: str
    available: bool
    version: str = ""
    message: str = ""
    #: 本次检查是否**真的做了网络探测**（False = 只读了版本号/熔断状态）
    #: 前端据此区分"真的通了"和"库装上了"——两者完全不同
    deep: bool = False
    checked_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    latency_ms: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "available": self.available,
            "version": self.version,
            "message": self.message,
            "deep": self.deep,
            "checked_at": self.checked_at,
            "latency_ms": self.latency_ms,
        }


def _copy_if_frame(value: Any) -> Any:
    """缓存命中时返回副本。

    为什么必须 copy：调用方常对返回的 DataFrame 做原地修改（补列、改类型），
    直接返回缓存对象会让缓存被污染，后续命中拿到的是"改过的"数据。
    """
    if isinstance(value, pd.DataFrame):
        return value.copy()
    return value


def _format_params(params: dict[str, Any] | None) -> str:
    if not params:
        return ""
    try:
        return ", ".join(f"{k}={v}" for k, v in params.items())
    except Exception:
        return repr(params)


class BaseDataSource(ABC):
    """所有数据源的统一契约。

    子类必须实现：
      - name
      - version / login / logout
      - fetch_daily()        —— 个股日线
      - fetch_index_daily()  —— 指数日线
      - fetch_trade_dates()  —— 交易日历

    ★ 子类发起真实请求时**必须**用 `self._invoke(...)` 包裹，不得直接调用底层库。
      子类如需更严格的节流，覆盖类属性 `_min_interval` / `_max_calls_per_minute`
      / `_circuit_break_seconds` 即可。
    """

    name: str = "base"

    #: 子类可覆盖的节流参数（None = 取 config.datasource.* 的全局值）
    _min_interval: float | None = None
    _max_calls_per_minute: int | None = None
    _circuit_break_seconds: float | None = None
    _cache_ttl: float | None = None

    def __init__(self) -> None:
        ds = settings.datasource
        self._limiter = SlidingWindowRateLimiter(
            min_interval=(
                self._min_interval if self._min_interval is not None else ds.min_interval
            ),
            max_calls_per_minute=self._max_calls_per_minute or 0,
            name=self.name,
        )
        self._breaker = CircuitBreaker(
            cooldown=(
                self._circuit_break_seconds
                if self._circuit_break_seconds is not None
                else 300.0
            ),
            name=self.name,
        )
        self._cache = TTLCache(ttl=self._cache_ttl or float(ds.try_cache_ttl))

    # ---------- 元信息 ----------
    @property
    @abstractmethod
    def version(self) -> str:
        """库版本号，用于锁版本校验。"""

    @abstractmethod
    def login(self) -> None:
        """建立会话（baostock 需要；akshare 为空实现）。"""

    @abstractmethod
    def logout(self) -> None:
        """释放会话。"""

    # ---------- 数据能力 ----------
    @abstractmethod
    def fetch_daily(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
        *,
        adjust: str = "hfq",
    ) -> pd.DataFrame:
        """获取个股日线。返回统一列名（见 QUOTE_COLUMNS）。"""

    @abstractmethod
    def fetch_index_daily(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
    ) -> pd.DataFrame:
        """获取指数日线。symbol 为内部格式（如 sh.000001）。"""

    @abstractmethod
    def fetch_trade_dates(self, start_date: str, end_date: str) -> list[str]:
        """返回区间内的交易日列表（YYYY-MM-DD）。"""

    # ---------- 健康检查 ----------
    def health(self, *, deep: bool = False) -> DataSourceStatus:
        """健康检查。

        Args:
            deep: False（默认）只做**轻量**检查 —— 看库能否导入、读版本号、
                  看熔断状态，**不发任何网络请求**。
                  True 需要子类实现真实探测；基类不提供，因为"探测什么"是
                  数据源自己的能力，基类无从知道。

        ⚠️ 子类若实现 `deep=True`，**必须**用 `force_refresh=True` 发真实请求 ——
           否则探测会被 `_invoke` 的缓存挡住，"探测"变成"读缓存"。
        """
        t0 = time.perf_counter()
        try:
            v = self.version
            breaker = self._breaker.stats()
            message = "ok"
            if deep:
                # 基类无法做真实探测，如实说明，不假装探测过
                message = "ok（轻量检查：未做网络探测）"
            if breaker["open"]:
                message = f"限频熔断中，剩余 {breaker['remaining_seconds']:.0f}s"
            return DataSourceStatus(
                name=self.name,
                available=not breaker["open"],
                version=v,
                message=message,
                deep=False,
                latency_ms=round((time.perf_counter() - t0) * 1000, 2),
            )
        except Exception as exc:
            return DataSourceStatus(
                name=self.name,
                available=False,
                message=f"{type(exc).__name__}: {exc}",
                deep=False,
            )

    # ==================================================================
    # ★ 核心：统一调用流水线
    # ==================================================================
    def _invoke(
        self,
        func_name: str,
        call: Callable[[], T],
        *,
        params: dict[str, Any] | None = None,
        cache_key: str | None = None,
        cache_ttl: float | None = None,
        force_refresh: bool = False,
    ) -> T:
        """执行一次数据源调用：缓存 → 熔断 → 限流 → 重试 → 落缓存 → 记日志。

        Args:
            func_name: 用于日志的接口名（如 stock_zh_a_hist）
            call: 真正发起请求的零参可调用对象
            params: 日志用参数摘要（**不得含密钥**，会被截断到 500 字符）
            cache_key: 非 None 则启用缓存
            cache_ttl: 缓存秒数，None 表示用默认 TTL
            force_refresh: **跳过缓存读取**，强制发真实请求。
                           ⚠️ 语义上是"刷新/探测"的方法**必须**传 True —— 否则
                           会变成"假刷新"：接口报成功，实际读的是缓存。
                           成功后**仍会写入缓存**，把缓存内容更新为最新值。

        Raises:
            DataSourceRateLimited: 熔断期内的调用，或上游返回限频
        """
        param_text = _format_params(params)
        if force_refresh:
            # 让"这次是强制刷新"在 datasource_log 里可查（便于排查假刷新类问题）
            param_text = f"{param_text} [force_refresh]".strip()

        # --- 1) 缓存（force_refresh 时跳过读取）---
        if cache_key is not None and not force_refresh:
            hit = self._cache.get(cache_key)
            if hit is not MISS:
                self._write_log(func_name, "cache_hit", params=param_text)
                return _copy_if_frame(hit)  # type: ignore[return-value]

        # --- 2) 熔断：熔断期内一个请求都不发 ---
        self._breaker.ensure_closed()

        # --- 3~4) 限流 + 重试 ---
        t0 = time.perf_counter()
        try:
            result = call_with_retry(lambda: self._attempt(call, func_name))
        except DataSourceRateLimited as exc:
            # ★ record_failure 按档位判断：definite 立即熔断，soft 累计到阈值才熔断。
            #   曾经这里无条件 trip() —— 一次网络抖动就熔断 60s，
            #   批量任务 44 个接口第一个就被短路，全部报"熔断中"。
            tier = rate_limit_tier(exc)
            tripped = self._breaker.record_failure(str(exc), tier=tier)
            self._write_log(
                func_name, "rate_limited" if tripped else "rate_suspect",
                params=param_text,
                duration=round(time.perf_counter() - t0, 4),
                error_message=f"[{tier}] {exc}",
            )
            raise
        except Exception as exc:
            self._write_log(
                func_name, "error",
                params=param_text,
                duration=round(time.perf_counter() - t0, 4),
                error_message=f"{type(exc).__name__}: {exc}",
            )
            raise

        # --- 5) 成功 → 清零连续失败计数 + 落缓存 + 记日志 ---
        # 没有这一步，零散的失败会永远累积，最终"拼"出一次假熔断
        self._breaker.record_success()
        duration = round(time.perf_counter() - t0, 4)
        rows = len(result) if isinstance(result, pd.DataFrame) else None
        if cache_key is not None:
            self._cache.set(cache_key, result, cache_ttl)
        self._write_log(func_name, "ok", params=param_text, rows=rows, duration=duration)
        return result

    def _attempt(self, call: Callable[[], T], func_name: str) -> T:
        """限流后执行一次真实调用。"""
        waited = self._limiter.acquire()
        if waited > 0.01:
            logger.debug(f"[{self.name}] 限流等待 {waited:.2f}s 后调用 {func_name}")
        return call()

    def wait_if_limited(self, *, max_wait: float = 300.0) -> float:
        """熔断中则阻塞等待其结束（**批量任务**专用）。

        批量补数据 / 接口探测遇到熔断时，正确动作是"等恢复再继续"，
        而不是把整批标记为失败。单次调用不该用它（会卡住请求线程）。
        """
        return self._breaker.wait_until_closed(max_wait=max_wait)

    def _write_log(
        self,
        func_name: str,
        status: str,
        *,
        params: str = "",
        rows: int | None = None,
        duration: float | None = None,
        error_message: str = "",
    ) -> None:
        """落 datasource_log（失败不影响主流程）。"""
        if status in ("error", "rate_limited"):
            logger.warning(f"[{self.name}] {func_name} → {status}：{error_message}")
        meta_repo.log_datasource_call(
            self.name,
            func_name,
            status,
            params=params,
            rows=rows,
            duration=duration,
            error_message=error_message,
        )

    # ---------- 观测 ----------
    def throttle_stats(self) -> dict[str, Any]:
        """限流与熔断状态（供系统状态页展示）。"""
        return {
            "datasource": self.name,
            "rate_limiter": self._limiter.stats(),
            "circuit_breaker": self._breaker.stats(),
            "cache": self._cache.stats(),
        }

    def reset_throttle(self) -> None:
        """手工解除熔断并清空限流窗口（运维用）。"""
        self._breaker.reset()
        self._limiter.reset()
        logger.info(f"[{self.name}] 已手工重置限流窗口与熔断状态")

    def clear_cache(self) -> None:
        self._cache.clear()


# ============================================================
# 结果规范化工具
# ============================================================
def normalize_date(value: Any) -> str:
    """把各种日期写法统一成 YYYY-MM-DD。

    支持：'20240101' / '2024-01-01' / '2024/01/01' / datetime / pd.Timestamp
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        raise DataSourceError("日期为空")
    if isinstance(value, (datetime, pd.Timestamp)):
        return value.strftime("%Y-%m-%d")
    s = str(value).strip()
    if not s:
        raise DataSourceError("日期为空")
    s = s.replace("/", "-").replace(".", "-")
    digits = s.replace("-", "")
    if len(digits) == 8 and digits.isdigit():
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"
    try:
        return pd.Timestamp(s).strftime("%Y-%m-%d")
    except Exception as exc:
        raise DataSourceError(f"无法解析日期：{value!r}") from exc


def to_compact_date(value: Any) -> str:
    """转换成 akshare 常用的 YYYYMMDD。"""
    return normalize_date(value).replace("-", "")


def ensure_columns(df: pd.DataFrame, columns: Iterable[str]) -> pd.DataFrame:
    """确保 DataFrame 含有全部列（缺失的补 NA），并按给定顺序重排。"""
    cols = list(columns)
    for c in cols:
        if c not in df.columns:
            df[c] = pd.NA
    return df[cols]


def coerce_numeric(df: pd.DataFrame, columns: Iterable[str]) -> pd.DataFrame:
    """把指定列强制转成数值类型（无法转换的置 NA）。"""
    for c in columns:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def raise_if_limited(exc: Exception) -> None:
    """把限频类异常识别出来，抛成可熔断的 DataSourceRateLimited。

    识别逻辑统一收敛在 `throttle.classify_limit_signal()`，
    包含 RemoteDisconnected / ConnectionAborted 这类"服务端掐连接"的限频表现。
    档位（definite / soft）写进 `detail`，供熔断器选择冷却时长。
    """
    tier = classify_limit_signal(exc)
    if tier is not None:
        raise DataSourceRateLimited(
            detail={"tier": tier, "raw": str(exc)[:300]}
        ) from exc


def rate_limit_tier(exc: DataSourceRateLimited) -> str:
    """取出限频异常的档位。

    默认按 `definite`（较长冷却）处理 —— 不确定时宁可多静默一会儿，
    也不要继续撞限频。
    """
    detail = getattr(exc, "detail", None)
    if isinstance(detail, dict):
        tier = detail.get("tier")
        if tier in (TIER_DEFINITE, TIER_SOFT):
            return str(tier)
    return classify_limit_signal(exc) or TIER_DEFINITE
