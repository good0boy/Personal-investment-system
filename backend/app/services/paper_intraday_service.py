"""模拟盘盘中检查（14:45）—— 用 fuyao 实时快照价做**清仓预警**。

为什么要有它（主人 2026-10-09 指定）
------------------------------------
quality_reversal 的卖出口径是「T 日信号 → **T 日尾盘按收盘价**成交」：
引擎在当晚 23:00 才用完整日线数据判定并撮合，消息推出来时收盘已过、无法操作。
本服务在 **14:45** 用 fuyao 全市场快照的最新价**近似**当日收盘，对当前持仓
逐只评估退出条件，提前 15 分钟把"今晚尾盘会被清掉"的持仓推到企微。

判定口径（与 `quality_reversal._sell_side` 同一集条件，但**是预警不是成交**）：

    time_stop        持有交易日数 >= hold_days_max            —— 精确
    over_realized    最新价 / T-20 收盘 - 1 >= 阈值(0.30)      —— 近似（用最新价）
    quality_broken   profit_yoy < 0 或 revenue_yoy < 0        —— 精确（季频因子盘中不变）
    limit_down       最新价 <= 前收 × 0.9×1.01                —— 近似
    stop_loss 等     oosma10-base 配置下为关闭；若开启则按最新价近似
    st               本地 is_st（T-1 截面）                    —— 信息项

⚠️ 14:45 的判定与当晚 23:00 引擎重算可能不一致（尾盘最后 15 分钟价格会变）。
   **实际成交以引擎为准**；本消息的作用是让人在尾盘有 15 分钟操作窗口。
"""
from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any

import pandas as pd

from app.core.config import settings
from app.core.logging import logger
from app.services import data_store
from app.services.calendar_service import calendar
from app.services.data_source import symbols as sym_mod
from app.services.data_source.fuyao_client import FuyaoClient

#: 快照价/前收的候选列名（fuyao 返回结构未在客户端固化，做宽容解析）
_PRICE_COLS = ("last_price", "latest_price", "price", "close", "last")
_PRECLOSE_COLS = ("prev_close", "pre_close", "preclose", "yesterday_close", "last_close")

#: limit_down 判定容差（跌停价 = round(前收×0.9, 2)，快照价可能差一分）
_LIMIT_DOWN_TOL = 1.01


def _pick_col(df: pd.DataFrame, names: tuple[str, ...]) -> str | None:
    for n in names:
        for c in df.columns:
            if c.lower() == n.lower():
                return c
    return None


def _load_account(account_id: str | None) -> tuple[dict[str, Any] | None, Path | None]:
    """拿账户行 + 产物目录（最近一次跑批的持仓状态）。"""
    from app.services import paper_service  # noqa: PLC0415 - paper_service 较重，保持懒加载

    acc_id = account_id or settings.paper.default_account_id
    try:
        row = paper_service.require_account(acc_id)
    except Exception as exc:
        logger.warning(f"[intraday] 账户 {acc_id} 读取失败：{exc!r}")
        return None, None
    return row, paper_service.account_dir(acc_id)


def _current_holdings(pdir: Path) -> pd.DataFrame:
    """最新一天的持仓（positions.parquet 是逐日快照）。"""
    p = pdir / "positions.parquet"
    if not p.exists():
        return pd.DataFrame()
    df = pd.read_parquet(p)
    if df.empty:
        return df
    last = str(df["date"].max())[:10]
    return df[df["date"].astype(str).str[:10] == last].copy()


