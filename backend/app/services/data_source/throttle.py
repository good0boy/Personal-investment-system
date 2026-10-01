"""数据源节流与熔断 —— 免费数据源的自我保护。

akshare / baostock 都没有配额查询接口，只能自己约束请求速率。两类机制：

**1. SlidingWindowRateLimiter —— 双约束限流**

    · `min_interval`         两次调用之间的最小间隔（防瞬时连发）
    · `max_calls_per_minute` 60 秒滑动窗口内的最大调用次数（防总体超速）

    两个约束**同时生效**，任一不满足则阻塞等待。注意二者并不等价：
    "1 秒 1 次"等于 60 次/分钟，所以"30 次/分钟"其实更严格。必须都实现。

**2. CircuitBreaker —— 熔断**

    一旦识别到"被限频"（HTTP 429 / RemoteDisconnected / 频率超限提示 / 空响应），
    立即进入熔断期，期间所有调用**直接抛异常，根本不发请求**。

    为什么要熔断：**被限频后继续请求只会加重封禁**。熔断是止损，不是惩罚。
    服务端限频通常带惩罚窗口（越撞越长），停手等窗口过去才是最优解。

⚠️ 两个机制必须被 `BaseDataSource._invoke()` 真正调用，否则等于没有。
   历史上 `_rate_limit()` 在 akshare 客户端的调用次数是 **0** ——
   配置写了、类写了、就是没接上。
"""
from __future__ import annotations

import threading
import time
from collections import deque
from datetime import datetime
from typing import Any

from app.core.errors import DataSourceRateLimited
from app.core.logging import logger


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ======================================================================
# 滑动窗口限流器
# ======================================================================
class SlidingWindowRateLimiter:
    """双约束限流器：最小间隔 + 滑动窗口次数上限（线程安全）。

    用法：
        limiter = SlidingWindowRateLimiter(min_interval=1.0, max_calls_per_minute=30)
        limiter.acquire()        # 需要时阻塞
        do_request()
    """

    def __init__(
        self,
        min_interval: float = 0.0,
        max_calls_per_minute: int = 0,
        *,
        window: float = 60.0,
        name: str = "",
    ) -> None:
        self.min_interval = max(0.0, float(min_interval))
        self.max_calls_per_minute = max(0, int(max_calls_per_minute))
        self.window = max(1.0, float(window))
        self.name = name

        self._lock = threading.Lock()
        self._last_call = 0.0                 # monotonic 时间戳
        self._stamps: deque[float] = deque()  # 窗口内的调用时刻

        # 统计（供系统状态页展示）
        self._total_calls = 0
        self._total_wait = 0.0
        self._max_wait = 0.0

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _prune(self, now: float) -> None:
        """丢弃滑出窗口的时间戳。调用方必须已持锁。"""
        cutoff = now - self.window
        while self._stamps and self._stamps[0] <= cutoff:
            self._stamps.popleft()

    def _wait_needed(self, now: float) -> float:
        """距离可以发起下一次调用还需等待多少秒（0 = 立刻可发）。调用方必须已持锁。"""
        wait = 0.0

        # 约束一：最小间隔
        if self.min_interval > 0:
            wait = max(wait, self.min_interval - (now - self._last_call))

        # 约束二：滑动窗口次数
        if self.max_calls_per_minute > 0:
            self._prune(now)
            if len(self._stamps) >= self.max_calls_per_minute:
                # 必须等到窗口内"最老的那次"滑出窗口
                oldest_keep = self._stamps[len(self._stamps) - self.max_calls_per_minute]
                wait = max(wait, oldest_keep + self.window - now)

        return max(0.0, wait)

    # ------------------------------------------------------------------
    # 对外
    # ------------------------------------------------------------------
    def acquire(self) -> float:
        """获取一次调用许可，必要时阻塞。返回实际等待秒数。"""
        total_wait = 0.0
        while True:
            with self._lock:
                now = time.monotonic()
                wait = self._wait_needed(now)
                if wait <= 0:
                    self._last_call = now
                    self._stamps.append(now)
                    self._prune(now)
                    self._total_calls += 1
                    self._total_wait += total_wait
                    self._max_wait = max(self._max_wait, total_wait)
                    return total_wait

            # 不在锁内睡眠（否则会阻塞其他线程的检查）
            nap = min(wait, 0.5)
            time.sleep(nap)
            total_wait += nap

    def try_acquire(self) -> bool:
        """非阻塞获取许可；立刻可用则返回 True，否则 False。"""
        with self._lock:
            now = time.monotonic()
            if self._wait_needed(now) > 0:
                return False
            self._last_call = now
            self._stamps.append(now)
            self._prune(now)
            self._total_calls += 1
            return True

    def reset(self) -> None:
        with self._lock:
            self._last_call = 0.0
            self._stamps.clear()

    def stats(self) -> dict[str, Any]:
        with self._lock:
            now = time.monotonic()
            self._prune(now)
            in_window = len(self._stamps)
            return {
                "name": self.name,
                "min_interval": self.min_interval,
                "max_calls_per_minute": self.max_calls_per_minute,
                "calls_in_window": in_window,
                "window_seconds": self.window,
                "total_calls": self._total_calls,
                "total_wait_seconds": round(self._total_wait, 3),
                "max_wait_seconds": round(self._max_wait, 3),
            }


