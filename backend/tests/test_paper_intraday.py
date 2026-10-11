"""盘中清仓预警（`paper_intraday_service`）的纯逻辑单测。

fuyao 快照与本地数据全部 mock —— 只钉判定逻辑本身。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services import paper_intraday_service as svc  # noqa: E402 - 需先插 sys.path


def _holdings(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


@pytest.fixture
def _fuyao_ok(monkeypatch):
    """fuyao 快照 mock：price=10、昨收=10、当日涨跌 0（中性）。"""
    def _snap(symbols):
        return {
            s: svc.FuyaoSnap(price=10.0, prev_close=10.0, day_chg=0.0)
            for s in symbols
        }, None
    monkeypatch.setattr(svc, "_fuyao_snapshots", _snap)


def test_time_stop_triggers_at_hold_days_max(monkeypatch, _fuyao_ok):
    """持有满 30 日 → time_stop 预警。"""
    monkeypatch.setattr(svc, "_hold_days", lambda entry, today: 30)
    monkeypatch.setattr(svc, "_local_close", lambda s, n: 11.0)
    monkeypatch.setattr(svc, "_local_preclose", lambda s: 10.0)
    monkeypatch.setattr(svc, "_factor_latest_lt_zero", lambda s, n: False)

    h = _holdings([{"symbol": "sh.600000", "entry_date": "2026-08-01",
                    "entry_price": 10.0, "date": "2026-09-15"}])
    alerts = svc.evaluate_holdings(h, "2026-09-15", {"hold_days_max": 30})

    assert len(alerts) == 1
    assert "time_stop" in alerts[0]["reasons"]


def test_no_alert_when_all_conditions_calm(monkeypatch, _fuyao_ok):
    """价格平静、基本面为正、未到期 → 无预警。"""
    monkeypatch.setattr(svc, "_hold_days", lambda entry, today: 5)
    monkeypatch.setattr(svc, "_local_close", lambda s, n: 10.0)   # 近20日 0%
    monkeypatch.setattr(svc, "_local_preclose", lambda s: 10.0)
    monkeypatch.setattr(svc, "_factor_latest_lt_zero", lambda s, n: False)

    h = _holdings([{"symbol": "sh.600000", "entry_date": "2026-09-08",
                    "entry_price": 10.0, "date": "2026-09-15"}])
    alerts = svc.evaluate_holdings(h, "2026-09-15", {"hold_days_max": 30})

    assert alerts == []


def test_over_realized_triggers(monkeypatch, _fuyao_ok):
    """近 20 日累计涨幅 >= 30% → over_realized 预警。

    ★ 分段合成口径（2026-10-11）：r20 = hfq 段（T-20→昨收）× 当日实时涨跌。
      mock：hfq c20=7.0 → 昨收 10.0（段收益 +42.9%），当日涨跌 0 → r20 ≈ +42.9%。
    """
    monkeypatch.setattr(svc, "_hold_days", lambda entry, today: 5)
    monkeypatch.setattr(
        svc, "_local_close", lambda s, n: 7.0 if n == 21 else 10.0
    )
    monkeypatch.setattr(svc, "_local_preclose", lambda s: 10.0)
    monkeypatch.setattr(svc, "_factor_latest_lt_zero", lambda s, n: False)

    h = _holdings([{"symbol": "sh.600000", "entry_date": "2026-09-08",
                    "entry_price": 6.0, "date": "2026-09-15"}])
    alerts = svc.evaluate_holdings(h, "2026-09-15", {"hold_days_max": 30})

    assert any("over_realized" in a["reasons"] for a in alerts)
    # 合成公式抽查：(1+10/7-1)×(1+0)-1 = +42.9%
    a = next(a for a in alerts if "over_realized" in a["reasons"])
    assert "+42.9%" in a["reasons"]


def test_over_realized_moutai_scenario_no_false_silence(monkeypatch):
    """★ 茅台式复权口径场景回归（2026-10-11 混算事故）：
    hfq 价（~9700）与真实价（~1263）差 7.7 倍。旧逻辑 fuyao 真实价 /
    hfq T-20 收盘 → r20 = -87%，over_realized 永久漏报。
    新逻辑：hfq 段收益 × fuyao 当日真实涨跌 → 正常小区间值。"""
    monkeypatch.setattr(svc, "_hold_days", lambda entry, today: 5)
    # hfq 段：c20=9961.44 → 昨收 9686.27（段收益 -2.76%）
    monkeypatch.setattr(
        svc, "_local_close", lambda s, n: 9961.44 if n == 21 else 9686.27
    )
    monkeypatch.setattr(svc, "_local_preclose", lambda s: 9686.27)
    monkeypatch.setattr(svc, "_factor_latest_lt_zero", lambda s, n: False)

    def _snap(symbols):
        return {
            s: svc.FuyaoSnap(price=1263.0, prev_close=1255.79, day_chg=0.0057)
            for s in symbols
        }, None
    monkeypatch.setattr(svc, "_fuyao_snapshots", _snap)

    h = _holdings([{"symbol": "sh.600519", "entry_date": "2026-09-08",
                    "entry_price": 9600.0, "date": "2026-09-15"}])
    alerts = svc.evaluate_holdings(h, "2026-09-15", {"hold_days_max": 30})

    # 合成 r20 = (1-0.0276)×(1+0.0057)-1 ≈ -2.22% —— 正常区间，不触发
    assert not any("over_realized" in a["reasons"] for a in alerts)


def test_over_realized_degrades_when_day_chg_missing(monkeypatch):
    """fuyao 未返回当日涨跌幅 → 降级为截至昨收的 hfq 口径，仍能触发预警。"""
    monkeypatch.setattr(svc, "_hold_days", lambda entry, today: 5)
    monkeypatch.setattr(
        svc, "_local_close", lambda s, n: 7.0 if n == 21 else 10.0
    )
    monkeypatch.setattr(svc, "_local_preclose", lambda s: 10.0)
    monkeypatch.setattr(svc, "_factor_latest_lt_zero", lambda s, n: False)

    def _snap(symbols):
        return {
            s: svc.FuyaoSnap(price=10.5, prev_close=10.0, day_chg=None)
            for s in symbols
        }, None
    monkeypatch.setattr(svc, "_fuyao_snapshots", _snap)

    h = _holdings([{"symbol": "sh.600000", "entry_date": "2026-09-08",
                    "entry_price": 6.0, "date": "2026-09-15"}])
    alerts = svc.evaluate_holdings(h, "2026-09-15", {"hold_days_max": 30})

    assert any("over_realized" in a["reasons"] for a in alerts)


def test_fuyao_unavailable_raises(monkeypatch):
    """fuyao 快照不可用 → 抛错（job 记 failed → 企微告警），不静默。"""
    monkeypatch.setattr(
        svc, "_fuyao_snapshots", lambda symbols: ({}, "fuyao 挂了")
    )
    h = _holdings([{"symbol": "sh.600000", "entry_date": "2026-08-01",
                    "entry_price": 10.0, "date": "2026-09-15"}])
    with pytest.raises(RuntimeError, match="fuyao"):
        svc.evaluate_holdings(h, "2026-09-15", {"hold_days_max": 30})