def _fuyao_prices(symbols: list[str]) -> tuple[dict[str, float], dict[str, float], str | None]:
    """fuyao 快照 → ({symbol: 最新价}, {symbol: 前收}, 错误信息)。"""
    ths = {sym_mod.to_thscode(s): s for s in symbols}
    client = FuyaoClient()
    raw = client.snapshot(thscodes=",".join(ths.keys()))
    if raw is None or raw.empty:
        return {}, {}, "fuyao snapshot 返回空"
    pcol = _pick_col(raw, _PRICE_COLS)
    if pcol is None:
        return {}, {}, f"fuyao snapshot 无法识别价格列（实际列：{list(raw.columns)}）"
    ccol = _pick_col(raw, _PRECLOSE_COLS)
    code_col = next((c for c in raw.columns if c.lower() in ("thscode", "code", "symbol")), None)
    if code_col is None:
        return {}, {}, f"fuyao snapshot 无法识别代码列（实际列：{list(raw.columns)}）"

    prices: dict[str, float] = {}
    precloses: dict[str, float] = {}
    for _, r in raw.iterrows():
        canon = ths.get(str(r[code_col]))
        if canon is None:
            continue
        try:
            prices[canon] = float(r[pcol])
        except (TypeError, ValueError):
            continue
        if ccol is not None:
            with contextlib.suppress(TypeError, ValueError):
                precloses[canon] = float(r[ccol])
    return prices, precloses, None


def _local_close(symbol: str, n_back: int) -> float | None:
    """本地日线：倒数第 n_back 个交易日的收盘价（1 = 最近本地交易日）。"""
    try:
        # ★ end_date 传 None（2026-10-11 修复）：显式给 "2099-12-31" 会超出
        #   `data_store` 的数据时间锁上界 → 抛 ReadUntilViolation → 被下面的
        #   `except` 吞成 None。于是 `_local_close` **恒返回 None**，
        #   「近 20 日累计兑现 >= 30%」这条主退出条件在 14:45 预警里永久失效，
        #   且与"文件缺失"完全不可区分、零日志。None 会自动收敛到
        #   min(时间锁, 今天)。
        q = data_store.read_quotes(symbol, "2020-01-01", None, columns=["date", "close"])
    except Exception as exc:
        logger.warning(f"[intraday] {symbol} 读本地收盘价失败：{exc!r}")
        return None
    if q.empty:
        return None
    closes = q["close"].dropna()
    if len(closes) < n_back:
        return None
    return float(closes.iloc[-n_back])


def _local_preclose(symbol: str) -> float | None:
    """本地最近交易日的收盘价（= 相对今日的"前收"近似；baostock 日线滞后一天）。"""
    return _local_close(symbol, 1)


def _factor_latest_lt_zero(symbol: str, name: str) -> bool | None:
    """基本面因子最新值是否 < 0（None = 不可用，不判）。"""
    path = data_store.factor_path(name, "fundamental")
    if not path.exists():
        return None
    try:
        df = data_store.query_duck(
            f"SELECT value FROM read_parquet('{path.as_posix()}') "
            f"WHERE symbol = ? ORDER BY date DESC LIMIT 1",
            [symbol],
        )
    except Exception:
        return None
    if df.empty or df["value"].iloc[0] is None:
        return None
    return float(df["value"].iloc[0]) < 0


def _hold_days(entry_date: str, today: str) -> int:
    """持有交易日数（含首尾，与引擎 hold_days 口径一致）。"""
    try:
        days = calendar.trading_days(entry_date, today)
        return len(days)
    except Exception:
        return -1  # 日历异常 → 时间条件不可判，宁漏报不误报


