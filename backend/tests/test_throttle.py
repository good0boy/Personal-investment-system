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
from app.services.data_source.line_groups import LINE_GROUPS, line_group_of
from app.services.data_source.throttle import (
    TIER_DEFINITE,
    TIER_SOFT,
    CircuitBreaker,
    GroupedCircuitBreaker,
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


# ======================================================================
# 分组熔断（GroupedCircuitBreaker）—— 2026-10-01 根因修复
# ======================================================================
def _grouped(**kw) -> GroupedCircuitBreaker:
    return GroupedCircuitBreaker(
        kw.pop("cooldown", 10.0), soft_cooldown=kw.pop("soft_cooldown", 0.2), **kw
    )


def test_grouped_breaker_isolates_trips():
    """★ 核心：一条线路熔断，不得连坐其他线路。

    实测故障：东财 push2 域名被网络掐断 → soft 连踩 3 次 → 原来的单例熔断器
    把**整个 akshare 通道**封了 300 秒，连本机正常的 push2ex（涨停池）与
    同花顺接口也一起报"数据源触发限频"。
    """
    g = _grouped(soft_threshold=3, name="akshare")

    for _ in range(3):
        tripped = g.record_failure("RemoteDisconnected 模拟", tier=TIER_SOFT, key="em_push2")
    assert tripped is True
    assert g.is_open("em_push2") is True

    # 其他线路不受影响
    assert g.is_open("ths") is False
    assert g.is_open("em_push2ex") is False
    # 连 one-shot 的 ensure_closed 也放行
    g.ensure_closed("ths")
    g.ensure_closed("em_push2ex")
    # 但故障线路必须拦住
    with pytest.raises(DataSourceRateLimited):
        g.ensure_closed("em_push2")


def test_grouped_breaker_aggregate_semantics():
    """缺省 key 的聚合语义：`open`=任一组，`reset()`=全部重置。"""
    g = _grouped(soft_threshold=1, name="akshare")
    g.record_failure("炸了", tier=TIER_SOFT, key="em_push2")
    g.record_failure("也炸了", tier=TIER_SOFT, key="em_push2his")

    st = g.stats()
    assert st["open"] is True                      # 任一组开 → 源整体不可用
    assert set(st["open_groups"]) == {"em_push2", "em_push2his"}
    assert {g_["key"] for g_ in st["groups"]} == {"em_push2", "em_push2his"}
    # 顶层字段仍在（兼容老前端/老断言）
    assert st["remaining_seconds"] > 0
    assert st["soft_threshold"] == 1

    g.reset()                                      # 一键解除
    assert g.stats()["open_groups"] == []
    assert g.is_open() is False


def test_grouped_breaker_default_key_is_backward_compatible():
    """缺省 key 的行为必须与旧的单组 CircuitBreaker 完全一致。"""
    g = _grouped(cooldown=0.2)
    g.trip("测试", cooldown=0.2)                   # 位置参数 + 关键字都不变
    assert g.is_open() is True
    assert g.remaining() > 0
    assert g.stats()["reason"] == "测试"
    time.sleep(0.25)
    assert g.is_open() is False


def test_grouped_breaker_stats_of_unknown_group_is_clean():
    """查询没熔断过的组不应"顺手创建"出脏状态。"""
    g = _grouped(name="akshare")
    st = g.stats("never_used")
    assert st["open"] is False
    assert st["remaining_seconds"] == 0


def test_line_group_of_known_and_fallback():
    """分组映射：显式声明 → `_ths` 后缀兜底 → 自成一组。"""
    assert line_group_of("stock_zh_a_spot_em") == "em_push2"
    assert line_group_of("stock_board_concept_name_em") == "em_push2"
    assert line_group_of("stock_zh_a_hist") == "em_push2his"
    assert line_group_of("stock_zt_pool_em") == "em_push2ex"
    assert line_group_of("stock_board_industry_name_ths") == "ths"
    assert line_group_of("stock_fund_flow_industry") == "ths"
    assert line_group_of("tool_trade_date_hist_sina") == "sina"
    # 未声明的接口自成一组（有保护，但不与别人共命运）
    assert line_group_of("some_brand_new_func") == "some_brand_new_func"
    assert line_group_of("") == "default"


def test_line_groups_are_disjoint():
    """同一函数不能出现在两个线路组里（会让熔断行为不确定）。"""
    seen: dict[str, str] = {}
    for group, funcs in LINE_GROUPS.items():
        for fn in funcs:
            assert fn not in seen, f"{fn} 同时属于 {seen[fn]} 与 {group}"
            seen[fn] = group
