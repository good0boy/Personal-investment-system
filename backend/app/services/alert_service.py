"""提醒服务（阶段 5）—— 事件派发、幂等、模板渲染、退避重试。

职责链
------
    dispatch(msg)                                   ← 业务层唯一入口
      1. 开关校验（alert.enabled）
      2. event_key 幂等去重（查 alert_log）
      3. 消息内容已渲染好（见下方"单次渲染"）
      4. 落库 pending
      5. 同步首试（send_markdown）
      6. 成功 → sent；失败且可重试 → 后台线程退避重试
      7. 重试用尽 → failed（UI 可手动重发）

★ 设计决定一：为什么"同步首试 + 后台重试"而不是"全异步"
------------------------------------------------------
`POST /alerts/test` 的验收标准是「点一下，消息到手机」。全异步会让接口只能
返回"已入队"，主人看不到到底成没成。所以**第一次尝试同步做掉**（正常 200ms），
只有失败时才把退避重试甩到后台 —— 那时接口已经返回了明确结论。

★ 设计决定二：为什么是"单次渲染"
-------------------------------
早先的设计想分两步：先从 orders 生成事件、再用 trades 补真实盈亏。
但模板一渲染，`{{ net_pnl }}` 就变成了文字，第二遍**没有占位符可换** ——
补数只能靠改字符串（脆弱且必然失效）。

正确顺序是反的：**先把数字备齐，再渲染一次**。所以本模块的入口是
`notify_trades(orders, trades, nav, ...)` —— 它先按 `(symbol, exit_date)`
把三张表对齐成完整上下文，再逐条渲染。

★ 幂等键
--------
    date|account|symbol|open_position      开仓
    date|account|symbol|close_position     清仓
    date|job|job_id|failed                 任务失败
    manual|test|<时间戳>                    测试消息（**故意不去重**）

测试消息必须每次点都发得出去，否则"点第二次没反应"会被当成 bug。
所以它的 event_key 带时间戳，天然唯一。

★ 交易事件怎么来
----------------
模块 4 是**全量重算**（每天从 2025-01-01 重跑一遍），所以不能拿 trades.parquet
"新文件 vs 旧文件"做差 —— 每次都是整体覆写。正确做法是看引擎的 **orders
流水**：它记录了每一笔成交（`exec_date` + `side` + `status=filled`），
`run_date` 当天的成交就是当天的事件。

因此 `paper_service` 会多落一份 `orders.parquet`（复用既有 `ORDER_COLUMNS`
契约，零新契约），本模块按 `exec_date == run_date` 过滤派生事件。

⚠️ **只对"最新交易日"派发**。补跑历史日期时若也派发，补 200 天 = 炸 200 条消息。
闸门在 `paper_service.run_paper_day(notify=...)`，由调用方决定。

★ 设计决定三：数字不全就**不发**
--------------------------------
推一条「盈亏 +0.00 元」的假消息，比不推更坏 —— 它会让人以为真的没赚没亏。
所以 `notify_trades` 对补不上盈亏的清仓事件**直接跳过并记 warning**，
绝不发半成品。
"""
from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import yaml
from jinja2 import Environment, StrictUndefined
from jinja2.exceptions import TemplateError
from loguru import logger

from app.core.config import settings
from app.core.errors import AlertError
from app.core.paths import ALERT_TEMPLATES_FILE
from app.repos import meta_repo
from app.services.wecom_client import SendResult, wecom_client

# ======================================================================
# 枚举：action / level / 中文映射
# ======================================================================
#: 支持的事件类型（与 alert_templates.yaml 的 templates 键一一对应）
ACTIONS: tuple[str, ...] = (
    "open_position",
    "close_position",
    "daily_summary",
    "test",
    "job_failed",
)

#: action → 中文名（前端筛选下拉与日志展示用）
#:
#: ★ 中文名**必须由后端给**，不能靠前端按名字 if-else 硬编码 ——
#:   那是本项目反复踩的"同一口径两处定义"（前端硬编码同样是病）。
ACTION_CN: dict[str, str] = {
    "open_position": "开仓",
    "close_position": "清仓",
    "daily_summary": "每日持仓早报",
    "test": "链路测试",
    "job_failed": "任务失败",
}

#: 卖出原因 → 中文（与 strategies/base.py 的 EXIT_REASONS 对齐）
EXIT_REASON_CN: dict[str, str] = {
    "time_stop": "持有期满",
    "take_profit": "止盈",
    "market_panic": "大盘恐慌",
    "limit_down": "跌停",
    "st": "ST 风险",
    "delist": "退市强平",
}

LEVELS: tuple[str, ...] = ("info", "warning", "error")

#: action 的默认级别（模板里也写了，这里是代码级兜底）
DEFAULT_LEVEL: dict[str, str] = {
    "open_position": "info",
    "close_position": "info",
    "daily_summary": "info",
    "test": "info",
    "job_failed": "error",
}


# ======================================================================
# 渲染结果
# ======================================================================
#: 标题归一化时剔除的字符：空白 + 中英文标点 + markdown 强调符
_HEADING_NOISE = re.compile(r"[\s·・、，,。.：:；;（）()\[\]【】「」《》<>*_~\-—]+")


def _norm_heading(text: str) -> str:
    """标题归一化：去空白/标点/加粗符，只留实义字符（用于重复标题判定）。"""
    return _HEADING_NOISE.sub("", text or "")


@dataclass
class RenderedMessage:
    """渲染后的一条消息。"""

    action: str
    title: str
    content: str
    level: str = "info"
    event_key: str = ""
    account_id: str | None = None
    symbol: str | None = None

    @property
    def full_text(self) -> str:
        """真正发出去的内容 = 标题 + 正文。

        ★ 标题**必须**出现在正文里：企微 markdown 消息没有独立的"标题"字段，
          只有 `content`。而且「持仓提醒」这个自定义关键词就在标题里 ——
          不拼进去，消息会被企微静默丢弃（返回 errcode=0 但收不到）。

        ★ 正文首行若与标题重复（模板里常写成 `**持仓提醒 · 开仓**`），
          这里自动去掉 —— 重复一行在手机上看很蠢。
        """
        body = self.content.strip()
        if self.title and self.title not in body:
            body = self._strip_duplicate_heading(body)
            return f"**{self.title}**\n{body}" if body else f"**{self.title}**"
        return body

    def _strip_duplicate_heading(self, body: str) -> str:
        """去掉正文首行中与标题重复的加粗小标题。

        判据：把两边都**归一化**（去加粗、去空白、去中英文标点）后，
        首行是标题的前缀 —— 或反过来标题以首行开头 —— 就认为是重复。

        ★ 早先的写法是**严格字符前缀匹配**，实测漏判：模板惯例是
          `title: "持仓提醒 · 今日持仓 2026-09-20"` + 正文首行
          `**持仓提醒 · 今日持仓（2026-09-20）**` —— 一个用空格、一个用
          「（」，前缀关系不成立，于是手机上出现两行近似标题。
          归一化后再比，这类"同一个标题两种写法"就都能识别。

        ⚠️ 归一化只在**比较时**用，返回的仍是原文（正文一个字都不改）。
        """
        lines = body.split("\n")
        if not lines:
            return body
        first = lines[0].strip().strip("*").strip()
        if not first:
            return body
        stem = self.title.strip()
        if not stem:
            return body

        if _norm_heading(first) and (
            _norm_heading(stem).startswith(_norm_heading(first))
            or _norm_heading(first).startswith(_norm_heading(stem))
        ):
            return "\n".join(lines[1:]).strip()
        return body


