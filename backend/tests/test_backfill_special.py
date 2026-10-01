"""`tools/backfill_special.py` 的单元测试。

★ 原则：**不联网、不读真实 `data/`、不动真实状态文件**。
   网络相关的方法（run_*）不在这里测 —— 它们的正确性由"回补后的数据与源端逐行比对
   （`--verify`）"兜底；这里测的是**纯逻辑**：待办判定、窗口计算、列名归一。

背景（为什么这几条值得测）：2026-10-01 首次全量回补时，这三个地方各咬过一口 ——
   1. `merge_parquet` 并发丢更新 → 已在 `test_data_store.py` 钉住（写锁）；
   2. 板块历史接口"start 早于数据首日 → 整段静默返回 0 行" → 起点改成实测首日；
   3. 跌停/炸板池只支持一年内（越界报 1002）→ `_pool_start` 分池。
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
bf = importlib.import_module("tools.backfill_special")


# ======================================================================
# _needs_fetch：待办判定的核心（所有数据集共用）
# ======================================================================
def test_needs_fetch_basic_semantics() -> None:
    recs: dict[str, Any] = {
        "2024-01-02": {"status": "ok"},
        "2024-01-03": {"status": "empty"},
        "2024-01-04": {"status": "failed"},
        "2024-01-05": {"status": "partial"},
    }
    # force → 一律重来
    assert bf._needs_fetch(recs, "2024-01-02", force=True, retry_failed=False)
    # ok / empty 是终态，默认不再抓
    assert not bf._needs_fetch(recs, "2024-01-02", force=False, retry_failed=False)
    assert not bf._needs_fetch(recs, "2024-01-03", force=False, retry_failed=False)
    # 没抓过的要抓
    assert bf._needs_fetch(recs, "2024-01-06", force=False, retry_failed=False)
    # failed 只在 retry_failed 时重试
    assert not bf._needs_fetch(recs, "2024-01-04", force=False, retry_failed=False)
    assert bf._needs_fetch(recs, "2024-01-04", force=False, retry_failed=True)
    # ★ partial 是"取不全"，必须允许重来（否则就是静默截断）
    assert not bf._needs_fetch(recs, "2024-01-05", force=False, retry_failed=False)
    assert bf._needs_fetch(recs, "2024-01-05", force=False, retry_failed=False, retry_partial=True)


def test_needs_fetch_incremental_semantics() -> None:
    """增量模式：没抓过的天要补（包括游标前的空洞）；ok 的不重抓。"""
    recs = {"2024-01-02": {"status": "ok"}}
    # 没抓过的 → 要抓（增量模式下补空洞）
    assert bf._needs_fetch(
        recs, "2024-01-01", force=False, retry_failed=False, incremental=True, after="2024-01-02"
    )
    # 已 ok 的 → 不重抓
    assert not bf._needs_fetch(
        recs, "2024-01-02", force=False, retry_failed=False, incremental=True, after="2024-01-02"
    )
    # 游标之后的新日期（不在 recs）→ 要抓
    assert bf._needs_fetch(
        recs, "2024-01-03", force=False, retry_failed=False, incremental=True, after="2024-01-02"
    )


def test_latest_done_ignores_failed_and_partial() -> None:
    """游标只认 ok/empty —— failed/partial 不能当"已经到过这里"。"""
    recs = {
        "2024-01-02": {"status": "ok"},
        "2024-01-03": {"status": "failed"},
        "2024-01-04": {"status": "partial"},
    }
    assert bf._latest_done(recs) == "2024-01-02"
    assert bf._latest_done({}) is None


# ======================================================================
# 涨跌停三池：起点分池（跌停/炸板只有一年窗口）
# ======================================================================
def test_pool_start_up_is_deep_but_down_break_are_one_year(monkeypatch: pytest.MonkeyPatch) -> None:
    assert bf._pool_start("up") == bf.LIMIT_POOL_START == "2020-07-13"
    # down/break 起点是滚动的一年之前，而不是 LIMIT_POOL_START
    for pool in ("down", "break"):
        start = bf._pool_start(pool)
        assert start == bf._one_year_ago()
        assert start > "2025-09-01", "一年窗口应远晚于涨停池的深历史起点"


def test_limit_pool_todo_uses_per_pool_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """down/break 的待办不应包含一年窗口之前的日期（否则白打一年量的 1002）。"""
    monkeypatch.setattr(bf, "_trading_days", lambda s, e: [s, "2021-01-04", e])
    state: dict[str, Any] = {"limit_pool": {}}
    todo = bf._limit_pool_todo(state, force=True, limit=None, incremental=False, retry_failed=False)
    by_pool: dict[str, list[str]] = {}
    for pool, day in todo:
        by_pool.setdefault(pool, []).append(day)
    assert by_pool["up"][0] == bf.LIMIT_POOL_START
    assert by_pool["down"][0] == bf._one_year_ago()
    assert by_pool["break"][0] == bf._one_year_ago()


# ======================================================================
# normalize：列名归一（源端英文字段 → 项目内部字段）
# ======================================================================
def test_normalize_reindexes_to_expected_columns() -> None:
    """★ schema 一致的唯一防御点：多出的源列要被丢掉，缺的列补 NA。"""
    raw = pd.DataFrame(
        {
            "thscode": ["600519.SH"],
            "ticker": ["600519"],
            "name": ["贵州茅台"],
            "last_price": [1.0],
            "seal_money": [2.0],
            "unmapped_col": ["x"],  # 源端多出来的列
        }
    )
    out = bf.normalize(raw, "up")
    assert list(out.columns) == list(bf.EXPECTED["up"]), "列集必须精确等于 EXPECTED"
    assert "unmapped_col" not in out.columns, "源端多出的列必须被丢弃"
    assert out["max_seal_money"].isna().all(), "源端缺的列补 NA"
    assert list(out["thscode"]) == ["600519.SH"]


def test_normalize_handles_empty_and_none() -> None:
    """空输入 → **带列的**空表（不是没有列的空表，否则写盘后 schema 会漂）。"""
    for empty in (None, pd.DataFrame()):
        out = bf.normalize(empty, "up")
        assert len(out) == 0
        assert list(out.columns) == list(bf.EXPECTED["up"])


# ======================================================================
# 状态文件：原子写 + 结构
# ======================================================================
def test_state_roundtrip_is_atomic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:

    state_file = tmp_path / "state.json"
    monkeypatch.setattr(bf, "BACKFILL_SPECIAL_STATE_FILE", state_file)
    bf.save_state({"limit_pool": {"up": {"2024-01-02": {"status": "ok"}}}})
    loaded = bf.load_state()
    assert loaded["limit_pool"]["up"]["2024-01-02"]["status"] == "ok"
    # 不留 .tmp 残留
    assert not state_file.with_suffix(".json.tmp").exists()
    assert state_file.exists()
