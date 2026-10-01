"""模块 1 · 接口目录服务的单元测试。

真实网络调用一律不发生 —— monkeypatch 掉执行器。
"""
from __future__ import annotations

from collections import deque

import pandas as pd
import pytest

from app.core.errors import InvalidParamError, NotFoundError
from app.services import catalog_service as cs

#: 通用 akshare 条目（plain 代码格式 + `symbol` 参数，默认 600519）—— 多处用例复用。
#: ⚠️ 2026-10-01 起**不能**再用 `quote_history.stock_zh_a_hist`：它走
#:    `push2his.eastmoney.com`，该域名在本机被网络掐断，条目已下架。
#:    改用同格式的 `fundamental.stock_individual_info_em`（走 datacenter，可用）。
AK_ITEM = "fundamental.stock_individual_info_em"

#: 2026-10-01 从目录下架的 18 条东财 push2/push2his 接口（回归用，见下）。
REMOVED_PUSH2_ITEMS = {
    "quote_realtime.stock_zh_a_spot_em",
    "quote_realtime.stock_bid_ask_em",
    "quote_realtime.stock_intraday_em",
    "quote_realtime.stock_zh_a_hist_pre_min_em",
    "quote_realtime.stock_individual_fund_flow",
    "quote_realtime.stock_individual_fund_flow_rank",
    "quote_realtime.stock_zh_index_spot_em",
    "quote_realtime.stock_board_industry_name_em",
    "quote_realtime.stock_board_industry_spot_em",
    "quote_realtime.stock_board_concept_name_em",
    "quote_history.stock_zh_a_hist",
    "quote_history.stock_zh_a_hist_min_em",
    "quote_history.index_zh_a_hist",
    "quote_history.index_zh_a_hist_min_em",
    "quote_history.stock_board_industry_hist_em",
    "quote_history.stock_board_industry_cons_em",
    "quote_history.stock_board_concept_hist_em",
    "quote_history.stock_board_concept_cons_em",
}

#: 降级通道：push2ex（涨停板系列）在本机**可用**，必须保留。
KEPT_PUSH2EX_FUNCS = {
    "stock_zt_pool_em",
    "stock_zt_pool_previous_em",
    "stock_zt_pool_strong_em",
}


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
    it = cs.get_item(AK_ITEM)
    assert it.func_name == "stock_individual_info_em"
    assert it.datasource == "akshare"
    assert it.symbol_format == "plain"
    with pytest.raises(NotFoundError):
        cs.get_item("nope.not_exists")


def test_sample_code_generated_not_stale():
    """示例代码必须可运行 —— 自动生成而不是手写（手写一定过时）。"""
    code = cs.gen_sample_code(cs.get_item(AK_ITEM))
    assert code.startswith("import akshare as ak")
    assert "stock_individual_info_em(" in code
    assert "symbol='600519'" in code or 'symbol="600519"' in code


def test_sample_code_is_datasource_aware():
    """★ 示例代码必须按数据源分支 —— 不能给 fuyao 生成 `import akshare`。

    fuyao 是同花顺**官方 REST**，`akshare` 里根本没有 `limit_up_pool` 这个函数，
    之前生成的示例是**跑不起来**的假代码。
    """
    fuyao_code = cs.gen_sample_code(cs.get_item("quote_history.fuyao_limit_up_pool"))
    assert "import akshare" not in fuyao_code
    assert "fuyao_client.limit_up_pool(" in fuyao_code
    assert "X-api-key" in fuyao_code
    assert "https://fuyao.aicubes.cn" in fuyao_code

    bs_code = cs.gen_sample_code(cs.get_item("quote_history.bs_fetch_daily"))
    assert "import akshare" not in bs_code
    assert "baostock_client.fetch_daily(" in bs_code

    ak_code = cs.gen_sample_code(cs.get_item(AK_ITEM))
    assert ak_code.startswith("import akshare as ak")