# ======================================================================
# 模板
# ======================================================================
class TemplateStore:
    """从 `config/alert_templates.yaml` 读模板并渲染（带 mtime 缓存）。

    ★ 为什么要 mtime 缓存而不是 `lru_cache`：改完模板想立刻生效，
      但不想重启服务。`lru_cache` 会一直吐旧模板，且**看不出来**
      —— 正是本项目反复踩的"改了没生效"那一类坑。
      mtime 变了就重载，代价只有一次 stat。
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = Path(path) if path else ALERT_TEMPLATES_FILE
        self._lock = threading.RLock()
        self._mtime: float = -1.0
        self._templates: dict[str, dict[str, str]] = {}
        self._required_keyword: str = ""
        self._env = Environment(
            undefined=StrictUndefined,
            autoescape=False,
            trim_blocks=True,
            lstrip_blocks=True,
        )

    # --- 加载 ---
    @property
    def path(self) -> Path:
        return self._path

    def _load_if_changed(self) -> None:
        """按 mtime 惰性重载。"""
        try:
            mtime = self._path.stat().st_mtime
        except OSError as exc:
            raise AlertError(
                f"提醒模板文件不存在或不可读：{self._path}（{exc}）"
            ) from exc

        if mtime == self._mtime:
            return

        try:
            raw = yaml.safe_load(self._path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise AlertError(f"提醒模板 YAML 解析失败：{self._path}（{exc}）") from exc

        templates_raw = raw.get("templates") or {}
        if not isinstance(templates_raw, dict) or not templates_raw:
            raise AlertError(f"提醒模板文件里没有 templates 段：{self._path}")

        parsed: dict[str, dict[str, str]] = {}
        for action, spec in templates_raw.items():
            if not isinstance(spec, dict):
                raise AlertError(f"模板 {action} 的结构不对（应为字典）：{self._path}")
            for key in ("title", "body"):
                if not str(spec.get(key) or "").strip():
                    raise AlertError(f"模板 {action} 缺少 `{key}`：{self._path}")
            parsed[str(action)] = {
                "title": str(spec["title"]),
                "body": str(spec["body"]),
                "level": str(spec.get("level") or DEFAULT_LEVEL.get(str(action), "info")),
            }

        self._templates = parsed
        self._required_keyword = str(raw.get("required_keyword") or "").strip()
        self._mtime = mtime
        logger.debug(f"[alert] 模板已加载：{len(parsed)} 个（{self._path.name}）")

    def reload(self) -> None:
        with self._lock:
            self._mtime = -1.0
            self._load_if_changed()

    def has(self, action: str) -> bool:
        with self._lock:
            self._load_if_changed()
            return action in self._templates

    def actions(self) -> list[str]:
        with self._lock:
            self._load_if_changed()
            return list(self._templates)

    @property
    def required_keyword(self) -> str:
        with self._lock:
            self._load_if_changed()
            return self._required_keyword

    def raw(self, action: str) -> dict[str, str]:
        """取原始模板（前端"模板预览"用，不做渲染）。"""
        with self._lock:
            self._load_if_changed()
            spec = self._templates.get(action)
            if spec is None:
                raise AlertError(
                    f"未定义的事件模板：{action}（可用：{', '.join(self._templates)}）"
                )
            return dict(spec)

    # --- 渲染 ---
    def render(self, action: str, ctx: dict[str, Any]) -> RenderedMessage:
        """渲染一条消息。

        ★ `StrictUndefined`：模板里写了 `{{ pnl }}` 而调用方忘了传 →
          **直接报错**，而不是渲染成空字符串。空字符串会让消息"发成功但缺内容"，
          这是最难发现的一类失效。
        """
        with self._lock:
            self._load_if_changed()
            spec = self._templates.get(action)
            if spec is None:
                raise AlertError(
                    f"未定义的事件模板：{action}（可用：{', '.join(self._templates)}）"
                )
            keyword = self._required_keyword

        # ★ jinja2 的 `%.2f|format` 遇到 None 会抛 TypeError（不是 TemplateError），
        #   所以这里统一把 None 变成 0.0 —— 消费方的责任是**不传 None**，
        #   这里只是最后一道防线（真出了脏数据也不该让整批派发崩掉）。
        safe_ctx = {k: ("" if v is None else v) for k, v in ctx.items()}
        try:
            title = self._env.from_string(spec["title"]).render(**safe_ctx).strip()
            body = self._env.from_string(spec["body"]).render(**safe_ctx).strip()
        except (TemplateError, TypeError, ValueError) as exc:
            raise AlertError(
                f"提醒模板渲染失败（{action}）：{exc}。"
                f"请检查模板变量与调用方传参是否一致"
            ) from exc

        # ★ 关键词兜底：企微「自定义关键词」安全设置下，消息不含关键词会被
        #   **静默丢弃**（返回 errcode=0 但收不到）。这里主动校验并补上，
        #   宁可标题多几个字，也不要"发送成功却收不到"。
        if keyword and keyword not in title and keyword not in body:
            logger.warning(
                f"[alert] 模板 {action} 的标题缺少安全关键词「{keyword}」，已自动补到标题前"
            )
            title = f"{keyword} · {title}"

        return RenderedMessage(
            action=action,
            title=title,
            content=body,
            level=spec["level"] if spec["level"] in LEVELS else "info",
        )


#: 全局模板仓
templates = TemplateStore()


# ======================================================================
# 重试执行器
# ======================================================================
#: 后台重试线程池。为什么是 4：并发派发的场景只有"一次跑批里的几笔交易"，
#: 4 条足够，且不会把企微限流器压爆。
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="alert-retry")

#: 记录在飞的重试任务，便于测试等待与优雅关闭
_inflight: set[int] = set()
_inflight_lock = threading.Lock()


def _backoff_schedule() -> list[int]:
    """退避序列（秒）。取配置，做健壮性清洗。"""
    raw = list(settings.alert.retry_backoff or [1, 5, 30])
    max_retry = max(0, int(settings.alert.max_retry))
    if max_retry <= 0:
        return []
    clean = [max(0, int(x)) for x in raw][:max_retry]
    while len(clean) < max_retry:            # 配置写短了就用最后一个值补
        clean.append(clean[-1] if clean else 1)
    return clean


def _send_with_retry(
    alert_id: int,
    text: str,
    *,
    action: str,
    sleep: Callable[[float], None] = time.sleep,
) -> SendResult:
    """同步首试 + 退避重试，全程更新 alert_log 状态。

    ★ 只有 `retryable=True` 的失败才继续重试。对 60020（IP 未白名单）、
      82001（接收人无效）这类配置错误重试 36 秒毫无意义，只会让用户
      以为"在努力"。立刻标 failed，让 UI 给出修复提示。
    """
    delays = _backoff_schedule()
    attempts = len(delays) + 1          # 首试 + 重试
    last_result: SendResult | None = None

    def _mark(status: str, *, result: SendResult, attempt: int) -> None:
        """写状态。`retry_count` = 已消耗的**重试**次数（首试不算）。

        ★ 为什么用绝对赋值而不是 `bump_retry`：bump 是相对语义，
          在循环的多个位置各调一次就会算错（实测把 3 次重试记成 4 次）。
          `attempt` 是循环变量，天然就是唯一真相，写它不会错。
        """
        meta_repo.alert_mark(
            alert_id,
            status,
            error_message="" if result.ok else result.message,
            retry_count=attempt,
        )

    for attempt in range(attempts):
        result = wecom_client.send_markdown(text)
        last_result = result

        if result.ok:
            _mark("sent", result=result, attempt=attempt)
            if attempt:
                logger.info(f"[alert] #{alert_id} 第 {attempt + 1} 次尝试成功")
            return result

        # 永久失败 / 已到末次 → 落 failed，退出
        is_last = attempt == attempts - 1
        if not result.retryable or is_last:
            _mark("failed", result=result, attempt=attempt)
            reason = "不可重试" if not result.retryable else "重试已用尽"
            logger.warning(
                f"[alert] #{alert_id}（{action}）发送失败"
                f"（共尝试 {attempt + 1} 次，{reason}）：{result.message}"
            )
            return result

        # 还要重试：状态留在 pending（确实还没成功），记下已消耗的重试次数
        _mark("pending", result=result, attempt=attempt)
        nap = delays[attempt]
        logger.info(
            f"[alert] #{alert_id} 第 {attempt + 1} 次失败，{nap}s 后重试：{result.message}"
        )
        sleep(nap)

    # 理论上到不了（循环末尾一定 return），兜底
    return last_result or SendResult(ok=False, message="未知错误", retryable=False)


def _schedule_retry(alert_id: int, text: str, *, action: str) -> None:
    """把带重试的发送甩到后台线程。

    ⚠️ 用 `_executor.submit` 而不是 `threading.Thread` —— 后者每次新建线程，
      密集调仓时会瞬间起一堆线程去抢限流锁。
    """
    with _inflight_lock:
        _inflight.add(alert_id)

    def _run() -> None:
        try:
            _send_with_retry(alert_id, text, action=action)
        except Exception as exc:
            logger.exception(f"[alert] #{alert_id} 后台重试线程异常：{exc!r}")
            try:
                meta_repo.alert_mark(alert_id, "failed", error_message=f"重试线程异常：{exc}")
            except Exception:
                logger.error(f"[alert] #{alert_id} 状态回写也失败了")
        finally:
            with _inflight_lock:
                _inflight.discard(alert_id)

    _executor.submit(_run)


def wait_for_retries(timeout: float = 5.0) -> None:
    """等所有后台重试结束（**测试专用**）。

    ★ 不这么做的话，测试会在重试线程还在跑的时候断言 alert_log 状态，
      拿到 `pending` 就以为坏了 —— 那是竞态，不是 bug。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with _inflight_lock:
            if not _inflight:
                return
        time.sleep(0.02)


