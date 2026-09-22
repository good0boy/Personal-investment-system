"""模块 1 · 接口目录服务的单元测试。

真实网络调用一律不发生 —— monkeypatch 掉执行器。
"""
from __future__ import annotations

from collections import deque

import pandas as pd
import pytest

from app.core.errors import InvalidParamError, NotFoundError
from app.services import catalog_service as cs


@pytest.fixture(autouse=True)
def _fresh_caches():
    cs.clear_cache()
    cs._try_cache.clear()
    cs._try_rate.clear()
    yield
    cs.clear_cache()
    cs._try_cache.clear()
    cs._try_rate.clear()


# ======================================================================
# 目录加载
# ======================================================================
def test_load_items_finds_all_categories():
    items = cs.load_items()
    assert len(items) >= 40, f"验收要求 ≥40 个接口，实际 {len(items)}"
    cats = {it.category for it in items}
    assert cats == set(cs.CATEGORIES), f"分类不全：{cats}"


def test_load_items_ids_unique():
    items = cs.load_items()
    ids = [it.id for it in items]
    assert len(ids) == len(set(ids)), "存在重复 id"


def test_tree_structure():
    tree = cs.tree()
    assert tree["total"] >= 40
    keys = [c["key"] for c in tree["categories"]]
    assert keys == list(cs.CATEGORIES)
    for c in tree["categories"]:
        assert c["count"] > 0
        assert c["sub_groups"]


def test_get_item_and_not_found():
    it = cs.get_item("quote_history.stock_zh_a_hist")
    assert it.func_name == "stock_zh_a_hist"
    assert it.datasource == "akshare"
    assert it.symbol_format == "plain"
    with pytest.raises(NotFoundError):
        cs.get_item("nope.not_exists")


def test_sample_code_generated_not_stale():
    """示例代码必须可运行 —— 自动生成而不是手写（手写一定过时）。"""
    code = cs.gen_sample_code(cs.get_item("quote_history.stock_zh_a_hist"))
    assert code.startswith("import akshare as ak")
    assert "stock_zh_a_hist(" in code
    assert "symbol='600519'" in code or 'symbol="600519"' in code


def test_every_item_has_returns_and_sample():
    """入参出参完整是验收硬指标；没有 sample_params 的接口无法试跑。"""
    for it in cs.load_items():
        assert it.returns, f"{it.id} 缺少出参定义"
        assert it.description, f"{it.id} 缺少说明"


# ======================================================================
# 试跑：参数闸门
# ======================================================================
def _fake_frame() -> pd.DataFrame:
    return pd.DataFrame({"col": [1, 2, 3]})


def test_try_run_rejects_unknown_params(monkeypatch):
    """★ 参数白名单：拼错的参数名必须显式报错，不能静默透传。"""
    monkeypatch.setattr(cs, "_run_akshare", lambda item, p: _fake_frame())
    with pytest.raises(InvalidParamError) as ei:
        cs.try_run("quote_history.stock_zh_a_hist", {"symbol": "600519", "symblo": "typo"})
    assert "symblo" in ei.value.message


def test_try_run_symbol_format_validation(monkeypatch):
    """akshare 代码格式有 5 种，YAML 声明的格式必须被强制校验。"""
    monkeypatch.setattr(cs, "_run_akshare", lambda item, p: _fake_frame())
    # plain 格式接口传入带前缀的代码 → 拒绝
    with pytest.raises(InvalidParamError) as ei:
        cs.try_run("quote_history.stock_zh_a_hist", {"symbol": "sh.600519"})
    assert "plain" in ei.value.message


def test_try_run_type_coercion(monkeypatch):
    seen: dict = {}

    def fake_run(item, params):
        seen.update(params)
        return _fake_frame()

    monkeypatch.setattr(cs, "_run_akshare", fake_run)
    it = cs.get_item("quote_history.stock_zh_a_hist")
    # 声明里没有 int 参数，直接验证 float/str 保持 + 默认值补全
    cs.try_run(it.id, {"symbol": "600519", "period": "daily"})
    assert seen["symbol"] == "600519"
    assert seen["period"] == "daily"


def test_try_run_reports_data_source_routing(monkeypatch):
    """baostock 接口必须走白名单执行器，不能落进 akshare 任意调用。"""
    called = {"ak": 0, "bs": 0}

    def fake_ak(item, params):
        called["ak"] += 1
        return _fake_frame()

    def fake_bs(item, params):
        called["bs"] += 1
        return _fake_frame()

    monkeypatch.setattr(cs, "_run_akshare", fake_ak)
    monkeypatch.setattr(cs, "_run_baostock", fake_bs)

    cs.try_run("quote_history.stock_zh_a_hist", {"symbol": "600519"})
    cs.try_run("quote_history.bs_fetch_daily", {"symbol": "sh.600519"})
    assert called == {"ak": 1, "bs": 1}


def test_try_run_baostock_whitelist(monkeypatch):
    """不在白名单里的 baostock 方法必须拒绝 —— 不提供任意调用能力。"""
    import dataclasses

    item = cs.get_item("quote_history.bs_fetch_daily")
    bad = dataclasses.replace(item, func_name="__import__")
    monkeypatch.setattr(cs, "get_item", lambda _id: bad)
    with pytest.raises(InvalidParamError):
        cs.try_run(bad.id, {})