# ======================================================================
# 熔断器
# ======================================================================
class CircuitBreaker:
    """限频熔断器：确认被限频后，强制静默一段时间。

    ⚠️ 熔断期间 `ensure_closed()` 直接抛 `DataSourceRateLimited`，
       **一个请求都不会发出去**。

    冷却时长分两档，因为两类"限频"的**证据强度**不同：

    ============ ============================================ ========== ==========
    档位          触发信号                                      连续阈值    冷却
    ============ ============================================ ========== ==========
    definite      429 / freq out of limit / "频率超限"          1 次       5 分钟
    soft          连接被掐断（RemoteDisconnected 等）            3 次       1 分钟
    ============ ============================================ ========== ==========

    ★ 为什么 soft 档必须有**连续次数阈值**（而不是一次就熔断）
    ---------------------------------------------------------
    实测踩过这个坑：批量探测 44 个接口时，**第一个请求**遇到一次
    `RemoteDisconnected`，被判定为 soft 限频后立即熔断 60 秒，
    导致后面 43 个接口全部被短路、一个真请求都没发出去。

    单次连接被掐断**不足以证明被限频** —— 它更可能是网络抖动。
    用一次抖动废掉整批任务是明显的过度反应；而连续多次被掐断，
    才说明服务端确实在拒绝我们。

    definite 档保持"一次即熔断"：服务端已经明确说了超速，
    再试就是主动加重封禁。
    """

    def __init__(
        self,
        cooldown: float = 300.0,
        *,
        soft_cooldown: float = 60.0,
        soft_threshold: int = 3,
        name: str = "",
    ) -> None:
        self.cooldown = max(0.0, float(cooldown))
        self.soft_cooldown = max(0.0, float(soft_cooldown))
        self.soft_threshold = max(1, int(soft_threshold))
        self.name = name
        self._lock = threading.Lock()
        self._open_until = 0.0     # monotonic；0 = 未熔断
        self._reason = ""
        self._trip_count = 0
        self._last_trip_at: str | None = None
        self._soft_strikes = 0     # 连续 soft 失败计数（成功一次即清零）

    # ------------------------------------------------------------------
    def _is_open_locked(self) -> bool:
        if self._open_until <= 0:
            return False
        if time.monotonic() >= self._open_until:
            # 冷却结束，自动恢复
            self._open_until = 0.0
            self._reason = ""
            return False
        return True

    def is_open(self) -> bool:
        with self._lock:
            return self._is_open_locked()

    def remaining(self) -> float:
        """剩余熔断秒数（未熔断返回 0）。"""
        with self._lock:
            if not self._is_open_locked():
                return 0.0
            return max(0.0, self._open_until - time.monotonic())

    # ------------------------------------------------------------------
    def trip(self, reason: str = "", *, cooldown: float | None = None) -> None:
        """**强制**熔断（不累计次数）。用于测试与人工干预。

        正常路径请用 `record_failure()` —— 它按档位判断是否该熔断。
        """
        span = self.cooldown if cooldown is None else max(0.0, float(cooldown))
        with self._lock:
            self._open_until = time.monotonic() + span
            self._reason = reason
            self._trip_count += 1
            self._soft_strikes = 0
            self._last_trip_at = _now_iso()
        logger.warning(
            f"[{self.name or 'datasource'}] 触发限频熔断，"
            f"静默 {span:.0f} 秒。原因：{reason or '未提供'}"
        )

    def record_failure(self, reason: str = "", *, tier: str | None = None) -> bool:
        """记录一次限频信号，返回**是否已触发熔断**。

        - `definite`：立即熔断（服务端已明确告知超速，再试只会加重封禁）
        - `soft`：累计连续次数，达到 `soft_threshold` 才熔断

        ⚠️ 返回值必须被调用方使用。只记不判，等于把 soft 档的阈值白设了。
        """
        tier_v = tier or TIER_DEFINITE
        with self._lock:
            if tier_v == TIER_DEFINITE:
                span = self.cooldown
                strikes_after = 0
            else:
                self._soft_strikes += 1
                span = self.soft_cooldown
                strikes_after = self._soft_strikes

            if tier_v == TIER_DEFINITE or strikes_after >= self.soft_threshold:
                self._open_until = time.monotonic() + span
                self._reason = reason
                self._trip_count += 1
                self._last_trip_at = _now_iso()
                self._soft_strikes = 0
                tripped = True
                strikes_now = strikes_after or self.soft_threshold
            else:
                tripped = False
                strikes_now = strikes_after

        if tripped:
            logger.warning(
                f"[{self.name or 'datasource'}] 触发限频熔断（{tier_v}），"
                f"静默 {span:.0f} 秒。累计 {strikes_now} 次。原因：{reason or '未提供'}"
            )
        else:
            logger.info(
                f"[{self.name or 'datasource'}] 疑似限频（{tier_v}）"
                f"第 {strikes_now}/{self.soft_threshold} 次，暂不熔断，继续尝试。"
                f"原因：{reason or '未提供'}"
            )
        return tripped

    def record_success(self) -> None:
        """记录一次成功调用 → 清零连续失败计数。

        没有这一步，失败计数会永远累积，一天里零散的几次闪断
        最终会拼成一次"熔断"，而实际上服务一直是通的。
        """
        with self._lock:
            self._soft_strikes = 0

    def wait_until_closed(self, *, max_wait: float = 300.0, poll: float = 1.0) -> float:
        """熔断中则阻塞等待其结束（供**批量任务**使用）。

        为什么需要它：熔断期间 `ensure_closed()` 会直接抛异常，
        批量任务（补数据、接口探测）若不做等待，就会把"等一会儿就能好"
        的情况当成"全部失败"，44 个接口一个都不跑。

        Returns:
            实际等待秒数；未熔断或在 max_wait 内未恢复则返回已等待时长。
        """
        waited = 0.0
        while waited < max_wait:
            remaining = self.remaining()
            if remaining <= 0:
                break
            nap = min(poll, remaining)
            time.sleep(nap)
            waited += nap
        if waited > 0:
            logger.info(f"[{self.name or 'datasource'}] 批量任务等待熔断结束 {waited:.1f}s")
        return waited

    def ensure_closed(self) -> None:
        """熔断中 → 抛 DataSourceRateLimited；否则放行。"""
        remaining = self.remaining()
        if remaining > 0:
            with self._lock:
                reason = self._reason
            raise DataSourceRateLimited(
                f"数据源处于限频熔断中，剩余 {remaining:.0f} 秒。"
                f"已跳过请求以避免加重封禁。原因：{reason or '触发了数据源限频'}",
                detail={
                    "breaker": self.name,
                    "remaining_seconds": round(remaining, 1),
                    "reason": reason,
                },
            )

    def reset(self) -> None:
        with self._lock:
            self._open_until = 0.0
            self._reason = ""

    def stats(self) -> dict[str, Any]:
        with self._lock:
            is_open = self._is_open_locked()
            remaining = max(0.0, self._open_until - time.monotonic()) if is_open else 0.0
            return {
                "name": self.name,
                "open": is_open,
                "remaining_seconds": round(remaining, 1),
                "cooldown_seconds": self.cooldown,
                "soft_cooldown_seconds": self.soft_cooldown,
                # 连续 soft 失败计数：前端可显示"疑似限频 2/3 次"
                "soft_strikes": self._soft_strikes,
                "soft_threshold": self.soft_threshold,
                "trip_count": self._trip_count,
                "last_trip_at": self._last_trip_at,
                "reason": self._reason,
            }