def inflight_count() -> int:
    with _inflight_lock:
        return len(_inflight)


# ======================================================================
# 派发（业务层唯一入口）
# ======================================================================
def dispatch(msg: RenderedMessage, *, sync: bool = True) -> dict[str, Any]:
    """派发一条已渲染的消息：幂等 → 落库 → 发送 → 重试。

    Args:
        msg: 渲染好的消息。
        sync: True（默认）同步完成首试；False 则整个发送都丢后台
              （用于"一次跑批几十条消息"的批量场景，避免阻塞跑批线程）。

    Returns:
        结构化结果：`{dispatched, alert_id, status, message, errcode, skipped}`。
        **不抛异常** —— 提醒失败不该让跑批失败。
    """
    if not settings.alert.enabled:
        return {
            "dispatched": False,
            "ok": False,
            "skipped": True,
            "reason": "提醒功能未开启（config.yaml → alert.enabled=false）",
        }

    if not msg.event_key:
        raise AlertError(f"事件 {msg.action} 缺少 event_key，无法保证幂等")
    if not msg.full_text.strip():
        return {
            "dispatched": False,
            "ok": False,
            "skipped": True,
            "reason": "消息内容为空，已跳过",
        }

    # --- 1. 幂等 ---
    if meta_repo.alert_exists(msg.event_key):
        logger.debug(f"[alert] 事件已推送过，跳过：{msg.event_key}")
        return {
            "dispatched": False,
            "ok": False,
            "skipped": True,
            "reason": f"事件已推送过（{msg.event_key}）",
        }

    text = msg.full_text

    # --- 2. 落库 pending ---
    alert_id = meta_repo.alert_insert(
        msg.event_key,
        msg.action,
        msg.title,
        text,
        account_id=msg.account_id,
        symbol=msg.symbol,
        level=msg.level,
        channel=wecom_client.channel,
    )
    if alert_id is None:
        # UNIQUE 冲突 —— 并发派发同一事件，另一路已经插进去了
        return {
            "dispatched": False,
            "ok": False,
            "skipped": True,
            "reason": "并发派发，已由另一路处理",
        }

    # --- 3. 发送 ---
    if not sync:
        _schedule_retry(alert_id, text, action=msg.action)
        return {
            "dispatched": True,
            "alert_id": alert_id,
            "status": "pending",
            "ok": True,
            "message": "已入队，后台发送中",
        }

    result = _send_with_retry(alert_id, text, action=msg.action)
    return {
        "dispatched": True,
        "alert_id": alert_id,
        "status": "sent" if result.ok else "failed",
        "ok": result.ok,
        "errcode": result.errcode,
        "message": result.message,
        "channel": result.channel,
        "waited": round(result.waited, 3),
    }


