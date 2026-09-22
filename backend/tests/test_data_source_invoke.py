"""`BaseDataSource._invoke()` 调用流水线测试。

★ 这是 P1 的回归防线。背景（2026-09-17 自查报告）：
  限流、缓存、重试、调用日志**四个机制全部写了但一次都没调用** ——
  `with_retry` / `_cache_get` / `_cache_set` / `log_datasource_call`
  的引用数都是 0，`datasource_log` 表永远是空的。

  这类问题不报错、不影响功能，只是让"保护"悄悄失效。
  所以必须有测试确认**每个机制都被真正触发**，而不只是"函数存在"。
"""
from __future__ import annotations

import pandas as pd
import pytest

import app.services.data_source.base as base_mod
from app.core.errors import DataSourceRateLimited
from app.repos import meta_repo
from app.services.data_source.base import BaseDataSource


class FakeSource(BaseDataSource):
    """最小可用数据源，用于测试流水线本身（不发任何真实请求）。"""

    name = "fake"
    _min_interval = 0.0
    _max_calls_per_minute = 0
    _cache_ttl = 60.0

    @property
    def version(self) -> str:
        return "test-1.0"

    def login(self) -> None:
        return None

    def logout(self) -> None:
        return None

    def fetch_daily(self, symbol, start_date, end_date, *, adjust="hfq"):
        return pd.DataFrame()

    def fetch_index_daily(self, symbol, start_date, end_date):
        return pd.DataFrame()

    def fetch_trade_dates(self, start_date, end_date):
        return []


