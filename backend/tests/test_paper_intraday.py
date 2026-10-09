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
    monkeypatch.setattr(
        svc, "_fuyao_prices",
        lambda symbols: (
            dict.fromkeys(symbols, 10.0),
            dict.fromkeys(symbols, 10.0),
            None,
        ),
    )


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
    """近 20 日累计涨幅 >= 30% → over_realized 预警。"""
    monkeypatch.setattr(svc, "_hold_days", lambda entry, today: 5)
    monkeypatch.setattr(svc, "_local_close", lambda s, n: 7.0)     # 10/7-1 ≈ +42.9%
    monkeypatch.setattr(svc, "_local_preclose", lambda s: 10.0)
    monkeypatch.setattr(svc, "_factor_latest_lt_zero", lambda s, n: False)

    h = _holdings([{"symbol": "sh.600000", "entry_date": "2026-09-08",
                    "entry_price": 6.0, "date": "2026-09-15"}])
    alerts = svc.evaluate_holdings(h, "2026-09-15", {"hold_days_max": 30})

    assert any("over_realized" in a["reasons"] for a in alerts)


def test_fuyao_unavailable_raises(monkeypatch):
    """fuyao 快照不可用 → 抛错（job 记 failed → 企微告警），不静默。"""
    monkeypatch.setattr(svc, "_fuyao_prices", lambda symbols: ({}, {}, "fuyao 挂了"))
    h = _holdings([{"symbol": "sh.600000", "entry_date": "2026-08-01",
                    "entry_price": 10.0, "date": "2026-09-15"}])
    with pytest.raises(RuntimeError, match="fuyao"):
        svc.evaluate_holdings(h, "2026-09-15", {"hold_days_max": 30})