def dispatch_event(
    action: str,
    ctx: dict[str, Any],
    *,
    event_key: str,
    account_id: str | None = None,
    symbol: str | None = None,
    level: str | None = None,
    sync: bool = True,
) -> dict[str, Any]:
    """渲染 + 派发（常用组合的语法糖）。

    提醒链路上的任何渲染异常都**不向外抛**（除参数错误）—— 提醒是附属功能，
    绝不能因为它挂了让跑批/回测失败。调用方看返回值的 `ok` 字段即可。
    """
    if not settings.alert.enabled:
        return {
            "dispatched": False,
            "ok": False,
            "skipped": True,
            "reason": "提醒功能未开启（config.yaml → alert.enabled=false）",
        }

    try:
        msg = templates.render(action, ctx)
    except AlertError as exc:
        logger.error(f"[alert] 渲染失败（{action}）：{exc.message}")
        return {
            "dispatched": False,
            "ok": False,
            "reason": exc.message,
            "action": action,
            "skipped": False,
        }

    msg.event_key = event_key
    msg.account_id = account_id
    msg.symbol = symbol
    if level:
        msg.level = level

    return dispatch(msg, sync=sync)


# ======================================================================
# 事件构造：测试消息
# ======================================================================
def send_test(*, operator: str = "手动测试") -> dict[str, Any]:
    """发一条链路测试消息（**故意不去重**，每次点都要真的发）。"""
    now = datetime.now()
    env = settings.env
    masked = env.wecom_webhook_key_masked
    if not masked and env.wecom_corp_id:
        masked = env.wecom_corp_id[:6] + "***"

    return dispatch_event(
        "test",
        {
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%Y-%m-%d %H:%M:%S"),
            "channel": wecom_client.channel,
            "channel_cn": (
                "群机器人（方案 A）" if wecom_client.channel == "webhook" else "自建应用（方案 B）"
            ),
            "key_masked": masked or "未配置",
            "app_version": settings.app.version,
            "env": env.app_env,
            "operator": operator,
        },
        # 带时间戳 → 永不重复，每次点都发得出去
        event_key=f"manual|test|{now.strftime('%Y%m%d%H%M%S%f')}",
    )


# ======================================================================
# 事件构造：交易事件（消费模块 4 的 orders + trades + nav）
# ======================================================================
def notify_trades(
    *,
    orders: pd.DataFrame | None,
    trades: pd.DataFrame | None,
    nav: pd.DataFrame | None,
    run_date: str,
    account_id: str,
    account_name: str,
    strategy_name: str,
    max_positions: int = 0,
    async_send: bool = True,
) -> dict[str, Any]:
    """把 `run_date` 当天的成交派发成提醒。

    Args:
        orders: 引擎委托流水（含 `exec_date` / `side` / `status`）。
        trades: 已平仓交易（含 `net_pnl` / `net_return` / `exit_reason`）。
        nav: 净值曲线（补 `nav` / `nav_pct` / `position_count`）。
        run_date: 逻辑运行日 —— **只挑这一天成交的**。
        async_send: True 则 N 条消息并发入队；False 则逐条同步发。
                   跑批场景用 True（别把跑批线程卡在退避睡眠里）。

    Returns:
        `{opened, closed, sent, failed, skipped, reason?}` —— 供跑批日志记录。
    """
    stats: dict[str, Any] = {"opened": 0, "closed": 0, "sent": 0, "failed": 0, "skipped": 0}
    if not settings.alert.enabled:
        stats["reason"] = "提醒功能未开启"
        return stats

    # --- 1. 挑出当天已成交的委托 ---
    if orders is None or orders.empty:
        return stats
    required = {"exec_date", "side", "status", "symbol"}
    if not required.issubset(set(orders.columns)):
        stats["reason"] = f"orders 缺少列 {sorted(required - set(orders.columns))}"
        logger.warning(f"[alert] {stats['reason']}，跳过交易提醒")
        return stats

    today = orders[
        (orders["exec_date"].astype(str).str[:10] == run_date)
        & (orders["status"].astype(str) == "filled")
    ]
    if today.empty:
        return stats

    # --- 2. 备齐上下文：名称 / 净值 / 当日盈亏 ---
    names = _name_map()
    nav_row = _nav_row(nav, run_date)
    trade_index = _trade_index(trades, run_date)
    max_pos = int(max_positions or settings.strategy.max_positions)

    # --- 3. 逐条渲染 + 派发 ---
    for row in today.to_dict("records"):
        side = str(row.get("side") or "")
        symbol = str(row.get("symbol") or "")
        name = names.get(symbol, symbol)

        if side == "sell":
            rec = trade_index.get(symbol)
            if rec is None:
                # ★ 数字不全宁可不发：推"+0.00 元盈亏"比不推更坏
                stats["skipped"] += 1
                logger.warning(
                    f"[alert] 清仓 {symbol} 在 trades 里找不到对应记录，"
                    f"为避免推送假数字已跳过（{run_date}）"
                )
                continue
            ctx = _close_ctx(
                rec=rec,
                symbol=symbol,
                name=name,
                run_date=run_date,
                nav_row=nav_row,
                account_name=account_name,
                strategy_name=strategy_name,
            )
            action = "close_position"
            stats["closed"] += 1
        elif side == "buy":
            ctx = _open_ctx(
                row=row,
                symbol=symbol,
                name=name,
                run_date=run_date,
                nav_row=nav_row,
                trades=trades,
                account_name=account_name,
                strategy_name=strategy_name,
                max_positions=max_pos,
            )
            action = "open_position"
            if ctx is None:
                stats["skipped"] += 1
                continue
            stats["opened"] += 1
        else:
            continue

        result = dispatch_event(
            action,
            ctx,
            event_key=f"{run_date}|{account_id}|{symbol}|{action}",
            account_id=account_id,
            symbol=symbol,
            sync=not async_send,
        )
        if result.get("ok"):
            stats["sent"] += 1
        elif result.get("skipped"):
            stats["skipped"] += 1
        else:
            stats["failed"] += 1
            logger.warning(f"[alert] {action} {symbol} 派发失败：{result.get('message')}")

    if stats["sent"] or stats["failed"]:
        logger.info(
            f"[alert] {run_date} 交易提醒派发完成："
            f"开仓 {stats['opened']} / 清仓 {stats['closed']}，"
            f"成功 {stats['sent']} / 失败 {stats['failed']} / 跳过 {stats['skipped']}"
        )
    return stats


