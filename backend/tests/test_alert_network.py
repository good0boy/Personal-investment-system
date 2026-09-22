"""模块 5 · **真实网络**验收（默认跳过）。

★ 为什么必须有这个文件
--------------------
前面所有测试都把 `send_markdown` 或底层 HTTP monkeypatch 掉了 —— 它们证明的是
「代码逻辑对」，**不能证明链路真的通**。而主人本阶段的验收标准原文是
「以消息触达用户为最终目的，确保链路能够跑通」。

只有真发一条到企微群、手机真收到，才算验收通过。所以这里有一个
`@pytest.mark.network` 的用例，默认不跑，在验收时手工执行：

    python -m pytest tests/test_alert_network.py -m network -v -s

★ 为什么默认 skip
  1. CI / 日常全量测试不应该往群里发消息（会被噪音淹没）；
  2. 群机器人有 20 条/分钟的限频，测试跑多了会互相干扰；
  3. 它依赖 `.env` 里的真实凭证 —— 换机器或没配 `.env` 时必须能优雅跳过。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.config import settings  # noqa: E402
from app.repos import db as db_module  # noqa: E402
from app.services import alert_service as svc  # noqa: E402
from app.services.wecom_client import wecom_client  # noqa: E402

pytestmark = pytest.mark.network


def _skip_reason() -> str | None:
    if not settings.alert.enabled:
        return "config.yaml 的 alert.enabled=false，跳过真实发送"
    if not settings.env.wecom_webhook_configured:
        return ".env 未配置 WECOM_WEBHOOK_URL，跳过真实发送"
    return None


@pytest.mark.skipif(_skip_reason() is not None, reason=_skip_reason() or "")
def test_real_send_reaches_wecom(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """真实发送一条测试消息到企微群。

    验收判据（**看手机**）：群里收到一条以「持仓提醒 · 链路测试」开头的消息。
    这里能自动断言的是企微返回 `errcode=0`（已受理）+ alert_log 落成 sent。

    ⚠️ `errcode=0` **不等于**手机一定收到：若群里开了「自定义关键词」而消息
       不含关键词，会被静默丢弃。本项目的模板强制带 `持仓提醒`，
       但这一条只能靠人眼看手机确认 —— 所以测试最后会打印提醒。
    """
    db_module.close_connection()
    monkeypatch.setattr(db_module, "META_DB", tmp_path / "meta" / "meta.db")
    conn = db_module.get_connection()
    conn.executescript((BACKEND_ROOT / "sql" / "schema.sql").read_text(encoding="utf-8"))

    result = svc.send_test(operator="真实网络验收（pytest -m network）")

    print("\n" + "=" * 62)
    print("  真实发送结果：", result)
    print("  渠道：", wecom_client.channel)
    print("  凭证：", settings.env.wecom_webhook_key_masked)
    print("=" * 62)
    print("  ⚠️ 请现在看一眼手机/企业微信：")
    print("     应收到一条以「持仓提醒 · 链路测试」开头的消息。")
    print("     若没收到 → 检查群机器人的「自定义关键词」设置。")
    print("=" * 62 + "\n")

    assert result.get("ok") is True, f"真实发送失败：{result.get('message') or result}"
    assert result["status"] == "sent"

    logs = svc.list_logs()
    assert logs["total"] == 1
    assert logs["items"][0]["status"] == "sent"
    assert logs["items"][0]["sent_at"]


@pytest.mark.skipif(_skip_reason() is not None, reason=_skip_reason() or "")
def test_real_send_bad_key_gives_chinese_hint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """真实错误路径：故意用错的 key，验证错误码被翻译成中文。

    ★ 这条测试的价值在于**验证错误码表的真值**。企微文档里的码是文档，
      实际返回什么只有真调一次才知道 —— 万一 93000 其实不是"key 无效"，
      主人按错误提示去修就会修错地方。
    """
    db_module.close_connection()
    monkeypatch.setattr(db_module, "META_DB", tmp_path / "meta" / "meta.db")
    conn = db_module.get_connection()
    conn.executescript((BACKEND_ROOT / "sql" / "schema.sql").read_text(encoding="utf-8"))

    monkeypatch.setattr(
        settings.env, "wecom_webhook_url",
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=00000000-0000-0000-0000-000000000000",
        raising=False,
    )

    result = svc.send_test(operator="错误 key 验收")

    print("\n" + "=" * 62)
    print("  故意用错 key 的结果：", result)
    print("=" * 62 + "\n")

    assert result.get("ok") is False, "错误的 key 不该发送成功"
    msg = str(result.get("message") or "")
    assert "errcode" not in msg.lower(), f"错误提示不该暴露原始 errcode：{msg}"
    # 提示应该是可执行的中文（要么命中 93000，要么至少是"未知错误 + 码"）
    assert any(kw in msg for kw in ("Webhook", "key", "未知错误")), f"提示不可执行：{msg}"
