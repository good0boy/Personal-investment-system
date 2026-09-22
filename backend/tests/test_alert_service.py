"""模块 5 · 提醒服务的测试。

覆盖四块：
  1. 模板渲染（含"关键词兜底"与"少传变量必须报错"）
  2. dispatch 幂等（同一事件重复派发只推一次）
  3. 退避重试（可重试才重试、不可重试立刻失败、次数累加）
  4. 交易事件派生（orders 挑当天成交 → 用 trades 补真实盈亏）

★ 夹具纪律（conftest 教训）：
  - tmp meta.db + **不碰真实 data/**；
  - 数量断言前先确认场地为空（`alert_env` 里是全新库，无需清场）；
  - 发消息一律 monkeypatch，不碰网络。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.errors import AlertError  # noqa: E402
from app.repos import db as db_module  # noqa: E402
from app.services import alert_service as svc  # noqa: E402
from app.services import wecom_client as wc_mod  # noqa: E402
from app.services.wecom_client import SendResult  # noqa: E402

RUN_DATE = "2026-09-18"


# ======================================================================
# 夹具
# ======================================================================
@pytest.fixture
def alert_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """全新 tmp meta.db + 打开开关 + 配好 webhook。"""
    db_module.close_connection()
    monkeypatch.setattr(db_module, "META_DB", tmp_path / "meta" / "meta.db")
    conn = db_module.get_connection()
    conn.executescript((BACKEND_ROOT / "sql" / "schema.sql").read_text(encoding="utf-8"))

    monkeypatch.setattr(svc.settings.alert, "enabled", True, raising=False)
    monkeypatch.setattr(svc.settings.env, "wecom_channel", "webhook", raising=False)
    monkeypatch.setattr(
        svc.settings.env, "wecom_webhook_url",
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=TESTKEY1234567890",
        raising=False,
    )
    # 退避改到亚秒级，否则一个重试用例要跑 36 秒
    monkeypatch.setattr(svc.settings.alert, "retry_backoff", [0, 0, 0], raising=False)
    monkeypatch.setattr(svc.settings.alert, "max_retry", 3, raising=False)
    svc.clear_name_cache()
    yield tmp_path
    db_module.close_connection()


@pytest.fixture
def _sent_ok(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """让所有发送成功，并记录发出的文本。"""
    sent: list[str] = []

    def fake_send(content: str, *, mention_all: bool = False) -> SendResult:
        sent.append(content)
        return SendResult(ok=True, errcode=0, channel="webhook")

    monkeypatch.setattr(wc_mod.wecom_client, "send_markdown", fake_send)
    return sent


def _fail_result(errcode: int, message: str, retryable: bool) -> SendResult:
    return SendResult(ok=False, errcode=errcode, message=message,
                      channel="webhook", retryable=retryable)


# ======================================================================
# 模板
# ======================================================================
def test_templates_load_all_actions(alert_env: Path):
    svc.templates.reload()
    actions = svc.templates.actions()
    for action in svc.ACTIONS:
        assert action in actions, f"模板缺少 {action}"


def test_every_template_title_contains_security_keyword():
    """🔴 所有模板的标题都必须含关键词。

    ★ 这条是本模块**最重要的测试之一**：企微群机器人开了「自定义关键词」
      安全设置时，消息不含关键词会被**静默丢弃**（返回 errcode=0 但收不到）。
      这是最难查的一类坑 —— 日志全绿、手机上啥也没有。
    """
    keyword = svc.templates.required_keyword
    assert keyword == "持仓提醒"

    for action in svc.templates.actions():
        msg = svc.templates.render(action, svc._sample_context(action))
        assert keyword in msg.full_text, f"模板 {action} 渲染后不含安全关键词「{keyword}」"


def test_render_missing_variable_raises():
    """少传变量必须**报错**，不能静默渲染成空。

    ★ 空字符串会让消息"发成功但缺内容" —— 比直接失败更难发现。
    """
    with pytest.raises(AlertError) as ei:
        svc.templates.render("open_position", {"date": "2026-09-18"})
    assert "渲染失败" in ei.value.message


def test_render_unknown_action_raises():
    with pytest.raises(AlertError) as ei:
        svc.templates.render("no_such_action", {})
    assert "未定义的事件模板" in ei.value.message


def test_keyword_auto_appended_when_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """模板标题漏了关键词 → 自动补到标题前（宁可多几个字，也不要收不到）。"""
    yml = tmp_path / "t.yaml"
    yml.write_text(
        """
required_keyword: "持仓提醒"
templates:
  test:
    title: "链路测试"
    body: "> 正文"