def _nav_row(nav: pd.DataFrame | None, run_date: str) -> dict[str, Any]:
    """取 `run_date` 当天的净值行（缺则空 dict，模板用默认 0 渲染）。"""
    if nav is None or nav.empty or "date" not in nav.columns:
        return {}
    rows = nav[nav["date"].astype(str).str[:10] == run_date]
    return rows.iloc[-1].to_dict() if not rows.empty else {}


def _trade_index(trades: pd.DataFrame | None, run_date: str) -> dict[str, dict[str, Any]]:
    """`symbol -> 当天平仓的交易记录`。

    ⚠️ 理论上同一标的同日只可能有一笔平仓（有 `no_rebuy_days=60` 禁买窗口），
       所以用 symbol 做键是安全的。真出现重复时取最后一笔并记 warning。
    """
    if trades is None or trades.empty or "exit_date" not in trades.columns:
        return {}
    today = trades[trades["exit_date"].astype(str).str[:10] == run_date]
    index: dict[str, dict[str, Any]] = {}
    for rec in today.to_dict("records"):
        sym = str(rec.get("symbol") or "")
        if sym in index:
            logger.warning(f"[alert] {sym} 在 {run_date} 有多笔平仓记录，取最后一笔用于提醒")
        index[sym] = rec
    return index


def _open_ctx(
    *,
    row: dict[str, Any],
    symbol: str,
    name: str,
    run_date: str,
    nav_row: dict[str, Any],
    trades: pd.DataFrame | None,
    account_name: str,
    strategy_name: str,
    max_positions: int,
) -> dict[str, Any] | None:
    """开仓上下文。费用从 trades 反查（orders 流水不含费用）。"""
    price = _f(row.get("price"))
    shares = _i(row.get("shares"))
    amount = _f(row.get("amount")) or price * shares

    # 开仓费用：这笔买入最终平仓时会出现在 trades.entry_cost 里，这里反查
    entry_cost = 0.0
    if trades is not None and not trades.empty and "entry_cost" in trades.columns:
        hit = trades[
            (trades["symbol"].astype(str) == symbol)
            & (trades["entry_date"].astype(str).str[:10] == run_date)
        ]
        if not hit.empty:
            entry_cost = _f(hit.iloc[-1].get("entry_cost"))

    return {
        "strategy_name": strategy_name,
        "account_name": account_name,
        "date": run_date,
        "symbol": symbol,
        "name": name,
        "price": price,
        "shares": shares,
        "amount": amount,
        "cost": entry_cost,
        "reason": str(row.get("reason") or ""),
        "position_count": int(nav_row.get("position_count") or 0),
        "max_positions": max_positions,
    }


def _close_ctx(
    *,
    rec: dict[str, Any],
    symbol: str,
    name: str,
    run_date: str,
    nav_row: dict[str, Any],
    account_name: str,
    strategy_name: str,
) -> dict[str, Any]:
    """清仓上下文 —— 数字全部来自 trades（真实盈亏）+ nav（真实净值）。"""
    exit_reason = str(rec.get("exit_reason") or "")
    nav_value = _f(nav_row.get("nav"))
    return {
        "strategy_name": strategy_name,
        "account_name": account_name,
        "date": run_date,
        "symbol": symbol,
        "name": name,
        "entry_date": str(rec.get("entry_date") or "")[:10],
        "entry_price": _f(rec.get("entry_price")),
        "exit_price": _f(rec.get("exit_price")),
        "shares": _i(rec.get("shares")),
        "net_pnl": _f(rec.get("net_pnl")),
        "net_return": _f(rec.get("net_return")),
        "cost": _f(rec.get("cost")),
        "hold_days": _i(rec.get("hold_days")),
        "exit_reason": exit_reason,
        "exit_reason_cn": EXIT_REASON_CN.get(exit_reason, exit_reason or "未知"),
        # nav 是"净值"（1.0 = 本金），nav_pct 是累计涨跌幅
        "nav": nav_value,
        "nav_pct": nav_value - 1.0 if nav_value else 0.0,
    }


def _f(value: Any) -> float:
    """安全转 float（NaN / None → 0.0）。"""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    return out if out == out else 0.0          # NaN 检测


def _i(value: Any) -> int:
    """安全转 int。"""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


# ======================================================================
# 事件构造：任务失败
# ======================================================================
def notify_job_failed(
    *,
    job_id: str,
    job_name: str,
    run_date: str,
    trigger_type: str,
    error: BaseException | str,
    duration: float = 0.0,
) -> dict[str, Any]:
    """任务失败告警（由 `scheduler/jobs.py::_run_with_log` 调用）。

    ★ 这个钩子是模块 5 最有价值的消费场景 —— 「跑批挂了你还不知道」
      是本项目最大的运维盲区：日志里写了 failed，但没人会天天翻日志。
    """
    err_type = type(error).__name__ if isinstance(error, BaseException) else "Error"
    err_msg = getattr(error, "message", None) or str(error)
    now = datetime.now()

    return dispatch_event(
        "job_failed",
        {
            "job_id": job_id,
            "job_name": job_name or job_id,
            "run_date": run_date,
            "trigger_type": trigger_type,
            "error_type": err_type,
            "error_message": str(err_msg)[:500],
            "duration": float(duration),
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%Y-%m-%d %H:%M:%S"),
        },
        # 同一任务同一天失败多次只推第一条（后几条大概率是同一个根因）
        event_key=f"{run_date}|job|{job_id}|failed",
        level="error",
    )


