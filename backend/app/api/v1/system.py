"""系统级接口：健康检查、调度任务、存储状态、数据源能力。

这些接口是阶段 0 的「验收面」——打开系统就能看到数据层是否真的就绪。
"""
from __future__ import annotations

import sys
import time
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Query

from app.core.config import settings
from app.core.errors import AppError, InvalidParamError
from app.core.logging import logger
from app.core.paths import META_DB, relative_to_project
from app.repos import db, meta_repo
from app.scheduler import scheduler as sched
from app.schemas.common import ApiResponse, HealthData
from app.services import calendar_service, data_store, datasource_matrix
from app.services.data_source import akshare_client, baostock_client, health_all

router = APIRouter(tags=["system"])

_PROCESS_START = time.time()


def _human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024  # type: ignore[assignment]
    return f"{n:.1f} PB"


# ======================================================================
# 健康检查
# ======================================================================
def _build_health(deep: bool = False) -> HealthData:
    warnings: list[str] = []

    # --- 数据源 ---
    try:
        ds_raw = health_all(deep=deep)
    except Exception as exc:
        logger.exception("数据源健康检查失败")
        ds_raw = []
        warnings.append(f"数据源检查异常：{exc}")

    versions: dict[str, str] = {}
    for item in ds_raw:
        versions[item["name"]] = item.get("version", "")
    akshare_version = versions.get("akshare", "")
    baostock_version = versions.get("baostock", "")

    # 版本锁校验
    pin = settings.env.akshare_version_pin
    if pin and akshare_version and akshare_version != pin:
        warnings.append(
            f"akshare 版本漂移：当前 {akshare_version}，要求锁定 {pin}。"
            f"升级前请跑 tools/check_catalog.py"
        )

    # --- 交易日历 ---
    cal_stats: dict[str, Any] | None = None
    try:
        cal_stats = calendar_service.calendar.stats()
        warn = calendar_service.calendar.coverage_warning()
        if warn:
            warnings.append(warn)
    except AppError as exc:
        warnings.append(f"交易日历未就绪：{exc.message}")
    except Exception as exc:
        warnings.append(f"交易日历检查异常：{exc}")

    # --- 存储 ---
    storage: dict[str, Any] | None = None
    try:
        storage = data_store.storage_stats()
    except Exception as exc:
        warnings.append(f"存储统计失败：{exc}")

    # --- 元数据库 ---
    database: dict[str, Any] | None = None
    try:
        size = db.db_size_bytes()
        database = {
            "db_path": relative_to_project(META_DB),
            "initialized": db.is_initialized(),
            "size_bytes": size,
            "size_human": _human_bytes(size),
            "table_counts": db.table_row_counts() if db.is_initialized() else {},
        }
        if not database["initialized"]:
            warnings.append("meta.db 尚未初始化，请执行：python -m tools.init_db")
    except Exception as exc:
        warnings.append(f"元数据库检查失败：{exc}")

    # --- 状态判定 ---
    ds_available = [d["available"] for d in ds_raw]
    if not ds_raw:
        status = "error"
    elif any(not a for a in ds_available) or warnings:
        status = "degraded"
    else:
        status = "ok"

    return HealthData(
        status=status,
        app_name=settings.app.name,
        app_version=settings.app.version,
        env=settings.env.app_env,
        server_time=datetime.now().astimezone().isoformat(timespec="seconds"),
        uptime_seconds=round(time.time() - _PROCESS_START, 2),
        python_version=sys.version.split()[0],
        akshare_version=akshare_version,
        baostock_version=baostock_version,
        data_last_date=meta_repo.get_meta_value("quote_last_date"),
        datasources=ds_raw,
        calendar=cal_stats,
        storage=storage,
        database=database,
        scheduler_enabled=settings.scheduler.enabled,
        jobs=sched.list_jobs(),
        warnings=warnings,
        wecom_configured=settings.env.wecom_webhook_configured,
        wecom_key_masked=settings.env.wecom_webhook_key_masked,
    )


