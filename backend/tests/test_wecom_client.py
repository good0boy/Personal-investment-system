"""模块 5 · 企微客户端的测试。

★ 本文件的核心是**验证限流真的生效**，而不是"对象创建成功"。
  限流写错了不会报错 —— 只是限流不生效（照样被企微限频）。
  `test_throttle.py` 已经踩过这个坑（"类写了没被调用 = 0 次"），这里照同一标准来。

★ HTTP 一律 monkeypatch 掉（`_client.post/get`），测试不碰网络。
  真实网络验证走 `test_alert_network.py` 的 `@pytest.mark.network` 用例。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services import wecom_client as mod  # noqa: E402
from app.services.wecom_client import (  # noqa: E402
    ERROR_CODES,
    WecomClient,
    explain_errcode,
    is_retryable_errcode,
)


# ======================================================================
# 夹具
# ======================================================================
class _Resp:
    """假的 httpx.Response。"""

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def json(self) -> dict:
        return self._payload


@pytest.fixture
def client() -> WecomClient:
    """独立客户端实例（**不用全局单例** —— 单例会带脏的 token 与限流窗口）。"""
    c = WecomClient()
    c.reset_rate_limiter()
    yield c
    c.close()


@pytest.fixture
def webhook_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """打开开关 + 配好 webhook 凭证。"""
    monkeypatch.setattr(mod.settings.alert, "enabled", True, raising=False)
    monkeypatch.setattr(
        mod.settings.env, "wecom_webhook_url",
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=ABCD1234-EF56-7890-ABCD-1234567890AB",
        raising=False,
    )
    monkeypatch.setattr(mod.settings.env, "wecom_channel", "webhook", raising=False)


@pytest.fixture
def app_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """打开开关 + 配好自建应用凭证。"""
    monkeypatch.setattr(mod.settings.alert, "enabled", True, raising=False)
    monkeypatch.setattr(mod.settings.env, "wecom_channel", "app", raising=False)
    monkeypatch.setattr(mod.settings.env, "wecom_corp_id", "ww1234567890", raising=False)
    monkeypatch.setattr(mod.settings.env, "wecom_agent_id", "1000002", raising=False)
    monkeypatch.setattr(mod.settings.env, "wecom_corp_secret", "secret-abc", raising=False)
    monkeypatch.setattr(mod.settings.env, "wecom_touser", "zhangsan", raising=False)


# ======================================================================
# 错误码表
# ======================================================================
def test_error_codes_cover_documented_ones():
    """文档里点名的错误码必须都有中文说明（不能暴露原始报错）。"""
    for code in (60020, 40014, 45009, 82001, 93000):
        assert code in ERROR_CODES, f"错误码 {code} 未在表中"
        text, _ = ERROR_CODES[code]
        assert text and text != "ok"
        # 提示必须指向"用户该做什么"，不能只是复述错误
        assert any(kw in text for kw in ("请", "检查", "添加", "到", "自动")), (
            f"错误码 {code} 的说明没有可执行指引：{text}"
        )


def test_retryable_classification():
    """限频/网络类可重试；配置错类不可重试。

    ★ 这条测试的意义：对 60020（IP 未白名单）重试 36 秒毫无意义，
      只会让人以为"系统在努力"，实际是白等。
    """
    assert is_retryable_errcode(45009) is True     # 限频
    assert is_retryable_errcode(40014) is True     # token 失效
    assert is_retryable_errcode(42001) is True     # token 超时
    assert is_retryable_errcode(60020) is False    # 可信 IP 未配置
    assert is_retryable_errcode(82001) is False    # 接收人无效
    assert is_retryable_errcode(93000) is False    # webhook key 无效
    assert is_retryable_errcode(999999) is False   # 未知码 → 不重试（保守）


def test_explain_errcode_unknown_has_code():
    text = explain_errcode(123456, "something weird")
    assert "123456" in text and "something weird" in text


# ======================================================================
# 发送：前置校验
# ======================================================================
def test_send_blocked_when_disabled(client: WecomClient, monkeypatch: pytest.MonkeyPatch):
    """开关关着时**一个请求都不发**（而不是发了被拒）。"""
    monkeypatch.setattr(mod.settings.alert, "enabled", False, raising=False)
    calls = []
    monkeypatch.setattr(client._client, "post", lambda *a, **k: calls.append(1))

    result = client.send_markdown("持仓提醒 · 测试")

    assert result.ok is False
    assert result.retryable is False
    assert calls == [], "开关关闭时不应发任何请求"
    assert "alert.enabled" in result.message


def test_send_blocked_when_not_configured(client: WecomClient, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(mod.settings.alert, "enabled", True, raising=False)
    monkeypatch.setattr(mod.settings.env, "wecom_channel", "webhook", raising=False)
    monkeypatch.setattr(mod.settings.env, "wecom_webhook_url", "", raising=False)

    calls = []
    monkeypatch.setattr(client._client, "post", lambda *a, **k: calls.append(1))

    result = client.send_markdown("持仓提醒 · 测试")
    assert result.ok is False
    assert calls == []
    assert "WECOM_WEBHOOK_URL" in result.message


def test_send_empty_content_rejected(client: WecomClient, webhook_on: None):
    result = client.send_markdown("   ")
    assert result.ok is False
    assert "为空" in result.message


# ======================================================================
# 发送：webhook 渠道
# ======================================================================
def test_webhook_success(client: WecomClient, webhook_on: None, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(client._client, "post", lambda *a, **k: _Resp({"errcode": 0, "errmsg": "ok"}))
    result = client.send_markdown("**持仓提醒**\n> 测试")
    assert result.ok is True
    assert result.errcode == 0
    assert result.channel == "webhook"


def test_webhook_error_is_translated(client: WecomClient, webhook_on: None,
                                    monkeypatch: pytest.MonkeyPatch):
    """93000 必须被翻译成可执行的中文提示，且**不可重试**。"""
    monkeypatch.setattr(
        client._client, "post",
        lambda *a, **k: _Resp({"errcode": 93000, "errmsg": "invalid webhook url"}),
    )
    result = client.send_markdown("**持仓提醒**\n> 测试")
    assert result.ok is False
    assert result.errcode == 93000
    assert result.retryable is False
    assert "Webhook" in result.message or "key" in result.message


def test_webhook_rate_limited_is_retryable(client: WecomClient, webhook_on: None,
                                           monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        client._client, "post",
        lambda *a, **k: _Resp({"errcode": 45009, "errmsg": "api freq out of limit"}),
    )
    result = client.send_markdown("**持仓提醒**\n> 测试")
    assert result.ok is False
    assert result.retryable is True


def test_transport_error_is_retryable(client: WecomClient, webhook_on: None,
                                      monkeypatch: pytest.MonkeyPatch):
    """网络抖动必须可重试 —— 它跟"配置错了"是两回事。"""

    def boom(*a, **k):
        raise ConnectionError("connection reset")

    monkeypatch.setattr(client._client, "post", boom)
    result = client.send_markdown("**持仓提醒**\n> 测试")
    assert result.ok is False
    assert result.retryable is True
    assert result.transport_error
    assert "ConnectionError" in result.transport_error


def test_webhook_payload_shape(client: WecomClient, webhook_on: None,
                               monkeypatch: pytest.MonkeyPatch):
    """请求体必须是企微要的 `{msgtype, markdown:{content}}` 形状。"""
    captured: dict = {}

    def fake_post(url, json=None, **kw):
        captured["url"] = url
        captured["json"] = json
        return _Resp({"errcode": 0})

    monkeypatch.setattr(client._client, "post", fake_post)
    client.send_markdown("**持仓提醒**\n> 正文")

    assert captured["json"]["msgtype"] == "markdown"
    assert captured["json"]["markdown"]["content"] == "**持仓提醒**\n> 正文"
    assert captured["url"].startswith("https://qyapi.weixin.qq.com/")


# ======================================================================
# 发送：自建应用渠道
# ======================================================================
def test_app_success_with_token(client: WecomClient, app_on: None,
                                monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        client._client, "get",
        lambda *a, **k: _Resp({"errcode": 0, "access_token": "TOK", "expires_in": 7200}),
    )
    monkeypatch.setattr(client._client, "post", lambda *a, **k: _Resp({"errcode": 0}))
    result = client.send_markdown("**持仓提醒**\n> 测试")
    assert result.ok is True
    assert result.channel == "app"


def test_app_reuses_cached_token(client: WecomClient, app_on: None,
                                 monkeypatch: pytest.MonkeyPatch):
    """token 必须被缓存 —— 每次都去 gettoken 会撞企微限频。"""
    token_calls = {"n": 0}

    def fake_get(*a, **k):
        token_calls["n"] += 1
        return _Resp({"errcode": 0, "access_token": f"TOK{token_calls['n']}", "expires_in": 7200})

    monkeypatch.setattr(client._client, "get", fake_get)
    monkeypatch.setattr(client._client, "post", lambda *a, **k: _Resp({"errcode": 0}))

    client.send_markdown("持仓提醒 1")
    client.send_markdown("持仓提醒 2")
    client.send_markdown("持仓提醒 3")
    assert token_calls["n"] == 1, f"token 应只取一次，实际 {token_calls['n']} 次"


def test_app_refreshes_token_on_40014(client: WecomClient, app_on: None,
                                      monkeypatch: pytest.MonkeyPatch):
    """收到 40014 → **当场**刷新 token 重试，不上报给退避循环白等 36 秒。"""
    token_calls = {"n": 0}
    sends = {"n": 0}

    def fake_get(*a, **k):
        token_calls["n"] += 1
        return _Resp({"errcode": 0, "access_token": f"TOK{token_calls['n']}", "expires_in": 7200})

    def fake_post(*a, **k):
        sends["n"] += 1
        if sends["n"] == 1:
            return _Resp({"errcode": 40014, "errmsg": "invalid access_token"})
        return _Resp({"errcode": 0})

    monkeypatch.setattr(client._client, "get", fake_get)
    monkeypatch.setattr(client._client, "post", fake_post)

    result = client.send_markdown("**持仓提醒**\n> 测试")
    assert result.ok is True, "40014 后应自动刷新 token 并重试成功"
    assert token_calls["n"] == 2
    assert sends["n"] == 2


def test_app_unrecoverable_40014_returns_not_retryable_after_second_fail(
    client: WecomClient, app_on: None, monkeypatch: pytest.MonkeyPatch
):
    """刷新后仍是 40014 → 返回失败（此时才交给上层判断，避免无限换 token）。"""
    monkeypatch.setattr(
        client._client, "get",
        lambda *a, **k: _Resp({"errcode": 0, "access_token": "TOK", "expires_in": 7200}),
    )
    monkeypatch.setattr(
        client._client, "post",
        lambda *a, **k: _Resp({"errcode": 40014, "errmsg": "invalid access_token"}),
    )
    result = client.send_markdown("**持仓提醒**\n> 测试")
    assert result.ok is False
    assert result.errcode == 40014


def test_app_gettoken_failure_raises(client: WecomClient, app_on: None,
                                    monkeypatch: pytest.MonkeyPatch):
    """gettoken 返回错误码 → 抛 AlertError（凭证配置错是硬故障，不能静默）。"""
    from app.core.errors import AlertError

    monkeypatch.setattr(
        client._client, "get",
        lambda *a, **k: _Resp({"errcode": 40001, "errmsg": "invalid corpsecret"}),
    )
    with pytest.raises(AlertError) as ei:
        client.send_markdown("**持仓提醒**\n> 测试")
    assert "corpsecret" in str(ei.value.message).lower() or "40001" in str(ei.value.message)


def test_app_payload_uses_touser(client: WecomClient, app_on: None,
                                 monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        client._client, "get",
        lambda *a, **k: _Resp({"errcode": 0, "access_token": "TOK", "expires_in": 7200}),
    )
    captured: dict = {}

    def fake_post(url, params=None, json=None, **kw):
        captured["params"] = params
        captured["json"] = json
        return _Resp({"errcode": 0})

    monkeypatch.setattr(client._client, "post", fake_post)
    client.send_markdown("**持仓提醒**\n> 测试")

    assert captured["json"]["touser"] == "zhangsan"
    assert captured["json"]["agentid"] == 1000002
    assert captured["params"]["access_token"] == "TOK"


# ======================================================================
# 🔴 限流：本文件最重要的测试
# ======================================================================
def test_rate_limit_actually_throttles(monkeypatch: pytest.MonkeyPatch):
    """连续超过上限条数时，实际速率必须被压住。

    ★ 判据是**耗时**，不是"没报错"。限流器写错（比如 acquire 没被调用）
      时这个测试必须失败 —— 否则它只是在测基础设施。
    """
    monkeypatch.setattr(mod.settings.alert, "enabled", True, raising=False)
    monkeypatch.setattr(mod.settings.env, "wecom_channel", "webhook", raising=False)
    monkeypatch.setattr(
        mod.settings.env, "wecom_webhook_url",
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x", raising=False,
    )
    # 上限 3 条 / 1 秒窗口（把 60s 缩短，让测试跑得快）
    monkeypatch.setattr(mod.settings.alert, "rate_limit_per_minute", 3, raising=False)

    c = WecomClient()
    c.close()
    c._rate_limiter = mod.SlidingWindowRateLimiter(
        min_interval=0.0, max_calls_per_minute=3, window=1.0, name="test"
    )
    monkeypatch.setattr(c._client, "post", lambda *a, **k: _Resp({"errcode": 0}))

    t0 = time.monotonic()
    for _ in range(4):          # 第 4 次必须等到窗口滑出
        c.send_markdown("持仓提醒")
    elapsed = time.monotonic() - t0

    assert elapsed >= 0.9, f"第 4 条应等待约 1s，实际仅 {elapsed:.3f}s（限流没生效）"
    stats = c.rate_stats()
    assert stats["total_calls"] == 4
    assert stats["max_wait_seconds"] > 0.5


def test_rate_limit_queues_not_drops(monkeypatch: pytest.MonkeyPatch):
    """超限必须**排队送达**，不是丢弃。

    ★ 这是主人明确的验收项（占位页写明：「突发 30 条消息时全部最终送达，无丢弃」）。
    """
    monkeypatch.setattr(mod.settings.alert, "enabled", True, raising=False)
    monkeypatch.setattr(mod.settings.env, "wecom_channel", "webhook", raising=False)
    monkeypatch.setattr(
        mod.settings.env, "wecom_webhook_url",
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x", raising=False,
    )
    monkeypatch.setattr(mod.settings.alert, "rate_limit_per_minute", 5, raising=False)

    c = WecomClient()
    c._rate_limiter = mod.SlidingWindowRateLimiter(
        min_interval=0.0, max_calls_per_minute=5, window=0.3, name="test"
    )
    sent: list[str] = []
    monkeypatch.setattr(
        c._client, "post",
        lambda url, json=None, **k: (sent.append(json["markdown"]["content"]), _Resp({"errcode": 0}))[1],
    )

    for i in range(8):
        r = c.send_markdown(f"持仓提醒 {i}")
        assert r.ok is True

    assert len(sent) == 8, f"8 条必须全部送达，实际 {len(sent)} 条（有丢弃）"
    c.close()
