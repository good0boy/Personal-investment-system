"""模块 5 · 提醒接口的测试。

★ 走 FastAPI TestClient（真实路由 + 真实异常处理器），
  但不碰网络（发送被 monkeypatch）与真实 data/（全新 tmp meta.db）。

★ 一个刻意的设计：`/test` 同步返回结果。这里要验证**失败时也返回
  明确的中文原因**（而不是 500），因为前端的体验就建立在这上面。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.repos import db as db_module  # noqa: E402
from app.services import alert_service as svc  # noqa: E402
from app.services import wecom_client as wc_mod  # noqa: E402
from app.services.wecom_client import SendResult  # noqa: E402


# ======================================================================
# 夹具
# ======================================================================
@pytest.fixture
def api_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """全新 tmp meta.db + 打开开关 + 配好 webhook + TestClient。"""
    db_module.close_connection()
    monkeypatch.setattr(db_module, "META_DB", tmp_path / "meta" / "meta.db")
    conn = db_module.get_connection()
    conn.executescript((BACKEND_ROOT / "sql" / "schema.sql").read_text(encoding="utf-8"))

    monkeypatch.setattr(svc.settings.alert, "enabled", True, raising=False)
    monkeypatch.setattr(svc.settings.env, "wecom_channel", "webhook", raising=False)
    monkeypatch.setattr(
        svc.settings.env, "wecom_webhook_url",
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=ABCD1234EFGH5678",
        raising=False,
    )
    monkeypatch.setattr(svc.settings.alert, "retry_backoff", [0, 0, 0], raising=False)
    svc.clear_name_cache()

    from app.main import app

    with TestClient(app) as client:
        yield client
    db_module.close_connection()


@pytest.fixture
def ok_sender(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    sent: list[str] = []

    def fake(content: str, *, mention_all: bool = False) -> SendResult:
        sent.append(content)
        return SendResult(ok=True, errcode=0, channel="webhook")

    monkeypatch.setattr(wc_mod.wecom_client, "send_markdown", fake)
    return sent


# ======================================================================
# 路由注册
# ======================================================================
def test_routes_registered(api_env: TestClient):
    """六个接口都在 OpenAPI 里（防"写了没被 include"）。"""
    paths = api_env.get("/openapi.json").json()["paths"]
    expected = {
        "/api/v1/alerts/logs",
        "/api/v1/alerts/test",
        "/api/v1/alerts/{alert_id}/resend",
        "/api/v1/alerts/status",
        "/api/v1/alerts/templates",
        "/api/v1/alerts/meta",
    }
    assert expected.issubset(set(paths)), f"缺接口：{expected - set(paths)}"


# ======================================================================
# GET /logs
# ======================================================================
def test_logs_empty(api_env: TestClient):
    r = api_env.get("/api/v1/alerts/logs")
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 0
    assert body["data"]["total"] == 0
    assert body["data"]["items"] == []
    assert body["data"]["status_counts"]["sent"] == 0


def test_logs_after_send(api_env: TestClient, ok_sender: list[str]):
    api_env.post("/api/v1/alerts/test")
    data = api_env.get("/api/v1/alerts/logs").json()["data"]
    assert data["total"] == 1
    item = data["items"][0]
    assert item["action"] == "test"
    assert item["action_cn"] == "链路测试"
    assert item["status"] == "sent"
    assert item["level"] == "info"
    assert "持仓提醒" in item["title"]


def test_logs_status_filter(api_env: TestClient, ok_sender: list[str]):
    api_env.post("/api/v1/alerts/test")
    assert api_env.get("/api/v1/alerts/logs?status=sent").json()["data"]["total"] == 1
    assert api_env.get("/api/v1/alerts/logs?status=failed").json()["data"]["total"] == 0


def test_logs_action_filter(api_env: TestClient, ok_sender: list[str]):
    api_env.post("/api/v1/alerts/test")
    assert api_env.get("/api/v1/alerts/logs?action=test").json()["data"]["total"] == 1
    assert api_env.get("/api/v1/alerts/logs?action=open_position").json()["data"]["total"] == 0


def test_logs_pagination_bounds(api_env: TestClient):
    """limit 有上下界（防有人拉 100 万条把服务拖死）。

    ★ 这里断言 400 而不是 FastAPI 默认的 422：本项目有统一异常处理器，
      `RequestValidationError` 被包成 `{code, message}` 且 http_status=400。
      前端只认这一套（见 client.ts 的响应拦截器），所以测试跟随实际契约。
    """
    assert api_env.get("/api/v1/alerts/logs?limit=0").status_code == 400
    assert api_env.get("/api/v1/alerts/logs?limit=9999").status_code == 400
    assert api_env.get("/api/v1/alerts/logs?offset=-1").status_code == 400


# ======================================================================
# POST /test
# ======================================================================
def test_test_endpoint_success(api_env: TestClient, ok_sender: list[str]):
    r = api_env.post("/api/v1/alerts/test")
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 0
    assert body["message"] == "测试消息已发送"
    assert body["data"]["ok"] is True
    assert body["data"]["status"] == "sent"
    assert len(ok_sender) == 1
    assert "持仓提醒" in ok_sender[0]


def test_test_endpoint_repeatable(api_env: TestClient, ok_sender: list[str]):
    """连点不幂等 —— 每次都要真的发出去。"""
    for _ in range(3):
        assert api_env.post("/api/v1/alerts/test").json()["code"] == 0
    assert len(ok_sender) == 3


def test_test_endpoint_failure_returns_chinese_reason(
    api_env: TestClient, monkeypatch: pytest.MonkeyPatch
):
    """失败时返回**中文可执行原因**，不是 500 或原始报错。"""
    monkeypatch.setattr(
        wc_mod.wecom_client, "send_markdown",
        lambda content, mention_all=False: SendResult(
            ok=False, errcode=93000, channel="webhook",
            message="Webhook key 无效或已被移除。请到企业微信群 → 群机器人 → 复制新的 Webhook 地址",
            retryable=False,
        ),
    )
    r = api_env.post("/api/v1/alerts/test")
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 93000
    assert "群机器人" in body["message"]


def test_test_endpoint_when_disabled(api_env: TestClient, monkeypatch: pytest.MonkeyPatch,
                                    ok_sender: list[str]):
    monkeypatch.setattr(svc.settings.alert, "enabled", False, raising=False)
    body = api_env.post("/api/v1/alerts/test").json()
    assert body["code"] != 0
    assert "未开启" in body["message"]
    assert ok_sender == []


def test_test_endpoint_not_configured(api_env: TestClient, monkeypatch: pytest.MonkeyPatch):
    """凭证缺失 → 明确的中文指引，且**真实发送被拦下**。

    ⚠️ 这里**不能**用 `ok_sender` 夹具 —— 它把 `send_markdown` 整个替换掉了，
      前置校验根本没机会跑（那个夹具是用来验证"成功路径"的）。
      换成记录"底层 HTTP 是否真的被调用"，这样才能证明拦住了。
    """
    monkeypatch.setattr(svc.settings.env, "wecom_webhook_url", "", raising=False)
    http_calls: list[int] = []
    monkeypatch.setattr(
        wc_mod.wecom_client._client, "post", lambda *a, **k: http_calls.append(1)
    )

    body = api_env.post("/api/v1/alerts/test").json()
    assert body["code"] != 0
    assert "WECOM_WEBHOOK_URL" in body["message"]
    assert http_calls == [], "凭证缺失时不应发出任何 HTTP 请求"


# ======================================================================
# POST /{id}/resend
# ======================================================================
def test_resend_endpoint(api_env: TestClient, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        wc_mod.wecom_client, "send_markdown",
        lambda content, mention_all=False: SendResult(
            ok=False, errcode=60020, channel="webhook",
            message="当前 IP 不在应用的可信 IP 列表内。请到企业微信后台 → 应用 → 企业可信 IP 添加本机公网 IP",
            retryable=False,
        ),
    )
    api_env.post("/api/v1/alerts/test")
    alert_id = api_env.get("/api/v1/alerts/logs").json()["data"]["items"][0]["id"]

    # 修好凭证后重发
    monkeypatch.setattr(
        wc_mod.wecom_client, "send_markdown",
        lambda content, mention_all=False: SendResult(ok=True, errcode=0, channel="webhook"),
    )
    r = api_env.post(f"/api/v1/alerts/{alert_id}/resend")
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 0
    assert body["data"]["status"] == "sent"
    assert api_env.get("/api/v1/alerts/logs").json()["data"]["items"][0]["status"] == "sent"


def test_resend_missing_returns_404(api_env: TestClient):
    """不存在的 id → 走统一异常处理器，给出中文原因。"""
    r = api_env.post("/api/v1/alerts/999999/resend")
    assert r.status_code in (404, 502)
    body = r.json()
    assert "不存在" in str(body)


def test_resend_already_sent(api_env: TestClient, ok_sender: list[str]):
    """已发送的记录重发 → 幂等返回成功，但**不重复发消息**。"""
    api_env.post("/api/v1/alerts/test")
    alert_id = api_env.get("/api/v1/alerts/logs").json()["data"]["items"][0]["id"]
    before = len(ok_sender)

    body = api_env.post(f"/api/v1/alerts/{alert_id}/resend").json()
    assert body["code"] == 0
    # 顶层 message 是"重发成功"，具体说明在 data.message 里
    assert "无需重发" in body["data"]["message"]
    assert len(ok_sender) == before, "已发送的记录不该真的再发一次"


# ======================================================================
# GET /status
# ======================================================================
def test_status_masks_credential(api_env: TestClient):
    data = api_env.get("/api/v1/alerts/status").json()["data"]
    assert data["channel"] == "webhook"
    assert data["enabled"] is True
    assert data["configured"] is True
    assert data["webhook_key_masked"] == "ABCD****5678"
    # 🔴 原始 key 绝不能出现在响应里
    assert "ABCD1234EFGH5678" not in api_env.get("/api/v1/alerts/status").text
    assert "rate" in data
    assert "required_keyword" in data


# ======================================================================
# GET /templates
# ======================================================================
def test_templates_endpoint(api_env: TestClient):
    data = api_env.get("/api/v1/alerts/templates").json()["data"]
    assert len(data) >= 5
    actions = {d["action"] for d in data}
    assert {"open_position", "close_position", "test", "job_failed"} <= actions
    for d in data:
        assert d["sample"]["content"]
        assert d["sample"]["title"]


# ======================================================================
# GET /meta
# ======================================================================
def test_meta_endpoint(api_env: TestClient):
    data = api_env.get("/api/v1/alerts/meta").json()["data"]
    assert any(a["value"] == "open_position" and a["label"] == "开仓" for a in data["actions"])
    assert any(s["value"] == "failed" for s in data["statuses"])
    assert any(r["value"] == "time_stop" and r["label"] == "持有期满"
               for r in data["exit_reasons"])
    assert data["channel"] == "webhook"


# ======================================================================
# 系统状态页的联动
# ======================================================================
def test_health_exposes_alert_state(api_env: TestClient):
    """系统状态接口要反映提醒开关（前端据此提示"未开启"）。"""
    health = api_env.get("/api/health")
    assert health.status_code == 200
    data = health.json()["data"]
    assert data["wecom_configured"] is True
    assert data["wecom_key_masked"] == "ABCD****5678"
