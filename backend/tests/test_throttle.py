"""限流器与熔断器测试。

★ 这两个机制是 akshare 的"防封 IP"闸门。它们的特点是：
  写错了不会报错 —— 只是限流不生效（照样被限频）。
  所以必须有测试验证**实际速率被压住**，而不只是"对象创建成功"。
"""
from __future__ import annotations

import threading
import time

import pytest

from app.core.errors import DataSourceRateLimited
from app.services.data_source.throttle import (
    TIER_DEFINITE,
    TIER_SOFT,
    CircuitBreaker,
    SlidingWindowRateLimiter,
    classify_limit_signal,
    looks_rate_limited,
)

# ======================================================================
# 滑动窗口限流器
# ======================================================================


def test_min_interval_enforced():
    lim = SlidingWindowRateLimiter(min_interval=0.05)
    t0 = time.monotonic()
    for _ in range(4):
        lim.acquire()
    elapsed = time.monotonic() - t0
    # 4 次调用 = 3 个间隔
    assert elapsed >= 0.14, f"3 个 0.05s 间隔至少 0.15s，实际 {elapsed:.3f}s"


def test_min_interval_zero_is_noop():
    lim = SlidingWindowRateLimiter(min_interval=0.0)
    t0 = time.monotonic()
    for _ in range(50):
        lim.acquire()
    assert time.monotonic() - t0 < 0.5


def test_window_cap_enforced():
    """窗口内次数达上限后必须等待窗口滑出。"""
    lim = SlidingWindowRateLimiter(min_interval=0.0, max_calls_per_minute=2, window=0.4)
    t0 = time.monotonic()
    for _ in range(3):          # 第 3 次必须等
        lim.acquire()
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.3, f"第 3 次调用应等待窗口滑出，实际仅 {elapsed:.3f}s"


def test_no_more_than_cap_calls_per_window():
    """核心正确性：任意 window 长度内调用次数不超过上限。"""
    cap, window = 3, 0.5
    lim = SlidingWindowRateLimiter(min_interval=0.0, max_calls_per_minute=cap, window=window)
    stamps: list[float] = []
    for _ in range(cap * 3):
        lim.acquire()
        stamps.append(time.monotonic())

    # 检查每个 cap+1 长的连续片段，其时间跨度必须 >= window
    for i in range(len(stamps) - cap):
        span = stamps[i + cap] - stamps[i]
        assert span >= window * 0.9, f"第 {i}~{i + cap} 次调用跨度过短：{span:.3f}s"


def test_try_acquire_is_non_blocking():
    lim = SlidingWindowRateLimiter(min_interval=10.0)
    assert lim.try_acquire() is True
    assert lim.try_acquire() is False      # 受 10s 间隔约束，立刻拿不到


def test_stats_tracks_calls_and_wait():
    lim = SlidingWindowRateLimiter(min_interval=0.02)
    for _ in range(3):
        lim.acquire()
    st = lim.stats()
    assert st["total_calls"] == 3
    assert st["total_wait_seconds"] > 0


def test_reset_clears_window():
    lim = SlidingWindowRateLimiter(min_interval=0.0, max_calls_per_minute=1, window=5.0)
    lim.acquire()
    assert lim.try_acquire() is False
    lim.reset()
    assert lim.try_acquire() is True


def test_concurrent_acquire_respects_min_interval():
    """并发场景下不能出现"多线程同时拿到许可"的竞态。"""
    lim = SlidingWindowRateLimiter(min_interval=0.03)
    stamps: list[float] = []
    lock = threading.Lock()

    def worker() -> None:
        for _ in range(3):
            lim.acquire()
            with lock:
                stamps.append(time.monotonic())

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    stamps.sort()
    assert len(stamps) == 12
    gaps = [stamps[i + 1] - stamps[i] for i in range(len(stamps) - 1)]
    assert min(gaps) >= 0.02, f"出现小于 min_interval 的间隔：{min(gaps):.4f}s"


def test_concurrent_calls_per_window_capped():
    lim = SlidingWindowRateLimiter(min_interval=0.0, max_calls_per_minute=4, window=0.4)
    lock = threading.Lock()
    stamps: list[float] = []

    def worker() -> None:
        for _ in range(4):
            lim.acquire()
            with lock:
                stamps.append(time.monotonic())

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    stamps.sort()
    for i in range(len(stamps) - 4):
        assert stamps[i + 4] - stamps[i] >= 0.35


# ======================================================================
# 熔断器
# ======================================================================


def test_breaker_starts_closed():
    cb = CircuitBreaker(cooldown=10, name="t")
    assert cb.is_open() is False
    assert cb.remaining() == 0.0
    cb.ensure_closed()          # 不抛异常


def test_breaker_opens_on_trip():
    cb = CircuitBreaker(cooldown=10, name="t")
    cb.trip("测试")
    assert cb.is_open() is True
    assert cb.remaining() > 0


