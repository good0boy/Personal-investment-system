"""模块 4 · 模拟仿真的接口（阶段 4）。

读路径只读（架构纪律 5）；`POST /{account_id}/run` 是**唯一的写路径**，
且它是"触发一次跑批"而不是"执行一次跑批" —— 一次跑批 = 增量回补
（约 30 分钟）+ 重算（45s），HTTP 请求链路里同步等必超时，
所以这里只把任务扔给调度器立即执行，前端轮询净值与任务日志看结果。
"""
from __future__ import annotations

from fastapi import APIRouter, Query

from app.schemas.common import ApiResponse
from app.services import paper_service as svc

router = APIRouter(tags=["paper"])


@router.get("/accounts", summary="模拟账户列表（含最新净值摘要）")
def list_accounts() -> ApiResponse[list[dict]]:
    """首次访问会幂等初始化默认账户（2025-01-01 起步，初始资金 100 万）。"""
    return ApiResponse.ok(svc.list_account_summaries())


@router.get("/{account_id}/nav", summary="模拟盘净值与概览（LTTB 降采样）")
def get_nav(
    account_id: str,
    max_points: int = Query(2000, ge=100, le=5000, description="净值曲线采样点数"),
) -> ApiResponse[dict]:
    """聚合返回：账户 + 净值曲线（策略 vs 基准 vs 回撤）+ 指标卡
    + 最新持仓 + 最近交易 + 跑批任务状态。

    首跑之前返回 `empty=true` 与引导文案，前端据此显示空态而不是报错。
    """
    data = svc.read_overview(account_id)
    if isinstance(data.get("nav_series"), list) and max_points < 2000:
        # 2000 已是服务端默认；这里支持更小的请求粒度（前端小屏场景）
        step = max(1, len(data["nav_series"]) // max_points)
        data["nav_series"] = data["nav_series"][::step]
    return ApiResponse.ok(data)


@router.post("/{account_id}/run", summary="手动触发一次跑批（立即入队，非同步执行）")
def run_now(account_id: str) -> ApiResponse[dict]:
    """校验账户存在后触发 `paper_daily`。结果通过任务日志与
    `last_run_date` 观察（系统页「调度任务」也能看到执行记录）。
    """
    svc.require_account(account_id)
    return ApiResponse.ok(svc.trigger_run_now())
