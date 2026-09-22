"""企业微信推送客户端（阶段 5）—— 消息触达用户的**唯一出口**。

渠道
----
两条渠道，业务层只调 `send_markdown(text)`，不感知差异：

    ======== ========================================== ==========================
    渠道      配置项                                     说明
    ======== ========================================== ==========================
    webhook    WECOM_WEBHOOK_URL                          群机器人（方案 A ⭐）
    app        WECOM_CORP_ID / AGENT_ID / CORP_SECRET     自建应用（方案 B）
               + WECOM_TOUSER
    ======== ========================================== ==========================

为什么选方案 A：只需 1 个 URL，无审批、无 IP 白名单、无 access_token 管理。
方案 B 的价值是能精准指定接收人（`touser`），所以两条都实现、按配置切换。

★ 三条实现纪律
--------------
1. **限流是排队，不是丢弃**。群机器人上限 20 条/分钟，密集调仓时
   （`position_count=5` 全部换手 = 10 条）很容易撞上。直接复用数据源那边
   验证过的 `SlidingWindowRateLimiter`（双约束 + 线程安全 + 带 stats），
   超限时阻塞等待，绝不静默丢消息。

2. **错误码必须翻译成人话**。企微返回 `errcode=60020` 时如果原样抛给用户，
   主人只能去翻文档。这里建了错误码表，每个码都给「哪错了 + 怎么修」。

3. **发送结果必须结构化**（`SendResult`），不能只抛异常。
   `alert_service` 要靠 `retryable` 区分「值得重试」与「重试也没用」
   —— 对着 40014（token 失效）重试 3 次纯属浪费 36 秒；而限频就该等。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import settings
from app.core.errors import AlertError, NotConfiguredError
from app.core.logging import logger
from app.services.data_source.throttle import SlidingWindowRateLimiter

# ======================================================================
# 常量
# ======================================================================
_WEBHOOK_BASE = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"
_TOKEN_URL = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
_APP_SEND_URL = "https://qyapi.weixin.qq.com/cgi-bin/message/send"

#: 单次 HTTP 请求超时（秒）。企微很少超过 1s，给 10s 是余量。
_TIMEOUT = 10.0

#: access_token 有效期 7200s，提前 300s 刷新（架构 §9 风险 #9）
_TOKEN_TTL = 7200
_TOKEN_REFRESH_MARGIN = 300

# ======================================================================
# 错误码表 —— 每个码都要指向「用户该做什么」
# ======================================================================
#: code -> (中文说明, 是否值得重试)
#:
#: ★ 判「可重试」的标准：**同样的请求再发一次有可能成功**。
#:   限频（等一会儿就好）可重试；凭证错、IP 没加白名单、key 拼错
#:   重试一万次也一样 —— 那类必须立刻标 failed 并让 UI 提示人去修配置。
ERROR_CODES: dict[int, tuple[str, bool]] = {
    0: ("ok", False),
    # --- webhook ---
    93000: ("Webhook key 无效或已被移除。请到企业微信群 → 群机器人 → 复制新的 Webhook 地址", False),
    45009: ("接口调用超过限制（20 条/分钟）。稍后会自动重发", True),
    # --- 通用 ---
    40014: ("access_token 无效或已过期。请检查 WECOM_CORP_SECRET 是否正确", True),
    40001: ("access_token 参数错误或获取失败。请检查 WECOM_CORP_ID / WECOM_CORP_SECRET", True),
    42001: ("access_token 已超时。稍后会自动重发", True),
    41001: ("缺少 access_token 参数。请检查自建应用配置是否完整", False),
    # --- 自建应用 ---
    60011: ("无权限操作指定的接收人（touser）。请确认成员在企业微信通讯录中且应用可见范围包含他", False),
    60020: ("当前 IP 不在应用的可信 IP 列表内。请到企业微信后台 → 应用 → 企业可信 IP 添加本机公网 IP", False),
    82001: ("接收人无效：请检查 WECOM_TOUSER 是否为企业微信通讯录里的成员账号（不是手机号/昵称）", False),
    40056: ("不合法的 agentid。请检查 WECOM_AGENT_ID", False),
    301002: ("无权限操作指定的接收人（应用可见范围不含该成员）", False),
    48002: ("API 接口无权限（可能是应用未发布或权限未授予）", False),
    45047: ("服务号接口调用超过限制", True),
}


def explain_errcode(errcode: int, errmsg: str = "") -> str:
    """把企微错误码翻译成中文可执行提示。"""
    known = ERROR_CODES.get(errcode)
    if known:
        text = known[0]
        if errmsg and errmsg.lower() not in {"ok", ""}:
            text = f"{text}（原始信息：{errmsg}）"
        return text
    return f"企业微信返回未知错误 errcode={errcode}：{errmsg or '无详细信息'}"


def is_retryable_errcode(errcode: int) -> bool:
    """该错误码重发一次有可能成功吗。"""
    return ERROR_CODES.get(errcode, ("", False))[1]


# ======================================================================
# 发送结果
# ======================================================================
@dataclass
class SendResult:
    """一次发送尝试的结果。

    ★ 刻意**不用异常表示失败**：`alert_service` 要在一次函数里区分
      三种结局（成功 / 可重试 / 永久失败），异常控制流表达起来很别扭，
      而且 try/except 里还想带上下文就得自定义异常类。
    """

    ok: bool
    errcode: int = 0
    message: str = "ok"
    channel: str = ""
    retryable: bool = False
    #: 实际等待限流的秒数（排查"为什么慢了"用）
    waited: float = 0.0
    #: HTTP 层异常（网络不通等），此时没有 errcode
    transport_error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errcode": self.errcode,
            "message": self.message,
            "channel": self.channel,
            "retryable": self.retryable,
            "waited": round(self.waited, 3),
            "transport_error": self.transport_error,
        }


def _fail(errcode: int, channel: str, errmsg: str, *, waited: float = 0.0) -> SendResult:
    return SendResult(
        ok=False,
        errcode=errcode,
        message=explain_errcode(errcode, errmsg),
        channel=channel,
        retryable=is_retryable_errcode(errcode),
        waited=waited,
    )


def _transport_fail(exc: Exception, channel: str, *, waited: float = 0.0) -> SendResult:
    """网络层失败 —— 一律可重试（网络抖动是暂时的）。"""
    return SendResult(
        ok=False,
        errcode=-1,
        message=f"网络请求失败：{type(exc).__name__}: {exc}",
        channel=channel,
        retryable=True,
        waited=waited,
        transport_error=f"{type(exc).__name__}: {exc}",
    )


# ======================================================================
# 客户端
# ======================================================================
class WecomClient:
    """企微推送客户端（双渠道）。

    ★ 线程安全：`send_markdown` 会被 APScheduler 线程与 FastAPI 线程池
      同时调用；限流器内部有锁，token 缓存另有 `_token_lock`。
    """

    def __init__(self) -> None:
        self._client = httpx.Client(timeout=_TIMEOUT, trust_env=False)
        self._rate_limiter: SlidingWindowRateLimiter | None = None
        self._rate_lock = threading.Lock()

        # access_token 缓存（方案 B 才用）
        self._token: str = ""
        self._token_expire_at: float = 0.0
        self._token_lock = threading.Lock()

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------
    @property
    def channel(self) -> str:
        return settings.env.wecom_channel

    def is_configured(self) -> bool:
        """当前渠道的凭证齐不齐。"""
        if not settings.alert.enabled:
            return False
        if self.channel == "app":
            env = settings.env
            return bool(env.wecom_corp_id and env.wecom_agent_id and env.wecom_corp_secret)
        return settings.env.wecom_webhook_configured

    def describe(self) -> dict[str, Any]:
        """渠道自述（供系统页 / 提醒页展示，**不含任何密钥原文**）。"""
        env = settings.env
        return {
            "channel": self.channel,
            "enabled": settings.alert.enabled,
            "configured": self.is_configured(),
            "rate_limit_per_minute": settings.alert.rate_limit_per_minute,
            "webhook_configured": env.wecom_webhook_configured,
            "webhook_key_masked": env.wecom_webhook_key_masked,
            "app_configured": bool(
                env.wecom_corp_id and env.wecom_agent_id and env.wecom_corp_secret
            ),
            "touser": env.wecom_touser,
        }

    # ------------------------------------------------------------------
    # 限流
    # ------------------------------------------------------------------
    def _limiter(self) -> SlidingWindowRateLimiter:
        """惰性建限流器（单例）。

        `max_calls_per_minute` 从 config.yaml 读 —— 但**不设 min_interval**：
        企微群机器人文档只给了「20 条/分钟」，没有最小间隔要求，
        硬塞一个 min_interval 反而会把「一条开仓 + 一条清仓」这种
        正常连发拖慢。
        """
        if self._rate_limiter is None:
            with self._rate_lock:
                if self._rate_limiter is None:
                    self._rate_limiter = SlidingWindowRateLimiter(
                        min_interval=0.0,
                        max_calls_per_minute=max(1, int(settings.alert.rate_limit_per_minute)),
                        name="wecom",
                    )
        return self._rate_limiter

    def reset_rate_limiter(self) -> None:
        """清空限流窗口（测试用；生产没场景需要）。"""
        self._limiter().reset()

    def rate_stats(self) -> dict[str, Any]:
        return self._limiter().stats()

    # ------------------------------------------------------------------
    # access_token（方案 B）
    # ------------------------------------------------------------------
    def _access_token(self, *, force_refresh: bool = False) -> str:
        """取 access_token（缓存 7200s，提前 300s 刷新）。

        ★ 双检锁：并发首次取 token 时只发一次请求。企微对 `gettoken`
          本身也有限频，多线程一起刷会被限。
        """
        now = time.monotonic()
        if not force_refresh and self._token and now < self._token_expire_at:
            return self._token

        with self._token_lock:
            now = time.monotonic()
            if not force_refresh and self._token and now < self._token_expire_at:
                return self._token

            env = settings.env
            if not (env.wecom_corp_id and env.wecom_corp_secret):
                raise NotConfiguredError(
                    "企业微信自建应用未配置：请在 .env 填写 "
                    "WECOM_CORP_ID / WECOM_CORP_SECRET"
                )
            try:
                resp = self._client.get(
                    _TOKEN_URL,
                    params={
                        "corpid": env.wecom_corp_id,
                        "corpsecret": env.wecom_corp_secret,
                    },
                )
                payload: dict[str, Any] = resp.json()
            except Exception as exc:
                raise AlertError(f"获取企业微信 access_token 失败：{exc}") from exc

            if int(payload.get("errcode", -1)) != 0:
                code = int(payload.get("errcode", -1))
                raise AlertError(explain_errcode(code, str(payload.get("errmsg", ""))))

            self._token = str(payload.get("access_token") or "")
            expires_in = int(payload.get("expires_in") or _TOKEN_TTL)
            self._token_expire_at = time.monotonic() + max(
                60, expires_in - _TOKEN_REFRESH_MARGIN
            )
            logger.info(f"[wecom] access_token 已刷新，有效期 {expires_in}s")
            return self._token

    def invalidate_token(self) -> None:
        """把缓存的 token 标记为失效（收到 40014/42001 时调用）。"""
        with self._token_lock:
            self._token = ""
            self._token_expire_at = 0.0

    # ------------------------------------------------------------------
    # 发送（唯一出口）
    # ------------------------------------------------------------------
    def send_markdown(self, content: str, *, mention_all: bool = False) -> SendResult:
        """发一条 markdown 消息。**只尝试一次**，不重试。

        重试由 `alert_service` 负责 —— 它才知道退避节奏、要不要落库、
        重试次数上限是多少。客户端保持"一次调用 = 一次尝试"的单一职责。

        Args:
            content: markdown 正文。
            mention_all: webhook 渠道下是否 @所有人（暂未使用，留作接口）。
        """
        if not settings.alert.enabled:
            return SendResult(
                ok=False,
                errcode=-2,
                message="提醒功能未开启：请把 config.yaml 的 alert.enabled 改为 true",
                channel=self.channel,
                retryable=False,
            )
        if not content.strip():
            return SendResult(
                ok=False,
                errcode=-3,
                message="消息内容为空，已跳过发送",
                channel=self.channel,
                retryable=False,
            )
        if not self.is_configured():
            return SendResult(
                ok=False,
                errcode=-4,
                message=self._not_configured_hint(),
                channel=self.channel,
                retryable=False,
            )

        # ★ 限流排队（阻塞到可以发为止）。统计口径：只统计真实发出的请求。
        waited = self._limiter().acquire()

        if self.channel == "app":
            return self._send_via_app(content, mention_all=mention_all, waited=waited)
        return self._send_via_webhook(content, waited=waited)

    def _not_configured_hint(self) -> str:
        if self.channel == "app":
            return (
                "自建应用渠道未配置完整：请在 .env 填写 "
                "WECOM_CORP_ID / WECOM_AGENT_ID / WECOM_CORP_SECRET"
            )
        return (
            "群机器人 Webhook 未配置：请在 .env 填写 WECOM_WEBHOOK_URL"
            "（企业微信群 → 群机器人 → 添加 → 复制 Webhook 地址）"
        )

    # --- 方案 A：群机器人 ---
    def _send_via_webhook(self, content: str, *, waited: float) -> SendResult:
        url = settings.env.wecom_webhook_url
        try:
            resp = self._client.post(
                url, json={"msgtype": "markdown", "markdown": {"content": content}}
            )
            payload: dict[str, Any] = resp.json()
        except Exception as exc:
            logger.warning(f"[wecom] webhook 发送网络异常：{exc!r}")
            return _transport_fail(exc, "webhook", waited=waited)

        code = int(payload.get("errcode", -1))
        if code == 0:
            logger.info(f"[wecom] webhook 已发送（等待 {waited:.2f}s）")
            return SendResult(ok=True, errcode=0, channel="webhook", waited=waited)

        errmsg = str(payload.get("errmsg", ""))
        logger.warning(f"[wecom] webhook 发送失败 errcode={code} errmsg={errmsg}")
        return _fail(code, "webhook", errmsg, waited=waited)

    # --- 方案 B：自建应用 ---
    def _send_via_app(self, content: str, *, mention_all: bool, waited: float) -> SendResult:
        env = settings.env
        touser = env.wecom_touser.strip() or "@all"
        body = {
            "touser": touser,
            "msgtype": "markdown",
            "agentid": int(env.wecom_agent_id or 0),
            "markdown": {"content": content},
        }
        # 自建应用侧没有等价于"@所有人"的语义（touser=@all 已表达），
        # 参数保留只为两条渠道签名一致。
        del mention_all

        for attempt in (1, 2):
            try:
                token = self._access_token(force_refresh=(attempt == 2))
                resp = self._client.post(_APP_SEND_URL, params={"access_token": token}, json=body)
                payload: dict[str, Any] = resp.json()
            except NotConfiguredError:
                raise
            except AlertError:
                raise
            except Exception as exc:
                logger.warning(f"[wecom] app 发送网络异常：{exc!r}")
                return _transport_fail(exc, "app", waited=waited)

            code = int(payload.get("errcode", -1))
            if code == 0:
                logger.info(f"[wecom] app 已发送给 {touser}（等待 {waited:.2f}s）")
                return SendResult(ok=True, errcode=0, channel="app", waited=waited)

            # 40014 / 42001 = token 失效。这种情况**当场换 token 重试一次**，
            # 不上报给 alert_service 的退避循环 —— 那会白等 36 秒。
            if code in (40014, 42001) and attempt == 1:
                logger.info(f"[wecom] access_token 失效（{code}），立即刷新后重试")
                self.invalidate_token()
                continue

            errmsg = str(payload.get("errmsg", ""))
            logger.warning(f"[wecom] app 发送失败 errcode={code} errmsg={errmsg}")
            return _fail(code, "app", errmsg, waited=waited)

        return _fail(40014, "app", "access_token 刷新后仍失败", waited=waited)

    # ------------------------------------------------------------------
    # 端口
    # ------------------------------------------------------------------
    def close(self) -> None:
        self._client.close()


#: 全局单例（与 `data_source` 客户端同款做法）
wecom_client = WecomClient()


__all__ = [
    "ERROR_CODES",
    "SendResult",
    "WecomClient",
    "explain_errcode",
    "is_retryable_errcode",
    "wecom_client",
]