@router.get("/health", response_model=ApiResponse[HealthData], summary="健康检查")
def health(deep: bool = Query(False, description="是否做真实请求探测（慢）")) -> ApiResponse[HealthData]:
    return ApiResponse.ok(_build_health(deep=deep))


# ======================================================================
# 调度任务
# ======================================================================
@router.get("/system/jobs", summary="调度任务列表与最近执行记录")
def list_jobs(limit: int = Query(20, ge=1, le=200)) -> ApiResponse[dict[str, Any]]:
    return ApiResponse.ok(
        {
            "scheduler": sched.scheduler_status(),
            "jobs": sched.list_jobs(),
            "recent_runs": meta_repo.job_recent(limit),
        }
    )


@router.post("/system/jobs/{job_id}/run", summary="手动触发一次任务（调试用）")
def run_job_now(
    job_id: str,
    run_date: str | None = Query(None, description="逻辑运行日，留空取最近交易日"),
) -> ApiResponse[dict[str, Any]]:
    from app.scheduler.jobs import JOB_REGISTRY  # noqa: PLC0415

    if job_id not in JOB_REGISTRY:
        raise InvalidParamError(
            f"未知任务：{job_id}",
            detail={"available": list(JOB_REGISTRY)},
        )
    result = sched.trigger_job_now(job_id, run_date)
    return ApiResponse.ok(result, message=f"任务 {job_id} 已执行")


@router.post("/system/catch-up", summary="手动执行启动补跑扫描")
def catch_up() -> ApiResponse[dict[str, Any]]:
    return ApiResponse.ok(sched.run_startup_catch_up(), message="补跑扫描完成")


# ======================================================================
# 交易日历
# ======================================================================
@router.get("/system/calendar", summary="交易日历状态")
def calendar_status() -> ApiResponse[dict[str, Any]]:
    try:
        return ApiResponse.ok(calendar_service.calendar.stats())
    except AppError as exc:
        return ApiResponse.fail(exc.code, exc.message)


@router.post("/system/calendar/refresh", summary="重新同步交易日历")
def calendar_refresh() -> ApiResponse[dict[str, Any]]:
    """重新同步交易日历。

    ⚠️ 本接口**必须**真的去拉数据。`calendar.refresh()` 内部用
       `force_refresh=True` 绕过 `_invoke` 的 1 小时缓存 ——
       否则会变成"假刷新"：返回成功、时间戳更新，但根本没连数据源。

    返回里的 `cache_bypassed` 就是给调用方/排查用的凭证：看到它才说明
    这次确实发起了真实请求。
    """
    try:
        info = calendar_service.calendar.refresh()
        return ApiResponse.ok(
            {**info, "cache_bypassed": True, "source": "baostock query_trade_dates"},
            message="交易日历已重新同步（已绕过缓存）",
        )
    except AppError as exc:
        return ApiResponse.fail(exc.code, exc.message)


@router.get("/system/calendar/days", summary="查询区间交易日")
def calendar_days(
    start: str = Query(..., description="YYYY-MM-DD"),
    end: str = Query(..., description="YYYY-MM-DD"),
) -> ApiResponse[dict[str, Any]]:
    days = calendar_service.calendar.trading_days(start, end)
    return ApiResponse.ok({"start": start, "end": end, "count": len(days), "days": days})


# ======================================================================
# 存储状态
# ======================================================================
@router.get("/system/storage", summary="本地存储统计")
def storage_status() -> ApiResponse[dict[str, Any]]:
    return ApiResponse.ok(data_store.storage_stats())


