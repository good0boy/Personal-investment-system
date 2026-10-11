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
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from app.core.config import settings
from app.core.logging import logger
from app.services import data_store
from app.services.calendar_service import calendar
from app.services.data_source import symbols as sym_mod
from app.services.data_source.fuyao_client import FuyaoClient
from tools.universe import price_limit_pct

#: 快照价/前收的候选列名（fuyao 返回结构未在客户端固化，做宽容解析）
_PRICE_COLS = ("last_price", "latest_price", "price", "close", "last")
_PRECLOSE_COLS = ("prev_close", "pre_close", "preclose", "prev_price", "yesterday_close", "last_close")

#: limit_down 判定容差（跌停价 = round(前收×(1-limit), 2)，快照价可能差一分）
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
    """最新一天的持仓（positions.parquet 是逐日快照），**带 is_st 列**。

    ★ 必须补 is_st（2026-10-11 修复）：`POSITION_COLUMNS` 契约里没有 ST 标记，
      而 `price_limit_pct(symbol, day, is_st=None)` 会跳过 ST 分支 → ST 股按
      ±10% 算限幅 → 跌停价（前收×0.95）永远高于阈值（×0.9）→ **ST 跌停永不告警**，
      恰恰漏掉"跌停卖不出去、要顺延"最该预警的场景。
      从本地行情的**最新交易日截面**读 is_st（ST 是逐日字段，不能用陈旧值）。
    """
    p = pdir / "positions.parquet"
    if not p.exists():
        return pd.DataFrame()
    df = pd.read_parquet(p)
    if df.empty:
        return df
    last = str(df["date"].max())[:10]
    df = df[df["date"].astype(str).str[:10] == last].copy()
    if "is_st" in df.columns:
        return df
    st_map = _latest_is_st_map(df["symbol"].astype(str).tolist())
    df["is_st"] = df["symbol"].astype(str).map(st_map).fillna(0).astype(int)
    return df


def _latest_is_st_map(symbols: list[str]) -> dict[str, int]:
    """各标的**最近一个交易日**的 is_st（读不到按 0 = 非 ST 处理）。"""
    out: dict[str, int] = {}
    for sym in symbols:
        try:
            q = data_store.read_quotes(sym, None, None, columns=["date", "is_st"])
        except Exception as exc:
            logger.warning(f"[intraday] {sym} 读 is_st 失败，按非 ST 处理：{exc!r}")
            out[sym] = 0
            continue
        if q is None or q.empty:
            out[sym] = 0
            continue
        out[sym] = int(q["is_st"].fillna(0).iloc[-1])
    return out


@dataclass
class FuyaoSnap:
    """单只标的的 fuyao 实时快照（**真实交易价口径，不复权**）。"""

    price: float                     # 最新价
    prev_close: float | None         # 昨收（真实口径）
    day_chg: float | None            # 当日实时涨跌幅（**小数**，0.574 = +0.574%；
                                     #  接口字段 price_change_ratio_pct 实测即小数）


