"""数据同步编排（`data_sync_service`）的单测。

重点钉两件事（都是 2026-10-08 审计的教训）：
1. **覆盖率闸门**：探针齐但全市场覆盖不足 → 必须拦下（09-18~09-30 缺口挂两周的根因）；
2. **编排顺序与容错**：单步失败不抛异常，由调用方做最终就绪校验；
   以及子步骤在单测里**绝不真去跑 baostock 子进程**。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

from app.core.config import settings
from app.core.errors import DataNotReadyError
from app.services import data_store
from app.services import data_sync_service as ds

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


# ======================================================================
# 夹具：tmp 行情目录
# ======================================================================
def _write_quotes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                  symbols: list[str], date: str) -> None:
    """写一天的行情（每个 symbol 一行）。"""
    d = tmp_path / "quote_daily"
    d.mkdir(parents=True, exist_ok=True)
    for sym in symbols:
        p = d / f"{sym}.parquet"
        df = pd.DataFrame(
            {"date": pd.to_datetime([date]), "symbol": [sym], "close": [10.0],
             "trade_status": [1], "is_st": [0]}
        )
        if p.exists():
            old = pd.read_parquet(p)
            df = pd.concat([old, df], ignore_index=True)
        data_store.write_parquet(df, p)
    monkeypatch.setattr(data_store, "QUOTE_DAILY_DIR", d)


@pytest.fixture(autouse=True)
def _fast_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """不真跑子进程；覆盖率阈值可按用例覆写。"""
    monkeypatch.setattr(ds, "sync_quotes_incremental",
                        lambda: {"step": "行情增量回补", "ok": True, "skipped": True})
    monkeypatch.setattr(ds, "sync_universe",
                        lambda run_date: {"step": "股票池快照", "ok": True, "skipped": True})
    monkeypatch.setattr(ds, "sync_factors",
                        lambda: {"step": "因子重算", "ok": True, "skipped": True})


# ======================================================================
# 覆盖率闸门
# ======================================================================
def test_ready_when_probe_and_coverage_ok(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings.data_sync, "min_daily_symbols", 3)
    _write_quotes(tmp_path, monkeypatch, ["sh.600519", "sz.000001", "sz.000002"], "2024-06-28")
    ds.assert_quotes_ready("2024-06-28")  # 不抛 = 通过


def test_rejects_low_coverage_even_when_probe_ok(tmp_path, monkeypatch) -> None:
    """★ 探针都在、但全市场覆盖不足 → 必须拦（09-18~09-30 缺口的根因）。"""
    monkeypatch.setattr(settings.data_sync, "min_daily_symbols", 3000)
    _write_quotes(tmp_path, monkeypatch, ["sh.600519", "sz.000001"], "2024-06-28")
    with pytest.raises(DataNotReadyError, match="覆盖"):
        ds.assert_quotes_ready("2024-06-28")


def test_rejects_missing_probe(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings.data_sync, "min_daily_symbols", 1)
    _write_quotes(tmp_path, monkeypatch, ["sz.000001"], "2024-06-28")  # 缺指数
    with pytest.raises(DataNotReadyError, match="探针"):
        ds.assert_quotes_ready("2024-06-28")


def test_readiness_report_shape(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings.data_sync, "min_daily_symbols", 2)
    _write_quotes(tmp_path, monkeypatch, ["sh.600519", "sz.000001"], "2024-06-28")
    rep = ds.quotes_readiness("2024-06-28")
    assert rep["ok"] is True
    assert rep["symbol_count"] == 2
    assert rep["probe_missing"] == []


# ======================================================================
# 编排
# ======================================================================
def test_sync_all_skips_quotes_when_ready(tmp_path, monkeypatch) -> None:
    """已就绪 → 不触发行情回补（幂等：不重复拉 30~50 分钟）。"""
    called: list[str] = []

    def _boom() -> dict:
        called.append("quotes")
        return {"step": "行情增量回补", "ok": True}

    monkeypatch.setattr(ds, "sync_quotes_incremental", _boom)
    monkeypatch.setattr(settings.data_sync, "min_daily_symbols", 1)
    _write_quotes(tmp_path, monkeypatch, ["sh.600519"], "2024-06-28")

    res = ds.sync_all("2024-06-28")
    assert called == [], "已就绪时不应再触发行情回补"
    assert res["readiness_before"]["ok"] is True
    names = [s["step"] for s in res["steps"]]
    assert any("行情增量回补" in n and s.get("skipped") for n, s in zip(names, res["steps"], strict=True))
    assert any("股票池快照" in n for n in names)
    assert any("因子重算" in n for n in names)
    assert any("基本面陈旧度" in n for n in names)


def test_sync_all_step_failure_does_not_raise(tmp_path, monkeypatch) -> None:
    """单步失败只记录、不抛异常 —— 最终闸门是 assert_quotes_ready。"""
    monkeypatch.setattr(settings.data_sync, "min_daily_symbols", 1)

    def _boom() -> dict:
        raise RuntimeError("boom")

    monkeypatch.setattr(ds, "sync_factors", _boom)
    _write_quotes(tmp_path, monkeypatch, ["sh.600519"], "2024-06-28")

    res = ds.sync_all("2024-06-28")  # 不抛
    failed = [s for s in res["steps"] if not s.get("ok")]
    assert failed and failed[0]["step"] == "因子重算"


def test_fundamental_staleness_missing_dir(tmp_path, monkeypatch) -> None:
    """原始数据目录不存在 → available=False（2026-10-11 起监控读原始数据，不再读因子产物）。"""
    monkeypatch.setattr(ds, "FUNDAMENTAL_DIR", tmp_path / "no_such_fundamental")
    rep = ds.fundamental_staleness()
    assert rep["available"] is False
    assert all(not v["available"] for v in rep["datasets"].values())


def test_fundamental_staleness_flags_old_raw_data(tmp_path, monkeypatch) -> None:
    """★ 原始数据真的陈旧时要报 stale —— 旧实现读因子产物（日期恒为昨天），
    连续几个月不回补也永远不会告警（假监控）。"""
    import pandas as pd

    root = tmp_path / "fundamental"
    for ds_name in ds._FUNDAMENTAL_DATASETS:
        d = root / ds_name
        d.mkdir(parents=True)
        # 公告日停在 2023-01-01（远超 120 天阈值）
        pd.DataFrame(
            {
                "symbol": [f"sh.60000{i}" for i in range(3)],
                "report_date": pd.to_datetime(["2022-12-31"] * 3),
                "notice_date": pd.to_datetime(["2023-01-01"] * 3),
            }
        ).to_parquet(d / "sh.600001.parquet", index=False)
    monkeypatch.setattr(ds, "FUNDAMENTAL_DIR", root)
    monkeypatch.setattr(ds, "data_store", type("DS", (), {"query_duck": staticmethod(data_store.query_duck)}))

    rep = ds.fundamental_staleness()
    assert rep["available"] is True
    assert sorted(rep["stale"]) == sorted(ds._FUNDAMENTAL_DATASETS)
    assert rep["datasets"]["financial_indicator"]["age_days"] > 120


# ======================================================================
# 兜底机制（2026-10-10）：僵尸 running 清理
# ======================================================================
@pytest.fixture
def meta_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """tmp meta.db（跑 schema.sql），测试结束关连接。"""
    from app.repos import db as db_module

    db_module.close_connection()
    db_file = tmp_path / "meta" / "meta.db"
    db_file.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(db_module, "META_DB", db_file)
    conn = db_module.get_connection()
    conn.executescript((BACKEND_ROOT / "sql" / "schema.sql").read_text(encoding="utf-8"))
    yield db_file
    db_module.close_connection()


def test_job_reap_zombies(meta_env: Path) -> None:
    """★ 进程崩溃留下的 running 记录（>24h）→ 启动收尸改判 failed；24h 内不动。"""
    import sqlite3

    from app.repos import meta_repo

    # 造两条：一条 48h 前的僵尸 running、一条 1h 前的正常 running
    conn = sqlite3.connect(meta_env)
    conn.execute(
        "INSERT INTO job_run_log (job_id, run_date, trigger_type, status, created_at) "
        "VALUES ('paper_daily', '2026-10-01', 'cron', 'running', "
        "datetime('now', 'localtime', '-48 hours'))"
    )
    conn.execute(
        "INSERT INTO job_run_log (job_id, run_date, trigger_type, status, created_at) "
        "VALUES ('paper_daily', '2026-10-09', 'cron', 'running', "
        "datetime('now', 'localtime', '-1 hours'))"
    )
    conn.commit()
    conn.close()

    n = meta_repo.job_reap_zombies(older_than_hours=24)
    assert n == 1, "只应收尸超过 24h 的"

    rows = meta_repo.job_recent(10)
    by_date = {r["run_date"]: r for r in rows}
    assert by_date["2026-10-01"]["status"] == "failed"
    assert "zombie" in (by_date["2026-10-01"]["message"] or "")
    assert by_date["2026-10-09"]["status"] == "running", "1h 内的不动"
