"""调度任务定义（APScheduler）。

    - 任务槽位已注册（`JOB_REGISTRY` + `@register_job`）
    - `job_run_log` 记录每次执行
    - **启动补跑**机制就位（本地关机会漏跑，这是刚需，见 技术架构.md 8.5.2）
    - 任务失败 → 企微告警（模块 5，`_notify_failure`）
"""
from __future__ import annotations

import time
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

from app.core.config import settings
from app.core.errors import AppError
from app.core.logging import logger
from app.repos import meta_repo
from app.services.calendar_service import calendar

#: 任务注册表：job_id -> (显示名, 可调用对象)
JOB_REGISTRY: dict[str, dict[str, Any]] = {}


def register_job(job_id: str, name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """装饰器：把函数登记为调度任务。"""

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        JOB_REGISTRY[job_id] = {"job_id": job_id, "name": name, "fn": fn}
        return fn

    return decorator


def _run_with_log(
    job_id: str,
    run_date: str,
    trigger_type: str,
    fn: Callable[..., Any],
) -> dict[str, Any]:
    """统一的执行包装：记录 job_run_log、捕获异常、统计耗时。

    约定：任务函数签名统一为 `fn(run_date, *, trigger_type=...)` ——
    某些任务（模拟盘）需要在产物里记录触发来源（cron / catch_up / manual）。
    """
    if meta_repo.job_has_run(job_id, run_date) and trigger_type == "catch_up":
        logger.info(f"[{job_id}] {run_date} 已成功执行过，跳过补跑")
        return {"job_id": job_id, "run_date": run_date, "status": "skipped", "reason": "already_done"}

    log_id = meta_repo.job_start(job_id, run_date, trigger_type)
    t0 = time.perf_counter()
    try:
        result = fn(run_date, trigger_type=trigger_type)
        duration = time.perf_counter() - t0
        meta_repo.job_finish(log_id, "success", duration, str(result)[:500])
        logger.info(f"[{job_id}] {run_date} 执行成功，耗时 {duration:.2f}s")
        return {
            "job_id": job_id,
            "run_date": run_date,
            "status": "success",
            "duration": round(duration, 3),
            "result": result,
        }
    except AppError as exc:
        duration = time.perf_counter() - t0
        meta_repo.job_finish(log_id, "failed", duration, exc.message)
        logger.error(f"[{job_id}] {run_date} 执行失败：{exc.message}")
        _notify_failure(job_id, run_date, trigger_type, exc, duration)
        return {
            "job_id": job_id,
            "run_date": run_date,
            "status": "failed",
            "duration": round(duration, 3),
            "error": exc.message,
        }
    except Exception as exc:
        duration = time.perf_counter() - t0
        meta_repo.job_finish(log_id, "failed", duration, repr(exc))
        logger.exception(f"[{job_id}] {run_date} 执行失败（未预期异常）")
        _notify_failure(job_id, run_date, trigger_type, exc, duration)
        return {
            "job_id": job_id,
            "run_date": run_date,
            "status": "failed",
            "duration": round(duration, 3),
            "error": repr(exc),
        }


def _notify_failure(
    job_id: str,
    run_date: str,
    trigger_type: str,
    error: BaseException,
    duration: float,
) -> None:
    """任务失败 → 企微告警（模块 5）。

    ★ 这是模块 5 最有价值的消费场景：「跑批挂了你还不知道」是本项目最大的
      运维盲区 —— 日志里写了 failed，但没人会天天翻日志。

    ⚠️ 本函数**绝不允许抛异常**。它跑在异常处理分支里，一旦再抛就会
      把原始失败原因替换掉（`During handling of ... another exception occurred`），
      排查时看到的是告警的错，而不是任务真正的错。所以这里是最外层的兜底。
    """
    try:
        if not settings.alert.enabled:
            return
        from app.services import alert_service  # noqa: PLC0415 - 避免模块级环

        alert_service.notify_job_failed(
            job_id=job_id,
            job_name=JOB_REGISTRY.get(job_id, {}).get("name", job_id),
            run_date=run_date,
            trigger_type=trigger_type,
            error=error,
            duration=duration,
        )
    except Exception as exc:
        logger.warning(f"[{job_id}] 失败告警发送异常（已忽略，不影响任务状态）：{exc!r}")


# ======================================================================
# 任务 1：模拟盘日度跑批（模块 4，2026-09-20 实现）
# ======================================================================
@register_job("paper_daily", "模拟盘日度跑批")
def paper_daily_job(run_date: str, *, trigger_type: str = "manual") -> dict[str, Any]:
    """交易日 23:00 触发（主人 2026-09-20 拍板），更新模拟盘净值。

    流程（见 `paper_service` 模块 docstring）：
        1. 增量回补当日行情（`backfill_quotes --incremental`，尽力而为）
        2. 就绪校验（唯一闸门）：探针标的当日无 K 线 → DataNotReadyError
           → 本 job 记 failed → 次日启动补跑兜底（数据源更新慢的保底方案）
        3. 引擎全量重算 2025-01-01 → run_date（幂等：净值已更新到
           run_date 之后就跳过，防止补跑旧日期把新净值覆盖回去）
        4. 覆盖落盘 data/paper/default/ + 更新 paper_account

    非交易日：直接跳过（cron 是周一至周五的粗筛，节假日在这里精确拦）。
    """
    from app.services import paper_service  # noqa: PLC0415 - 延迟导入避免环

    if not calendar.is_trading_day(run_date):
        return {"skipped": True, "reason": f"{run_date} 非交易日"}

    # 回补由"数据在不在"驱动：先就绪校验（秒级），未就绪才增量回补
    # （30~50 分钟）再校验一次。补跑历史日期时数据通常已在盘上，零回补成本；
    # 但关机期间那段若从未回补过，也能在这里自动补上。
    result = paper_service.run_paper_day(run_date, trigger=trigger_type)
    return {"implemented": True, **result}


# ======================================================================
# 任务 4：模拟盘盘中检查（14:45，2026-10-09 新增）
# ======================================================================
@register_job("paper_intraday_check", "模拟盘盘中清仓预警")
def paper_intraday_check_job(run_date: str, *, trigger_type: str = "manual") -> dict[str, Any]:
    """交易日 14:45 触发：用 fuyao 实时快照价对当前持仓做清仓预警。

    背景：quality_reversal 的卖出口径是「T 日信号 -> T 日尾盘按收盘价成交」，
    引擎 23:00 才判定并撮合，消息推出来时已收盘。本任务提前到 14:45，
    用 fuyao 快照价近似当日收盘，给主人留 15 分钟操作窗口。
    ⚠️ 推送的是**预警**，实际成交以当晚 23:00 引擎重算为准（可能不一致）。
    补跑无意义（盘中检查是"当下播报"）→ 已加入 _NO_CATCH_UP_JOBS。
    """
    if not calendar.is_trading_day(run_date):
        return {"skipped": True, "reason": f"{run_date} 非交易日"}

    from app.services import paper_intraday_service  # noqa: PLC0415 - 延迟导入避免环

    result = paper_intraday_service.run_intraday_check()
    return {"implemented": True, "run_date": run_date, **result}

# ======================================================================
# 任务 3：每日持仓早报（模块 5，2026-09-20 实现）
# ======================================================================
@register_job("alert_daily_summary", "每日持仓早报")
def alert_daily_summary_job(run_date: str, *, trigger_type: str = "manual") -> dict[str, Any]:
    """交易日 **08:00** 发一条「今日持仓」早报（主人 2026-09-20 拍板）。

    为什么是 08:00 而不是更早/更晚：
    - 上一交易日 23:00 的跑批已经把净值算到最新交易日，
      **T+1 开盘的成交也已计入** —— 所以这时候的持仓是**确定值**；
    - 08:00 是人起床看手机的时间，9:30 开盘前留足反应时间。

    ⚠️ 与 `paper_daily_job` 的关键差异：**本任务不判断"是不是交易日"**。
       早报读的是**已有产物**（positions/nav），未开市也照样能发；
       而如果节假日不发，用户会觉得"这系统死了"。
       真正的闸门是**产物是否存在**（`_daily_summary_ctx` 返回 None 就跳过）。

    ⚠️ 本任务**绝不抛异常**：早报发不出去不该被记成"跑批失败"。
       `notify_daily_summary` 内部已吞掉所有异常，这里再包一层是防"连它也崩"。
    """
    try:
        from app.services import alert_service  # noqa: PLC0415 - 延迟导入避免环

        result = alert_service.notify_daily_summary(sync=True)
        return {"implemented": True, "run_date": run_date, **result}
    except Exception as exc:
        logger.warning(f"[alert_daily_summary] 早报派发异常（已忽略）：{exc!r}")
        return {"implemented": True, "skipped": True, "reason": f"异常：{exc!r}"}


# ======================================================================
# 任务 2：接口目录自检（模块 1）
# ======================================================================
@register_job("catalog_check", "接口目录自检")
def catalog_check_job(run_date: str, *, trigger_type: str = "manual") -> dict[str, Any]:
    """每日 09:00 校验目录里的接口是否仍然可用。

    实现：tools/check_catalog.run_check（CLI 与 API 共用同一份逻辑）。
    - 静态：函数存在性 + YAML 参数与签名一致性
    - 抽样：真实调用 3 个接口（受 akshare 1 秒限流，约 5~15 秒）

    为什么抽样失败不把接口标 broken：网络抖动与接口失效是两回事。
    静态结论才回写 status；抽样失败只记录在 live.results 里供人工判断。
    """
    from tools.check_catalog import run_check  # noqa: PLC0415

    summary = run_check(live_sample=3)
    if summary["static"]["broken"] or summary["static"]["missing"]:
        # 有接口失效 → 醒目日志（阶段 5 可在此触发企微提醒）
        logger.warning(
            f"[catalog_check] 发现失效接口：broken={summary['static']['broken']} "
            f"missing={summary['static']['missing']} —— 请查看 catalog_interface 表"
        )
    return {
        "implemented": True,
        "static": summary["static"],
        "live": {k: v for k, v in summary["live"].items() if k != "results"},
        "source_version": summary["source_version"],
        "run_date": run_date,
    }


# ======================================================================
# 应用启动初始化（不是定时任务，但放在一起便于统一调度）
# ======================================================================
@register_job("app_bootstrap", "应用启动初始化")
def app_bootstrap_job(run_date: str, *, trigger_type: str = "manual") -> dict[str, Any]:
    """应用启动时执行一次：检查本地数据就绪度，必要时自动补数据。"""
    from app.core.paths import TRADE_CALENDAR_PARQUET  # noqa: PLC0415

    actions: list[str] = []

    # 1) 交易日历缺失 → 自动同步
    if not TRADE_CALENDAR_PARQUET.exists():
        logger.warning("交易日历缺失，自动同步中...")
        calendar.refresh()
        actions.append("synced_trade_calendar")
    else:
        calendar.load(force=True)
        warn = calendar.coverage_warning()
        if warn:
            logger.warning(warn)

    # 2) 记录数据新鲜度
    meta_repo.set_meta_bulk(
        {
            "calendar_last_date": calendar.stats().get("last_date"),
            "app_bootstrapped_at": datetime.now().isoformat(timespec="seconds"),
        }
    )

    # 3) 数据新鲜度告警（**不阻塞启动** —— 实际补数由 paper_daily 的
    #    覆盖率闸门 + catch_up 兜底；这里只把"数据旧了"大声说出来）
    _warn_stale_data()

    return {"actions": actions, "calendar": calendar.stats()}


def _warn_stale_data() -> None:
    """启动时的数据新鲜度告警：行情落后日历 / 基本面陈旧（2026-10-08 新增）。

    为什么只告警不补：启动路径要快（回补 30~50 分钟会让浏览器长时间
    "拒绝连接"，见 run_startup_catch_up 的警告）；真正的补数由
    paper_daily 的 `sync_all`（覆盖率闸门驱动）+ 启动补跑负责。
    """
    from app.core.paths import QUOTE_DAILY_DIR  # noqa: PLC0415
    from app.services import data_store  # noqa: PLC0415
    from app.services.data_sync_service import (  # noqa: PLC0415
        READY_PROBE_SYMBOLS,
        fundamental_staleness,
    )

    last_trade = calendar.stats().get("last_date")
    probe = READY_PROBE_SYMBOLS[0]
    probe_path = QUOTE_DAILY_DIR / f"{probe}.parquet"
    if last_trade and probe_path.exists():
        try:
            df = data_store.query_duck(
                f"SELECT max(date) AS d FROM read_parquet('{probe_path.as_posix()}')"
            )
            last_quote = str(df["d"].iloc[0])[:10] if not df.empty else None
            if last_quote and last_quote < last_trade:
                logger.warning(
                    f"[bootstrap] 行情落后日历：最新 {last_quote} < {last_trade}。"
                    f"将由 paper_daily 的覆盖率闸门 + 启动补跑自动回补；"
                    f"也可手动执行 python -m tools.backfill_quotes --incremental"
                )
        except Exception as exc:
            logger.warning(f"[bootstrap] 行情新鲜度检查失败（忽略）：{exc!r}")

    fund = fundamental_staleness()
    stale_days = int(settings.data_sync.fundamental_stale_days)
    if fund.get("available") and fund.get("last_date"):
        age = (date.today() - date.fromisoformat(str(fund["last_date"]))).days
        if age > stale_days:
            logger.warning(
                f"[bootstrap] 基本面数据已 {age} 天未更新（> 阈值 {stale_days}，最新 "
                f"{fund['last_date']}）。⚠️ 不自动补（全量约 9 小时）；"
                f"季报披露期建议手动执行 python -m tools.backfill_fundamental"
            )
    elif not fund.get("available"):
        logger.warning(f"[bootstrap] 基本面数据不可用：{fund.get('reason')}")


# ======================================================================
# 统一执行入口
# ======================================================================
def run_job(job_id: str, run_date: str | None = None, trigger_type: str = "manual") -> dict[str, Any]:
    """按 job_id 执行任务（供 API / CLI / 调度器调用）。"""
    if job_id not in JOB_REGISTRY:
        return {"status": "failed", "error": f"未知任务：{job_id}"}

    target = run_date or calendar.latest_trading_day()
    fn = JOB_REGISTRY[job_id]["fn"]
    return _run_with_log(job_id, target, trigger_type, fn)


def catch_up_missing_days(job_id: str, *, max_days: int | None = None) -> dict[str, Any]:
    """启动补跑：补齐上次成功执行到今天的缺失交易日。

    本地阶段电脑不可能 7×24 开机，这是**刚需**（见 技术架构.md 8.5.2）。
    """
    if job_id not in JOB_REGISTRY:
        return {"status": "failed", "error": f"未知任务：{job_id}"}

    limit = max_days or settings.scheduler.catch_up_max_days
    today = date.today().strftime("%Y-%m-%d")
    last_done = meta_repo.job_last_success_date(job_id)

    if last_done is None:
        logger.info(f"[{job_id}] 无历史成功记录，跳过补跑（首次运行由 cron 触发）")
        return {"status": "skipped", "reason": "no_history", "job_id": job_id}

    start = calendar.next_trading_day(last_done) if last_done < today else None
    if start is None or start > today:
        return {"status": "ok", "job_id": job_id, "missing": 0}

    missing = calendar.trading_days(start, today)
    if not missing:
        return {"status": "ok", "job_id": job_id, "missing": 0}

    if len(missing) > limit:
        logger.warning(
            f"[{job_id}] 缺失 {len(missing)} 个交易日，超过上限 {limit}，"
            f"只补最近 {limit} 个。请人工确认是否需要全量补跑。"
        )
        missing = missing[-limit:]

    # ★ 阶段 5 修正（2026-09-20）：此处原先残留阶段 0 的 stub
    #   （`if job_id == "paper_daily": return {"status": "pending", ...}`），
    #   导致模块 4 的「启动补跑兜底」**实际从未生效** ——
    #   而模块 4 的 docstring 里写着它已就位。这就是本项目头号坑
    #   「写了没被调用」的又一例：stub 看起来像在拦截，其实是在短路。
    #   现所有已注册任务一视同仁，直接执行补跑。
    logger.info(
        f"[{job_id}] 检测到 {len(missing)} 个缺失交易日："
        f"{missing[0]} ~ {missing[-1]}，开始补跑"
    )
    results = [run_job(job_id, d, "catch_up") for d in missing]
    succeeded = sum(1 for r in results if r.get("status") == "success")
    skipped = sum(1 for r in results if r.get("status") == "skipped")
    failed = len(results) - succeeded - skipped
    if failed:
        logger.warning(f"[{job_id}] 补跑结束：成功 {succeeded} / 跳过 {skipped} / 失败 {failed}")
    return {
        "status": "ok" if not failed else "partial",
        "job_id": job_id,
        "missing": len(missing),
        "executed": len(results),
        "succeeded": succeeded,
        "skipped": skipped,
        "failed": failed,
        "details": results,
    }


# ======================================================================
# 任务 5：基本面/财务周频回补（每周一 06:00，主人 2026-10-10 拍板）
# ======================================================================
#: 子进程超时：全量补完 ~9h（本地实测）；服务器 IO 更慢留余量。
#: 周一 06:00 + 12h 上限 = 18:00 前结束，不与 23:00 paper_daily 重叠。
_FUNDAMENTAL_TIMEOUT = 12 * 60 * 60


@register_job("fundamental_weekly", "基本面/财务周频回补")
def fundamental_weekly_job(run_date: str, *, trigger_type: str = "manual") -> dict[str, Any]:
    """每周一 06:00 一次性补完基本面/财务数据（`tools.backfill_fundamental --force`）。

    设计（2026-10-10 主人拍板：一次性补完、接受长时间运行）：
    - **子进程**运行 —— akshare 会话与主进程隔离，状态文件断点续传
      （`data/meta/backfill_fundamental_state.json`），中断重跑即续上；
    - **`--force` 全量重拉**：三大报表/财务指标接口只能全量分页拉取（20~25 页/次），
      东财没有"是否有新数据"的廉价探测接口 —— 无法做到"没有就不拉"。
      落盘 `save_fundamental` 按 [report_date, notice_date] 去重：
      **数据效果 = 有更新则补上、没有则不变**（操作上是全量，结果上是增量）；
    - 批量数据集（业绩报表/预告/股东户数/分红/基金持仓）按报告期记录，
      没有新报告期时自动"没有待办"直接跳过 —— 这部分天然符合"没有则不做操作"；
    - **不判交易日**：东财披露不受交易日限制，周一固定跑；
    - 已加入 `_NO_CATCH_UP_JOBS`：9 小时级任务不适合启动补跑，错过等下周一；
    - 06:00 起跑 + 12h 超时上限 = 18:00 前结束，不与 23:00 paper_daily 重叠
      （不再有新旧基本面混合读窗口）。
    """
    import subprocess  # noqa: PLC0415 - 仅本任务用
    import sys  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    backend_root = Path(__file__).resolve().parents[2]
    t0 = time.perf_counter()
    try:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "tools.backfill_fundamental",
                "--force",
            ],
            cwd=str(backend_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_FUNDAMENTAL_TIMEOUT,
            check=False,
        )
        ok = proc.returncode == 0
        tail = (proc.stdout or "").strip().splitlines()[-5:]
        if not ok:
            logger.error(f"[fundamental_weekly] 回补失败 rc={proc.returncode}，尾部输出：{tail}")
        return {
            "implemented": True,
            "ok": ok,
            "mode": "force_full",
            "returncode": proc.returncode,
            "cost_seconds": round(time.perf_counter() - t0, 1),
            "tail": tail,
        }
    except subprocess.TimeoutExpired:
        logger.warning(
            f"[fundamental_weekly] 回补超时（>{_FUNDAMENTAL_TIMEOUT // 3600}h），"
            "状态文件已续传 —— 下周一自动接着跑"
        )
        return {
            "implemented": True,
            "ok": False,
            "timeout": True,
            "cost_seconds": round(time.perf_counter() - t0, 1),
        }