def _fuyao_snapshots(symbols: list[str]) -> tuple[dict[str, FuyaoSnap], str | None]:
    """fuyao 快照 → ({symbol: FuyaoSnap}, 错误信息)。

    ★ 为什么结构化而不是两个 dict（2026-10-11）：over_realized / 止损的判定
      此前拿 **fuyao 真实价** 去除以 **本地 baostock hfq 价** —— 两者复权口径
      不同（茅台 hfq≈9686 vs 真实≈1263，差 7.7 倍），混算后 r20 恒为 -87%，
      over_realized **永久漏报**。修法：当日涨跌用 fuyao 自己的
      price_change_ratio_pct（真实口径），与本地 hfq 段收益分段连乘合成。
    """
    ths = {sym_mod.to_thscode(s): s for s in symbols}
    client = FuyaoClient()
    raw = client.snapshot(thscodes=",".join(ths.keys()))
    if raw is None or raw.empty:
        return {}, "fuyao snapshot 返回空"
    pcol = _pick_col(raw, _PRICE_COLS)
    if pcol is None:
        return {}, f"fuyao snapshot 无法识别价格列（实际列：{list(raw.columns)}）"
    ccol = _pick_col(raw, _PRECLOSE_COLS)
    chgcol = _pick_col(raw, ("price_change_ratio_pct", "price_change_ratio", "pct_chg"))
    code_col = next((c for c in raw.columns if c.lower() in ("thscode", "code", "symbol")), None)
    if code_col is None:
        return {}, f"fuyao snapshot 无法识别代码列（实际列：{list(raw.columns)}）"

    snaps: dict[str, FuyaoSnap] = {}
    for _, r in raw.iterrows():
        canon = ths.get(str(r[code_col]))
        if canon is None:
            continue
        try:
            price = float(r[pcol])
        except (TypeError, ValueError):
            continue
        prev_close = None
        if ccol is not None:
            with contextlib.suppress(TypeError, ValueError):
                v = float(r[ccol])
                if v > 0:
                    prev_close = v
        day_chg = None
        if chgcol is not None:
            with contextlib.suppress(TypeError, ValueError):
                v = float(r[chgcol])
                if pd.notna(v):
                    day_chg = v / 100.0 if abs(v) > 1.5 else v
                    # 接口字段名带 _pct，实测为小数（0.574=+0.574%）；这里按
                    # ">1.5 视为百分数"防御性归一，避免上游口径变化翻 100 倍
        snaps[canon] = FuyaoSnap(price=price, prev_close=prev_close, day_chg=day_chg)
    return snaps, None