class CallRecorder:
    """记录 `log_datasource_call` 的调用（避免测试写入真实 meta.db）。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, datasource, func_name, status, **kw) -> None:
        self.calls.append(
            {"datasource": datasource, "func_name": func_name, "status": status, **kw}
        )

    def statuses(self) -> list[str]:
        return [c["status"] for c in self.calls]

    def find(self, status: str) -> dict | None:
        return next((c for c in self.calls if c["status"] == status), None)


@pytest.fixture
def src() -> FakeSource:
    return FakeSource()


@pytest.fixture
def recorder(monkeypatch) -> CallRecorder:
    rec = CallRecorder()
    monkeypatch.setattr(meta_repo, "log_datasource_call", rec)
    return rec


# ======================================================================
# 调用日志
# ======================================================================
def test_success_is_logged_with_rows(src, recorder):
    df = pd.DataFrame({"a": [1, 2, 3]})
    src._invoke("some_fn", lambda: df, params={"x": 1}, cache_key=None)

    assert recorder.statuses() == ["ok"]
    entry = recorder.find("ok")
    assert entry["datasource"] == "fake"
    assert entry["func_name"] == "some_fn"
    assert entry["rows"] == 3
    assert entry["duration"] is not None
    assert "x=1" in entry["params"]


def test_error_is_logged_and_propagated(src, recorder):
    def boom():
        raise ValueError("炸了")

    with pytest.raises(ValueError):
        src._invoke("bad_fn", boom, cache_key=None)

    entry = recorder.find("error")
    assert entry is not None
    assert "ValueError" in entry["error_message"]


def test_rate_limit_is_logged_and_trips_breaker(src, recorder):
    def limited():
        raise DataSourceRateLimited(
            "被限频", detail={"tier": "definite", "raw": "429"}
        )

    with pytest.raises(DataSourceRateLimited):
        src._invoke("rl_fn", limited, cache_key=None)

    assert "rate_limited" in recorder.statuses()
    assert src.throttle_stats()["circuit_breaker"]["open"] is True


def test_soft_rate_limit_needs_consecutive_strikes(src, recorder):
    """★ 新语义：单次 soft 信号不熔断（可能是网络抖动），连续达到阈值才熔断。

    实测事故：批量探测时第一个请求遇到一次断连就熔断 60s，
    后面 43 个接口全被短路 —— 阈值就是为此加的。
    """
    def aborted():
        raise DataSourceRateLimited("连接被拒", detail={"tier": "soft", "raw": "x"})

    # 前两次：不熔断
    for _ in range(src._breaker.soft_threshold - 1):
        with pytest.raises(DataSourceRateLimited):
            src._invoke("abort_fn", aborted, cache_key=None)
        assert src.throttle_stats()["circuit_breaker"]["open"] is False

    # 第 3 次（达到默认阈值）：熔断，且是 soft 短冷却
    with pytest.raises(DataSourceRateLimited):
        src._invoke("abort_fn", aborted, cache_key=None)
    st = src.throttle_stats()["circuit_breaker"]
    assert st["open"] is True
    assert st["remaining_seconds"] <= st["soft_cooldown_seconds"]


def test_success_resets_soft_strikes(src, recorder):
    """成功一次必须清零连续失败计数 —— 否则零散闪断会拼成假熔断。"""
    def aborted():
        raise DataSourceRateLimited("连接被拒", detail={"tier": "soft", "raw": "x"})

    for _ in range(src._breaker.soft_threshold - 1):
        with pytest.raises(DataSourceRateLimited):
            src._invoke("abort_fn", aborted, cache_key=None)
    # 成功 → 清零
    src._invoke("ok_fn", lambda: "fine", cache_key=None)
    assert src.throttle_stats()["circuit_breaker"]["soft_strikes"] == 0
    # 之后再来一次失败，重新从 1 计数
    with pytest.raises(DataSourceRateLimited):
        src._invoke("abort_fn", aborted, cache_key=None)
    assert src.throttle_stats()["circuit_breaker"]["soft_strikes"] == 1
    assert src.throttle_stats()["circuit_breaker"]["open"] is False


# ======================================================================
# 缓存
# ======================================================================
def test_cache_hit_skips_real_call(src, recorder):
    calls = {"n": 0}

    def real():
        calls["n"] += 1
        return pd.DataFrame({"a": [1]})

    src._invoke("cached_fn", real, cache_key="k1")
    src._invoke("cached_fn", real, cache_key="k1")
    src._invoke("cached_fn", real, cache_key="k1")

    assert calls["n"] == 1
    assert recorder.statuses() == ["ok", "cache_hit", "cache_hit"]


def test_cache_returns_independent_copy(src, recorder):
    """缓存命中必须返回副本，否则调用方的原地修改会污染缓存。"""
    src._invoke("fn", lambda: pd.DataFrame({"a": [1, 2, 3]}), cache_key="k")

    first = src._invoke("fn", lambda: pd.DataFrame({"a": [9]}), cache_key="k")
    first["a"] = 999

    second = src._invoke("fn", lambda: pd.DataFrame({"a": [9]}), cache_key="k")
    assert second["a"].iloc[0] == 1, "缓存被调用方污染了"


def test_no_cache_key_means_no_caching(src, recorder):
    calls = {"n": 0}

    def real():
        calls["n"] += 1
        return pd.DataFrame({"a": [1]})

    src._invoke("fn", real, cache_key=None)
    src._invoke("fn", real, cache_key=None)
    assert calls["n"] == 2


def test_different_cache_keys_are_isolated(src, recorder):
    calls = {"n": 0}

    def real():
        calls["n"] += 1
        return pd.DataFrame({"a": [calls["n"]]})

    src._invoke("fn", real, cache_key="a")
    src._invoke("fn", real, cache_key="b")
    assert calls["n"] == 2


def test_cache_expiry_after_ttl(src, recorder):
    calls = {"n": 0}

    def real():
        calls["n"] += 1
        return pd.DataFrame({"a": [1]})

    src._invoke("fn", real, cache_key="k", cache_ttl=0.05)
    src._invoke("fn", real, cache_key="k", cache_ttl=0.05)
    assert calls["n"] == 1

    import time

    time.sleep(0.08)
    src._invoke("fn", real, cache_key="k", cache_ttl=0.05)
    assert calls["n"] == 2


# ======================================================================
# 缓存值可以是 None（哨兵，2026-09-17 修复）
# ======================================================================
def test_cache_can_store_none_result(src, recorder):
    """★ 回归：返回 `None` 的函数也必须能被缓存。

    历史缺陷：`TTLCache.get()` 用 `None` 表示"未命中"，而被缓存的值本身也可能
    是 `None` —— 两者无法区分，于是返回 None 的接口**永远命中不了缓存**，
    静默退化成"不缓存"（实测：连续 3 次调用 = 3 次真实请求）。

    影响面：`AkshareClient.call()` 可以调用任意 akshare 函数，其中存在返回
    `None` 的接口；退化成不缓存虽不影响正确性，但会让限流压力成倍上升。
    """
    calls = {"n": 0}

    def returns_none():
        calls["n"] += 1
        return None

    r1 = src._invoke("none_fn", returns_none, cache_key="kn")
    r2 = src._invoke("none_fn", returns_none, cache_key="kn")
    r3 = src._invoke("none_fn", returns_none, cache_key="kn")

    assert calls["n"] == 1, f"应只真实调用 1 次，实际 {calls['n']} 次"
    assert r1 is None and r2 is None and r3 is None, "命中也应原样返回 None"
    assert recorder.statuses() == ["ok", "cache_hit", "cache_hit"]


def test_cache_sentinel_is_falsy(src):
    """哨兵刻意保持 falsy —— 让 `if cached:` 的旧写法行为不变（仍视为未命中）。"""
    from app.services.data_source.base import MISS

    assert bool(MISS) is False
    assert repr(MISS) == "<CACHE_MISS>"
    # 未命中返回哨兵，而不是 None
    assert src._cache.get("definitely-not-there") is MISS


# ======================================================================
# force_refresh：跳过缓存读取（2026-09-17 修复"假刷新"）
# ======================================================================
def test_force_refresh_bypasses_cache(src, recorder):
    """★ 回归：force_refresh=True 必须每次都发真实请求。

    历史缺陷：`calendar.refresh()` 内部固定调 `fetch_all_trade_dates`，
    而后者被 1 小时 TTL 缓存 → "刷新"变成"读缓存"：
    接口报成功、时间戳更新，但根本没连数据源。
    """
    calls = {"n": 0}

    def real():
        calls["n"] += 1
        return pd.DataFrame({"a": [1]})

    src._invoke("fn", real, cache_key="k", force_refresh=True)
    src._invoke("fn", real, cache_key="k", force_refresh=True)
    src._invoke("fn", real, cache_key="k", force_refresh=True)

    assert calls["n"] == 3, f"每次都应真实调用，实际 {calls['n']} 次"


def test_force_refresh_still_writes_cache(src, recorder):
    """force_refresh 只跳过**读**，成功后仍写缓存 —— 把缓存更新为最新值。

    这样"刷新"既做到了真实请求，又顺手修正了原先可能过期的缓存。
    """
    calls = {"n": 0}

    def real():
        calls["n"] += 1
        return pd.DataFrame({"a": [calls["n"]]})

    # 先灌一个旧值进缓存
    src._invoke("fn", real, cache_key="k")
    assert calls["n"] == 1

    # 强制刷新 → 真实调用，并把缓存更新为 v2
    fresh = src._invoke("fn", real, cache_key="k", force_refresh=True)
    assert calls["n"] == 2
    assert fresh["a"].iloc[0] == 2

    # 之后的普通调用应命中"刷新后"的值，且不再真实调用
    cached = src._invoke("fn", real, cache_key="k")
    assert calls["n"] == 2, "刷新后应能命中缓存"
    assert cached["a"].iloc[0] == 2, "缓存应已被刷新为新值"


def test_force_refresh_marked_in_log(src, recorder):
    """强制刷新必须在 datasource_log 里可辨认（便于事后排查假刷新）。"""
    src._invoke("fn", lambda: None, params={"x": 1}, cache_key="k", force_refresh=True)
    entry = recorder.find("ok")
    assert entry is not None
    assert "[force_refresh]" in entry["params"]


def test_force_refresh_without_cache_key_is_noop(src, recorder):
    """没有 cache_key 时 force_refresh 无副作用（本就不走缓存）。"""
    calls = {"n": 0}

    def real():
        calls["n"] += 1
        return pd.DataFrame({"a": [1]})

    src._invoke("fn", real, force_refresh=True)
    src._invoke("fn", real, force_refresh=True)
    assert calls["n"] == 2


# ======================================================================
# 熔断短路
# ======================================================================
def test_breaker_prevents_any_request(src, recorder):
    """★ 熔断期内必须"一个请求都不发"，而不是发了再失败。"""
    src._breaker.trip("测试", cooldown=30)
    calls = {"n": 0}

    def should_not_run():
        calls["n"] += 1
        return None

    with pytest.raises(DataSourceRateLimited):
        src._invoke("fn", should_not_run)

    assert calls["n"] == 0
    # 熔断短路发生在缓存之后、请求之前，因此不产生 ok/error 日志
    assert "ok" not in recorder.statuses()


def test_reset_throttle_reenables_calls(src, recorder):
    src._breaker.trip("测试", cooldown=30)
    src.reset_throttle()
    result = src._invoke("fn", lambda: "done")
    assert result == "done"


# ======================================================================
# 限流接入
# ======================================================================
def test_rate_limiter_is_used_by_invoke(src, recorder, monkeypatch):
    """★ 回归：限流器必须被 `_invoke` 真正调用（历史引用数为 0）。"""
    acquired = {"n": 0}
    original = src._limiter.acquire

    def counting_acquire() -> float:
        acquired["n"] += 1
        return original()

    monkeypatch.setattr(src._limiter, "acquire", counting_acquire)

    src._invoke("fn", lambda: None)
    assert acquired["n"] == 1


def test_retry_is_used_by_invoke(src, recorder, monkeypatch):
    """★ 回归：重试必须被 `_invoke` 真正调用（历史引用数为 0）。

    注意中间失败的日志走 `call_with_retry` 自己的 logger（`[retry] ...`），
    **不写 datasource_log** —— 否则一次重试会往库里塞多行噪音。
    datasource_log 只记录整次 `_invoke` 的最终结果。
    """
    monkeypatch.setattr(base_mod.time, "sleep", lambda _s: None)
    state = {"n": 0}

    def flaky():
        state["n"] += 1
        if state["n"] < 2:
            raise ValueError("临时故障")
        return "ok"

    assert src._invoke("fn", flaky) == "ok"
    assert state["n"] == 2, "应当重试了一次"
    assert recorder.statuses() == ["ok"], "只记最终结果，不记中间重试"


# ======================================================================
# 观测
# ======================================================================
def test_throttle_stats_shape(src):
    st = src.throttle_stats()
    assert st["datasource"] == "fake"
    assert "rate_limiter" in st and "circuit_breaker" in st and "cache" in st
    assert st["rate_limiter"]["total_calls"] == 0


def test_health_reports_breaker_open(src):
    src._breaker.trip("测试", cooldown=30)
    status = src.health()
    assert status.available is False
    assert "熔断" in status.message


def test_health_ok_when_closed(src):
    status = src.health()
    assert status.available is True
    assert status.version == "test-1.0"


# ======================================================================
# 健康检查：shallow / deep 必须能区分（2026-09-17 修复）
# ======================================================================
def test_health_shallow_marks_deep_false(src):
    """轻量检查必须自报 `deep=False` —— 它只说明"库装上了"，不代表"能连上网"。"""
    status = src.health(deep=False)
    assert status.deep is False
    assert status.to_dict()["deep"] is False


def test_base_health_does_not_pretend_to_probe(src):
    """★ 回归：基类不支持真实探测时，不得假装探测过。

    历史缺陷：`/datasources/health?deep=true` 对 akshare 完全无效 ——
    它继承基类的 `health()`，只读版本号 + 熔断状态（零网络请求），
    但接口照样返回"可用"，用户以为做了真实探测。
    """
    status = src.health(deep=True)
    assert status.deep is False, "基类无法探测，必须如实报告 deep=False"
    assert "未做网络探测" in status.message


def test_deep_health_included_in_health_all_shape():
    """health_all 返回项应含 deep 字段，供前端区分两种"正常"。"""
    from app.services.data_source import health_all

    items = health_all(deep=False)
    assert len(items) == 2
    for it in items:
        assert "deep" in it
        assert it["deep"] is False


def test_params_not_leaked_into_log_when_absent(src, recorder):
    src._invoke("fn", lambda: None)
    assert recorder.find("ok")["params"] == ""