class GroupedCircuitBreaker:
    """**按线路分组**的熔断门面 —— 一条线路挂了只熔断该线路，不连坐。

    ★ 为什么需要它（2026-10-01 实测故障）
    ------------------------------------
    东财 `push2` 域名在本机被网络掐断 → soft 信号连踩 3 次 → 原来的
    单例 `CircuitBreaker` 把**整个 akshare 通道**封 300 秒 →
    连本机正常的 `push2ex`（涨停池）与同花顺接口也一起报"数据源触发限频"。
    现象是"所有东财接口都挂了"，真相是一个域名把整个源带下了线。

    本类用 `dict[key, CircuitBreaker]` 隔离各组：`key` 由
    `line_groups.line_group_of()` 从 `func_name` 推出。

    ★ 设计取舍：**不修改 `CircuitBreaker` 本身**
    ------------------------------------------
    保留单组版 `CircuitBreaker` 作原样单元（现有 20+ 条断言零改动），
    本类只做"分桶 + 分发 + 聚合"。风险最小，且两边职责清晰。

    ★ `key` 缺省的语义（刻意设计，用于向后兼容）
    --------------------------------------------
    - `is_open()` / `remaining()` / `stats()` / `wait_until_closed()`
      → **聚合**：任一组熔断即视为"这个源现在不能整体用"。
    - `reset()` → **全部重置**（就是运维要的"一键解除"）。
    - `ensure_closed()` / `record_failure()` / `record_success()` / `trip()`
      → 命中 `default_key` 组，行为与旧单例完全一致。
    """

    def __init__(
        self,
        cooldown: float = 300.0,
        *,
        soft_cooldown: float = 60.0,
        soft_threshold: int = 3,
        name: str = "",
        default_key: str = "default",
    ) -> None:
        self.cooldown = max(0.0, float(cooldown))
        self.soft_cooldown = max(0.0, float(soft_cooldown))
        self.soft_threshold = max(1, int(soft_threshold))
        self.name = name
        self.default_key = default_key
        self._lock = threading.Lock()
        self._groups: dict[str, CircuitBreaker] = {}

    # ------------------------------------------------------------------
    def _group(self, key: str | None) -> CircuitBreaker:
        """取（必要时创建）某组的熔断器。"""
        k = key or self.default_key
        with self._lock:
            cb = self._groups.get(k)
            if cb is None:
                cb = CircuitBreaker(
                    self.cooldown,
                    soft_cooldown=self.soft_cooldown,
                    soft_threshold=self.soft_threshold,
                    name=f"{self.name}:{k}" if self.name else k,
                )
                self._groups[k] = cb
            return cb

    def group_keys(self) -> list[str]:
        """已创建过状态的组名（供状态查询 / 测试断言）。"""
        with self._lock:
            return sorted(self._groups)

    # ------------------------------------------------------------------
    def ensure_closed(self, key: str | None = None) -> None:
        """该组熔断中 → 抛 DataSourceRateLimited；否则放行。"""
        self._group(key).ensure_closed()

    # ⚠️ `reason` 必须是第一个位置参数 —— `base.py` 以 `record_failure(str(exc), tier=...)`
    #    的位置传参调用，签名顺序改了会静默把异常文本当成 key。
    def record_failure(
        self, reason: str = "", *, tier: str | None = None, key: str | None = None
    ) -> bool:
        """记录一次限频信号，返回**是否已触发熔断**。"""
        return self._group(key).record_failure(reason, tier=tier)

    def record_success(self, key: str | None = None) -> None:
        self._group(key).record_success()

    def trip(
        self, reason: str = "", *, cooldown: float | None = None, key: str | None = None
    ) -> None:
        """**强制**熔断某组（不累计次数）。用于测试与人工干预。"""
        self._group(key).trip(reason, cooldown=cooldown)

    # ------------------------------------------------------------------
    def is_open(self, key: str | None = None) -> bool:
        """`key` 给定 → 只看该组；缺省 → **任一组**熔断即 True。"""
        if key is not None:
            return self._group(key).is_open()
        with self._lock:
            groups = list(self._groups.values())
        return any(cb.is_open() for cb in groups)

    def remaining(self, key: str | None = None) -> float:
        """`key` 给定 → 该组剩余秒数；缺省 → 各组中**最长**的剩余。"""
        if key is not None:
            return self._group(key).remaining()
        with self._lock:
            groups = list(self._groups.values())
        return max((cb.remaining() for cb in groups), default=0.0)

    def wait_until_closed(
        self, key: str | None = None, *, max_wait: float = 300.0, poll: float = 1.0
    ) -> float:
        """熔断中则阻塞等待（**批量任务**专用）。缺省 = 等所有组结束。"""
        if key is not None:
            return self._group(key).wait_until_closed(max_wait=max_wait, poll=poll)

        waited = 0.0
        while waited < max_wait:
            remaining = self.remaining()
            if remaining <= 0:
                break
            nap = min(poll, remaining)
            time.sleep(nap)
            waited += nap
        if waited > 0:
            logger.info(
                f"[{self.name or 'datasource'}] 批量任务等待熔断结束 {waited:.1f}s"
            )
        return waited

    def reset(self, key: str | None = None) -> None:
        """`key` 给定 → 重置该组；缺省 → **全部重置**（运维"一键解除"）。"""
        if key is not None:
            self._group(key).reset()
            return
        with self._lock:
            groups = list(self._groups.values())
        for cb in groups:
            cb.reset()

    def stats(self, key: str | None = None) -> dict[str, Any]:
        """`key` 给定 → 该组明细；缺省 → 聚合（顶层取最严重的组 + `groups` 明细）。

        顶层字段保持与单组版**完全相同的键** —— 前端与既有测试不必判空，
        `open` / `remaining_seconds` 等直接可用。
        """
        if key is not None:
            return self._group(key).stats()

        with self._lock:
            items = [(k, cb.stats()) for k, cb in self._groups.items()]

        if items:
            worst_key, worst = max(
                items, key=lambda kv: kv[1]["remaining_seconds"]
            )
            base = dict(worst)
            base["name"] = f"{self.name}:{worst_key}" if self.name else worst_key
        else:
            base = {
                "name": self.name,
                "open": False,
                "remaining_seconds": 0.0,
                "cooldown_seconds": self.cooldown,
                "soft_cooldown_seconds": self.soft_cooldown,
                "soft_strikes": 0,
                "soft_threshold": self.soft_threshold,
                "trip_count": 0,
                "last_trip_at": None,
                "reason": "",
            }

        base["groups"] = [{"key": k, **s} for k, s in sorted(items)]
        base["open_groups"] = [k for k, s in sorted(items) if s["open"]]
        return base