# ======================================================================
# 数据源
# ======================================================================
@router.get("/datasources", summary="数据源能力矩阵")
def datasource_matrix_api() -> ApiResponse[dict[str, Any]]:
    """读取 config/datasource_capabilities/*.yaml 并返回对比矩阵。

    - `_` 开头的文件是能力项元数据（表头），不是数据源
    - `verified=false` 的源是"依据官方文档整理，未实测"，前端整列标灰
    - 排序按关键能力覆盖数降序：字段最全的主源自然排前面
    """
    return ApiResponse.ok(datasource_matrix.load_matrix())


@router.get("/datasources/health", summary="数据源实时健康状态")
def datasource_health(
    deep: bool = Query(False, description="是否做真实请求探测（慢）"),
) -> ApiResponse[list[dict[str, Any]]]:
    return ApiResponse.ok(health_all(deep=deep))


@router.post("/datasources/{name}/probe", summary="探测单个数据源能力")
def datasource_probe(name: str) -> ApiResponse[dict[str, Any]]:
    """对指定数据源做一次**真实**能力探测。

    ⚠️ 探测必须真的发请求。所有取数都用 `force_refresh=True` ——
       否则会被 `_invoke` 的缓存挡住，"探测"变成"读缓存"，
       页面上显示正常但其实没连过网（这正是本轮修掉的假刷新类缺陷）。
    """
    key = name.lower()
    client = {"baostock": baostock_client, "akshare": akshare_client}.get(key)
    if client is None:
        raise InvalidParamError(f"不支持的数据源：{name}（可选 baostock / akshare）")

    out: dict[str, Any] = {"name": key, "version": client.version, "probes": {}}
    if key == "baostock":
        out["version_raw"] = baostock_client.version_raw

    # 1) 深度健康状态（真实请求，内含熔断/限流状态）
    try:
        status = client.health(deep=True)
        out["health"] = status.to_dict()
    except Exception as exc:
        out["health"] = {"available": False, "message": f"{type(exc).__name__}: {exc}", "deep": True}

    # 2) 交易日历真实取值
    try:
        t0 = time.perf_counter()
        days = client.fetch_trade_dates("2024-01-01", "2024-12-31", force_refresh=True)
        out["probes"]["trade_calendar"] = {
            "ok": bool(days),
            "count": len(days),
            "cache_bypassed": True,
            "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
        }
    except Exception as exc:
        out["probes"]["trade_calendar"] = {
            "ok": False,
            "cache_bypassed": True,
            "error": str(exc)[:300],
        }

    out["throttle"] = client.throttle_stats()
    return ApiResponse.ok(out)


# ======================================================================
# 配置
# ======================================================================
@router.get("/system/config", summary="当前生效配置（脱敏）")
def current_config() -> ApiResponse[dict[str, Any]]:
    """返回生效配置。密钥一律脱敏，只暴露是否已配置。"""
    return ApiResponse.ok(
        {
            "app": settings.app.model_dump(),
            "env": settings.env.app_env,
            "data": settings.data.model_dump(),
            "backtest": settings.backtest.model_dump(),
            "universe": settings.universe.model_dump(),
            "cost": settings.cost.model_dump(),
            "strategy_defaults": settings.strategy.model_dump(),
            "datasource": settings.datasource.model_dump(),
            "scheduler": {
                "enabled": settings.scheduler.enabled,
                "timezone": settings.scheduler.timezone,
                "jobs": settings.scheduler.jobs.model_dump(),
                "catch_up_on_startup": settings.scheduler.catch_up_on_startup,
            },
            "alert": {
                "enabled": settings.alert.enabled,
                "rate_limit_per_minute": settings.alert.rate_limit_per_minute,
                "channel": settings.env.wecom_channel,
                "wecom_configured": settings.env.wecom_webhook_configured,
                "wecom_key_masked": settings.env.wecom_webhook_key_masked,
            },
        }
    )


@router.post("/system/config/reload", summary="热重载配置")
def reload_config() -> ApiResponse[dict[str, Any]]:
    settings.reload()
    return ApiResponse.ok({"reloaded": True}, message="配置已重载")
