"""调度任务 · 启动补跑的测试。

★ 为什么要专门写这个文件
------------------------
`catch_up_missing_days()` 长期残留阶段 0 的 stub：

    if job_id == "paper_daily":
        return {"status": "pending", ...}

导致模块 4 的「启动补跑兜底」**实际从未生效**，而模块 4 的 docstring
里写着它已就位。这个 bug 活了整整一个阶段没被发现，根因就是
**补跑链路零测试覆盖** —— 没有任何断言会在 stub 短路时失败。

本文件的职责：让"补跑被短路"这件事必然被测出来。
断言的核心不是返回值形状，而是 **任务函数真的被调用了**。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import ClassVar

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.repos import db as db_module  # noqa: E402
from app.repos import meta_repo  # noqa: E402
from app.scheduler import jobs  # noqa: E402


# ======================================================================
# 夹具：tmp meta.db + 假日历
# ======================================================================
@pytest.fixture
def jobs_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """tmp meta.db（含 job_run_log）+ 可控日历 + 清空任务注册表。

    ★ 清空 `JOB_REGISTRY` 是必须的：它是**模块级全局字典**，
      测试往里注册的假任务会污染其他测试（夹具污染 → 数量断言全错）。
    """
    db_module.close_connection()
    monkeypatch.setattr(db_module, "META_DB", tmp_path / "meta" / "meta.db")
    conn = db_module.get_connection()
    schema = (BACKEND_ROOT / "sql" / "schema.sql").read_text(encoding="utf-8")
    conn.executescript(schema)

    # 保存并清空注册表，测试后恢复
    saved = dict(jobs.JOB_REGISTRY)
    jobs.JOB_REGISTRY.clear()

    yield

    jobs.JOB_REGISTRY.clear()
    jobs.JOB_REGISTRY.update(saved)
    db_module.close_connection()


class _FrozenDate:
    """假的 `datetime.date`：`today()` 返回钉死的那天。

    ⚠️ `datetime.date` 是**不可变 C 类型**，不能 `setattr` 它的 `today`
      （`TypeError: cannot set 'today' attribute of immutable type`）。
      只能把 `jobs.date` 这个模块级名字整个换掉。
    """

    _fixed = "2026-09-18"

    @classmethod
    def today(cls):
        from datetime import date as _real

        return _real.fromisoformat(cls._fixed)


@pytest.fixture
def freeze_today(monkeypatch: pytest.MonkeyPatch):
    """把补跑看到的"今天"钉死为 2026-09-18（FakeCalendar 的最后一个交易日）。"""
    monkeypatch.setattr(jobs, "date", _FrozenDate)
    return _FrozenDate


class FakeCalendar:
    """可控日历：交易日固定为 `2026-09-14` ~ `2026-09-18`（周一到周五）。"""

    DAYS: ClassVar[list[str]] = [
        "2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18",
    ]

    def next_trading_day(self, day: str) -> str:
        after = [d for d in self.DAYS if d > day]
        return after[0] if after else "2099-01-01"

    def trading_days(self, start: str, end: str) -> list[str]:
        return [d for d in self.DAYS if start <= d <= end]


@pytest.fixture
def fake_calendar(monkeypatch: pytest.MonkeyPatch) -> FakeCalendar:
    cal = FakeCalendar()
    monkeypatch.setattr(jobs, "calendar", cal)
    return cal


def _register_recorder(job_id: str = "paper_daily") -> list[str]:
    """注册一个只记录调用日期的假任务，返回记录列表。"""
    called: list[str] = []

    @jobs.register_job(job_id, "假任务")
    def _fake(run_date: str, *, trigger_type: str = "cron") -> dict[str, str]:
        called.append(f"{run_date}|{trigger_type}")
        return {"ok": "1"}

    return called


# ======================================================================
# 核心：补跑必须真的执行
# ======================================================================
def test_catch_up_executes_registered_job(jobs_env, fake_calendar, freeze_today):
    """🔴 回归主断言：缺失交易日必须**真的调用了任务函数**。

    修 bug 前：`paper_daily` 撞上 stub，直接返回 `{"status": "pending"}`
      → 任务函数零调用 → 补跑静默失效。
    修 bug 后：一视同仁执行。
    """
    called = _register_recorder("paper_daily")

    # 上次成功在 09-15，之后 09-16/17/18 三个交易日缺失
    log_id = meta_repo.job_start("paper_daily", "2026-09-15", "cron")
    meta_repo.job_finish(log_id, "success", 1.0, "")
    _force_last_success("paper_daily", "2026-09-15")

    result = jobs.catch_up_missing_days("paper_daily")

    assert result["status"] in ("ok", "partial")
    assert result["missing"] == 3, f"应补 09-16/17/18 三天，实际 {result}"
    assert len(called) == 3, f"任务函数应被调用 3 次，实际 {called}"
    assert all(t == "catch_up" for _, t in (c.split("|") for c in called))


def test_catch_up_no_history_skips(jobs_env, fake_calendar, freeze_today, monkeypatch):
    """无历史成功记录 → 跳过（首次运行交给 cron，不擅自全量补）。

    ⚠️ 必须显式把 last_success 钉成 None —— 库是 tmp 的但**同文件内共享**，
      前一个用例写的 09-15 success 记录会污染本用例（夹具污染）。
    """
    called = _register_recorder()
    _force_last_success("paper_daily", None)
    result = jobs.catch_up_missing_days("paper_daily")
    assert result["status"] == "skipped"
    assert result["reason"] == "no_history"
    assert called == [], "无历史时不该执行任何补跑"


def test_catch_up_up_to_date_does_nothing(jobs_env, fake_calendar, freeze_today):
    """上次成功已是最后交易日 → missing=0，不执行。"""
    called = _register_recorder()
    _force_last_success("paper_daily", "2026-09-18")

    result = jobs.catch_up_missing_days("paper_daily")
    assert result["missing"] == 0
    assert called == []


def test_catch_up_respects_max_days(jobs_env, fake_calendar, freeze_today):
    """缺失超过上限 → 只补最近 N 天（防手滑全量重跑）。"""
    called = _register_recorder()
    _force_last_success("paper_daily", "2026-09-13")

    result = jobs.catch_up_missing_days("paper_daily", max_days=2)
    assert result["missing"] == 2, "上限生效，只补最近 2 天"
    assert len(called) == 2


def test_catch_up_unknown_job_fails(jobs_env, fake_calendar):
    """未注册的任务 → 明确报错，不静默。"""
    result = jobs.catch_up_missing_days("no_such_job")
    assert result["status"] == "failed"
    assert "未知任务" in result["error"]


def test_catch_up_partial_when_some_fail(jobs_env, fake_calendar, freeze_today):
    """部分失败 → status=partial，且**不因一条失败中断后续**。"""
    called: list[str] = []

    @jobs.register_job("paper_daily", "会失败的任务")
    def _flaky(run_date: str, *, trigger_type: str = "cron") -> dict[str, str]:
        called.append(run_date)
        if run_date == "2026-09-16":
            raise RuntimeError("模拟 09-16 失败")
        return {"ok": "1"}

    _force_last_success("paper_daily", "2026-09-15")

    result = jobs.catch_up_missing_days("paper_daily")
    assert result["status"] == "partial"
    assert result["succeeded"] == 2
    assert result["failed"] == 1
    assert len(called) == 3, "失败的当天不应中断后续补跑"


# ======================================================================
# 每日持仓早报任务（alert_daily_summary）
# ======================================================================
def test_daily_summary_job_is_registered():
    """任务必须真的注册进 `JOB_REGISTRY`，否则调度器静默不排它。

    ★ 这是本项目头号坑「写了没被调用」的防线：装饰器没生效 / 名字写错
      都不会有任何报错，只会"到点没消息"。
      所以这里断言的是**注册表里有它**，而不是"函数存在"。
    """
    assert "alert_daily_summary" in jobs.JOB_REGISTRY, "早报任务未注册"
    assert jobs.JOB_REGISTRY["alert_daily_summary"]["name"] == "每日持仓早报"


def test_daily_summary_job_excluded_from_catch_up():
    """早报**不参与启动补跑** —— 它不是"某天的计算任务"。

    补跑历史日期时它播报的是**当前**持仓，与日期对不上；
    且 30 天补跑会连发 30 次（幂等键只挡住 29 次，剩下的是白跑）。
    """
    from app.scheduler.scheduler import _NO_CATCH_UP_JOBS

    assert "alert_daily_summary" in _NO_CATCH_UP_JOBS


def test_daily_summary_job_not_weekday_only():
    """早报不受 `day_of_week=mon-fri` 粗筛限制。

    它读的是已有产物，周末也发得出来；而"周末静默"会被误认为"系统死了"。
    真正闸门是产物是否存在（`_daily_summary_ctx` 返回 None）。
    """
    from app.scheduler.scheduler import _WEEKDAY_ONLY_JOBS

    assert "alert_daily_summary" not in _WEEKDAY_ONLY_JOBS


def test_daily_summary_job_swallows_exception(
    jobs_env, monkeypatch: pytest.MonkeyPatch
):
    """任务体内抛异常也不许冒泡 —— 早报失败不该被记成"跑批失败"。"""
    from app.services import alert_service

    def boom(**kwargs):
        raise RuntimeError("渲染炸了")

    monkeypatch.setattr(alert_service, "notify_daily_summary", boom)

    # 重新拿到注册表里的真函数（jobs_env 清空过，这里直接用模块级函数）
    result = jobs.alert_daily_summary_job("2026-09-18", trigger_type="cron")

    assert result["skipped"] is True
    assert "渲染炸了" in result["reason"]


def test_daily_summary_job_reports_result(jobs_env, monkeypatch: pytest.MonkeyPatch):
    """正常路径：把 alert_service 的结果原样带出来（供 job_run_log 记录）。"""
    from app.services import alert_service

    monkeypatch.setattr(
        alert_service, "notify_daily_summary",
        lambda **kw: {"dispatched": True, "ok": True, "status": "sent"},
    )

    result = jobs.alert_daily_summary_job("2026-09-18", trigger_type="cron")

    assert result["implemented"] is True
    assert result["status"] == "sent"
    assert result["run_date"] == "2026-09-18"


# ======================================================================
# 辅助
# ======================================================================
def _force_last_success(job_id: str, day: str | None) -> None:
    """把 `job_last_success_date` 的结果钉死为指定日期（`None` = 无历史）。

    ⚠️ 不直接写库而是 monkeypatch —— `job_last_success_date` 的 SQL 口径
      （只认 success、取 max(run_date)）不该在本文件里被复述一遍。
      另一个原因：库是**同文件内共享的 tmp 库**，不钉死就会被前一个用例污染。
    """
    import app.scheduler.jobs as m

    m.meta_repo.job_last_success_date = lambda jid: day if jid == job_id else None


# ======================================================================
# 兜底（2026-10-10）：fundamental_weekly 失败必须抛（此前被 job 层吞成 success）
# ======================================================================
def test_fundamental_weekly_failure_raises(jobs_env, monkeypatch):
    """★ 子进程 ok=False（超时/停滞/rc≠0）→ AppError → job 记 failed + 企微告警。

    旧实现返回 {"ok": False} 不抛 —— _run_with_log 只看异常，
    一周基本面静默缺失、企微零感知（调研确认的缺口）。
    """
    from app.core.errors import AppError
    from app.core.subprocess_runner import run_with_watchdog as _real

    def _fake_fail(*args, **kwargs):
        return {"name": "基本面周频回补", "ok": False, "stalled": True,
                "reason": "进度停滞超过 1800s（挂死特征）", "cost_seconds": 1801.0,
                "tail": ["..."]}

    monkeypatch.setattr("app.core.subprocess_runner.run_with_watchdog", _fake_fail)
    # jobs.py 里是函数内 import（from ... import run_with_watchdog）→ 必须打补丁在源头模块，
    # 函数内 import 每次都从模块取属性，源头替换即可生效。

    with pytest.raises(AppError) as ei:
        jobs.fundamental_weekly_job("2026-10-12", trigger_type="manual")

    msg = str(ei.value.message if hasattr(ei.value, "message") else ei.value)
    assert "基本面周频回补失败" in msg
    assert "停滞" in msg

    # 成功路径不受影响
    monkeypatch.setattr(
        "app.core.subprocess_runner.run_with_watchdog",
        lambda *a, **k: {"name": "基本面周频回补", "ok": True, "returncode": 0,
                          "cost_seconds": 1.0, "tail": []},
    )
    res = jobs.fundamental_weekly_job("2026-10-12", trigger_type="manual")
    assert res["ok"] is True and res["mode"] == "force_full"
    assert _real is not None  # 占位引用（保持导入一致性）
