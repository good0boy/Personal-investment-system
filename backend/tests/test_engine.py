"""回测引擎测试。

★ 本文件盯的是**回测里最要命的几件事**（错了不会报错，只会让收益虚高）：

1. **T+1 成交**：信号在 T 日收盘产生，成交在 T+1 开盘。若写成 T 日成交，
   就等于用"当天收盘价"买卖 —— 那一天的涨跌你已经知道了，是未来函数。
2. **整手 100 股 + 不透支**：买入股数向下取整，且**扣得起费用**。
3. **跌停卖不掉要顺延**：不能假设跌停能成交，否则会严重低估尾部风险。
4. **时间止损口径**：`offset >= 10`（含首尾，买入成交日算第 1 天）。
   差一天就是 10% 的持有期误差。
5. **不设个股止损**：买入后一路阴跌也必须扛满 10 个交易日 ——
   这是本策略的性格，也是「最大单笔亏损」这个指标存在的原因。

测试用**合成行情**（不依赖 data/ 下的真实数据），手工构造价格路径以精确
触发每一条规则。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app.services import data_store
from strategies.engine import BacktestEngine
from strategies.impl.momentum import MomentumStrategy

#: 合成行情 —— 交易日用工作日，足够长以跨过"新股 90 交易日"门槛
DATES = pd.bdate_range("2020-01-01", periods=200)
DATE_STRS = [d.strftime("%Y-%m-%d") for d in DATES]
N = len(DATES)

#: 价格路径的关键节点（手工构造，用来精确触发每一条规则）
FLAT_UNTIL = 100            # 0~100 横盘（让"上市满 90 交易日"成立）
SIGNAL_IDX = 125            # 信号日：当日 +4%（落在 1%~5% 区间内）
ENTRY_IDX = SIGNAL_IDX + 1  # T+1 开盘买入
TIME_STOP_SIGNAL_IDX = ENTRY_IDX + 9    # 持有第 10 天（含首尾）→ 触发时间止损
TIME_STOP_EXEC_IDX = ENTRY_IDX + 10     # 次日开盘卖出

#: 回测区间取在信号日之前、预热之后
BT_START = DATE_STRS[101]
BT_END = DATE_STRS[-1]
PREHEAT = DATE_STRS[0]
INDEX_SYM = "sh.000001"

SLOW_SYM = "sh.600001"      # 慢牛（时间止损）
FLAT_SYM = "sh.600002"      # 横盘（不该被买）
FAST_SYM = "sh.600003"      # 快涨（扣成本止盈）


def _ramp(base: float, *, daily: float, signal_pct: float, tail_daily: float) -> np.ndarray:
    """构造价格路径：横盘 → 每日 `daily` 爬升 → 信号日 `signal_pct` → 之后 `tail_daily`。

    ⚠️ 信号日的**累计涨幅**必须同时满足策略的三个门槛：
       今日 ∈ (1%, 5%)、近 3 日 > 5%、近 10/20 日 > 5%。
       所以爬升段要够长（≥20 日）且日涨幅要够大 —— 用 `daily=0.007` 时，
       信号日近 3 日累计 = 1.007² × 1.04 - 1 ≈ 5.4%，刚好过线。
       （踩过：早期版本把爬升段写成了横盘，结果一条信号都触发不了，
        而断言只是"没有交易"就静默通过了 —— 所以本文件加了
        `test_signal_fires_on_the_designed_day` 专门验证夹具本身有效。）
    """
    out = np.full(N, float(base))
    for i in range(FLAT_UNTIL + 1, SIGNAL_IDX):
        out[i] = out[i - 1] * (1 + daily)
    out[SIGNAL_IDX] = out[SIGNAL_IDX - 1] * (1 + signal_pct)
    for i in range(SIGNAL_IDX + 1, N):
        out[i] = out[i - 1] * (1 + tail_daily)
    return out


def _frame(symbol: str, closes: np.ndarray, *, spread: float = 0.02) -> pd.DataFrame:
    """按收盘价序列造一份行情（开盘=收盘，成交价口径一致；高低留振幅避免被判一字板）。"""
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


def _install(tmp_path: Path, monkeypatch, frames: dict[str, pd.DataFrame], *, index_rate: float):
    """把合成行情写进临时目录并替换 `data_store` 的目录常量。"""
    daily = tmp_path / "quote_daily"
    index = tmp_path / "quote_index"
    daily.mkdir(parents=True, exist_ok=True)
    index.mkdir(parents=True, exist_ok=True)
    for sym, df in frames.items():
        data_store.write_parquet(df, daily / f"{sym}.parquet")
    idx = _frame(INDEX_SYM, 3000.0 * np.cumprod(1 + np.full(N, index_rate)))
    data_store.write_parquet(idx, index / f"{INDEX_SYM}.parquet")
    monkeypatch.setattr(data_store, "QUOTE_DAILY_DIR", daily)
    monkeypatch.setattr(data_store, "QUOTE_INDEX_DIR", index)
    return daily


@pytest.fixture
def market(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """3 只标的 + 基准指数（指数每日 +0.3% → 择时闸门通过）。"""
    frames = {
        SLOW_SYM: _frame(SLOW_SYM, _ramp(10.0, daily=0.007, signal_pct=0.04, tail_daily=0.007)),
        FLAT_SYM: _frame(FLAT_SYM, np.full(N, 10.0)),
        FAST_SYM: _frame(FAST_SYM, _ramp(10.0, daily=0.007, signal_pct=0.04, tail_daily=0.03)),
    }
    _install(tmp_path, monkeypatch, frames, index_rate=0.003)
    return frames


def _engine(**kw) -> BacktestEngine:
    params = kw.pop("params", None)
    defaults = {
        "start": BT_START,
        "end": BT_END,
        "preheat_start": PREHEAT,
        "initial_cash": 1_000_000.0,
        "max_positions": 5,
        "max_weight_per_stock": 0.20,
        "benchmark": INDEX_SYM,
    }
    defaults.update(kw)
    return BacktestEngine(MomentumStrategy(params), **defaults)


# ======================================================================
# 夹具自检 + 基本管线
# ======================================================================
def test_signal_fires_on_the_designed_day(market, tmp_path):
    """先确认夹具本身有效：信号日确实生成了买单。

    没有这条断言，一旦价格路径写错（信号没触发），后面所有"应当有交易"
    的断言都会变成空过 —— 测试全绿但什么都没验证。
    """
    res = _engine().run()
    buys = res.orders[res.orders["side"] == "buy"]
    assert not buys.empty, "夹具没有触发任何买入，说明价格路径没满足开仓条件"
    assert (buys["signal_date"] == DATE_STRS[SIGNAL_IDX]).any()


def test_engine_runs_and_produces_contract_columns(market, tmp_path):
    res = _engine().run()

    assert len(res.equity) == len([d for d in DATE_STRS if BT_START <= d <= BT_END])
    assert {"date", "cash", "market_value", "total_asset", "position_count"} <= set(
        res.equity.columns
    )
    assert not res.trades.empty


def test_signal_generates_order_filled_next_trading_day(market, tmp_path):
    """★ T+1 成交：成交日必须**正好是**信号日的下一个交易日。"""
    res = _engine().run()
    idx = {d: i for i, d in enumerate(DATE_STRS)}

    filled = res.orders[(res.orders["side"] == "buy") & (res.orders["status"] == "filled")]
    assert not filled.empty
    for r in filled.itertuples():
        assert idx[r.exec_date] - idx[r.signal_date] == 1, (
            f"应当 T+1 成交：信号 {r.signal_date} → 成交 {r.exec_date}"
        )


def test_entry_is_at_next_open_not_signal_close(market, tmp_path):
    """★ 成交价必须是 T+1 的**开盘价** —— 用 T 日收盘价成交就是未来函数。"""
    res = _engine().run()
    q = market[SLOW_SYM].set_index(market[SLOW_SYM]["date"].dt.strftime("%Y-%m-%d"))

    ts = res.trades[res.trades["symbol"] == SLOW_SYM]
    assert not ts.empty
    for t in ts.itertuples():
        expected = float(q.loc[str(t.entry_date), "open"])
        assert t.entry_price == pytest.approx(expected), (
            f"{t.entry_date} 的买入价应为当日开盘价 {expected}，实际 {t.entry_price}"
        )
        signal_day = DATE_STRS[DATE_STRS.index(str(t.entry_date)) - 1]
        assert t.entry_price != pytest.approx(float(q.loc[signal_day, "close"])), (
            "成交价等于信号日收盘价 → 未来函数"
        )


# ======================================================================
# 撮合约束
# ======================================================================
def test_shares_are_whole_lots_and_cash_never_negative(market, tmp_path):
    """整手 100 股；且任意时点现金不得为负（含费用）。"""
    res = _engine().run()

    assert (res.trades["shares"] % 100 == 0).all(), "买入股数必须是整手"
    assert (res.equity["cash"] >= -1e-6).all(), "现金不允许为负（费用也算在内）"
    assert (res.equity["position_count"] <= 5).all(), "持仓数不得超过上限"


def test_position_size_respects_max_weight(market, tmp_path):
    """单票投入不超过「当前总资产 × 单票上限」。"""
    res = _engine().run()

    equity = res.equity.set_index("date")["total_asset"]
    for t in res.trades.itertuples():
        cap = float(equity.loc[str(t.entry_date)]) * 0.20
        assert t.entry_amount <= cap * 1.0001, (
            f"{t.symbol} 在 {t.entry_date} 投入 {t.entry_amount:.0f} 超过 20% 上限 {cap:.0f}"
        )


def test_cost_is_charged_on_every_trade(market, tmp_path):
    """每笔交易都要有成本，且净盈亏 = 毛盈亏 - 成本。"""
    res = _engine().run()

    assert (res.trades["cost"] > 0).all()
    diff = (res.trades["gross_pnl"] - res.trades["cost"] - res.trades["net_pnl"]).abs()
    assert diff.max() < 1e-6, "净盈亏必须等于毛盈亏减成本"


# ======================================================================
# 清仓规则
# ======================================================================
def test_time_stop_triggers_at_tenth_trading_day(market, tmp_path):
    """★ 时间止损口径：持有满 10 个交易日（含首尾）就必须卖。

    买入日 = T+1（第 1 天）→ 第 10 天收盘出信号 → 第 11 天开盘成交
    ⇒ 成交日与买入日的**交易日跨度正好是 10**。
    """
    res = _engine().run()

    ts = res.trades[
        (res.trades["symbol"] == SLOW_SYM)
        & (res.trades["exit_reason"].str.contains("time_stop", na=False))
    ]
    assert not ts.empty, "慢牛标的够不到 10% 止盈，应当由时间止损出局"

    idx = {d: i for i, d in enumerate(DATE_STRS)}
    for t in ts.itertuples():
        held = idx[str(t.exit_date)] - idx[str(t.entry_date)]
        assert held == 10, (
            f"应持满 10 个交易日再成交：entry={t.entry_date} exit={t.exit_date} 跨度 {held}"
        )
        assert t.hold_days == 11, "hold_days 含买入日，成交当日为第 11 天"
        assert t.entry_date == DATE_STRS[ENTRY_IDX]
        assert t.exit_date == DATE_STRS[TIME_STOP_EXEC_IDX]


def test_take_profit_uses_net_return_after_cost(market, tmp_path):
    """★ 止盈必须**扣成本**：判据是净收益率 > 10%，不是毛涨 10%。"""
    res = _engine().run()

    tp = res.trades[res.trades["exit_reason"].str.contains("take_profit", na=False)]
    assert not tp.empty, "快涨标的应当触发止盈"

    for t in tp.itertuples():
        assert t.net_return > 0.10, f"止盈单净收益 {t.net_return:.4f} 应 > 10%"
        gross = t.exit_price / t.entry_price - 1
        assert gross > 0.10, "毛涨幅必须更高（成本抬高了门槛）"
        assert gross > t.net_return, "毛涨幅必然高于净收益"


def test_no_individual_stop_loss_losing_position_waits_ten_days(tmp_path, monkeypatch):
    """★ 本策略**不设个股止损**：阴跌的单子也必须扛满 10 个交易日。

    这是策略的性格（低胜率高盈亏比），也是「最大单笔亏损」指标存在的理由。
    如果这里出现"亏 3% 就卖"，说明有人偷偷加了止损 —— 那是改了策略定义。
    """
    closes = _ramp(10.0, daily=0.007, signal_pct=0.04, tail_daily=-0.02)
    _install(tmp_path, monkeypatch, {SLOW_SYM: _frame(SLOW_SYM, closes)}, index_rate=0.003)

    res = _engine().run()
    assert not res.trades.empty
    losing = res.trades[res.trades["net_return"] < -0.03]
    assert not losing.empty, "这个路径应当产生亏损单"
    for t in losing.itertuples():
        assert "time_stop" in str(t.exit_reason), (
            f"亏损单 {t.symbol} 只能由时间止损出局，实际原因 {t.exit_reason}"
        )


# ======================================================================
# 涨跌停无法成交 → 顺延
# ======================================================================
def test_limit_down_sell_is_deferred_not_filled(tmp_path, monkeypatch):
    """★ 跌停时**没有买盘**，必须顺延重试，不能按理想价撮合。

    构造：持有到第 10 天该卖，但次日开盘即跌停 → 当日卖不掉；
    跌停打开后才成交。`orders` 里应留下 pending（顺延）记录，
    且成交日必须晚于跌停日。
    """
    closes = _ramp(10.0, daily=0.007, signal_pct=0.04, tail_daily=-0.02)
    df = _frame(SLOW_SYM, closes)

    i_exec = TIME_STOP_EXEC_IDX
    pre = float(df.loc[i_exec - 1, "close"])
    down = round(pre * 0.9, 2)                      # 开盘即跌停
    df.loc[i_exec, ["open", "high", "low", "close"]] = down
    df.loc[i_exec, "preclose"] = pre
    df.loc[i_exec, "pct_chg"] = -10.0
    df.loc[i_exec + 1, "preclose"] = down           # 保持前后一致

    _install(tmp_path, monkeypatch, {SLOW_SYM: df}, index_rate=0.003)
    res = _engine().run()

    deferred = res.orders[res.orders["status"] == "pending"]
    assert not deferred.empty, "跌停当日应当留下未成交（顺延）记录"
    assert deferred["note"].str.contains("跌停").any(), (
        f"顺延原因应当写明跌停，实际 {deferred['note'].tolist()}"
    )

    sells = res.orders[(res.orders["side"] == "sell") & (res.orders["status"] == "filled")]
    assert not sells.empty, "跌停打开后应当最终卖出"
    exec_day = DATE_STRS[i_exec]
    assert (sells["exec_date"] > exec_day).all(), "跌停日不得成交，成交必须晚于它"
    assert str(res.trades.iloc[0]["exit_date"]) == DATE_STRS[i_exec + 1]


def test_limit_down_at_hfq_scale_is_still_deferred(tmp_path, monkeypatch):
    """★ 后复权尺度上的开盘跌停必须被识别（旧绝对容差漏判的复现用例）。

    2026-09-18 审计发现：真实跌停价取整发生在不复权价上，hfq 复权因子把
    取整错位放大后，绝对元数容差（0.005）漏判 55%。本用例在 hfq 尺度
    （pre=150，模拟复权因子 ×15）构造"开盘跌幅距跌停价只差半分钱"的场景
    —— 旧口径判为可卖（错），比例口径判为跌停顺延（对）。
    """
    closes = _ramp(10.0, daily=0.007, signal_pct=0.04, tail_daily=-0.02)
    df = _frame(SLOW_SYM, closes)

    k = 15.0
    df[["open", "high", "low", "close", "preclose"]] = df[
        ["open", "high", "low", "close", "preclose"]
    ] * k

    i_exec = TIME_STOP_EXEC_IDX
    pre = float(df.loc[i_exec - 1, "close"])
    # 真实开盘即跌停，但价格比 round2(pre×0.9) 偏上 2 分钱（复权取整错位的形态；
    # 偏差比例 ≈ 0.011%，仍远小于 0.5% 的比例容差）
    down = pre * 0.9 + 0.02
    assert abs(down - round(pre * 0.9, 2)) > 0.005, "构造必须落在旧绝对容差的漏判区"
    assert abs(down / pre - 1 + 0.10) <= 0.005, "构造必须落在新比例容差内（触及跌停）"
    df.loc[i_exec, ["open", "high", "low", "close"]] = down
    df.loc[i_exec, "preclose"] = pre
    df.loc[i_exec, "pct_chg"] = -10.0
    df.loc[i_exec + 1, "preclose"] = down

    _install(tmp_path, monkeypatch, {SLOW_SYM: df}, index_rate=0.003)
    res = _engine().run()

    deferred = res.orders[res.orders["status"] == "pending"]
    assert not deferred.empty, "hfq 尺度的开盘跌停也应顺延，不得按理想价成交"
    assert deferred["note"].str.contains("跌停").any(), (
        f"顺延原因应写明跌停，实际 {deferred['note'].tolist()}"
    )
    exec_day = DATE_STRS[i_exec]
    sells = res.orders[(res.orders["side"] == "sell") & (res.orders["status"] == "filled")]
    assert (sells["exec_date"] > exec_day).all(), "跌停日不得成交"


# ======================================================================
# 退市 / 数据终止强平
# ======================================================================
def test_delisted_position_is_force_liquidated(tmp_path, monkeypatch):
    """★ 持仓标的数据永久终止（退市）→ 按最后可用收盘价强平。

    构造：正常买入后，把 T+3 之后的行情**整段删掉**（数据物理终止，
    模拟退市）。强平应发生在 `last_data_di + confirm(3)` 的下一个交易日，
    exit_reason=delist，orders 留有"退市强平"痕迹。
    """
    closes = _ramp(10.0, daily=0.007, signal_pct=0.04, tail_daily=0.007)
    df = _frame(SLOW_SYM, closes)

    last_row = ENTRY_IDX + 2                     # 数据终止前最后一根 K 线
    df = df.iloc[: last_row + 1].reset_index(drop=True)

    # 陪跑标的：数据完整，让行情矩阵覆盖整个回测区间
    # （矩阵日期由个股行情决定 —— 只装退市标的会让矩阵提前终止，
    #   强平日根本不在矩阵里，测试就空转了）
    frames = {SLOW_SYM: df, FLAT_SYM: _frame(FLAT_SYM, np.full(N, 10.0))}
    _install(tmp_path, monkeypatch, frames, index_rate=0.003)
    res = _engine().run()

    dl = res.trades[res.trades["exit_reason"] == "delist"]
    assert not dl.empty, "数据终止的持仓应当被退市强平"
    # last_di = last_row；di > last_di + 3 → 强平日 = last_row + 4
    expect_day = DATE_STRS[last_row + 4]
    assert (dl["exit_date"] == expect_day).all(), (
        f"强平日应为 {expect_day}，实际 {dl['exit_date'].tolist()}"
    )
    # 强平价 = 最后可用收盘价（数据终止日的 close）
    assert dl["exit_price"].iloc[0] == pytest.approx(float(df.loc[last_row, "close"]))
    # orders 留痕
    ol = res.orders[res.orders["reason"] == "delist"]
    assert not ol.empty and (ol["status"] == "filled").all()
    assert ol["note"].str.contains("退市").any()
    # 强平后持仓清零、现金到账
    after = res.equity[res.equity["date"] >= expect_day]
    assert (after["position_count"] == 0).all()


def test_suspended_position_with_future_data_is_not_liquidated(tmp_path, monkeypatch):
    """停牌（后续仍有数据）不得被误强平 —— 只挂账等待，复牌后正常卖出。

    区分退市与停牌的判据是"矩阵里还有没有后续数据"，不是"连续几天没行情"。
    """
    closes = _ramp(10.0, daily=0.007, signal_pct=0.04, tail_daily=0.007)
    df = _frame(SLOW_SYM, closes)

    # 买入后第 4~9 天停牌：数据行保留（close 沿用停牌前价），trade_status=0
    for i in range(ENTRY_IDX + 3, ENTRY_IDX + 10):
        df.loc[i, "trade_status"] = 0
        df.loc[i, ["open", "high", "low", "close"]] = df.loc[i - 1, "close"]

    _install(tmp_path, monkeypatch, {SLOW_SYM: df}, index_rate=0.003)
    res = _engine().run()

    assert res.trades[res.trades["exit_reason"] == "delist"].empty, (
        "后续仍有数据的停牌标的不应触发退市强平"
    )
    assert not res.trades.empty, "复牌后时间止损应当正常卖出"
    ts = res.trades[res.trades["exit_reason"].str.contains("time_stop", na=False)]
    assert not ts.empty


# ======================================================================
# 择时闸门
# ======================================================================
def test_timing_gate_blocks_entries_when_index_falls(tmp_path, monkeypatch):
    """★ 闸门 1：上证当日跌超 0.5%（收盘口径）→ 一轮都不开仓。"""
    frames = {
        SLOW_SYM: _frame(
            SLOW_SYM, _ramp(10.0, daily=0.007, signal_pct=0.04, tail_daily=0.007)
        )
    }
    _install(tmp_path, monkeypatch, frames, index_rate=-0.007)

    res = _engine().run()
    assert res.orders[res.orders["side"] == "buy"].empty, "大盘走弱时不应产生任何买入委托"
    assert res.trades.empty


def test_stock_without_momentum_is_never_bought(market, tmp_path):
    """闸门 3：横盘标的（今日涨幅不在 1%~5%）任何时候都不该被买入。"""
    res = _engine().run()

    touched = set(res.trades["symbol"]).union(set(res.orders["symbol"]))
    assert FLAT_SYM not in touched, "横盘标的不该被选中"


# ======================================================================
# 可复现性
# ======================================================================
def test_same_config_produces_identical_results(market, tmp_path):
    """★ 同一份配置跑两次，净值与交易必须逐行一致。

    这是验收标准之一。排序不稳定（排序键没加代码 tiebreaker）、或用到
    集合/字典的迭代顺序，都会让两次结果不同 —— 那种回测结论没法信。
    """
    a = _engine().run()
    b = _engine().run()

    pd.testing.assert_frame_equal(
        a.equity.reset_index(drop=True), b.equity.reset_index(drop=True)
    )
    pd.testing.assert_frame_equal(
        a.trades.reset_index(drop=True), b.trades.reset_index(drop=True)
    )
    pd.testing.assert_frame_equal(
        a.orders.reset_index(drop=True), b.orders.reset_index(drop=True)
    )
