"""模块 5 · 用户提醒的接口（阶段 5）。

契约（见 `docs/技术架构-详细设计.md` §7.1）：

    GET  /api/v1/alerts/logs          提醒日志（分页 + 按状态/类型筛选）
    POST /api/v1/alerts/test          发一条链路测试消息
    POST /api/v1/alerts/{id}/resend   重发失败的提醒

另有三个只读辅助接口（不在原契约里，但前端页面需要）：
    GET  /api/v1/alerts/status        渠道状态（当前渠道、凭证、限流、模板数）
    GET  /api/v1/alerts/templates     模板预览（含脱敏样例渲染）
    GET  /api/v1/alerts/meta          枚举字典（action/level 的中文名）

★ 为什么 `/test` 是**同步**的（与模块 4 的 `/run` 相反）
------------------------------------------------------
模块 4 的跑批要 30 分钟，只能异步。而发一条消息正常 200ms，
主人的验收标准就是「点一下，消息到手机」——
返回"已入队"等于没验证。所以这里同步等首试结果，
只有失败进退避重试时才转后台（那时接口已经返回明确结论）。
"""
from __future__ import annotations

from fastapi import APIRouter, Query

from app.schemas.common import ApiResponse
from app.services import alert_service as svc
from app.services.wecom_client import wecom_client

router = APIRouter(tags=["alerts"])


@router.get("/logs", summary="提醒日志（分页 + 筛选）")
def list_logs(
    limit: int = Query(50, ge=1, le=500, description="每页条数"),
    offset: int = Query(0, ge=0, description="偏移量"),
    status: str | None = Query(
        None, description="按状态筛选：pending | sent | failed | skipped"
    ),
    action: str | None = Query(
        None,
        description="按类型筛选：open_position | close_position | daily_summary | test | job_failed",
    ),
) -> ApiResponse[dict]:
    """返回 `{total, items, limit, offset, status_counts}`。

    `status_counts` 是各状态的总数（不受当前筛选影响），
    前端把它显示在筛选标签上，省一次请求。
    """
    return ApiResponse.ok(svc.list_logs(limit=limit, offset=offset, status=status, action=action))


@router.post("/test", summary="发送链路测试消息（同步返回结果）")
def send_test(operator: str = Query("手动测试", description="触发来源，仅记录用")) -> ApiResponse[dict]:
    """发一条测试消息，**同步**返回发送结果。

    返回体里 `ok` 为 true 表示企微已受理（`errcode=0`）。
    ⚠️ 企微返回 `errcode=0` **不等于**手机一定收到 —— 若群里开了
    「自定义关键词」而消息不含关键词，会被静默丢弃。本项目所有消息都带
    `持仓提醒` 关键词，模板里已强制保证（见 `alert_templates.yaml`）。
    """
    result = svc.send_test(operator=operator)
    if not result.get("ok"):
        # 用统一响应体，但保持 HTTP 200 + code!=0，让前端直接展示 message
        return ApiResponse.fail(
            int(result.get("errcode") or 4200) or 4200,
            str(result.get("message") or result.get("reason") or "测试消息发送失败"),
            result,
        )
    return ApiResponse.ok(result, message="测试消息已发送")


@router.post("/{alert_id}/resend", summary="重发失败的提醒")
def resend(alert_id: int) -> ApiResponse[dict]:
    """重发一条 failed 记录。

    已 `sent` 的直接返回成功（幂等，不重复发）；`pending` 的提示稍后再试。
    """
    result = svc.resend(alert_id)
    if not result.get("ok"):
        return ApiResponse.fail(
            int(result.get("errcode") or 4200) or 4200,
            str(result.get("message") or "重发失败"),
            result,
        )
    return ApiResponse.ok(result, message="重发成功")


@router.get("/status", summary="提醒渠道状态（当前渠道 / 凭证 / 限流 / 模板）")
def channel_status() -> ApiResponse[dict]:
    """渠道自述 —— 前端状态卡。

    ★ 凭证一律脱敏（只回 masked），**绝不返回原文**。
    """
    return ApiResponse.ok(svc.channel_status())


@router.get("/templates", summary="消息模板预览（含样例渲染）")
def templates() -> ApiResponse[list[dict]]:
    """返回每个模板的原始定义 + 一份样例渲染。

    样例渲染的价值：主人改模板时能在页面上直接看效果，
    不必"改一次往群里发一次"。样例数据是固定的脱敏假数据。
    """
    return ApiResponse.ok(svc.preview_templates())


@router.get("/meta", summary="枚举字典（动作 / 状态 / 级别的中文名）")
def meta() -> ApiResponse[dict]:
    """前端筛选下拉用。

    ★ 中文名**由后端给**（`alert_service.ACTION_CN`），前端不做 if-else 映射
      —— 那是本项目反复踩的"同一口径两处定义"。
    """
    return ApiResponse.ok(
        {
            "actions": [
                {"value": a, "label": svc.ACTION_CN.get(a, a)} for a in svc.ACTIONS
            ],
            "statuses": [
                {"value": "pending", "label": "发送中"},
                {"value": "sent", "label": "已发送"},
                {"value": "failed", "label": "发送失败"},
                {"value": "skipped", "label": "已跳过"},
            ],
            "levels": [
                {"value": "info", "label": "提示"},
                {"value": "warning", "label": "警告"},
                {"value": "error", "label": "错误"},
            ],
            "exit_reasons": [
                {"value": k, "label": v} for k, v in svc.EXIT_REASON_CN.items()
            ],
            "channel": wecom_client.channel,
        }
    )