# ======================================================================
# 事件构造：每日早报（08:00「今日持仓」）
# ======================================================================
def notify_daily_summary(
    *,
    account_id: str | None = None,
    account_name: str | None = None,
    strategy_name: str | None = None,
    data_date: str | None = None,
    sync: bool = True,
) -> dict[str, Any]:
    """发一条「今日持仓」早报（交易日 08:00）。

    ★ 数据口径（主人 2026-09-20 拍板，方案 A）
    ------------------------------------------
    数据源 = `data/paper/{account}/positions.parquet` 的**最新交易日快照**
    + `nav.parquet` 的总资产 / 现金 / 净值。

    **为什么不用 `orders.parquet` 的「计划单」**（我原以为是它，实测推翻）：
    引擎在 T 日收盘挂单只写进内存 `self.pending`，**不落 orders 流水**；
    orders 里出现的 pending 全是"无法成交被顺延"的**历史僵尸单**
    （实测 7 条：2025-01-13 跌停卖不出、2025-08-19 停牌买不进、
    2026-07-13 跌停卖不出），而**最后一天挂出的单反而被记成
    `rejected / 回测结束，未及成交`**。
    → 拿 orders 取"明日计划"会把五个月前的僵尸单推出去，是错误口径。

    **positions 反而是对的**：23:00 跑批是「全量重算到最新交易日」，
    T+1 开盘的成交**已经计入**，所以 08:00 看到的持仓就是今天开盘后的
    确定仓位 —— 不是预测，是已经发生的事实。

    Args:
        account_id: 模拟账户 id，默认取 `settings.paper.default_account_id`。
        account_name: 账户显示名（缺则从账户行取）。
        strategy_name: 策略名（缺则从账户行取）。
        data_date: 数据日期覆盖（默认用 products 里最新日期）。测试与补发用。
        sync: True 同步返回结果；False 入队后台。

    Returns:
        `{dispatched, ok, status, ...}`；无产物 / 未开启时返回
        `{dispatched: False, skipped: True, reason}`。

    ⚠️ **本函数绝不抛异常** —— 它是定时任务里跑的，抛了会把 job 记成 failed，
       而"早报没发出去"不该被当成"跑批失败"。
    """
    if not settings.alert.enabled:
        return {"dispatched": False, "ok": False, "skipped": True, "reason": "提醒功能未开启"}

    try:
        ctx = _daily_summary_ctx(
            account_id=account_id,
            account_name=account_name,
            strategy_name=strategy_name,
            data_date=data_date,
        )
    except Exception as exc:
        logger.warning(f"[alert] 每日早报上下文构造失败，已跳过：{exc!r}")
        return {"dispatched": False, "ok": False, "skipped": True, "reason": f"上下文构造失败：{exc!r}"}

    if ctx is None:
        return {
            "dispatched": False,
            "ok": False,
            "skipped": True,
            "reason": "模拟盘暂无产物（positions/nav 缺失），无法生成早报",
        }

    date_key = str(ctx["date"])
    return dispatch_event(
        "daily_summary",
        ctx,
        # 幂等键：同一天只发一条。补跑/重启不会重复轰炸
        event_key=f"{date_key}|daily_summary",
        account_id=str(account_id or settings.paper.default_account_id),
        level="info",
        sync=sync,
    )


def _daily_summary_ctx(
    *,
    account_id: str | None = None,
    account_name: str | None = None,
    strategy_name: str | None = None,
    data_date: str | None = None,
) -> dict[str, Any] | None:
    """构造早报渲染上下文。无产物返回 None。

    ⚠️ 严格只用**数据日期当天**的快照（`positions.parquet` 里
      `date == max(date)` 的那批），不做跨日合并 —— 否则会混入已清仓标的。
    """
    from app.services import paper_service  # noqa: PLC0415 - 避免模块级循环导入

    aid = str(account_id or settings.paper.default_account_id)

    # 账户行：拿名字与策略（调用方没传时才查）
    acct: dict[str, Any] = {}
    if account_name is None or strategy_name is None:
        try:
            from app.repos import meta_repo  # noqa: PLC0415

            acct = dict(meta_repo.get_paper_account(aid) or {})
        except Exception as exc:
            logger.debug(f"[alert] 读取模拟账户失败（用默认名）：{exc!r}")
    acc_name = account_name or str(acct.get("name") or settings.paper.default_account_name)
    strat = strategy_name or str(acct.get("strategy") or "momentum")

    # 复用 paper_service 的读盘入口，不另写一份 read_parquet ——
    # 否则「产物在哪、列怎么洗」就有了两处定义（本项目反复踩的口径分叉）。
    # 代价是跨模块用了私有名：它没有公开的只读替代品，且同属 services 包。
    artifacts = paper_service._load_artifacts(aid)
    nav_df: pd.DataFrame = artifacts["nav"]
    pos_df: pd.DataFrame = artifacts["positions"]
    cfg: dict[str, Any] | None = artifacts["config"]

    if nav_df is None or nav_df.empty:
        return None

    nav_df = nav_df.sort_values("date")
    last_nav = nav_df.iloc[-1]
    date_key = str(data_date or last_nav.get("date") or "")[:10]
    # 取数据日期那一行净值；若指定了 data_date 而它不在曲线里，回退最后一行
    nav_rows = nav_df[nav_df["date"].astype(str).str[:10] == date_key]
    if nav_rows.empty:
        nav_rows = nav_df.tail(1)
        date_key = str(nav_rows.iloc[-1].get("date") or "")[:10]
    nav_row = nav_rows.iloc[-1]

    total_asset = _f(nav_row.get("total_asset"))
    cash = _f(nav_row.get("cash"))
    market_value = _f(nav_row.get("market_value"))
    nav_value = _f(nav_row.get("nav"))
    drawdown = _f(nav_row.get("drawdown"))
    max_pos = int(settings.strategy.max_positions)
    if cfg and cfg.get("params"):
        max_pos = _i(cfg["params"].get("max_positions")) or max_pos

    # 当天持仓快照（严格同日，不 ffill）
    holdings: list[dict[str, Any]] = []
    if pos_df is not None and not pos_df.empty and "date" in pos_df.columns:
        same_day = pos_df[pos_df["date"].astype(str).str[:10] == date_key]
        holdings = same_day.to_dict("records")

    names = _name_map() if holdings else {}

    if not holdings:
        # 空仓也照发（主人拍板）：写「今日无开仓」
        return {
            "date": date_key,
            "account_name": acc_name,
            "strategy_name": strat,
            "has_position": False,
            "holdings_text": "> - <font color=\"comment\">无</font>",
            "position_summary": "<font color=\"comment\">今日无开仓</font>",
            "position_ratio_text": "<font color=\"comment\">0.0%（空仓）</font>",
            "position_count": 0,
            "max_positions": max_pos,
            "position_ratio": 0.0,
            "cash_ratio": 1.0 if total_asset else 0.0,
            "total_asset": total_asset,
            "cash": cash,
            "nav": nav_value,
            "nav_pct": nav_value - 1.0 if nav_value else 0.0,
            "unrealized_pnl": 0.0,
            "drawdown": drawdown,
        }

    # 按市值降序（与前端持仓表同序）
    holdings.sort(key=lambda r: -_f(r.get("market_value")))

    lines: list[str] = []
    parts: list[str] = []
    for rec in holdings:
        sym = str(rec.get("symbol") or "")
        nm = names.get(sym, sym)
        mv = _f(rec.get("market_value"))
        pnl = _f(rec.get("unrealized_pnl"))
        ratio = (mv / total_asset) if total_asset else 0.0
        parts.append(f"{nm}（{sym}）")
        # 红涨绿跌（中国市场）：浮盈 warning 橙红 / 浮亏 info 绿
        lines.append(
            f"> - {nm}（{sym}）<font color=\"warning\">{ratio * 100:.1f}%</font>"
            f"　市值 {mv:,.0f} 元"
            f"　<font color=\"{'warning' if pnl >= 0 else 'info'}\">{pnl:+,.0f} 元</font>"
        )

    unrealized = sum(_f(r.get("unrealized_pnl")) for r in holdings)
    # 总仓位优先用 nav 的 market_value（与页面口径一致）；缺则按持仓市值加总
    mv_total = market_value or sum(_f(r.get("market_value")) for r in holdings)
    pos_ratio = (mv_total / total_asset) if total_asset else 0.0
    cash_ratio = (cash / total_asset) if total_asset else 0.0

    return {
        "date": date_key,
        "account_name": acc_name,
        "strategy_name": strat,
        "has_position": True,
        "holdings_text": "\n".join(lines),
        "position_summary": "、".join(parts),
        # 文案在**代码里**拼好整段：模板只做行内插值，避免 Jinja 块标签
        # 吃掉换行导致两行引用挤成一行（见模板注释「排版铁律」）
        "position_ratio_text": (
            f"总 <font color=\"warning\">**{pos_ratio * 100:.1f}%**</font>"
            f"（{len(holdings)}/{max_pos} 只）"
            f"　现金 {cash_ratio * 100:.1f}%"
        ),
        "position_count": len(holdings),
        "max_positions": max_pos,
        "position_ratio": pos_ratio,
        "cash_ratio": cash_ratio,
        "total_asset": total_asset,
        "cash": cash,
        "nav": nav_value,
        "nav_pct": nav_value - 1.0 if nav_value else 0.0,
        "unrealized_pnl": unrealized,
        "drawdown": drawdown,
    }