# ======================================================================
# 限频信号识别
# ======================================================================
#: 「确定限频」信号 —— 服务端明确告诉你超速了
DEFINITE_LIMIT_KEYWORDS = (
    "freq out of limit",
    "too many requests",
    "rate limit",
    "429",
    "频率",
    "限频",
    "访问过于频繁",
    "请求过于频繁",
    "操作过于频繁",
)

#: 「疑似限频」信号 —— 连接被掐断。服务端限频时常这么做，但也可能是网络抖动
CONNECTION_ABORT_KEYWORDS = (
    "remote end closed connection",
    "remotedisconnected",
    "connection aborted",
    "connection reset",
)

#: ★ 明确的「非限频」网络故障 —— 与限频无关，误判会造成连锁误熔断
#：
#: 为什么必须有这个黑名单：实测踩过的坑 ——
#:   `requests.exceptions.ProxyError` 的 str() 里含 "Max retries exceeded"，
#:   若按关键词匹配会被判成 soft 限频，一次代理抖动就熔断整批任务。
#:   代理故障 / 证书问题应该走普通重试路径，与限频无关。
NOT_LIMIT_KEYWORDS = (
    "proxyerror",           # requests.exceptions.ProxyError.__name__.lower()
    "proxy error",
    "proxy_",
    "sslerror",
    "ssl error",
    "certificate verify failed",
    "name or service not known",   # DNS 解析失败
    "nodename nor servname",
    "getaddrinfo failed",
)

