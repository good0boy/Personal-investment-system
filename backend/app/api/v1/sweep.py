"""模块 3 · 参数寻优的接口（**全部只读**）。

与回测接口同一条纪律：前端请求不触发计算。寻优由
`python -m strategies.sweep --config sweeps/hold_tp.yaml` 在 CLI 跑，
结果落盘到 `data/sweep/{sweep_id}/`，这里只把文件读出来。

路由设计说明
------------
路径用 `/sweep/...` 而不是 `/backtest/sweep/...`：
寻优产物在 `data/sweep/` 而不是 `data/backtest/`，是**独立的一类产物**；
挂在各自前缀下，后端扫盘逻辑与前端菜单能一一对应。

⚠️ **`/list` 与 `/{sweep_id}` 的顺序**：`/list` 是固定路径，必须先注册，
   否则 `{sweep_id}` 会把 "list" 当成一个 sweep 名去命中。
"""
from __future__ import annotations

from fastapi import APIRouter

from app.schemas.common import ApiResponse
from app.services import sweep_service as svc

router = APIRouter(tags=["sweep"])


@router.get("/list", summary="参数寻优任务列表")
def list_sweeps() -> ApiResponse[list[dict]]:
    return ApiResponse.ok(svc.list_sweeps())


@router.get("/{sweep_id}", summary="寻优详情（网格 + 结果 + 敏感性 + 结论）")
def get_sweep(sweep_id: str) -> ApiResponse[dict]:
    return ApiResponse.ok(svc.get_sweep(sweep_id))