# ======================================================================
# 名称解析（带一天缓存）
# ======================================================================
_name_cache: dict[str, Any] = {"date": "", "map": {}}
_name_lock = threading.Lock()


def _name_map() -> dict[str, str]:
    """`symbol -> 股票名称`（当日全市场清单，缓存 1 天）。

    ★ 已知局限：baostock 的 `code_name` 是**当前名称**而非历史名称
      （实测 4220 只共同股票在 2020 与 2024 的名称差异为 0 处）。
      用来推送"今天发生的事"没问题 —— 这里的语义就是"现在叫什么"。
      它**不能**用来重建历史 ST 状态（那必须走日线的 isST 字段，
      见 `baostock_client.fetch_all_stock` 的文档）。

    🔴 **必须回退到最近交易日**（2026-09-20 实测修）：`fetch_all_stock()`
      不传日期时用"今天"，而 baostock 在**非交易日返回 0 行**
      （实测 09-20 周日 = 0 行、09-18 周五 = 7401 行）。
      周末跑通链路测试、或跑批顺延到次日补跑时，名称会**静默全丢**，
      消息退回股票代码 —— 正是主人明确要求「要带股票名称」的那一项。
      回退链路：今天 → 日历里的最后一个已发生交易日 → 无则放弃（退回代码）。

    ⚠️ 取不到时返回空 dict，消息里退回代码 —— **绝不阻塞派发**。
      提醒链路的网络依赖必须是"失败了也能发出去"。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    with _name_lock:
        if _name_cache["date"] == today and _name_cache["map"]:
            return dict(_name_cache["map"])

    mapping: dict[str, str] = {}
    try:
        from app.services.data_source.baostock_client import baostock_client  # noqa: PLC0415

        # 今天 → 最近交易日，逐个试；非交易日 baostock 会返回空表
        for probe in _name_probe_days(today):
            df = baostock_client.fetch_all_stock(probe)
            if df is not None and not df.empty:
                mapping = {str(r["code"]): str(r["name"]) for r in df.to_dict("records")}
                if probe != today:
                    logger.info(f"[alert] 非交易日，股票名称取自最近交易日 {probe}（{len(mapping)} 条）")
                break
    except Exception as exc:
        logger.warning(f"[alert] 股票名称清单获取失败，消息将显示代码：{exc!r}")
        mapping = {}

    if mapping:
        with _name_lock:
            _name_cache["date"] = today
            _name_cache["map"] = mapping
    return mapping


def _name_probe_days(today: str) -> list[str]:
    """探测用日期序列：`[今天, 最近交易日]`（去重）。

    baostock 的 `query_all_stock` 在**非交易日返回空表**，所以周末/节假日
    必须回退。最近交易日取日历服务里最后一个已发生交易日
    （与 `paper_service._latest_trading_day` 同一来源，避免两处口径）。
    """
    days = [today]
    try:
        from app.services.calendar_service import calendar  # noqa: PLC0415

        stats = calendar.stats()
        last = stats.get("last_date") or stats.get("latest_trading_day")
        if last:
            last_s = str(last)[:10]
            if last_s not in days:
                days.append(last_s)
    except Exception as exc:
        logger.debug(f"[alert] 读取最近交易日失败，名称仅按今天探测：{exc!r}")
    return days


def clear_name_cache() -> None:
    """清空名称缓存（测试用）。"""
    with _name_lock:
        _name_cache["date"] = ""
        _name_cache["map"] = {}


# ======================================================================
# 手动重发
# ======================================================================
def resend(alert_id: int) -> dict[str, Any]:
    """重发一条 failed 记录（UI 上的「重发」按钮）。

    ★ 不重新渲染 —— 内容已在库里了。理由：日志列表展示的就是那条 message，
      重发后内容必须与展示的完全一致，否则"我看着这条重发的，收到的是另一条"
      会让人怀疑整个系统。
    """
    rows = meta_repo.db.query("SELECT * FROM alert_log WHERE id = ? LIMIT 1", (alert_id,))
    if not rows:
        raise AlertError(f"提醒记录 #{alert_id} 不存在")
    row = dict(rows[0])

    status = str(row.get("status") or "")
    if status == "sent":
        return {"ok": True, "status": "sent", "alert_id": alert_id,
                "message": "该消息已发送成功，无需重发"}
    if status == "pending":
        return {"ok": False, "status": "pending", "alert_id": alert_id,
                "message": "该消息仍在发送中，请稍后刷新"}

    text = str(row.get("content") or "")
    if not text.strip():
        raise AlertError(f"提醒记录 #{alert_id} 的正文为空，无法重发")

    # 重置为 pending 再走完整重试链（重发就是要再发一次，不查 event_key）
    meta_repo.alert_mark(alert_id, "pending", error_message="")
    result = _send_with_retry(alert_id, text, action=str(row.get("action") or "resend"))
    return {
        "ok": result.ok,
        "status": "sent" if result.ok else "failed",
        "alert_id": alert_id,
        "errcode": result.errcode,
        "message": result.message,
        "channel": result.channel,
    }


# ======================================================================
# 只读查询
# ======================================================================
def list_logs(
    *,
    limit: int = 50,
    offset: int = 0,
    status: str | None = None,
    action: str | None = None,
) -> dict[str, Any]:
    """提醒日志（分页 + 筛选）。"""
    where: list[str] = []
    params: list[Any] = []
    if status:
        where.append("status = ?")
        params.append(status)
    if action:
        where.append("action = ?")
        params.append(action)
    clause = f"WHERE {' AND '.join(where)}" if where else ""

    # 子句由白名单字段（status/action）拼成，值走参数绑定 —— 无注入面
    count_rows = meta_repo.db.query(
        f"SELECT COUNT(*) AS n FROM alert_log {clause}", tuple(params)
    )
    total = int(count_rows[0]["n"]) if count_rows else 0

    rows = meta_repo.db.query(
        f"SELECT * FROM alert_log {clause} ORDER BY id DESC LIMIT ? OFFSET ?",
        (*params, limit, offset),
    )
    return {
        "total": total,
        "items": [_decorate(dict(r)) for r in rows],
        "limit": limit,
        "offset": offset,
        "status_counts": status_counts(),
    }


def status_counts() -> dict[str, int]:
    """各状态的条数（前端筛选标签上显示数字）。"""
    rows = meta_repo.db.query("SELECT status, COUNT(*) AS n FROM alert_log GROUP BY status")
    counts = {"pending": 0, "sent": 0, "failed": 0, "skipped": 0}
    for r in rows:
        counts[str(r["status"])] = int(r["n"])
    return counts


def _decorate(row: dict[str, Any]) -> dict[str, Any]:
    """给日志行补前端要用的派生字段（中文名由后端给，前端不硬编码）。"""
    action = str(row.get("action") or "")
    row["action_cn"] = ACTION_CN.get(action, action)
    row["level"] = str(row.get("level") or "info")
    return row


# ======================================================================
# 模板预览（前端展示用）
# ======================================================================
def preview_templates() -> list[dict[str, Any]]:
    """返回所有模板的原始定义 + 一份样例渲染（前端"模板预览"用）。

    ★ 样例渲染的价值：让主人**在不发真消息的前提下**看到模板长什么样。
      否则每改一次模板就得往群里发一次，很烦。
    """
    out: list[dict[str, Any]] = []
    for action in templates.actions():
        spec = templates.raw(action)
        try:
            sample = templates.render(action, _sample_context(action))
            preview = {"title": sample.title, "content": sample.full_text, "level": sample.level}
        except AlertError as exc:
            preview = {"title": "", "content": f"（样例渲染失败：{exc.message}）", "level": "info"}
        out.append(
            {
                "action": action,
                "action_cn": ACTION_CN.get(action, action),
                "title": spec["title"],
                "body": spec["body"],
                "level": spec["level"],
                "sample": preview,
            }
        )
    return out


def _sample_context(action: str) -> dict[str, Any]:
    """样例渲染上下文（覆盖所有模板变量）。"""
    now = datetime.now()
    return {
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%Y-%m-%d %H:%M:%S"),
        "account_name": settings.paper.default_account_name,
        "strategy_name": "momentum",
        "symbol": "sh.600519",
        "name": "贵州茅台",
        "shares": 100,
        "price": 1680.55,
        "amount": 168055.0,
        "cost": 47.05,
        "reason": "momentum",
        "position_count": 3,
        "max_positions": settings.strategy.max_positions,
        "entry_date": "2026-09-10",
        "entry_price": 1620.0,
        "exit_price": 1680.55,
        "net_pnl": 5963.4,
        "net_return": 0.0368,
        "hold_days": 5,
        "exit_reason": "take_profit",
        "exit_reason_cn": "止盈",
        "nav": 1.0234,
        "nav_pct": 0.0234,
        "drawdown": -0.0412,
        "total_asset": 1023400.0,
        "trade_count": 4,
        "open_count": 2,
        "close_count": 2,
        "net_pnl_today": 5963.4,
        # --- daily_summary（今日持仓早报）---
        "has_position": True,
        "holdings_text": (
            "> - 贵州茅台（sh.600519）<font color=\"warning\">19.2%</font>"
            "　市值 196,480 元　<font color=\"warning\">+2,410 元</font>\n"
            "> - 宁德时代（sz.300750）<font color=\"warning\">18.6%</font>"
            "　市值 190,220 元　<font color=\"info\">-3,180 元</font>"
        ),
        "position_summary": "贵州茅台（sh.600519）、宁德时代（sz.300750）",
        "position_ratio_text": (
            "总 <font color=\"warning\">**37.8%**</font>（2/5 只）　现金 62.2%"
        ),
        "position_ratio": 0.378,
        "cash_ratio": 0.622,
        "cash": 636_400.0,
        "unrealized_pnl": -770.0,
        "channel": wecom_client.channel,
        "channel_cn": (
            "群机器人（方案 A）" if wecom_client.channel == "webhook" else "自建应用（方案 B）"
        ),
        "key_masked": settings.env.wecom_webhook_key_masked or "未配置",
        "app_version": settings.app.version,
        "env": settings.env.app_env,
        "operator": "模板预览",
        "job_id": "paper_daily",
        "job_name": "模拟盘跑批",
        "run_date": now.strftime("%Y-%m-%d"),
        "trigger_type": "cron",
        "error_type": "DataNotReadyError",
        "error_message": "行情未就绪：sh.600519 缺少 2026-09-18 数据",
        "duration": 12.34,
    }


# ======================================================================
# 渠道自述
# ======================================================================
def channel_status() -> dict[str, Any]:
    """渠道状态（提醒页顶部状态卡）。"""
    info = wecom_client.describe()
    info["rate"] = wecom_client.rate_stats()
    info["templates_file"] = str(templates.path)
    info["template_count"] = len(templates.actions())
    info["required_keyword"] = templates.required_keyword
    return info


__all__ = [
    "ACTIONS",
    "ACTION_CN",
    "DEFAULT_LEVEL",
    "EXIT_REASON_CN",
    "LEVELS",
    "RenderedMessage",
    "TemplateStore",
    "channel_status",
    "clear_name_cache",
    "dispatch",
    "dispatch_event",
    "inflight_count",
    "list_logs",
    "notify_daily_summary",
    "notify_job_failed",
    "notify_trades",
    "preview_templates",
    "resend",
    "send_test",
    "status_counts",
    "templates",
    "wait_for_retries",
]
