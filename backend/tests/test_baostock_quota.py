"""baostock 每日 API 配额计数器（6 小时封禁保护，2026-10-10）的单测。"""
from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from app.core.errors import DataSourceError

# ⚠️ data_source/__init__ 把单例实例也命名为 baostock_client（遮蔽模块名）
# —— 必须 importlib 取**模块**本身，monkeypatch 才打在源码上
bc = importlib.import_module("app.services.data_source.baostock_client")


@pytest.fixture
def quota_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """tmp 配额文件 + 小限额（便于触发）。"""
    qf = tmp_path / "meta" / "baostock_quota.json"
    qf.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(bc, "BAOSTOCK_QUOTA_FILE", qf)
    monkeypatch.setattr(bc.settings.datasource, "baostock_daily_api_limit", 3)
    return qf


def test_counts_accumulate_within_day(quota_env: Path) -> None:
    """同日多次调用计数累加，未到限额不抛。"""
    for i in range(1, 4):
        bc._quota_check_and_count()
        data = json.loads(quota_env.read_text(encoding="utf-8"))
        assert data["count"] == i


def test_limit_rejected_within_day(quota_env: Path) -> None:
    """★ 达到当日上限 → 再调用抛 DataSourceError（6 小时封禁保护）。"""
    for _ in range(3):
        bc._quota_check_and_count()
    with pytest.raises(DataSourceError) as ei:
        bc._quota_check_and_count()
    assert "已达上限" in str(ei.value.message)
    # 计数不再增长（拒绝时不 +1）
    data = json.loads(quota_env.read_text(encoding="utf-8"))
    assert data["count"] == 3


def test_resets_next_day(quota_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 跨天自动重置：昨日打满 → 今日从 0 重新计数。"""
    quota_env.write_text(json.dumps({"date": "2020-01-01", "count": 999}), encoding="utf-8")
    bc._quota_check_and_count()   # 昨日计数不继承
    data = json.loads(quota_env.read_text(encoding="utf-8"))
    assert data["count"] == 1


def test_corrupt_file_starts_from_zero(
    quota_env: Path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """计数文件损坏 → 告警并从 0 重计（不崩）。"""
    quota_env.write_text("not-json{{", encoding="utf-8")
    bc._quota_check_and_count()
    data = json.loads(quota_env.read_text(encoding="utf-8"))
    assert data["count"] == 1


def test_client_query_counts(monkeypatch, tmp_path: Path) -> None:
    """★ 客户端 _query 真实调用计数 +1；缓存命中不计数。"""
    qf = tmp_path / "q.json"
    monkeypatch.setattr(bc, "BAOSTOCK_QUOTA_FILE", qf)
    monkeypatch.setattr(bc.settings.datasource, "baostock_daily_api_limit", 100)

    client = bc.BaostockClient()
    # mock 会话层：不真连 baostock
    monkeypatch.setattr(client, "_with_session", lambda call: call())
    monkeypatch.setattr(client, "_logged_in", True)

    def _call():
        return {"error_code": "0", "data": []}

    client._query("query_x", _call, cache_key="k1", cache_ttl=60)
    client._query("query_x", _call, cache_key="k1", cache_ttl=60)   # 命中缓存
    data = json.loads(qf.read_text(encoding="utf-8"))
    assert data["count"] == 1, "缓存命中不应计数"
