"""策略 v2.0「中频质量反转」（`quality_reversal`）的策略级测试。

盯的是四类最要命的事：

1. **每道门确实生效**：不合格的候选不得进入任何订单。
   （断言同时看 `trades` **和** `orders` —— 只看 trades 会被"挂了单但没成交"糊弄过去。）
2. **调仓节奏生效**：只有 `di % rebalance_every == 0` 的日子才建仓。
3. **因子缺失必须降级而不是崩**：因子文件缺失时回测照常跑完，绝不崩。
4. **可复现**：同一份配置跑两次，交易明细逐行一致。

夹具思路
--------
价格全设成**横盘**（除单独一个"上涨"标的外）→ 价格侧不会触发任何退出，
于是**入场完全由因子文件控制**，逐门断言干净可读。基准指数每日 +0.3%
→ 门1（大盘）恒放行。4 只主板标的 → 截面百分位秩干净（0.25/0.5/0.75/1.0）。

默认参数（`_params`）把**所有门都关掉**，各用例只打开自己要测的那道 ——
这样"某只票没被买"的原因唯一，不会几条规则混在一起说不清。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app.services import data_store
from strategies.engine import BacktestEngine
from strategies.impl.quality_reversal import QualityReversalStrategy
from strategies.registry import get_strategy_cls

# ----------------------------------------------------------------------
# 合成行情
# ----------------------------------------------------------------------
N = 200
DATES = pd.bdate_range("2020-01-01", periods=N)
DATE_STRS = [d.strftime("%Y-%m-%d") for d in DATES]

BT_START_IDX = 101                    # 回测首日（此时已上市 102 个交易日 > 90）
BT_END_IDX = 130                      # 回测末日：**只留 30 个交易日**
# ★ 窗口刻意压短：默认 `hold_days_max=100`（持仓不退出）→ 首个建仓日就把槽位占满，
#   之后不再买卖。否则窗口一长就会"卖出 → 触发 60 日禁买 → 股票池缩小"，
#   届时分位/中位会漂移，断言就说不清"到底是哪道门在起作用"。
BT_START = DATE_STRS[BT_START_IDX]
BT_END = DATE_STRS[BT_END_IDX]
PREHEAT = DATE_STRS[0]
INDEX_SYM = "sh.000001"

S0, S1, S2, S3 = "sh.600001", "sh.600002", "sh.600003", "sh.600004"
SYMS = (S0, S1, S2, S3)


def _frame(symbol: str, closes: np.ndarray, *, spread: float = 0.02) -> pd.DataFrame:
    closes = np.asarray(closes, dtype=float)
    preclose = np.concatenate([[closes[0]], closes[:-1]])
    pct = np.where(preclose > 0, closes / preclose - 1.0, 0.0) * 100.0
    return pd.DataFrame(
        {
            "date": DATES,
            "symbol": symbol,
            "open": closes,
            "high": closes * (1 + spread),
            "low": closes * (1 - spread),
            "close": closes,
            "preclose": preclose,
            "pct_chg": pct,
            "trade_status": 1,
            "is_st": 0,
        }
    )


def _install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    rising_sym: str | None = None,
    rise_daily: float = 0.015,
    drift: dict[str, float] | None = None,
) -> None:
    """4 只横盘标的（可按 `drift` 指定某只的日涨跌幅）+ 基准指数（每日 +0.3%）。

    `drift = {symbol: 日涨跌幅}`（正=涨、负=跌），从回测首日开始生效。
    `rising_sym` / `rise_daily` 是 `drift` 的语法糖（保持向后兼容）。

    同时把 `FACTOR_DIR` 指向临时目录 —— 即使不装因子也要指过去，
    否则会读到真实 `data/factor` 污染结果（本项目踩过的坑）。
    """
    rates = dict(drift or {})
    if rising_sym is not None:
        rates[rising_sym] = rise_daily

    daily = tmp_path / "quote_daily"
    index = tmp_path / "quote_index"
    factor = tmp_path / "factor"
    for d in (daily, index, factor):
        d.mkdir(parents=True, exist_ok=True)

    for sym in SYMS:
        closes = np.full(N, 10.0)
        rate = rates.get(sym, 0.0)
        if rate:
            for i in range(BT_START_IDX, N):
                closes[i] = closes[i - 1] * (1 + rate)
        data_store.write_parquet(_frame(sym, closes), daily / f"{sym}.parquet")

    idx = _frame(INDEX_SYM, 3000.0 * np.cumprod(1 + np.full(N, 0.003)))
    data_store.write_parquet(idx, index / f"{INDEX_SYM}.parquet")

    monkeypatch.setattr(data_store, "QUOTE_DAILY_DIR", daily)
    monkeypatch.setattr(data_store, "QUOTE_INDEX_DIR", index)
    monkeypatch.setattr(data_store, "FACTOR_DIR", factor)


def _install_factors(
    spec: dict[str, tuple[str, dict[str, float]]],
) -> None:
    """`{因子名: (分类, {symbol: value})}` → 写进临时 FACTOR_DIR。

    ★ 值写在**全部交易日**上：只写一天会让后续信号日找不到截面 → 门降级，
      于是"门到底生没生效"就说不清了。
    """
    for name, (category, values) in spec.items():
        rows = [
            {"date": pd.Timestamp(d), "symbol": sym, "value": float(val)}
            for d in DATE_STRS
            for sym, val in values.items()
        ]
        data_store.save_factor(name, pd.DataFrame(rows), category)


def _carrier(**over: float) -> dict[str, tuple[str, dict[str, float]]]:
    """打分载体：`ret_50` 只用来提供打分维度（默认门槛 1.0 → 不裁人）。

    为什么需要它：打分的 5 个维度里必须**至少有一个可用**，否则 `_buy_side`
    直接返回（一只都不买）。若只装"被测的那一个门"的因子，而它又不在打分维度里
    （如 `amount_mean_20`），就会因为"没有打分维度"而空跑 —— 那是假通过。
    """
    vals = {S0: 0.00, S1: 0.01, S2: 0.02, S3: 0.03}
    vals.update(over)
    return {"ret_50": ("daily", vals)}


def _params(**over: object) -> dict:
    """默认把**所有门关掉**，用例只打开自己要测的那道。"""
    p: dict[str, object] = {
        "max_positions": 10,
        "max_weight_per_stock": 0.10,
        "hold_days_max": 100,       # 默认不退出（断言只由首个建仓日决定）
        "rebalance_every": 1,
        "index_ret20_min": -0.10,
        "reversal_pct": 1.0,        # 默认不裁
        "max_ret_20_max": 1.0,      # 默认不裁
        "use_quality_gate": False,
        "use_lowvol_gate": False,
        "use_lowturn_gate": False,
        "use_liquidity_gate": False,
        "min_amount_mean_20": 5.0e7,
        "realized_ret20_max": 10.0,  # 默认不触发
        # v2.1 新增：止损/止盈（默认全 0 = 关闭 → 等价 v2.0 行为）
        "stop_loss_p_mult": 0.0,
        "take_profit_p_mult": 0.0,
        "take_profit_abs": 0.0,
    }
    p.update(over)
    return p


def _engine(params: dict) -> BacktestEngine:
    return BacktestEngine(
        QualityReversalStrategy(params),
        start=BT_START,
        end=BT_END,
        preheat_start=PREHEAT,
        initial_cash=1_000_000.0,
        max_positions=int(params.get("max_positions", 10)),  # type: ignore[arg-type]
        max_weight_per_stock=float(params.get("max_weight_per_stock", 0.10)),  # type: ignore[arg-type]
        benchmark=INDEX_SYM,
    )


def _bought(res) -> set[str]:
    """**有买单的标的**（`orders` 里 side=buy 的 symbol）。

    ★ 必须看 `orders` 而不是 `trades`：`trades` 只记**已平仓**的交易，
      而本文件的夹具默认 `hold_days_max=100`（窗口内不平仓）—— 只看 trades
      会得到"0 笔"，把"门是否正确生效"的断言全部变成空过。
    """
    o = res.orders
    if o.empty:
        return set()
    return set(o.loc[o["side"] == "buy", "symbol"].astype(str))


def _touched(res) -> set[str]:
    """出现过的标的 = 已成交交易 ∪ 委托流水（兜底用）。"""
    return set(res.trades["symbol"]).union(set(res.orders["symbol"]))


# ======================================================================
# 夹具自检
# ======================================================================
def test_signal_fires_on_designed_day(tmp_path, monkeypatch):
    """★ 先证明夹具真的能触发买入 —— 否则后面"某只没被买"的断言都是空过。"""
    _install(tmp_path, monkeypatch)
    _install_factors(_carrier())

    res = _engine(_params()).run()

    bought = _bought(res)
    assert bought == {S0, S1, S2, S3}, f"默认参数下 4 只都应被买入，实际 {bought}"
    assert S0 in bought, "打分最优（ret_50 最低）的标的应被买入"


# ======================================================================
# 逐门生效
# ======================================================================
def test_reversal_gate_excludes_strongest_half(tmp_path, monkeypatch):
    """★ 门2 反转门：不买近期最强的半数（rank(ret_50) > 分位）。"""
    _install(tmp_path, monkeypatch)
    _install_factors(
        {"ret_50": ("daily", {S0: 0.00, S1: 0.10, S2: 0.50, S3: 0.90})}
    )

    res = _engine(_params(reversal_pct=0.5)).run()

    bought = _bought(res)
    assert S3 not in bought and S2 not in bought, "最强的两只应被反转门剔除"
    assert S0 in bought and S1 in bought, "分位线以内的应保留"


def test_quality_gate_rejects_negative_growth(tmp_path, monkeypatch):
    """★ 门3 质量门：成长（营收/净利同比）转负的不买。"""
    _install(tmp_path, monkeypatch)
    spec = _carrier()
    spec["profit_yoy"] = ("fundamental", {S0: 0.20, S1: -0.30, S2: 0.10, S3: 0.20})
    spec["revenue_yoy"] = ("fundamental", {S0: 0.20, S1: 0.20, S2: -0.10, S3: 0.20})
    _install_factors(spec)

    res = _engine(_params(use_quality_gate=True)).run()

    bought = _bought(res)
    assert S1 not in bought, "净利同比为负 → 不应买入"
    assert S2 not in bought, "营收同比为负 → 不应买入"
    assert S0 in bought and S3 in bought, "成长为正的应保留"


def test_lowvol_gate_rejects_high_volatility(tmp_path, monkeypatch):
    """★ 门4 低波动：波动率高于截面中位的不买。"""
    _install(tmp_path, monkeypatch)
    spec = _carrier()
    spec["volatility_20"] = ("daily", {S0: 0.01, S1: 0.02, S2: 0.30, S3: 0.40})
    _install_factors(spec)

    res = _engine(_params(use_lowvol_gate=True)).run()

    bought = _bought(res)
    assert S2 not in bought and S3 not in bought, "高波动应被剔除"
    assert S0 in bought and S1 in bought


def test_liquidity_gate_rejects_small_amount(tmp_path, monkeypatch):
    """★ 门0 流动性：近 20 日日均成交额低于下限的不买（成交额与换手是两个口径）。"""
    _install(tmp_path, monkeypatch)
    spec = _carrier()
    spec["amount_mean_20"] = ("daily", {S0: 1.0e8, S1: 2.0e8, S2: 1.0e6, S3: 5.0e6})
    _install_factors(spec)

    res = _engine(_params(use_liquidity_gate=True, min_amount_mean_20=5.0e7)).run()

    bought = _bought(res)
    assert S2 not in bought and S3 not in bought, "成交额不足 5000 万应被剔除"
    assert S0 in bought and S1 in bought


# ======================================================================
# 调仓节奏
# ======================================================================
def test_rebalance_cadence_controls_entry_days(tmp_path, monkeypatch):
    """★ 每 `rebalance_every` 个交易日才建仓：门槛设到极大 → 窗口内无建仓日 → 零买单。"""
    _install(tmp_path, monkeypatch)
    _install_factors(_carrier())

    every_day = _engine(_params(rebalance_every=1)).run()
    never = _engine(_params(rebalance_every=10**6)).run()

    assert _bought(every_day), "rebalance_every=1 应正常建仓"
    assert not _bought(never), "rebalance_every 极大时窗口内无建仓日 → 不应有任何买单"
    assert never.orders.empty, "不应有任何委托"


# ======================================================================
# 退出
# ======================================================================
def test_time_stop_exits_after_hold_days(tmp_path, monkeypatch):
    """★ 到期换仓：持有满 hold_days_max 个交易日即卖出，且持有天数记录正确。"""
    _install(tmp_path, monkeypatch)
    _install_factors(_carrier())

    res = _engine(_params(hold_days_max=10)).run()

    assert not res.trades.empty
    reasons = res.trades["exit_reason"].astype(str)
    assert reasons.str.contains("time_stop").all(), "价格横盘 + 无其他退出条件 → 只能时间止损"
    assert set(res.trades["hold_days"].astype(int)) == {10}, "持有天数应为 10（含首尾）"


def test_over_realized_exit_fires_when_gain_reaches_threshold(tmp_path, monkeypatch):
    """★ 过度兑现退出：近 20 日累计涨幅达阈值（反转已兑现）→ 提前卖出。"""
    _install(tmp_path, monkeypatch, rising_sym=S0, rise_daily=0.015)
    _install_factors(_carrier())

    # hold 放得很长（60），确保不是时间止损先触发
    res = _engine(_params(hold_days_max=60, realized_ret20_max=0.30)).run()

    assert not res.trades.empty
    reasons = res.trades["exit_reason"].astype(str)
    assert reasons.str.contains("over_realized").any(), "涨幅达 30% 后应触发过度兑现退出"
    # 触发时持有天数应远小于 60（否则说明是时间止损在起作用，用例失效）
    hit = res.trades[reasons.str.contains("over_realized")]
    assert int(hit["hold_days"].max()) < 60


# ======================================================================
# 降级与可复现
# ======================================================================
def test_missing_factors_degrade_without_crash(tmp_path, monkeypatch):
    """★ 因子文件全缺：门全部降级、不崩、回测跑完（0 笔是合理结果，不是失败）。"""
    _install(tmp_path, monkeypatch)          # FACTOR_DIR 指向空目录
    _install_factors({})                     # 一个因子都不装

    res = _engine(_params(use_quality_gate=True, use_lowvol_gate=True)).run()

    assert res.orders.empty, "没有任何打分维度可用 → 不应建仓"
    assert str(res.equity["date"].iloc[0])[:10] == BT_START, "回测应照常跑完"


def test_same_config_two_runs_identical(tmp_path, monkeypatch):
    """★ 可复现：同一份配置跑两次，交易明细逐行一致。"""
    _install(tmp_path, monkeypatch)
    _install_factors(_carrier())

    a = _engine(_params()).run()
    b = _engine(_params()).run()

    pd.testing.assert_frame_equal(a.trades, b.trades)
    pd.testing.assert_frame_equal(a.equity, b.equity)


# ======================================================================
# v2.1 新增：止损 / 止盈（阈值 = mult × p，p = |近5日累计涨跌幅| ÷ 5）
# ======================================================================
def _reasons(res) -> str:
    if res.trades.empty:
        return ""
    return "|".join(res.trades["exit_reason"].astype(str))


def test_stop_loss_by_p_multiple_fires(tmp_path, monkeypatch):
    """★ 止损：毛亏 ≥ mult × p 即卖出（p 随个股近 5 日波动自适应）。"""
    _install(tmp_path, monkeypatch, drift={S0: -0.01})   # S0 买入后每日 −1%
    _install_factors(_carrier())

    # 关闭止损 → 30 日窗口内 S0 不会出场（无其他退出条件）
    off = _engine(_params()).run()
    # 打开 2×p 止损 → S0 应被快速止损
    on = _engine(_params(stop_loss_p_mult=2.0)).run()

    assert "stop_loss" not in _reasons(off), "止损关闭时不应出现 stop_loss"
    assert "stop_loss" in _reasons(on), "打开 2×p 止损后应触发 stop_loss"
    hit = on.trades[on.trades["exit_reason"].astype(str).str.contains("stop_loss")]
    assert set(hit["symbol"]) == {S0}, "只该止损下跌的那只"
    assert int(hit["hold_days"].max()) <= 5, "p 口径刻意取小 → 应当很快被打掉"


def test_take_profit_by_p_multiple_fires(tmp_path, monkeypatch):
    """★ 止盈（×p）：毛盈 ≥ mult × p 即卖出。"""
    _install(tmp_path, monkeypatch, drift={S0: 0.01})    # S0 买入后每日 +1%
    _install_factors(_carrier())

    off = _engine(_params()).run()
    on = _engine(_params(take_profit_p_mult=2.0)).run()

    assert "take_profit" not in _reasons(off), "止盈关闭时不应出现 take_profit"
    assert "take_profit" in _reasons(on), "打开 2×p 止盈后应触发 take_profit"
    hit = on.trades[on.trades["exit_reason"].astype(str).str.contains("take_profit")]
    assert set(hit["symbol"]) == {S0}, "只该止盈上涨的那只"


def test_take_profit_abs_6pct_fires(tmp_path, monkeypatch):
    """★ 止盈（6% 绝对档）：与 p 无关，毛盈达 6% 即卖出。"""
    _install(tmp_path, monkeypatch, drift={S0: 0.02})    # 每日 +2% → 约 3 日达 6%
    _install_factors(_carrier())

    off = _engine(_params()).run()
    on = _engine(_params(take_profit_abs=0.06)).run()

    assert "take_profit" not in _reasons(off)
    assert "take_profit" in _reasons(on), "打开 6% 绝对止盈后应触发"
    hit = on.trades[on.trades["exit_reason"].astype(str).str.contains("take_profit")]
    assert set(hit["symbol"]) == {S0}
    # 6% 档应对应约 +6% 的毛收益（不是 p 倍数那种极紧的阈值）
    assert float(hit["net_return"].mean()) > 0.03


def test_exit_rules_off_by_zero(tmp_path, monkeypatch):
    """★ 三个新参数全 0 = 关闭 → 不产生 stop_loss / take_profit（等价 v2.0 行为）。"""
    _install(tmp_path, monkeypatch, drift={S0: -0.01, S1: 0.01})   # 一跌一涨
    _install_factors(_carrier())

    res = _engine(_params()).run()

    r = _reasons(res)
    assert "stop_loss" not in r and "take_profit" not in r, "0 档必须完全关闭这两条规则"


# ======================================================================
# 元信息 / 契约
# ======================================================================
def test_registered_and_snapshot_records_v2_contract():
    """策略已注册；config.json 快照能回答"当时跑的是哪套规则"。"""
    assert get_strategy_cls("quality_reversal") is QualityReversalStrategy

    inst = QualityReversalStrategy()
    assert inst.name == "quality_reversal"
    assert inst.version == "2.1"
    # 默认参数来自独立配置节 strategy_defaults_v2，不是 v1.6 的 strategy_defaults
    assert inst.params["max_positions"] == 10
    assert inst.params["max_weight_per_stock"] == 0.10
    assert inst.params["hold_days_max"] == 30
    assert inst.params["rebalance_every"] == 5
    # v2.1：止损/止盈默认全 0（关闭）→ 行为与 v2.0/M1 基线一致
    assert inst.params["stop_loss_p_mult"] == 0.0
    assert inst.params["take_profit_p_mult"] == 0.0
    assert inst.params["take_profit_abs"] == 0.0

    snap = inst.config_snapshot()
    assert "v2.1" in snap["rules_version"]
    assert snap["buy_fill_rule"] == "next_open"
    assert snap["sell_fill_rule"] == "same_close"
    assert snap["gates"]["quality"] is True
    assert snap["gates"]["reversal_pct"] == 0.5
    assert snap["score_dims"]["ret_50"] == -1
    assert snap["score_dims"]["profit_yoy"] == 1
    # v2.1：p 口径与止损/止盈档位必须进快照（否则新旧口径的产物无法区分）
    assert snap["exit_p_rules"]["p_def"] == "abs(ret_5) / 5"
    assert snap["exit_p_rules"]["stop_loss_p_mult"] == 0.0