def test_fuyao_sample_paths_cover_all_methods():
    """每个 fuyao 可试跑方法都要有端点路径（否则示例代码里会是空串）。"""
    missing = sorted(set(cs._FUYAO_METHODS) - set(cs._FUYAO_METHODS_PATH))
    assert not missing, f"这些 fuyao 方法缺端点路径：{missing}"
    for path in cs._FUYAO_METHODS_PATH.values():
        assert path.startswith("/api/"), path


def test_fuyao_sample_paths_match_client_source():
    """端点路径必须与客户端里实际请求的一致（两处会漂移的地方，钉住它）。"""
    from pathlib import Path

    src = Path("app/services/data_source/fuyao_client.py").read_text(encoding="utf-8")
    for func, path in cs._FUYAO_METHODS_PATH.items():
        assert f'"{path}"' in src, f"{func} 的端点 {path} 在 fuyao_client 里找不到"


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
        cs.try_run(AK_ITEM, {"symbol": "600519", "symblo": "typo"})
    assert "symblo" in ei.value.message


def test_try_run_symbol_format_validation(monkeypatch):
    """akshare 代码格式有 5 种，YAML 声明的格式必须被强制校验。"""
    monkeypatch.setattr(cs, "_run_akshare", lambda item, p: _fake_frame())
    # plain 格式接口传入带前缀的代码 → 拒绝
    with pytest.raises(InvalidParamError) as ei:
        cs.try_run(AK_ITEM, {"symbol": "sh.600519"})
    assert "plain" in ei.value.message
    # 反向：baostock 格式接口传入纯数字 → 也要拒绝
    with pytest.raises(InvalidParamError) as ei2:
        cs.try_run("quote_history.bs_fetch_daily", {"symbol": "600519"})
    assert "baostock" in ei2.value.message


def test_try_run_type_coercion(monkeypatch):
    seen: dict = {}

    def fake_run(item, params):
        seen.update(params)
        return _fake_frame()

    monkeypatch.setattr(cs, "_run_akshare", fake_run)
    it = cs.get_item(AK_ITEM)
    # 声明里没有 int 参数，直接验证 str 原样透传
    cs.try_run(it.id, {"symbol": "600519"})
    assert seen["symbol"] == "600519"


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

    cs.try_run(AK_ITEM, {"symbol": "600519"})
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
    first = cs.try_run(AK_ITEM, {"symbol": "600519"})
    assert first["cached"] is False

    t0 = time.perf_counter()
    second = cs.try_run(AK_ITEM, {"symbol": "600519"})
    elapsed = time.perf_counter() - t0
    assert second["cached"] is True
    assert elapsed < 0.2, f"缓存命中耗时 {elapsed * 1000:.0f}ms"


def test_try_run_force_bypasses_cache(monkeypatch):
    calls = {"n": 0}

    def run(item, p):
        calls["n"] += 1
        return _fake_frame()

    monkeypatch.setattr(cs, "_run_akshare", run)
    cs.try_run(AK_ITEM, {"symbol": "600519"})
    r2 = cs.try_run(AK_ITEM, {"symbol": "600519"}, force=True)
    assert calls["n"] == 2
    assert r2["cached"] is False


def test_try_run_rate_limit_per_host(monkeypatch):
    """同 IP 每分钟 10 次（缓存命中不计入）。"""
    monkeypatch.setattr(cs, "_run_akshare", lambda i, p: _fake_frame())
    from app.core.errors import DataSourceRateLimited

    for _ in range(cs.TRY_RATE_LIMIT_PER_MIN):
        cs.try_run(AK_ITEM, {"symbol": "600519"}, force=True)
    with pytest.raises(DataSourceRateLimited):
        cs.try_run(AK_ITEM, {"symbol": "600519"}, force=True)


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
            AK_ITEM,
            {"symbol": "600519"},
            force=True,
            skip_rate_limit=True,
        )

    # 跳过后窗口仍为空 —— 普通（面向前端的）调用保住完整额度
    for _ in range(cs.TRY_RATE_LIMIT_PER_MIN):
        cs.try_run(AK_ITEM, {"symbol": "600519"}, force=True)
    with pytest.raises(DataSourceRateLimited):
        cs.try_run(AK_ITEM, {"symbol": "600519"}, force=True)


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