def test_breaker_ensure_closed_raises_while_open():
    cb = CircuitBreaker(cooldown=10, name="t")
    cb.trip("限频了")
    with pytest.raises(DataSourceRateLimited) as ei:
        cb.ensure_closed()
    assert "熔断" in ei.value.message
    assert ei.value.detail["breaker"] == "t"


def test_breaker_auto_recovers_after_cooldown():
    cb = CircuitBreaker(cooldown=0.2, name="t")
    cb.trip()
    assert cb.is_open() is True
    time.sleep(0.25)
    assert cb.is_open() is False
    cb.ensure_closed()          # 恢复后放行


def test_breaker_soft_cooldown_is_shorter():
    """soft 档（连接被掐断）冷却应明显短于 definite 档。"""
    cb = CircuitBreaker(cooldown=10.0, soft_cooldown=0.2, name="t")
    cb.trip("soft", cooldown=cb.soft_cooldown)
    assert cb.remaining() <= 0.3
    time.sleep(0.25)
    assert cb.is_open() is False


def test_breaker_trip_extends_window():
    cb = CircuitBreaker(cooldown=0.5, name="t")
    cb.trip()
    time.sleep(0.2)
    first = cb.remaining()
    cb.trip()                       # 再次触发 → 重新计时
    assert cb.remaining() > first


def test_breaker_reset():
    cb = CircuitBreaker(cooldown=10, name="t")
    cb.trip()
    cb.reset()
    assert cb.is_open() is False
    cb.ensure_closed()


def test_breaker_stats_shape():
    cb = CircuitBreaker(cooldown=10, soft_cooldown=3, name="t")
    cb.trip("原因X")
    st = cb.stats()
    assert st["name"] == "t"
    assert st["open"] is True
    assert st["trip_count"] == 1
    assert st["cooldown_seconds"] == 10.0
    assert st["soft_cooldown_seconds"] == 3.0
    assert "原因X" in st["reason"]


# ======================================================================
# 限频信号识别
# ======================================================================


@pytest.mark.parametrize(
    "msg",
    [
        "HTTP 429 Too Many Requests",
        "freq out of limit",
        "Rate limit exceeded",
        "访问过于频繁，请稍后重试",
        "请求过于频繁",
        "接口调用频率超限",
    ],
)
def test_definite_signals(msg):
    assert classify_limit_signal(msg) == TIER_DEFINITE


@pytest.mark.parametrize(
    "msg",
    [
        "RemoteDisconnected('Remote end closed connection without response')",
        "Connection aborted.",
        "Connection reset by peer",
    ],
)
def test_soft_signals(msg):
    assert classify_limit_signal(msg) == TIER_SOFT


@pytest.mark.parametrize(
    "msg",
    [
        "",
        "KeyError: 'date'",
        "ValueError: 参数不合法",
        "TypeError: unhashable type",
        # ★ 以下均为「非限频」网络/环境故障 —— 误判成限频会造成连锁误熔断：
        #   实测事故：ProxyError 的消息含 urllib3 包装语 "Max retries exceeded"，
        #   曾被当成 soft 限频，一次代理抖动就熔断整批任务
        "requests.exceptions.ProxyError: Max retries exceeded with url: ...",
        "HTTPSConnectionPool: Max retries exceeded",
        "SSLError: certificate verify failed",
        "Name or service not known",
        "getaddrinfo failed",
    ],
)
def test_non_limit_signals(msg):
    assert classify_limit_signal(msg) is None
    assert looks_rate_limited(msg) is False


def test_breaker_definite_trips_immediately_soft_needs_strikes():
    """definite 一次即熔断；soft 累计到阈值才熔断。"""
    cb = CircuitBreaker(cooldown=30, soft_cooldown=5, soft_threshold=3, name="t")

    assert cb.record_failure("429", tier=TIER_DEFINITE) is True
    assert cb.is_open() is True
    cb.reset()

    assert cb.record_failure("断连1", tier=TIER_SOFT) is False
    assert cb.record_failure("断连2", tier=TIER_SOFT) is False
    assert cb.is_open() is False
    assert cb.record_failure("断连3", tier=TIER_SOFT) is True
    assert cb.is_open() is True
    assert cb.stats()["remaining_seconds"] <= 5.0


def test_breaker_success_clears_strikes():
    cb = CircuitBreaker(cooldown=30, soft_threshold=3, name="t")
    cb.record_failure("a", tier=TIER_SOFT)
    cb.record_failure("b", tier=TIER_SOFT)
    cb.record_success()
    assert cb.stats()["soft_strikes"] == 0
    assert cb.is_open() is False


def test_breaker_wait_until_closed(monkeypatch):
    cb = CircuitBreaker(cooldown=0.3, name="t")
    cb.trip("测试等待")
    waited = cb.wait_until_closed(max_wait=5.0, poll=0.05)
    assert waited >= 0.2
    assert cb.is_open() is False


def test_looks_rate_limited_covers_both_tiers():
    assert looks_rate_limited("429") is True
    assert looks_rate_limited("Connection aborted") is True
