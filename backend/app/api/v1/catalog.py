"""模块 1 · 数据分类的接口：目录浏览 + 在线试跑 + 自检 + 本地数据清单。

设计要点
--------
- 目录定义来自 `config/catalog/*.yaml`（唯一来源）；自检状态来自 meta.db。
- `/try` 是全系统唯一"任意参数 + 真实网络"的入口，安全闸门在
  `catalog_service.try_run`（白名单 / 类型与格式校验 / 限流 / 截断 / 缓存）。
- 试跑可能较慢（真实网络 + akshare 1 秒限流），前端要显示 loading。
- `/local/*` 走的是另一条路：**不联网**，只读本地已落盘的数据
  （真相在磁盘，见 `local_data_service` 的模块说明）。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from app.core.logging import logger
from app.schemas.common import ApiResponse
from app.services import catalog_service, local_data_service

router = APIRouter(tags=["catalog"])


class TryRequest(BaseModel):
    """试跑请求体：参数名 → 参数值（均为字符串，由服务端按声明转换）。"""

    params: dict[str, Any] = Field(default_factory=dict)
    force: bool = False


# ======================================================================
# 目录
# ======================================================================
@router.get("/tree", summary="接口分类树（三大类 → 子分类）")
def catalog_tree() -> ApiResponse[dict[str, Any]]:
    return ApiResponse.ok(catalog_service.tree())


@router.get("/items", summary="全部接口定义（含自检状态）")
def catalog_items(
    category: str | None = Query(None, description="按大类过滤"),
) -> ApiResponse[dict[str, Any]]:
    items = catalog_service.load_items()
    merged = catalog_service.merge_status(items)
    if category:
        merged = [m for m in merged if m["category"] == category]
    summary = catalog_service.check_summary()
    return ApiResponse.ok(
        {
            "items": merged,
            "total": len(merged),
            "categories": [
                {"key": k, "label": v["label"], "icon": v["icon"]}
                for k, v in catalog_service.CATEGORIES.items()
            ],
            "check": summary,
        }
    )


@router.get("/check/status", summary="自检状态汇总")
def catalog_check_status() -> ApiResponse[dict[str, Any]]:
    return ApiResponse.ok(catalog_service.check_summary())


# ======================================================================
# 本地数据（磁盘上已经落盘的数据清单）
#
# ⚠️ 路由顺序有讲究：这几条都在 `/{item_id}` **之前**注册。
#    `/local/datasets` 是两个路径段、`/{item_id}` 是一个，本身不会撞；
#    但万一以后有人把 /local 收成单段，先注册的仍会优先匹配 —— 不至于
#    静默把 `/local` 当成某个接口 ID 去查。
# ======================================================================
@router.get("/local/datasets", summary="本地已落盘的数据维度清单（规模 + 示例取法）")
def catalog_local_datasets(
    refresh: bool = Query(False, description="强制重扫（默认走 10 分钟缓存；重扫约 12 秒）"),
) -> ApiResponse[dict[str, Any]]:
    """本地数据清单：以**磁盘**为唯一真相。

    首次约 12 秒 —— 要读约 2.5 万个 parquet 的文件尾才能拿到行数与覆盖区间，
    之后走进程内缓存。`refresh=true` 用于"刚跑完回补，想确认落盘没有"。
    """
    return ApiResponse.ok(local_data_service.datasets(refresh=refresh))


@router.get("/local/datasets/{key}/sample", summary="某个数据维度的示例数据（真读真表）")
def catalog_local_sample(
    key: str,
    limit: int = Query(20, ge=1, le=100, description="取多少行（默认 20）"),
) -> ApiResponse[dict[str, Any]]:
    """取某个维度最近 N 行的真实数据。

    行数、列数都可能被裁（宽表 151 列铺出来没法看），裁剪情况在 `note` 里写明。
    """
    return ApiResponse.ok(local_data_service.sample(key, limit))


@router.get("/{item_id}", summary="单个接口完整定义")
def catalog_item(item_id: str) -> ApiResponse[dict[str, Any]]:
    item = catalog_service.get_item(item_id)
    merged = catalog_service.merge_status([item])[0]
    return ApiResponse.ok(merged)


# ======================================================================
# 试跑
# ======================================================================
@router.post("/{item_id}/try", summary="在线试跑（真实调用数据源）")
def catalog_try(
    item_id: str,
    body: TryRequest,
    request: Request,
) -> ApiResponse[dict[str, Any]]:
    """真实执行接口。

    ⚠️ 慢操作：真实网络 + akshare 全局 1 秒限流。首次点击通常 1~5 秒，
    相同参数 5 分钟内重复点击走缓存（<200ms，响应里 cached=true）。

    ⚠️ 错误语义：参数/限频/数据源错误由全局异常处理器返回对应 HTTP 状态码
    （400/429/502），不要在这里 catch —— 吞掉状态码会让前端无法区分
    "校验失败"与"服务错误"。
    """
    client_host = request.client.host if request.client else ""
    result = catalog_service.try_run(
        item_id,
        body.params,
        client_host=client_host,
        force=body.force,
    )
    return ApiResponse.ok(result, message="试跑成功" if not result.get("cached") else "命中缓存")


# ======================================================================
# 自检
# ======================================================================
@router.post("/check", summary="手动触发一次自检")
def catalog_check(
    live_sample: int = Query(0, ge=0, le=20, description="抽样真实调用的接口数（0=只做静态检查）"),
) -> ApiResponse[dict[str, Any]]:
    """静态检查全部接口 + 抽样真实调用。

    注意：真实调用受 akshare **全局 1 秒/次**限流约束，live_sample=20 约需 20~60 秒。
    抽样走 `skip_rate_limit=True`，**不受**试跑接口"同 IP 10 次/分钟"的防滥用限流
    （该限流面向浏览器端，自检是后端自己发起，两者受众不同）。
    实现位于 `tools/check_catalog.py`（CLI 与本接口共用同一份逻辑）。
    """
    from tools.check_catalog import run_check  # noqa: PLC0415

    return ApiResponse.ok(run_check(live_sample=live_sample))


@router.post("/check/broken/{item_id}", summary="标记接口为 broken（演示自检标红）")
def catalog_mark_broken(item_id: str) -> ApiResponse[dict[str, Any]]:
    """把一个接口标成 broken，用于演示/验证前端标红链路。

    正常流程中 status 由 check_catalog.py 写入；提供这个接口是为了
    不改文件就能验证 UI 的"标红"展示。
    """
    from app.repos import meta_repo  # noqa: PLC0415

    item = catalog_service.get_item(item_id)
    meta_repo.catalog_status_upsert(
        item.id, item.category, item.func_name, item.datasource,
        "broken",
        status_message="手动标记（演示自检标红链路）",
        source_version="manual",
    )
    logger.info(f"[catalog] 接口 {item_id} 被手动标记为 broken")
    return ApiResponse.ok({"id": item_id, "status": "broken"}, message="已标记为 broken")