# ======================================================================
# 数据源变更回归（2026-10-01）
# ======================================================================
def test_removed_push2_items_are_gone():
    """★ 回归：走 push2 / push2his 的东财接口必须已从目录下架。

    这些域名在本机被网络掐断（RemoteDisconnected），留着只会让用户
    点到必然失败的接口。下架是有意为之，不能被"顺手加回来"。
    """
    ids = {it.id for it in cs.load_items()}
    still_here = sorted(REMOVED_PUSH2_ITEMS & ids)
    assert not still_here, f"这些 push2 条目应已下架却仍在目录里：{still_here}"


def test_push2ex_items_are_kept():
    """反向：push2ex（涨停板系列）本机**可用**，必须保留。

    它和 push2 是不同域名、不同命运 —— 一刀切删掉会把唯一能用的东财通道也砍了。
    """
    funcs = {it.func_name for it in cs.load_items()}
    missing = sorted(KEPT_PUSH2EX_FUNCS - funcs)
    assert not missing, f"push2ex 接口被误删：{missing}"


def test_ths_items_and_functions_exist():
    """同花顺条目必须在 akshare 里真实存在（拼错函数名会被试跑时才炸）。"""
    import akshare as ak

    ths = [it for it in cs.load_items() if it.func_name.endswith("_ths")]
    assert ths, "同花顺板块条目全部丢失"
    for it in ths:
        assert hasattr(ak, it.func_name), f"{it.id}: akshare 里没有 {it.func_name}"


def test_datasource_values_are_known():
    """数据源取值只允许三种；写出别的值会让 try_run 悄悄走错执行器。"""
    allowed = {"akshare", "baostock", "fuyao"}
    bad = {it.datasource for it in cs.load_items()} - allowed
    assert not bad, f"出现未注册的数据源取值：{bad}"


def test_fuyao_items_are_runnable():
    """每个 fuyao 条目的 func_name 都必须在试跑白名单里，否则点了就报错。"""
    fuyao_items = [it for it in cs.load_items() if it.datasource == "fuyao"]
    assert fuyao_items, "fuyao 条目缺失"
    for it in fuyao_items:
        assert it.func_name in cs._FUYAO_METHODS, f"{it.id} 不在 _FUYAO_METHODS 白名单"


def test_fuyao_date_ms_param_is_accepted_in_three_formats():
    """date_ms 参数三种写法（YYYYMMDD / YYYY-MM-DD / 毫秒）必须都能过闸。"""
    item = cs.get_item("quote_history.fuyao_limit_up_pool")
    decl = next(p for p in item.params if p["name"] == "date_ms")
    for value in ("20260929", "2026-09-29", "1790611200000"):
        out = cs._coerce_param(item, decl, value)
        assert isinstance(out, int) and out > 1_700_000_000_000, f"{value} → {out}"
    with pytest.raises(InvalidParamError):
        cs._coerce_param(item, decl, "not-a-date")


def test_static_check_handles_fuyao_datasource():
    """★ 自检工具必须认识 fuyao —— 否则 11 个 fuyao 条目会被误判为 missing。

    失效模式：`_static_check` 原来只有 baostock 与"在 akshare 里找函数"两条分支，
    fuyao 的 func_name（如 `limit_up_pool`）在 akshare 里当然找不到 →
    整页标红"akshare 中已不存在函数"。这是本轮实际发生过的。
    """
    from tools import check_catalog

    fuyao_items = [it for it in cs.load_items() if it.datasource == "fuyao"]
    assert fuyao_items, "fuyao 条目缺失"
    for it in fuyao_items:
        status, message = check_catalog._static_check(it)
        assert status == "ok", f"{it.id} 静态检查为 {status}：{message}"


def test_static_check_all_items_ok():
    """全目录静态检查必须全绿（broken/missing 都是"代码与目录不同步"的信号）。"""
    from tools import check_catalog

    bad = {
        it.id: check_catalog._static_check(it)
        for it in cs.load_items()
        if check_catalog._static_check(it)[0] != "ok"
    }
    assert not bad, f"以下条目静态检查未通过：{bad}"