def _fuyao_prices(symbols: list[str]) -> tuple[dict[str, float], dict[str, float], str | None]:
    """兼容包装：旧签名（两个 dict）。新代码请用 `_fuyao_snapshots`。"""
    snaps, err = _fuyao_snapshots(symbols)
    if err:
        return {}, {}, err
    return (
        {k: v.price for k, v in snaps.items()},
        {k: v.prev_close for k, v in snaps.items() if v.prev_close is not None},
        None,
    )


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
    holdings: pd.DataFrame,
    today: str,
    params: dict[str, Any],
    data_date: str | None = None,
) -> list[dict[str, Any]]:
    """对每只持仓评估清仓条件，返回预警明细（空列表 = 无预警）。

    ★ `today` 必须是**真实今天**（2026-10-11 整改）：原先传的是 positions 快照日，
      跑批失败/延迟时快照停在 09-30，于是
        · `event_key=intraday_exit|{id}|{快照日}` 天天相同 → 第二天起被
          `alert_log` 唯一键判为"已派发" → **推送永久静默**，直到某天跑批成功；
        · `_hold_days(entry, 快照日)` 少算交易日（实测 7 vs 9）→ time_stop
          在 hold_days_max 边界上单向漏报。
      `data_date` 只用于日志/回执标注快照日，不参与任何判断。
    """
    hold_max = int(params.get("hold_days_max", 30))
    r20_max = float(params.get("realized_ret20_max", 0.30))
    sl_mult = float(params.get("stop_loss_p_mult", 0.0) or 0.0)
    tp_mult = float(params.get("take_profit_p_mult", 0.0) or 0.0)

    symbols = holdings["symbol"].astype(str).tolist()
    snaps, err = _fuyao_snapshots(symbols)
    if err:
        raise RuntimeError(f"fuyao 快照不可用：{err}")

    alerts: list[dict[str, Any]] = []
    for _, row in holdings.iterrows():
        sym = str(row["symbol"])
        snap = snaps.get(sym)
        price = snap.price if snap else None
        if price is None or price <= 0:
            continue
        reasons: list[str] = []

        entry_date = str(row.get("entry_date") or "")[:10]
        hd = _hold_days(entry_date, today) if entry_date else -1
        if hd >= 0 and hd >= hold_max:
            reasons.append(f"time_stop（已持有 {hd} >= {hold_max} 个交易日）")

        # ★ over_realized 用**分段收益合成**（2026-10-11 修复复权口径混算）：
        #   hfq 段收益（T-20→昨收，hfq 对 hfq，跨除权天然正确）
        #   × 当日实时涨跌（fuyao price_change_ratio_pct，真实对真实）
        #   两者连乘 ≈ T-20→现在的真实累计收益。此前直接用 fuyao 真实价 /
        #   本地 hfq T-20 收盘，口径混算后 r20 恒为 -87%（茅台实证），
        #   over_realized 永久漏报。
        c20 = _local_close(sym, 21)          # T-20 收盘（hfq）
        c1 = _local_close(sym, 1)            # 昨收（hfq）
        r20 = None
        if c20 and c20 > 0 and c1 and c1 > 0:
            r20 = c1 / c20 - 1.0             # hfq 段：T-20 → 昨收
            day_chg = snap.day_chg if snap else None
            if day_chg is not None:
                r20 = (1.0 + r20) * (1.0 + day_chg) - 1.0   # 叠加今日盘中真实涨跌
            else:
                logger.warning(
                    f"[intraday] {sym} fuyao 未返回当日涨跌幅，"
                    f"r20 降级为截至昨收的 hfq 口径（{r20:+.1%}）"
                )
            if r20 >= r20_max:
                reasons.append(
                    f"over_realized（近20日累计 {r20:+.1%} >= {r20_max:.0%}）"
                )

        p_neg = _factor_latest_lt_zero(sym, "profit_yoy")
        r_neg = _factor_latest_lt_zero(sym, "revenue_yoy")
        if p_neg or r_neg:
            reasons.append("quality_broken（基本面转负）")

        pc = (snaps[sym].prev_close if sym in snaps else None) or None
        if pc is None:
            # ★ 不再回退到本地 hfq 前收（2026-10-11）：hfq 前收 vs fuyao 真实现价
            #   口径混算会把阈值放大 7 倍 → 无脑误报"逼近跌停"。缺 prev_close
            #   就跳过该条判定（宁可漏报单条，不出错误信号）。
            logger.warning(f"[intraday] {sym} fuyao 未返回前收，跳过 limit_down 判定")
        if pc and pc > 0:
            # ★ 用统一的板块限幅（`tools.universe.price_limit_pct`），不再硬编码 0.9
            #   （2026-10-11 修复）：主板 ±10% 时碰巧正确，但创业板 ±20% 会**误报**
            #   （跌 10% 就喊"逼近跌停"），ST ±5% 又**永不报警**（跌停价 9.50 > 9.09）
            #   —— 恰恰漏掉"跌停卖不出去、要顺延"最该预警的场景。
            limit = price_limit_pct(sym, today, row.get("is_st", row.get("st")))
            if price <= pc * (1.0 - limit) * _LIMIT_DOWN_TOL:
                reasons.append(
                    f"limit_down（最新价 {price:.2f} 封跌停，前收 {pc:.2f}，限幅 {limit:.0%}）"
                )

        # ★ 止损/止盈同样分段合成（2026-10-11）：entry_price 是引擎的 **hfq 成交价**，
        #   直接除 fuyao 真实现价又是口径混算。hfq 段（entry→昨收）× 今日实时涨跌。
        entry_price = float(row.get("entry_price") or 0)
        if entry_price > 0 and (sl_mult > 0 or tp_mult > 0):
            c1e = _local_close(sym, 1)       # 昨收（hfq，与 entry_price 同口径）
            hfq_gain = (c1e / entry_price - 1.0) if (c1e and c1e > 0 and entry_price > 0) else None
            day_chg = snap.day_chg if snap else None
            gross = None
            if hfq_gain is not None:
                gross = (1.0 + hfq_gain) * (1.0 + day_chg) - 1.0 if day_chg is not None else hfq_gain
            if gross is not None and sl_mult > 0 and gross <= -abs(sl_mult) * 0.0225:
                # p 的样本中位 ~2.25%，预警用近似
                reasons.append(f"stop_loss（毛收益 {gross:+.1%} 触及止损带）")
            if gross is not None and tp_mult > 0 and gross >= abs(tp_mult) * 0.0225:
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

    # 快照日（只标注）与真实今天（判断 + 幂等）必须分开 —— 见 evaluate_holdings
    data_date = str(holdings["date"].max())[:10]
    today = date.today().strftime("%Y-%m-%d")
    if data_date < today:
        logger.warning(
            f"[intraday] 持仓快照日 {data_date} 落后于今天 {today}"
            f"（跑批未成功？）—— 已按真实今天计算持有天数与幂等键，"
            f"但持仓/成本价可能已不是最新的"
        )
    alerts = evaluate_holdings(holdings, today, params, data_date=data_date)

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