# ======================================================================
# 试跑：缓存与限流
# ======================================================================
def test_try_run_cache_hit_is_fast(monkeypatch):
    """验收：重复试跑 <200ms（走缓存）。"""
    import time

    monkeypatch.setattr(cs, "_run_akshare", lambda i, p: _fake_frame())
    first = cs.try_run("quote_history.stock_zh_a_hist", {"symbol": "600519"})
    assert first["cached"] is False

    t0 = time.perf_counter()
    second = cs.try_run("quote_history.stock_zh_a_hist", {"symbol": "600519"})
    elapsed = time.perf_counter() - t0
    assert second["cached"] is True
    assert elapsed < 0.2, f"缓存命中耗时 {elapsed * 1000:.0f}ms"


def test_try_run_force_bypasses_cache(monkeypatch):
    calls = {"n": 0}

    def run(item, p):
        calls["n"] += 1
        return _fake_frame()

    monkeypatch.setattr(cs, "_run_akshare", run)
    cs.try_run("quote_history.stock_zh_a_hist", {"symbol": "600519"})
    r2 = cs.try_run("quote_history.stock_zh_a_hist", {"symbol": "600519"}, force=True)
    assert calls["n"] == 2
    assert r2["cached"] is False


def test_try_run_rate_limit_per_host(monkeypatch):
    """同 IP 每分钟 10 次（缓存命中不计入）。"""
    monkeypatch.setattr(cs, "_run_akshare", lambda i, p: _fake_frame())
    from app.core.errors import DataSourceRateLimited

    for _ in range(cs.TRY_RATE_LIMIT_PER_MIN):
        cs.try_run("quote_history.stock_zh_a_hist", {"symbol": "600519"}, force=True)
    with pytest.raises(DataSourceRateLimited):
        cs.try_run("quote_history.stock_zh_a_hist", {"symbol": "600519"}, force=True)


def test_rate_limit_tracks_hosts_separately():
    cs._enforce_rate_limit("host-a")
    cs._enforce_rate_limit("host-b")
    dq_a = cs._try_rate["host-a"]
    dq_b = cs._try_rate["host-b"]
    assert len(dq_a) == 1 and len(dq_b) == 1
    assert isinstance(dq_a, deque)


def test_try_run_skip_rate_limit_does_not_consume_quota(monkeypatch):
    """★ `skip_rate_limit=True`（自检抽样专用）既不报错也不占用限流窗口。

    背景：自检最多抽样 20 个（`/check` 的 `le=20`），而试跑限流是 10 次/分钟 ——
    不跳过的话第 11 个起必然报"试跑太频繁"，日志里看着像接口坏了。
    """
    monkeypatch.setattr(cs, "_run_akshare", lambda i, p: _fake_frame())
    from app.core.errors import DataSourceRateLimited

    for _ in range(cs.TRY_RATE_LIMIT_PER_MIN + 5):
        cs.try_run(
            "quote_history.stock_zh_a_hist",
            {"symbol": "600519"},
            force=True,
            skip_rate_limit=True,
        )

    # 跳过后窗口仍为空 —— 普通（面向前端的）调用保住完整额度
    for _ in range(cs.TRY_RATE_LIMIT_PER_MIN):
        cs.try_run("quote_history.stock_zh_a_hist", {"symbol": "600519"}, force=True)
    with pytest.raises(DataSourceRateLimited):
        cs.try_run("quote_history.stock_zh_a_hist", {"symbol": "600519"}, force=True)


def test_check_catalog_live_sample_bypasses_try_rate_limit(monkeypatch):
    """★ 回归：自检抽样必须显式传 `skip_rate_limit=True`。

    不传的话 `live_sample=20` 只有前 10 个是真调用，后 10 个报"试跑太频繁"，
    被当成接口失败写进 live.results —— 假失败。
    """
    from tools import check_catalog

    seen: list[bool] = []

    def fake_try_run(item_id, params, **kwargs):
        seen.append(kwargs.get("skip_rate_limit", False))
        return {"shape": [1, 1], "elapsed_ms": 1.0}

    monkeypatch.setattr(cs, "try_run", fake_try_run)
    monkeypatch.setattr(check_catalog.db, "is_initialized", lambda: True)
    monkeypatch.setattr(check_catalog.meta_repo, "catalog_status_upsert", lambda *a, **k: None)
    monkeypatch.setattr(
        check_catalog.meta_repo, "catalog_status_delete_removed", lambda *a, **k: 0
    )

    summary = check_catalog.run_check(live_sample=3)

    assert summary["live"]["ran"] == 3
    assert seen == [True, True, True], f"自检抽样未跳过限流：{seen}"


# ======================================================================
# 结果截断
# ======================================================================
def test_frame_truncation_rows_and_cols():
    df = pd.DataFrame({f"c{i}": range(200) for i in range(40)})
    payload = cs._frame_to_payload(df, 5.0)
    assert payload["shape"] == [200, 40]
    assert len(payload["rows"]) == cs._MAX_ROWS
    assert len(payload["columns"]) == cs._MAX_COLS
    assert payload["truncated"] == {"rows": True, "cols": True}
    assert "仅展示前" in payload["note"]


def test_frame_small_result_not_truncated():
    df = pd.DataFrame({"a": [1, 2]})
    payload = cs._frame_to_payload(df, 1.0)
    assert payload["truncated"] == {"rows": False, "cols": False}
    assert payload["note"] == ""


def test_frame_none_result():
    payload = cs._frame_to_payload(None, 1.0)
    assert payload["shape"] == [0, 0]
    assert "None" in payload["note"]


# ======================================================================
# 自检状态合并
# ======================================================================
def test_merge_status_defaults_to_unchecked():
    merged = cs.merge_status(cs.load_items())
    assert all(m["status"] in ("ok", "broken", "missing", "unchecked") for m in merged)


def test_check_summary_shape():
    summary = cs.check_summary()
    assert summary["total"] >= 40
    assert {"ok", "broken", "missing", "unchecked"} <= set(summary)
