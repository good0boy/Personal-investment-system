"""v1 路由聚合。

各模块的接口在各自阶段追加。
"""
from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import alerts, backtest, catalog, factors, paper, sweep, system

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(system.router)
api_router.include_router(catalog.router, prefix="/catalog", tags=["catalog"])        # 阶段 1
api_router.include_router(backtest.router, prefix="/backtest", tags=["backtest"])    # 阶段 2
api_router.include_router(sweep.router, prefix="/sweep", tags=["sweep"])             # 阶段 2
api_router.include_router(factors.router, prefix="/factors", tags=["factors"])       # 阶段 3
api_router.include_router(paper.router, prefix="/paper", tags=["paper"])             # 阶段 4
api_router.include_router(alerts.router, prefix="/alerts", tags=["alerts"])          # 阶段 5
