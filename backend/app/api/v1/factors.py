"""模块 2 · 因子库的接口（**全部只读**）。

与模块 1/3 同一条纪律：前端请求不触发计算。
因子由 `python -m factors.calc` 算好落盘、有效性分析由
`python -m factors.analyze` 离线跑，这里只把结果读出来。

路由设计说明
------------
⚠️ **`/check` 与 `/symbols` 必须注册在 `/{name}` 之前** ——
   否则 FastAPI 会把 "check" / "symbols" 当作因子名去匹配，
   返回 404「未注册的因子：'check'」。这类"路径被通配吃掉"的 bug
   在 `/sweep/list` 上已经踩过一次（见 app/api/v1/sweep.py 的说明），
   所以这里把固定路径全部前置，并在注释里钉死。
"""
from __future__ import annotations

from fastapi import APIRouter, Query

from app.schemas.common import ApiResponse
from app.services import factor_service as svc

router = APIRouter(tags=["factors"])


# ======================================================================
# 固定路径（必须在前）
# ======================================================================
@router.get("", summary="因子注册表列表（定义 + 计算状态）")
def list_factors(
    category: str | None = Query(None, description="按分类过滤：daily | minute | fundamental"),
) -> ApiResponse[dict]:
    """返回**全部三类分类**（即使用户只看其中一类）。

    minute / fundamental 目前没有因子，但它们的 `status` / `note` 会照常返回 ——
    前端据此显示「待开发 / 数据源未接入」，而不是让人以为系统漏了两类。
    """
    return ApiResponse.ok(svc.list_factors(category=category))


@router.get("/check", summary="因子自检（导入 + 签名 + 冒烟调用）")
def check_factors(
    smoke: bool = Query(True, description="是否跑合成数据的冒烟调用"),
) -> ApiResponse[dict]:
    """与模块 1 的接口自检同构：让"YAML 改错了""实现签名对不上"在页面标红。

    这是唯一允许在请求里执行因子实现代码的入口 —— 跑的是 40 行合成数据，
    毫秒级，且只在显式调用时发生。
    """
    return ApiResponse.ok(svc.check(with_smoke=smoke))


@router.get("/symbols", summary="本地已有日线数据的标的（供选择器用）")
def search_symbols(
    q: str | None = Query(None, description="代码子串，如 600519 或 sh.6"),
    limit: int = Query(50, ge=1, le=500),
) -> ApiResponse[list[str]]:
    """按文件名列举，**不扫 Parquet** —— 4792 个文件的目录列举是毫秒级，
    而 `SELECT DISTINCT symbol` 要扫 700 万行。"""
    return ApiResponse.ok(svc.search_symbols(q, limit=limit))


# ======================================================================
# 详情与三类视图（通配路径放最后）
# ======================================================================
@router.get("/{name}", summary="因子详情（定义 + 状态 + 分析摘要）")
def get_factor(name: str) -> ApiResponse[dict]:
    return ApiResponse.ok(svc.get_factor(name))


@router.get("/{name}/series", summary="因子时序（截面聚合 或 多标的对比）")
def get_series(
    name: str,
    symbols: str | None = Query(
        None, description="逗号分隔的标的；**不填则返回全市场每日截面聚合**（均值/中位数/分位）"
    ),
    start: str | None = Query(None, description="起始日 YYYY-MM-DD"),
    end: str | None = Query(None, description="结束日 YYYY-MM-DD"),
    max_points: int = Query(svc.DEFAULT_MAX_POINTS, ge=50, le=20000),
) -> ApiResponse[dict]:
    syms = [s.strip() for s in (symbols or "").split(",") if s.strip()] or None
    return ApiResponse.ok(
        svc.get_series(name, symbols=syms, start=start, end=end, max_points=max_points)
    )


@router.get("/{name}/cross-section", summary="某交易日的截面分布（直方图 + 分位）")
def get_cross_section(
    name: str,
    day: str | None = Query(None, description="交易日 YYYY-MM-DD；默认取产物最后一天"),
    bins: int = Query(svc.DEFAULT_BINS, ge=5, le=200),
    top_n: int = Query(10, ge=1, le=50),
) -> ApiResponse[dict]:
    return ApiResponse.ok(svc.get_cross_section(name, day=day, bins=bins, top_n=top_n))


@router.get("/{name}/layered", summary="分层回测（分组累计净值 + 换手 + 成本）")
def get_layered(name: str) -> ApiResponse[dict]:
    """按因子值分 N 组，各组等权持有 —— **判断因子有没有 alpha 的核心图**。

    产物由 `python -m factors.analyze` 离线生成；没跑过会返回 404 并附上该命令。
    """
    return ApiResponse.ok(svc.get_layered(name))


@router.get("/{name}/ic", summary="RankIC 序列与摘要（IC 均值 / ICIR / t 值 / 胜率）")
def get_ic(
    name: str,
    max_points: int = Query(svc.DEFAULT_MAX_POINTS, ge=50, le=20000),
) -> ApiResponse[dict]:
    return ApiResponse.ok(svc.get_ic(name, max_points=max_points))