#: 兼容旧调用
_RATE_LIMIT_KEYWORDS = DEFINITE_LIMIT_KEYWORDS + CONNECTION_ABORT_KEYWORDS

#: classify_limit_signal 的返回值
TIER_DEFINITE = "definite"
TIER_SOFT = "soft"


def classify_limit_signal(exc: BaseException | str) -> str | None:
    """判断异常/消息属于哪一档限频信号。

    Returns:
        "definite" —— 确定限频（429 / 频率超限提示）
        "soft"     —— 疑似限频（连接被掐断）
        None       —— 不是限频信号

    为什么要分档：东财/新浪这类接口在限频时往往**直接掐断连接**而不返回 429，
    表现为 `RemoteDisconnected`。把它当普通网络错误重试会持续撞墙，
    所以它必须参与熔断；但证据强度不如 429，冷却时长应当更短。

    ⚠️ 为什么"非限频"黑名单要先于关键词匹配：`ProxyError` 的消息里带着
       urllib3 的 "Max retries exceeded"，曾因此被误判成限频 →
       一次代理抖动 = 立即熔断 60s = 整批任务全废（实测事故）。
       另外 "max retries exceeded" 这类**包装语**本身不携带限频信息，
       几乎所有 requests 连接错误都带，不能当信号用。
    """
    msg = str(exc).lower()
    if not msg:
        return None

    # 0) 先排除明确的非限频故障（代理 / SSL / DNS）
    if any(kw in msg for kw in NOT_LIMIT_KEYWORDS):
        return None

    # 1) 明确限频
    if any(kw in msg for kw in DEFINITE_LIMIT_KEYWORDS):
        return TIER_DEFINITE
    # 2) 疑似限频（连接被掐断）
    if any(kw in msg for kw in CONNECTION_ABORT_KEYWORDS):
        return TIER_SOFT
    return None


def looks_rate_limited(exc: BaseException | str) -> bool:
    """是否属于限频信号（含两档）。"""
    return classify_limit_signal(exc) is not None
