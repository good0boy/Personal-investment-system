"""APScheduler 调度器封装。

设计要点（见 技术架构.md 8.5.2）：
- 进程内调度，不引入 Celery/Redis
- 时区固定 Asia/Shanghai
- 交易日判断在任务内部做（cron 无法表达 A 股日历）
- 启动时执行补跑扫描
"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.core.logging import logger
from app.scheduler.jobs import JOB_REGISTRY, catch_up_missing_days, run_job

_scheduler: BackgroundScheduler | None = None


def get_scheduler() -> BackgroundScheduler:
    """获取调度器（单例）。"""
    global _scheduler
    if _scheduler is None:
        _scheduler = BackgroundScheduler(
            timezone=settings.scheduler.timezone,
            job_defaults={
                "coalesce": True,        # 错过的多次执行合并为一次
                "max_instances": 1,      # 同一任务不并发
                "misfire_grace_time": 3600,
            },
        )
    return _scheduler


def _make_runner(job_id: str):
    """生成任务执行包装（APScheduler 会传入无参调用）。"""
    def _runner() -> None:
        # ★ 启动补跑仍在进行时，定时任务**干净跳过**（2026-09-22 新增）。
        #   为什么必须显式跳过而不是"让它去跑"：
        #     补跑与 23:00 的 paper_daily 是同一件事（把净值推进到最新交易日），
        #     两者撞上时 `paper_service._RUN_LOCK` 会拒掉后者 → 该 job 记 failed →
        #     模块 5 立刻推一条"任务失败"企微告警。**那是误报**：
        #     系统没坏，只是补跑还没跑完；而补跑本身会把那天补上。
        #   跳过是安全的：`last_nav_date` 幂等闸门 + 补跑自身的推进保证状态一致。
        if catch_up_running():
            logger.warning(
                f"启动补跑仍在进行，跳过本次 {job_id} 的定时触发"
                f"（补跑会把缺失交易日照样补齐，无需人工干预）"
            )
            return
        try:
            run_job(job_id, None, "cron")
        except Exception:
            logger.exception(f"任务 {job_id} 执行异常")
    return _runner


#: job_id -> 配置键映射（config.yaml scheduler.jobs 下）
_JOB_CONFIG_KEYS = {
    "paper_daily": "paper_daily",
    "paper_intraday_check": "paper_intraday_check",
    "fundamental_weekly": "fundamental_weekly",
    "alert_daily_summary": "alert_daily_summary",
    "catalog_check": "catalog_check",
}


#: 只在周一至周五触发的任务（cron 粗筛；精确交易日判断在任务内做）。
#: ⚠️ 早报**不在此列** —— 它读的是已有产物，周末也发得出来，
#:    而且"周末静默"会被误认为"系统死了"。见 `alert_daily_summary_job`。
_WEEKDAY_ONLY_JOBS = {"paper_daily", "paper_intraday_check", "catalog_check"}


def start_scheduler() -> dict[str, Any]:
    """注册任务并启动。幂等。"""
    if not settings.scheduler.enabled:
        logger.info("调度器已在配置中禁用（scheduler.enabled=false），跳过启动")
        return {"started": False, "reason": "disabled_by_config", "jobs": []}

    sched = get_scheduler()
    if sched.running:
        logger.debug("调度器已在运行")
        return {"started": True, "reason": "already_running", "jobs": list_jobs()}

    jobs_cfg = settings.scheduler.jobs
    registered: list[dict[str, Any]] = []

    for job_id, cfg_key in _JOB_CONFIG_KEYS.items():
        if job_id not in JOB_REGISTRY:
            continue
        spec = getattr(jobs_cfg, cfg_key, None)
        if spec is None:
            continue

        meta = JOB_REGISTRY[job_id]
        if not spec.enabled:
            logger.info(f"任务 {job_id}（{meta['name']}）未启用，跳过注册")
            registered.append({"job_id": job_id, "name": meta["name"], "enabled": False})
            continue

        cron_kwargs: dict[str, Any] = {
            "hour": spec.cron.hour,
            "minute": spec.cron.minute,
            "timezone": settings.scheduler.timezone,
        }
        if job_id in _WEEKDAY_ONLY_JOBS:
            cron_kwargs["day_of_week"] = "mon-fri"   # 粗筛（精确判断在任务内）
        elif spec.cron.day_of_week:
            # 周频任务的 day_of_week 来自配置（如 "mon"）—— 不透传就会注册成每天跑
            cron_kwargs["day_of_week"] = spec.cron.day_of_week

        sched.add_job(
            _make_runner(job_id),
            trigger=CronTrigger(**cron_kwargs),
            id=job_id,
            name=meta["name"],
            replace_existing=True,
        )
        weekday_txt = (
            "每周一至周五 "
            if job_id in _WEEKDAY_ONLY_JOBS
            else f"每周{spec.cron.day_of_week} "
            if spec.cron.day_of_week
            else "每日 "
        )
        registered.append(
            {
                "job_id": job_id,
                "name": meta["name"],
                "enabled": True,
                "cron": f"{spec.cron.hour:02d}:{spec.cron.minute:02d}",
            }
        )
        logger.info(
            f"已注册任务 {job_id}（{meta['name']}）→ {weekday_txt}"
            f"{spec.cron.hour:02d}:{spec.cron.minute:02d}"
        )

    sched.start()
    logger.info(f"调度器已启动（时区 {settings.scheduler.timezone}）")

    return {"started": True, "reason": "ok", "jobs": registered}


def stop_scheduler() -> None:
    """停止调度器。"""
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("调度器已停止")


def list_jobs() -> list[dict[str, Any]]:
    """列出全部已注册任务及其状态。

    注意：即使任务因 `enabled: false` 未加入调度器，也要列出来并标注 enabled=False，
    否则前端会看到空列表，无法知道"框架已就位、只是开关没打开"。
    """
    sched = get_scheduler()
    running = sched.running

    # 调度器里实际在跑的任务 -> 下次运行时间
    live: dict[str, str | None] = {}
    if running:
        for job in sched.get_jobs():
            live[job.id] = (
                job.next_run_time.isoformat(timespec="seconds") if job.next_run_time else None
            )

    out: list[dict[str, Any]] = []
    for job_id, meta in JOB_REGISTRY.items():
        if job_id == "app_bootstrap":
            continue  # 启动任务不是定时任务，不在列表里展示

        cfg_key = _JOB_CONFIG_KEYS.get(job_id)
        spec = getattr(settings.scheduler.jobs, cfg_key, None) if cfg_key else None
        in_scheduler = job_id in live

        # 与 start_scheduler 用**同一个集合**判文案，避免两处口径打架
        weekday_only = job_id in _WEEKDAY_ONLY_JOBS
        weekday_txt = "周一至周五" if weekday_only else "每日"

        if in_scheduler:
            trigger = (
                f"cron {spec.cron.hour:02d}:{spec.cron.minute:02d}"
                f"（{weekday_txt}；非交易日"
                f"{'在任务内跳过' if weekday_only else '照发'}）"
            )
            enabled = True
        elif spec is not None and spec.enabled:
            trigger = f"cron {spec.cron.hour:02d}:{spec.cron.minute:02d}"
            enabled = True
        else:
            cron_txt = f"{spec.cron.hour:02d}:{spec.cron.minute:02d}" if spec else "--:--"
            trigger = f"已注册未启用（计划 {cron_txt}）"
            enabled = False

        out.append(
            {
                "job_id": job_id,
                "name": meta["name"],
                "trigger": trigger,
                "next_run_time": live.get(job_id),
                "enabled": enabled,
                "in_scheduler": in_scheduler,
            }
        )
    return out


def trigger_job_now(job_id: str, run_date: str | None = None) -> dict[str, Any]:
    """手动触发一次任务（调试用）。

    ★ 补跑期间必须拒绝（2026-10-11）：`POST /system/jobs/{id}/run` 直连本函数，
      绕过了 cron 路径上的 `catch_up_running()` 守卫（scheduler.py:50-55）与
      `paper_service.trigger_run_now` 的 `_RUN_LOCK` 预检 —— 补跑进行中点它，
      `run_paper_day` 抢锁失败抛 PaperError → job_run_log 记 **failed** →
      **误推一条"模拟盘跑批失败"企微告警**，正是 cron 路径当初要避免的那类误报。
    """
    if catch_up_running():
        return {
            "status": "skipped",
            "reason": "catch_up_running",
            "job_id": job_id,
            "note": "启动补跑正在进行中，请等补跑结束后再手动触发（避免抢锁失败误告警）",
        }
    return run_job(job_id, run_date, "manual")


#: 不参与「启动补跑」的任务。
#:
#: ⚠️ 早报**必须排除**：它不是"某天的计算任务"，而是"把当前产物播报出去"。
#:    补跑 30 天会连发 30 次（好在幂等键 `{date}|daily_summary` 会撞掉 29 次，
#:    但那 29 次是**白跑引擎外的读盘 + 渲染**，且日志里一片 skipped 噪音）。
#:    更糟的是语义错误：补跑历史日期时播报的是**当前**持仓，与日期对不上。
_NO_CATCH_UP_JOBS = {"alert_daily_summary", "paper_intraday_check", "fundamental_weekly"}


def run_startup_catch_up() -> dict[str, Any]:
    """启动补跑：扫描缺失的交易日并补齐（**同步**执行）。

    本地阶段是刚需 —— 电脑关机就会漏跑（见 技术架构.md 8.5.2）。

    ⚠️ 实测这里会跑 **30~90 分钟**（缺的交易日要增量回补全市场行情，
       baostock 单只一请求）。所以 **lifespan 不要直接调它** ——
       那会让 uvicorn 在补跑结束前都不监听端口，浏览器一律"拒绝连接"。
       Web 入口请用 `start_catch_up_background()`。
       保留同步版本是给测试与 CLI（`python -m tools...` 之类）用的。
    """
    if not settings.scheduler.catch_up_on_startup:
        return {"enabled": False}

    result: dict[str, Any] = {
        "enabled": True,
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "jobs": {},
    }
    for job_id in _JOB_CONFIG_KEYS:
        if job_id not in JOB_REGISTRY:
            continue
        if job_id in _NO_CATCH_UP_JOBS:
            result["jobs"][job_id] = {"status": "skipped", "reason": "not_catch_up_able"}
            continue
        try:
            result["jobs"][job_id] = catch_up_missing_days(job_id)
        except Exception as exc:
            logger.exception(f"补跑 {job_id} 失败")
            result["jobs"][job_id] = {"status": "failed", "error": repr(exc)}
    return result


# ======================================================================
# 后台补跑（P1-1，2026-09-22）
# ======================================================================
#: 补跑的进程内状态。用一把锁保护，因为读它的有主线程、APScheduler 线程与 HTTP 线程。
_catch_up_lock = threading.Lock()
_catch_up_state: dict[str, Any] = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "result": None,
    "error": None,
}


def catch_up_running() -> bool:
    """启动补跑是否正在进行（供定时任务与状态接口判断）。"""
    with _catch_up_lock:
        return bool(_catch_up_state["running"])


def catch_up_state() -> dict[str, Any]:
    """补跑状态快照（**只读副本**，别把内部 dict 泄出去）。"""
    with _catch_up_lock:
        return {
            "running": bool(_catch_up_state["running"]),
            "started_at": _catch_up_state["started_at"],
            "finished_at": _catch_up_state["finished_at"],
            "error": _catch_up_state["error"],
            "result": _catch_up_state["result"],
        }


def start_catch_up_background() -> dict[str, Any]:
    """把启动补跑丢进后台线程，**立即返回**。

    这是 P1-1 的核心：`lifespan` 里改成调这个之后，uvicorn 会在几秒内开始
    监听端口，UI 与 `/api/health` 立刻可用；补跑在后台继续，跑完打一行日志。

    幂等：已在跑就返回 `{"started": False}`，不会起第二个线程。
    """
    if not settings.scheduler.catch_up_on_startup:
        logger.info("启动补跑已在配置中禁用（scheduler.catch_up_on_startup=false）")
        return {"started": False, "reason": "disabled_by_config"}

    with _catch_up_lock:
        if _catch_up_state["running"]:
            logger.info("启动补跑已在进行中，不再重复启动")
            return {"started": False, "reason": "already_running"}
        _catch_up_state.update(
            running=True,
            started_at=datetime.now().isoformat(timespec="seconds"),
            finished_at=None,
            result=None,
            error=None,
        )

    def _worker() -> None:
        logger.info(
            "启动补跑开始（后台线程）—— 端口已在监听，补跑期间接口可正常访问；"
            "若日志出现「检测到 N 个缺失交易日」，说明正在增量回补，可能持续 30~90 分钟"
        )
        try:
            res = run_startup_catch_up()
            with _catch_up_lock:
                _catch_up_state["result"] = res
            summary = {k: v.get("status") for k, v in (res.get("jobs") or {}).items()}
            logger.info(f"启动补跑结束：{summary}")
        except Exception as exc:
            logger.exception("启动补跑失败")
            with _catch_up_lock:
                _catch_up_state["error"] = repr(exc)
        finally:
            with _catch_up_lock:
                _catch_up_state["running"] = False
                _catch_up_state["finished_at"] = datetime.now().isoformat(timespec="seconds")

    thread = threading.Thread(target=_worker, name="startup-catch-up", daemon=True)
    thread.start()
    return {"started": True, "thread": thread.name}


def scheduler_status() -> dict[str, Any]:
    sched = get_scheduler()
    return {
        "enabled": settings.scheduler.enabled,
        "running": sched.running,
        "timezone": settings.scheduler.timezone,
        "job_count": len(sched.get_jobs()) if sched.running else 0,
        "catch_up_on_startup": settings.scheduler.catch_up_on_startup,
        # ★ 补跑状态（P1-1）：前端据此显示"启动补跑进行中"，
        #   而不是让人对着空页面猜"服务是不是没起来"。
        "catch_up": catch_up_state(),
        "catch_up_max_days": settings.scheduler.catch_up_max_days,
    }