def evaluate_holdings(
    holdings: pd.DataFrame, today: str, params: dict[str, Any]
) -> list[dict[str, Any]]:
    """对每只持仓评估清仓条件，返回预警明细（空列表 = 无预警）。"""
    hold_max = int(params.get("hold_days_max", 30))
    r20_max = float(params.get("realized_ret20_max", 0.30))
    sl_mult = float(params.get("stop_loss_p_mult", 0.0) or 0.0)
    tp_mult = float(params.get("take_profit_p_mult", 0.0) or 0.0)

    symbols = holdings["symbol"].astype(str).tolist()
    prices, precloses, err = _fuyao_prices(symbols)
    if err:
        raise RuntimeError(f"fuyao 快照不可用：{err}")

    alerts: list[dict[str, Any]] = []
    for _, row in holdings.iterrows():
        sym = str(row["symbol"])
        price = prices.get(sym)
        if price is None or price <= 0:
            continue
        reasons: list[str] = []

        entry_date = str(row.get("entry_date") or "")[:10]
        hd = _hold_days(entry_date, today) if entry_date else -1
        if hd >= 0 and hd >= hold_max:
            reasons.append(f"time_stop（已持有 {hd} >= {hold_max} 个交易日）")

        c20 = _local_close(sym, 21)          # T-20 收盘（本地最近 21 根的第 1 根）
        if c20 and c20 > 0 and price / c20 - 1.0 >= r20_max:
            reasons.append(f"over_realized（近20日累计 {price / c20 - 1.0:+.1%} >= {r20_max:.0%}）")

        p_neg = _factor_latest_lt_zero(sym, "profit_yoy")
        r_neg = _factor_latest_lt_zero(sym, "revenue_yoy")
        if p_neg or r_neg:
            reasons.append("quality_broken（基本面转负）")

        pc = precloses.get(sym) or _local_preclose(sym)
        if pc and pc > 0 and price <= pc * 0.9 * _LIMIT_DOWN_TOL:
            reasons.append(f"limit_down（最新价 {price:.2f} 逼近跌停，前收 {pc:.2f}）")

        entry_price = float(row.get("entry_price") or 0)
        if entry_price > 0 and sl_mult > 0:
            gross = price / entry_price - 1.0
            if gross <= -abs(sl_mult) * 0.0225:  # p 的样本中位 ~2.25%，预警用近似
                reasons.append(f"stop_loss（毛收益 {gross:+.1%} 触及止损带）")
        if entry_price > 0 and tp_mult > 0:
            gross = price / entry_price - 1.0
            if gross >= abs(tp_mult) * 0.0225:
                reasons.append(f"take_profit（毛收益 {gross:+.1%} 触及止盈带）")

        if reasons:
            alerts.append({
                "symbol": sym,
                "entry_date": entry_date,
                "entry_price": entry_price,
                "live_price": price,
                "reasons": "；".join(reasons),
            })
    return alerts


def run_intraday_check(*, account_id: str | None = None) -> dict[str, Any]:
    """14:45 入口：清仓预警 → 企微（幂等键 = 账户+日期，一天最多一条）。"""
    row, pdir = _load_account(account_id)
    if row is None:
        return {"skipped": True, "reason": "账户不存在"}
    if pdir is None:
        return {"skipped": True, "reason": "账户产物目录缺失"}

    holdings = _current_holdings(pdir)
    if holdings.empty:
        return {"skipped": True, "reason": "当前无持仓"}

    try:
        params = json.loads(row.get("param_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        params = {}

    today = str(holdings["date"].max())[:10]
    alerts = evaluate_holdings(holdings, today, params)

    if not alerts:
        logger.info("[intraday] 14:45 检查：无清仓预警")
        return {"ok": True, "alerts": 0, "holdings": len(holdings)}

    from app.services import alert_service  # noqa: PLC0415

    res = alert_service.dispatch_event(
        "intraday_exit_alert",
        {
            "account_name": str(row.get("name") or ""),
            "strategy_name": str(row.get("strategy") or ""),
            "date": today,
            "holdings_count": len(holdings),
            "alerts": alerts,
            "alert_rows": "".join(
                f"> {a['symbol']}｜现价 {a['live_price']:.2f}｜{a['reasons']}<br>"
                for a in alerts
            ),
        },
        event_key=f"intraday_exit|{row.get('id')}|{today}",
        account_id=str(row.get("id")),
        level="warning",
        sync=False,
    )
    logger.info(f"[intraday] 14:45 清仓预警 {len(alerts)} 条：{[a['symbol'] for a in alerts]}")
    return {"ok": True, "alerts": len(alerts), "holdings": len(holdings), "dispatch": res}