""",
        encoding="utf-8",
    )
    store = svc.TemplateStore(yml)
    msg = store.render("test", {})
    assert msg.title.startswith("持仓提醒")
    assert "持仓提醒" in msg.full_text


def test_full_text_avoids_duplicate_heading():
    """标题已并入正文时，正文首行的重复小标题应被去掉。"""
    msg = svc.RenderedMessage(
        action="open_position",
        title="持仓提醒 · 开仓 贵州茅台",
        content="**持仓提醒 · 开仓**\n> 标的：贵州茅台",
    )
    text = msg.full_text
    assert text.count("持仓提醒 · 开仓") == 1, f"标题重复了：\n{text}"


def test_template_mtime_cache_invalidates(tmp_path: Path):
    """改模板文件后必须重载（不是 lru_cache 那种吐旧值）。"""
    import time as _t

    yml = tmp_path / "t.yaml"
    yml.write_text(
        "required_keyword: ''\ntemplates:\n  test:\n    title: 'A'\n    body: '> a'\n",
        encoding="utf-8",
    )
    store = svc.TemplateStore(yml)
    assert store.render("test", {}).title == "A"

    _t.sleep(0.01)
    yml.write_text(
        "required_keyword: ''\ntemplates:\n  test:\n    title: 'B'\n    body: '> b'\n",
        encoding="utf-8",
    )
    # 强制改 mtime（部分文件系统精度不够）
    import os
    os.utime(yml, (_t.time() + 1, _t.time() + 1))
    assert store.render("test", {}).title == "B", "模板改了却没重载"


# ======================================================================
# dispatch：幂等
# ======================================================================
def test_dispatch_sends_once(alert_env: Path, _sent_ok: list[str]):
    r1 = svc.dispatch_event("open_position", svc._sample_context("open_position"),
                            event_key="2026-09-18|default|sh.600519|open_position")
    assert r1["ok"] is True
    assert r1["status"] == "sent"
    assert len(_sent_ok) == 1


def test_dispatch_is_idempotent(alert_env: Path, _sent_ok: list[str]):
    """同一事件派发两次，**只推一次**。"""
    ctx = svc._sample_context("open_position")
    key = "2026-09-18|default|sh.600519|open_position"

    first = svc.dispatch_event("open_position", ctx, event_key=key)
    second = svc.dispatch_event("open_position", ctx, event_key=key)

    assert first["ok"] is True
    assert second["skipped"] is True
    assert "已推送过" in second["reason"]
    assert len(_sent_ok) == 1, f"应只发 1 条，实际 {len(_sent_ok)} 条"


def test_dispatch_skipped_when_disabled(alert_env: Path, monkeypatch: pytest.MonkeyPatch,
                                        _sent_ok: list[str]):
    monkeypatch.setattr(svc.settings.alert, "enabled", False, raising=False)
    r = svc.dispatch_event("test", svc._sample_context("test"), event_key="k1")
    assert r["skipped"] is True
    assert _sent_ok == []
    assert svc.list_logs()["total"] == 0


def test_dispatch_requires_event_key(alert_env: Path):
    msg = svc.RenderedMessage(action="test", title="x", content="y", event_key="")
    with pytest.raises(AlertError):
        svc.dispatch(msg)


def test_dispatch_missing_template_returns_reason(alert_env: Path, _sent_ok: list[str]):
    """模板渲染失败 → 返回 reason，**不抛异常**（不能因提醒挂了让跑批失败）。"""
    r = svc.dispatch_event("open_position", {"date": "2026-09-18"}, event_key="k2")
    assert r["ok"] is False
    assert "渲染失败" in r["reason"]
    assert _sent_ok == []


# ======================================================================
# 退避重试
# ======================================================================
def test_retry_on_retryable_failure(alert_env: Path, monkeypatch: pytest.MonkeyPatch):
    """可重试失败 → 重试到成功，retry_count 与状态都写对。"""
    calls = {"n": 0}

    def fake_send(content: str, *, mention_all: bool = False) -> SendResult:
        calls["n"] += 1
        if calls["n"] < 3:
            return _fail_result(45009, "限频", retryable=True)
        return SendResult(ok=True, errcode=0, channel="webhook")

    monkeypatch.setattr(wc_mod.wecom_client, "send_markdown", fake_send)

    r = svc.dispatch_event("test", svc._sample_context("test"), event_key="retry-key",
                           sync=True)

    assert r["ok"] is True
    assert calls["n"] == 3, f"应在第 3 次成功，实际调用 {calls['n']} 次"
    row = svc.list_logs()["items"][0]
    assert row["status"] == "sent"
    assert row["retry_count"] == 2, f"retry_count 应为 2，实际 {row['retry_count']}"
    assert row["sent_at"], "sent 状态下 sent_at 必须有值"


def test_no_retry_on_permanent_failure(alert_env: Path, monkeypatch: pytest.MonkeyPatch):
    """不可重试的失败（如 IP 未白名单）→ **只试一次**，不白等 36 秒。"""
    calls = {"n": 0}

    def fake_send(content: str, *, mention_all: bool = False) -> SendResult:
        calls["n"] += 1
        return _fail_result(60020, "IP 不在可信列表", retryable=False)

    monkeypatch.setattr(wc_mod.wecom_client, "send_markdown", fake_send)

    r = svc.dispatch_event("test", svc._sample_context("test"), event_key="perm-key")

    assert r["ok"] is False
    assert calls["n"] == 1, f"不可重试的失败应只试 1 次，实际 {calls['n']} 次"
    row = svc.list_logs()["items"][0]
    assert row["status"] == "failed"
    assert "可信" in row["error_message"]


def test_retry_exhausted_marks_failed(alert_env: Path, monkeypatch: pytest.MonkeyPatch):
    """重试用尽 → failed，且次数等于 max_retry + 1。"""
    calls = {"n": 0}

    def fake_send(content: str, *, mention_all: bool = False) -> SendResult:
        calls["n"] += 1
        return _fail_result(45009, "限频", retryable=True)

    monkeypatch.setattr(wc_mod.wecom_client, "send_markdown", fake_send)

    r = svc.dispatch_event("test", svc._sample_context("test"), event_key="exhaust-key")

    assert r["ok"] is False
    assert calls["n"] == 4, f"1 首试 + 3 重试 = 4 次，实际 {calls['n']} 次"
    row = svc.list_logs()["items"][0]
    assert row["status"] == "failed"
    assert row["retry_count"] == 3


def test_async_dispatch_returns_pending_then_completes(
    alert_env: Path, monkeypatch: pytest.MonkeyPatch
):
    """异步派发：立刻返回 pending，后台跑完变 sent（跑批场景用）。"""
    monkeypatch.setattr(
        wc_mod.wecom_client, "send_markdown",
        lambda content, mention_all=False: SendResult(ok=True, errcode=0, channel="webhook"),
    )
    r = svc.dispatch_event("test", svc._sample_context("test"), event_key="async-key",
                           sync=False)
    assert r["status"] == "pending"

    svc.wait_for_retries(timeout=5.0)
    row = svc.list_logs()["items"][0]
    assert row["status"] == "sent", "后台发送完成后状态应更新为 sent"


# ======================================================================
# 手动重发
# ======================================================================
def test_resend_failed_record(alert_env: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        wc_mod.wecom_client, "send_markdown",
        lambda content, mention_all=False: _fail_result(60020, "IP 未白名单", retryable=False),
    )
    svc.dispatch_event("test", svc._sample_context("test"), event_key="resend-key")
    alert_id = svc.list_logs()["items"][0]["id"]
    assert svc.list_logs()["items"][0]["status"] == "failed"

    # 修好之后重发
    monkeypatch.setattr(
        wc_mod.wecom_client, "send_markdown",
        lambda content, mention_all=False: SendResult(ok=True, errcode=0, channel="webhook"),
    )
    r = svc.resend(alert_id)

    assert r["ok"] is True
    assert r["status"] == "sent"
    row = svc.list_logs()["items"][0]
    assert row["status"] == "sent"
    assert row["sent_at"]


def test_resend_sent_is_noop(alert_env: Path, _sent_ok: list[str]):
    """已发送的成功记录重发 → 不重复发（幂等）。"""
    svc.dispatch_event("test", svc._sample_context("test"), event_key="ok-key")
    alert_id = svc.list_logs()["items"][0]["id"]
    before = len(_sent_ok)

    r = svc.resend(alert_id)
    assert r["ok"] is True
    assert "无需重发" in r["message"]
    assert len(_sent_ok) == before, "已发送的记录不应真的再发一次"


def test_resend_missing_raises(alert_env: Path):
    with pytest.raises(AlertError) as ei:
        svc.resend(999999)
    assert "不存在" in ei.value.message


# ======================================================================
# 交易事件派生
# ======================================================================
def _orders() -> pd.DataFrame:
    """合成委托流水：当天 5 笔成交 + 1 笔被拒 + 1 笔昨天成交。"""
    return pd.DataFrame(
        [
            # 当天成交：买入
            {"signal_date": "2026-09-17", "exec_date": RUN_DATE, "symbol": "sh.600519",
             "side": "buy", "shares": 100, "price": 1680.0, "amount": 168000.0,
             "status": "filled", "reason": "momentum", "note": "momentum"},
            # 当天成交：卖出
            {"signal_date": "2026-09-17", "exec_date": RUN_DATE, "symbol": "sz.000001",
             "side": "sell", "shares": 1000, "price": 12.5, "amount": 12500.0,
             "status": "filled", "reason": "take_profit", "note": "take_profit"},
            # 当天被拒（不该推）
            {"signal_date": "2026-09-17", "exec_date": RUN_DATE, "symbol": "sh.601398",
             "side": "buy", "shares": 100, "price": None, "amount": None,
             "status": "rejected", "reason": "momentum", "note": "现金不足"},
            # 昨天成交（不该推）
            {"signal_date": "2026-09-16", "exec_date": "2026-09-17", "symbol": "sh.600036",
             "side": "buy", "shares": 100, "price": 40.0, "amount": 4000.0,
             "status": "filled", "reason": "momentum", "note": "momentum"},
        ]
    )


def _trades() -> pd.DataFrame:
    """合成已平仓交易（含真实盈亏）。"""
    return pd.DataFrame(
        [
            {"symbol": "sz.000001", "entry_date": "2026-09-10", "entry_price": 12.0,
             "shares": 1000, "entry_amount": 12000.0, "entry_cost": 8.0,
             "exit_date": RUN_DATE, "exit_price": 12.5, "exit_amount": 12500.0,
             "exit_cost": 18.0, "cost": 26.0, "gross_pnl": 500.0, "net_pnl": 474.0,
             "net_return": 0.0395, "hold_days": 7, "entry_reason": "momentum",
             "exit_reason": "take_profit"},
        ]
    )


def _nav() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"date": "2026-09-17", "nav": 0.99, "position_count": 4},
            {"date": RUN_DATE, "nav": 0.995, "position_count": 5},
        ]
    )


def test_notify_trades_selects_only_today_filled(alert_env: Path, monkeypatch: pytest.MonkeyPatch,
                                                _sent_ok: list[str]):
    """只挑 `run_date` 当天且 `status=filled` 的委托。

    ★ 判据是**发出的条数**：4 条流水里只有 2 条该推（当天成交的买卖各一笔）。
      被拒的那笔推了会误导（"我买了"其实没成交），
      昨天成交的那笔推了是重复（已推过）。
    """
    monkeypatch.setattr(svc, "_name_map", lambda: {"sh.600519": "贵州茅台", "sz.000001": "平安银行"})

    stats = svc.notify_trades(
        orders=_orders(), trades=_trades(), nav=_nav(),
        run_date=RUN_DATE, account_id="default",
        account_name="默认模拟账户", strategy_name="momentum",
        max_positions=5, async_send=False,
    )

    assert stats["opened"] == 1
    assert stats["closed"] == 1
    assert stats["sent"] == 2, f"应发 2 条，实际 {stats['sent']}"
    assert len(_sent_ok) == 2
    joined = "\n".join(_sent_ok)
    assert "贵州茅台" in joined, "买入消息应带股票名称"
    assert "平安银行" in joined, "卖出消息应带股票名称"
    assert "sh.601398" not in joined, "被拒委托不应推送"
    assert "sh.600036" not in joined, "非当天的成交不应推送"


def test_notify_trades_uses_real_pnl(alert_env: Path, monkeypatch: pytest.MonkeyPatch,
                                     _sent_ok: list[str]):
    """清仓消息里的盈亏必须是 trades 里的**真实数字**，不能是 0。"""
    monkeypatch.setattr(svc, "_name_map", lambda: {})
    svc.notify_trades(
        orders=_orders(), trades=_trades(), nav=_nav(),
        run_date=RUN_DATE, account_id="default",
        account_name="默认模拟账户", strategy_name="momentum", async_send=False,
    )

    closed = [t for t in _sent_ok if "清仓" in t]
    assert len(closed) == 1
    text = closed[0]
    assert "+474.00" in text, f"应为真实净盈亏 +474.00，实际：\n{text}"
    assert "+3.95%" in text, f"应为真实收益率 +3.95%，实际：\n{text}"
    assert "止盈" in text, "卖出原因应翻译成中文"


def test_notify_trades_skips_close_without_pnl(alert_env: Path, monkeypatch: pytest.MonkeyPatch,
                                              _sent_ok: list[str]):
    """🔴 找不到盈亏记录时**宁可不发**，绝不推假数字。

    ★ 推一条"+0.00 元盈亏"比不推更坏 —— 它会让人以为真的没赚没亏。
    """
    monkeypatch.setattr(svc, "_name_map", lambda: {})
    stats = svc.notify_trades(
        orders=_orders(), trades=pd.DataFrame(), nav=_nav(),   # ← trades 为空
        run_date=RUN_DATE, account_id="default",
        account_name="默认模拟账户", strategy_name="momentum", async_send=False,
    )
    assert stats["closed"] == 0
    assert stats["skipped"] == 1
    assert stats["sent"] == 1, "只有开仓那笔该发出去"
    assert all("清仓" not in t for t in _sent_ok), "缺盈亏的清仓消息不该发出去"


def test_notify_trades_empty_orders(alert_env: Path, _sent_ok: list[str]):
    stats = svc.notify_trades(
        orders=pd.DataFrame(), trades=pd.DataFrame(), nav=pd.DataFrame(),
        run_date=RUN_DATE, account_id="default",
        account_name="默认模拟账户", strategy_name="momentum", async_send=False,
    )
    assert stats["sent"] == 0
    assert _sent_ok == []


def test_notify_trades_missing_columns_is_safe(alert_env: Path, _sent_ok: list[str]):
    """orders 缺列 → 记 reason 而不是崩（引擎契约变了也不该让跑批挂）。"""
    bad = pd.DataFrame([{"symbol": "sh.600519"}])
    stats = svc.notify_trades(
        orders=bad, trades=pd.DataFrame(), nav=pd.DataFrame(),
        run_date=RUN_DATE, account_id="default",
        account_name="默认模拟账户", strategy_name="momentum", async_send=False,
    )
    assert stats["sent"] == 0
    assert "orders 缺少列" in stats.get("reason", "")


def test_notify_trades_idempotent_across_runs(alert_env: Path, monkeypatch: pytest.MonkeyPatch,
                                             _sent_ok: list[str]):
    """同一 run_date 跑两次 → 第二次全部跳过（幂等键生效）。"""
    monkeypatch.setattr(svc, "_name_map", lambda: {})
    kw = {
        "orders": _orders(),
        "trades": _trades(),
        "nav": _nav(),
        "run_date": RUN_DATE,
        "account_id": "default",
        "account_name": "账户",
        "strategy_name": "momentum",
        "async_send": False,
    }

    first = svc.notify_trades(**kw)
    second = svc.notify_trades(**kw)

    assert first["sent"] == 2
    assert second["sent"] == 0
    assert second["skipped"] == 2
    assert len(_sent_ok) == 2, "重复派发不应多发消息"


def test_notify_trades_disabled(alert_env: Path, monkeypatch: pytest.MonkeyPatch,
                                _sent_ok: list[str]):
    monkeypatch.setattr(svc.settings.alert, "enabled", False, raising=False)
    monkeypatch.setattr(svc, "_name_map", lambda: {})
    stats = svc.notify_trades(
        orders=_orders(), trades=_trades(), nav=_nav(), run_date=RUN_DATE,
        account_id="default", account_name="账户", strategy_name="momentum", async_send=False,
    )
    assert stats["sent"] == 0
    assert _sent_ok == []


# ======================================================================
# 任务失败告警
# ======================================================================
def test_notify_job_failed(alert_env: Path, _sent_ok: list[str]):
    r = svc.notify_job_failed(
        job_id="paper_daily", job_name="模拟盘日度跑批",
        run_date=RUN_DATE, trigger_type="cron",
        error=RuntimeError("引擎炸了"), duration=12.5,
    )
    assert r["ok"] is True
    assert len(_sent_ok) == 1
    text = _sent_ok[0]
    assert "模拟盘日度跑批" in text
    assert "RuntimeError" in text
    assert "引擎炸了" in text
    assert "持仓提醒" in text     # 🔴 安全关键词必须在


def test_notify_job_failed_dedupes_same_day(alert_env: Path, _sent_ok: list[str]):
    """同一任务同一天失败多次 → 只推第一条（后几条大概率同根因）。"""
    for _ in range(3):
        svc.notify_job_failed(
            job_id="paper_daily", job_name="模拟盘", run_date=RUN_DATE,
            trigger_type="cron", error=RuntimeError("x"),
        )
    assert len(_sent_ok) == 1


def test_notify_job_failed_with_string_error(alert_env: Path, _sent_ok: list[str]):
    """传字符串（不是异常对象）也不能崩。"""
    r = svc.notify_job_failed(
        job_id="catalog_check", job_name="接口目录自检", run_date=RUN_DATE,
        trigger_type="manual", error="纯文本错误",
    )
    assert r["ok"] is True
    assert "纯文本错误" in _sent_ok[0]


# ======================================================================
# 只读查询
# ======================================================================
def test_list_logs_filters(alert_env: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        wc_mod.wecom_client, "send_markdown",
        lambda content, mention_all=False: SendResult(ok=True, errcode=0, channel="webhook"),
    )
    svc.dispatch_event("test", svc._sample_context("test"), event_key="f1")
    svc.dispatch_event("test", svc._sample_context("test"), event_key="f2")

    monkeypatch.setattr(
        wc_mod.wecom_client, "send_markdown",
        lambda content, mention_all=False: _fail_result(60020, "IP", retryable=False),
    )
    svc.dispatch_event("job_failed", svc._sample_context("job_failed"), event_key="f3")

    all_logs = svc.list_logs()
    assert all_logs["total"] == 3
    assert all_logs["status_counts"]["sent"] == 2
    assert all_logs["status_counts"]["failed"] == 1

    only_failed = svc.list_logs(status="failed")
    assert only_failed["total"] == 1
    assert only_failed["items"][0]["action"] == "job_failed"

    only_test = svc.list_logs(action="test")
    assert only_test["total"] == 2


def test_list_logs_decorates_action_cn(alert_env: Path, _sent_ok: list[str]):
    """中文名由后端给（前端不做 if-else 映射）。"""
    svc.dispatch_event("open_position", svc._sample_context("open_position"), event_key="d1")
    row = svc.list_logs()["items"][0]
    assert row["action_cn"] == "开仓"


def test_list_logs_pagination(alert_env: Path, _sent_ok: list[str]):
    for i in range(5):
        svc.dispatch_event("test", svc._sample_context("test"), event_key=f"p{i}")

    page1 = svc.list_logs(limit=2, offset=0)
    page2 = svc.list_logs(limit=2, offset=2)
    assert page1["total"] == 5
    assert len(page1["items"]) == 2
    assert len(page2["items"]) == 2
    assert page1["items"][0]["id"] != page2["items"][0]["id"]


def test_list_logs_empty(alert_env: Path):
    data = svc.list_logs()
    assert data["total"] == 0
    assert data["items"] == []
    assert data["status_counts"] == {"pending": 0, "sent": 0, "failed": 0, "skipped": 0}


# ======================================================================
# 渠道状态 / 模板预览
# ======================================================================
def test_channel_status_masks_credential(alert_env: Path):
    """状态接口**绝不返回凭证原文**。"""
    info = svc.channel_status()
    assert info["channel"] == "webhook"
    assert info["enabled"] is True
    assert info["configured"] is True
    blob = str(info)
    assert "TESTKEY1234567890" not in blob, "凭证原文泄露到状态接口里了"
    assert "TEST****7890" in info["webhook_key_masked"]


def test_preview_templates_renders_samples(alert_env: Path):
    previews = svc.preview_templates()
    assert len(previews) == len(svc.ACTIONS)
    for p in previews:
        assert p["action_cn"], f"{p['action']} 缺中文名"
        assert p["sample"]["content"], f"{p['action']} 样例渲染为空"
        assert "持仓提醒" in p["sample"]["content"]
        # 样例里不该出现未渲染的占位符
        assert "{{" not in p["sample"]["content"], f"{p['action']} 样例残留占位符"


def test_sample_context_covers_all_template_vars():
    """🔴 样例上下文必须覆盖所有模板变量。

    ★ 这条防的是「模板加了变量、预览的假数据没跟上」——
      表现是模板预览页显示"样例渲染失败"，而真实发送是好的，很难归因。
    """
    import re

    for action in svc.templates.actions():
        spec = svc.templates.raw(action)
        names = set(re.findall(r"{{\s*([a-zA-Z_][a-zA-Z0-9_]*)", spec["title"] + spec["body"]))
        provided = set(svc._sample_context(action))
        missing = names - provided
        assert not missing, f"模板 {action} 用了未提供的样例变量：{sorted(missing)}"


def test_send_test_creates_unique_events(alert_env: Path, _sent_ok: list[str]):
    """测试消息**故意不去重** —— 每次点都要发得出去。"""
    dispatches = [svc.send_test() for _ in range(3)]
    assert all(d["ok"] for d in dispatches)
    assert len(_sent_ok) == 3, "连点 3 次测试应发 3 条"


def test_send_test_failure_returns_reason(alert_env: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        wc_mod.wecom_client, "send_markdown",
        lambda content, mention_all=False: _fail_result(93000, "key 无效", retryable=False),
    )
    r = svc.send_test()
    assert r["ok"] is False
    assert r["status"] == "failed"
    assert "key" in r["message"] or "Webhook" in r["message"]


# ======================================================================
# 股票名称解析（🔴 2026-09-20 实测修的两个坑）
# ======================================================================
def test_name_probe_days_includes_last_trading_day():
    """非交易日必须回退到最近交易日。

    🔴 背景：`baostock.query_all_stock` 在**非交易日返回 0 行**
      （实测 09-20 周日 = 0 行、09-18 周五 = 7401 行）。
      不修的话周末跑链路测试、或跑批顺延到次日补跑时，
      股票名称会**静默全丢**，消息退回股票代码。
    """
    svc.clear_name_cache()
    days = svc._name_probe_days("2026-09-20")  # 周日
    assert days[0] == "2026-09-20"
    assert len(days) >= 2, "非交易日应回退到最近交易日"
    assert days[1] != days[0], "回退日期必须与今天不同（去重过了）"


def test_name_map_falls_back_to_last_trading_day(monkeypatch: pytest.MonkeyPatch):
    """今天空表 → 自动换最近交易日再试。"""
    import pandas as pd

    from app.services.data_source.baostock_client import baostock_client as bs

    calls: list[str | None] = []

    def fake_fetch(day=None):
        calls.append(day)
        if day == "2026-09-20":  # 今天（周日）—— 空
            return pd.DataFrame(columns=["code", "name", "trade_status"])
        return pd.DataFrame([{"code": "sh.600519", "name": "贵州茅台", "trade_status": 1}])

    # ⚠️ `app.services.data_source.baostock_client` 是**单例实例**而非模块
    #    （`__init__` 里同名子模块属性被遮蔽，见项目通用坑清单）——
    #    所以 patch 的是实例方法，不是模块属性。
    monkeypatch.setattr(bs, "fetch_all_stock", fake_fetch)
    monkeypatch.setattr(svc, "_name_probe_days", lambda today: ["2026-09-20", "2026-09-18"])
    svc.clear_name_cache()

    m = svc._name_map()
    assert m == {"sh.600519": "贵州茅台"}, "应回退到最近交易日拿到名称"
    assert calls == ["2026-09-20", "2026-09-18"], "先试今天，再试最近交易日"


def test_name_map_all_empty_degrades_to_empty(monkeypatch: pytest.MonkeyPatch):
    """全试完都空 → 返回空 dict（消息退回代码），**不抛异常**。"""
    import pandas as pd

    from app.services.data_source.baostock_client import baostock_client as bs

    monkeypatch.setattr(
        bs, "fetch_all_stock",
        lambda day=None: pd.DataFrame(columns=["code", "name", "trade_status"]),
    )
    monkeypatch.setattr(svc, "_name_probe_days", lambda today: ["2026-09-20"])
    svc.clear_name_cache()

    assert svc._name_map() == {}


def test_name_map_swallows_exception(monkeypatch: pytest.MonkeyPatch):
    """取名称的网络异常不能冒泡 —— 提醒链路的依赖必须"挂了也能发"。"""
    from app.services.data_source.baostock_client import baostock_client as bs

    def boom(day=None):
        raise RuntimeError("baostock 掉线了")

    monkeypatch.setattr(bs, "fetch_all_stock", boom)
    monkeypatch.setattr(svc, "_name_probe_days", lambda today: ["2026-09-20"])
    svc.clear_name_cache()

    assert svc._name_map() == {}


# ======================================================================
# 每日持仓早报（daily_summary，交易日 08:00）
# ======================================================================
def _fake_artifacts(*, positions: pd.DataFrame, nav: pd.DataFrame, cfg: dict | None = None):
    """构造 `paper_service._load_artifacts` 的假返回值。

    ★ 为什么要 monkeypatch 而不是真读 `data/paper/`：
      真产物会随跑批变化，测试断言会跟着漂 —— 而且它是**用户数据**，
      测试绝不该依赖它。这里给的是与真实产物同列名的构造数据。
    """
    return {
        "nav": nav,
        "positions": positions,
        "trades": pd.DataFrame(),
        "orders": pd.DataFrame(),
        "config": cfg or {"params": {"max_positions": 5}},
    }


def _fake_positions(rows: list[dict]) -> pd.DataFrame:
    cols = [
        "date", "symbol", "shares", "entry_date", "entry_price",
        "close", "market_value", "unrealized_pnl", "hold_days",
    ]
    return pd.DataFrame(rows, columns=cols)


def _fake_nav(date: str, *, total_asset: float, cash: float, market_value: float, nav: float):
    return pd.DataFrame(
        [
            {
                "date": date, "cash": cash, "market_value": market_value,
                "total_asset": total_asset, "position_count": 1,
                "nav": nav, "drawdown": -0.1,
            }
        ]
    )


@pytest.fixture
def fake_paper(monkeypatch: pytest.MonkeyPatch):
    """替换 `paper_service._load_artifacts`，让早报读假产物。"""
    from app.services import paper_service

    holder: dict[str, object] = {"artifacts": None}

    def fake_load(account_id: str):
        art = holder["artifacts"]
        assert art is not None, "测试必须先设置 holder['artifacts']"
        return art

    monkeypatch.setattr(paper_service, "_load_artifacts", fake_load)
    # 名称一律走假表，别碰 baostock
    monkeypatch.setattr(svc, "_name_map", lambda: {"sh.600519": "贵州茅台", "sz.300750": "宁德时代"})
    return holder


def test_daily_summary_ctx_with_positions(alert_env: Path, fake_paper):
    """有持仓：占比/总仓位/浮盈合计都要对得上。"""
    fake_paper["artifacts"] = _fake_artifacts(
        positions=_fake_positions([
            {"date": RUN_DATE, "symbol": "sh.600519", "shares": 100, "entry_date": "2026-09-10",
             "entry_price": 1600.0, "close": 1680.0, "market_value": 168000.0,
             "unrealized_pnl": 8000.0, "hold_days": 5},
            {"date": RUN_DATE, "symbol": "sz.300750", "shares": 500, "entry_date": "2026-09-12",
             "entry_price": 400.0, "close": 390.0, "market_value": 195000.0,
             "unrealized_pnl": -5000.0, "hold_days": 3},
        ]),
        nav=_fake_nav(RUN_DATE, total_asset=1_000_000.0, cash=637_000.0,
                      market_value=363_000.0, nav=1.0),
    )

    ctx = svc._daily_summary_ctx(account_id="default")

    assert ctx is not None
    assert ctx["date"] == RUN_DATE
    assert ctx["has_position"] is True
    assert ctx["position_count"] == 2
    assert ctx["max_positions"] == 5
    # 总仓位用 nav.market_value（与页面一致），不是持仓市值加总
    assert ctx["position_ratio"] == pytest.approx(0.363)
    assert ctx["cash_ratio"] == pytest.approx(0.637)
    assert ctx["unrealized_pnl"] == pytest.approx(3000.0)
    # 名称要带上（主人 Q3 的硬要求）
    assert "贵州茅台" in ctx["position_summary"]
    assert "宁德时代" in ctx["position_summary"]
    # 单只占比按市值降序
    assert ctx["holdings_text"].index("宁德时代") < ctx["holdings_text"].index("贵州茅台")


def test_daily_summary_ctx_empty_still_sends(alert_env: Path, fake_paper):
    """空仓也照发（主人拍板）：必须给出「今日无开仓」文案，不能是空串。"""
    fake_paper["artifacts"] = _fake_artifacts(
        positions=_fake_positions([]),
        nav=_fake_nav(RUN_DATE, total_asset=1_000_000.0, cash=1_000_000.0,
                      market_value=0.0, nav=1.0),
    )

    ctx = svc._daily_summary_ctx(account_id="default")

    assert ctx is not None
    assert ctx["has_position"] is False
    assert ctx["position_count"] == 0
    assert ctx["position_ratio"] == 0.0
    assert "今日无开仓" in ctx["position_summary"], "空仓文案必须在变量里（模板不含 Jinja 块）"
    assert "空仓" in ctx["position_ratio_text"]
    assert ctx["holdings_text"], "空仓时 holdings_text 也要有值，否则模板渲染出空行"


def test_daily_summary_ctx_no_nav_returns_none(alert_env: Path, fake_paper):
    """完全没有产物（首次跑批前）→ 返回 None，调用方跳过，不抛异常。"""
    fake_paper["artifacts"] = _fake_artifacts(
        positions=_fake_positions([]), nav=pd.DataFrame()
    )
    assert svc._daily_summary_ctx(account_id="default") is None


def test_notify_daily_summary_sends_and_carries_keyword(
    alert_env: Path, fake_paper, _sent_ok: list[str]
):
    """端到端：渲染 + 发送，且**必须含「持仓提醒」关键词**（否则企微静默丢弃）。"""
    fake_paper["artifacts"] = _fake_artifacts(
        positions=_fake_positions([
            {"date": RUN_DATE, "symbol": "sh.600519", "shares": 100, "entry_date": "2026-09-10",
             "entry_price": 1600.0, "close": 1680.0, "market_value": 168000.0,
             "unrealized_pnl": 8000.0, "hold_days": 5},
        ]),
        nav=_fake_nav(RUN_DATE, total_asset=1_000_000.0, cash=832_000.0,
                      market_value=168_000.0, nav=1.0),
    )

    result = svc.notify_daily_summary(sync=True)

    assert result["ok"] is True, result
    assert len(_sent_ok) == 1
    text = _sent_ok[0]
    assert "持仓提醒" in text, "缺关键词会被企微静默丢弃"
    assert "贵州茅台" in text
    assert "16.8%" in text, "单只占比要出现在消息里"
    # 换行必须独立 —— 引用行挤在一起会让手机上排版塌掉（实测踩过）
    assert "> 开仓股票：" in text
    assert "\n> 开仓仓位：" in text


def test_notify_daily_summary_idempotent_same_day(
    alert_env: Path, fake_paper, _sent_ok: list[str]
):
    """同一天连调两次只发一条（幂等键 `{date}|daily_summary`）。"""
    fake_paper["artifacts"] = _fake_artifacts(
        positions=_fake_positions([]),
        nav=_fake_nav(RUN_DATE, total_asset=1_000_000.0, cash=1_000_000.0,
                      market_value=0.0, nav=1.0),
    )

    first = svc.notify_daily_summary(sync=True)
    second = svc.notify_daily_summary(sync=True)

    assert first["ok"] is True
    assert second["skipped"] is True
    assert len(_sent_ok) == 1, "同一天第二次必须被幂等键挡住"


def test_notify_daily_summary_different_days_both_send(
    alert_env: Path, fake_paper, _sent_ok: list[str]
):
    """🔴 幂等键必须**带日期** —— 换一天照样要发得出去。

    ★ 这个用例是用**变异测试**换来的：早先只测了"同一天连调两次"，
      把幂等键写死成固定串（`"FIXED|daily_summary"`）**照样全绿** ——
      因为固定串也能挡住同一天的第二次。
      只有"换日期"这个维度才能把"键里有没有日期"这件事测出来。
    """
    nav1 = _fake_nav("2026-09-17", total_asset=1_000_000.0, cash=1_000_000.0,
                     market_value=0.0, nav=1.0)
    nav2 = _fake_nav(RUN_DATE, total_asset=1_010_000.0, cash=1_010_000.0,
                     market_value=0.0, nav=1.01)

    fake_paper["artifacts"] = _fake_artifacts(positions=_fake_positions([]), nav=nav1)
    first = svc.notify_daily_summary(sync=True)

    fake_paper["artifacts"] = _fake_artifacts(positions=_fake_positions([]), nav=nav2)
    second = svc.notify_daily_summary(sync=True)

    assert first["ok"] is True, first
    assert second["ok"] is True, f"换一天必须重新发，实际 {second}"
    assert len(_sent_ok) == 2


def test_notify_daily_summary_no_artifacts_skips(alert_env: Path, fake_paper, _sent_ok: list[str]):
    """无产物 → 明确 skipped，**不抛异常**（否则定时任务会被记成 failed）。"""
    fake_paper["artifacts"] = _fake_artifacts(positions=_fake_positions([]), nav=pd.DataFrame())

    result = svc.notify_daily_summary(sync=True)

    assert result["dispatched"] is False
    assert result["skipped"] is True
    assert _sent_ok == []


def test_notify_daily_summary_swallows_exception(alert_env: Path, monkeypatch: pytest.MonkeyPatch,
                                                 _sent_ok: list[str]):
    """上下文构造抛异常也不许冒泡 —— 早报发不出去 != 跑批失败。"""
    def boom(**kwargs):
        raise RuntimeError("磁盘炸了")

    monkeypatch.setattr(svc, "_daily_summary_ctx", boom)

    result = svc.notify_daily_summary(sync=True)

    assert result["dispatched"] is False
    assert result["skipped"] is True
    assert "磁盘炸了" in result["reason"]
