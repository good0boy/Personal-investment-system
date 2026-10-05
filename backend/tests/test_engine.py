"""回测引擎测试。

★ 本文件盯的是**回测里最要命的几件事**（错了不会报错，只会让收益虚高）：

1. **成交时点（v1.6 分两侧）**：
   - **买入**：信号在 T 日收盘产生 → 成交在 **T+1 开盘**（未变）；
   - **卖出/清仓**：信号在 T 日收盘产生 → 成交在 **T 日尾盘（收盘价）**；
   - **例外**：T 日收盘跌停/停牌卖不掉 → 顺延到 **T+1 开盘**重试；
   - **A 股 T+1 守卫**：当日买入的持仓当日不得卖出（会被挡到次日）。
   若把成交价或成交日写错，就等于用"已经知道结果的那一天"的价格成交 —— 未来函数。
2. **整手 100 股 + 不透支**：买入股数向下取整，且**扣得起费用**。
3. **跌停卖不掉要顺延**：不能假设跌停能成交，否则会严重低估尾部风险。
4. **时间止损口径**：`offset >= hold_days_max`（含首尾，买入成交日算第 1 天）。
   差一天就是 10% 的持有期误差。v1.6 起：**触发当日收盘即成交**。
5. **同值止盈/止损（v1.5）**：止盈为**毛**收益率 6%（两板块同值）；
   止损为毛亏损 ≥ **1.2 ×** 近5日平均振幅（两板块同值）。

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
#: v1.6：正常清仓在**信号当日尾盘按收盘价**成交，成交日 = 信号日
TIME_STOP_CLOSE_IDX = TIME_STOP_SIGNAL_IDX
#: 只有"信号当日收盘跌停/停牌"才顺延到**次日开盘**成交
TIME_STOP_DEFER_IDX = ENTRY_IDX + 10

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


def _install(
    tmp_path: Path,
    monkeypatch,
    frames: dict[str, pd.DataFrame],
    *,
    index_rate: float,
    index_closes: np.ndarray | None = None,
):
    """把合成行情写进临时目录并替换 `data_store` 的目录常量。

    `index_closes` 给定时优先用它作为指数收盘序列（用于构造"某天跌破 -1%"等
    逐日择时场景）；否则按常数日涨幅 `index_rate` 生成。
    """
    daily = tmp_path / "quote_daily"
    index = tmp_path / "quote_index"
    daily.mkdir(parents=True, exist_ok=True)
    index.mkdir(parents=True, exist_ok=True)
    for sym, df in frames.items():
        data_store.write_parquet(df, daily / f"{sym}.parquet")
    if index_closes is None:
        idx = _frame(INDEX_SYM, 3000.0 * np.cumprod(1 + np.full(N, index_rate)))
    else:
        idx = _frame(INDEX_SYM, np.asarray(index_closes, dtype=float))
    data_store.write_parquet(idx, index / f"{INDEX_SYM}.parquet")
    monkeypatch.setattr(data_store, "QUOTE_DAILY_DIR", daily)
    monkeypatch.setattr(data_store, "QUOTE_INDEX_DIR", index)
    # ★ 必须把因子目录也隔离到临时路径：否则闸门 4 会去读**真实的**
    #   data/factor（合成标的代码如 sh.600001 在真因子文件里存在、合成日期又与
    #   真实交易日部分重叠）→ 测试结果被真实数据污染、且不确定。
    #   指向空目录 → 闸门 4 走"因子缺失降级"路径（有专门的用例覆盖该分支）。
    monkeypatch.setattr(data_store, "FACTOR_DIR", tmp_path / "factor")
    return daily


def _install_factors(
    tmp_path: Path,
    monkeypatch,
    *,
    vr1: dict[str, float],
    vr5: dict[str, float],
) -> None:
    """把合成量比因子写进临时因子目录（闸门 4 测试用）。

    ⚠️ 必须 monkeypatch `data_store.FACTOR_DIR`，否则策略会读到 **真实的**
       data/factor 目录 —— 合成标的/日期不在真因子截面里（本文件其他用例正是
       靠"读不到 → 降级"来隔离的）。
    """
    fdir = tmp_path / "factor"
    (fdir / "daily").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(data_store, "FACTOR_DIR", fdir)
    for name, vals in (("volume_ratio_1", vr1), ("volume_ratio_5", vr5)):
        rows = [
            {"date": pd.Timestamp(d), "symbol": sym, "value": float(v)}
            for d in DATE_STRS
            for sym, v in vals.items()
        ]
        data_store.write_parquet(pd.DataFrame(rows), fdir / "daily" / f"{name}.parquet")


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
    """★ 时间止损口径：持有满 10 个交易日（含首尾）就必须卖，**当日收盘即成交**（v1.6）。

    买入日 = T+1（第 1 天）→ 第 10 天收盘出信号 → **同一天尾盘按收盘价成交**
    ⇒ 成交日与买入日的**交易日跨度正好是 9**（旧口径是"次日开盘成交"，跨度为 10）。
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
        assert held == 9, (
            f"应在第 10 个交易日**收盘**成交：entry={t.entry_date} exit={t.exit_date} 跨度 {held}"
        )
        assert t.hold_days == 10, "hold_days 含买入日，成交当日为第 10 天（v1.6 起不再多算 1 天）"
        assert t.entry_date == DATE_STRS[ENTRY_IDX]
        assert t.exit_date == DATE_STRS[TIME_STOP_CLOSE_IDX], (
            "v1.6：清仓成交日 = 信号日当日（不再等到次日开盘）"
        )


def test_sell_fills_at_signal_day_close(market, tmp_path):
    """★ v1.6 核心口径：卖出成交价 = **信号日 T 的后复权收盘价**（不是次日开盘价）。

    这条是本轮改造的"钉子" —— 它一旦失效，说明卖出又回到 T+1 开盘成交，
    所有回测数字都会悄悄变成另一种口径。
    """
    res = _engine().run()

    sells = res.orders[(res.orders["side"] == "sell") & (res.orders["status"] == "filled")]
    assert not sells.empty, "应当有卖出成交"

    # 夹具里既无跌停/停牌、也没有"买入当日即触发清仓" → 卖出必须**全部当日成交**
    bad = sells.loc[sells["signal_date"] != sells["exec_date"], ["signal_date", "exec_date"]]
    assert (sells["signal_date"] == sells["exec_date"]).all(), (
        f"正常清仓必须当日成交，实际 {bad.to_dict('records')}"
    )

    # 逐笔核对成交价 == 该标的该日的收盘价（用夹具行情反查）
    close_by_symbol = {
        sym: dict(zip(DATE_STRS, df["close"].astype(float), strict=True))
        for sym, df in market.items()
    }
    for o in sells.itertuples():
        expected = close_by_symbol[o.symbol][str(o.exec_date)]
        assert abs(float(o.price) - expected) < 1e-6, (
            f"{o.symbol}@{o.exec_date} 成交价 {o.price} 应等于当日收盘价 {expected}"
        )


def test_bought_today_cannot_be_sold_today(tmp_path, monkeypatch):
    """★ A 股 T+1 守卫：**当日买入的持仓当日不得卖出**。

    构造：让买入成交日**当天收盘就冲到 +11%**（把这天的开盘价压到收盘价的 90%），
    于是 `take_profit` 在买入当天就触发 —— 但持仓 `entry_idx == di`，
    收盘撮合必须**拒绝**它，卖单顺延到**次日开盘**。

    没有这道守卫，"T 日收盘卖"就会让当天早上刚买的股票当天尾盘卖掉，
    等于当日回转交易（A 股不允许），回测会凭空多出一批不存在的交易。
    """
    closes = _ramp(10.0, daily=0.007, signal_pct=0.04, tail_daily=0.007)
    df = _frame(SLOW_SYM, closes)
    # 买入当天：开盘 = 收盘的 90% → 当日收盘毛收益 +11.1% ≥ 6% → 当天就止盈
    df.loc[ENTRY_IDX, "open"] = float(df.loc[ENTRY_IDX, "close"]) * 0.90
    df.loc[ENTRY_IDX, "low"] = df.loc[ENTRY_IDX, "open"]
    _install(tmp_path, monkeypatch, {SLOW_SYM: df}, index_rate=0.003)

    res = _engine().run()

    assert len(res.trades) == 1
    t = res.trades.iloc[0]
    assert str(t["entry_date"]) == DATE_STRS[ENTRY_IDX]
    assert "take_profit" in str(t["exit_reason"]), "当天收盘毛收益已达标，应触发止盈"
    assert str(t["exit_date"]) == DATE_STRS[ENTRY_IDX + 1], (
        f"当日买入不得当日卖出：应在次日开盘成交，实际 {t['exit_date']}"
    )
    assert t["hold_days"] == 2, "买入日算第 1 天、次日卖 → 2 天"

    # 被守卫挡下的证据必须留在订单流水里（否则事后无法归因）
    # ⚠️ `regex=False`：`str.contains` 默认按正则解析，而 "T+1" 里的 `+` 是量词
    #    （会去找"一个以上 T 紧跟 1"），匹配不到字面量，会静默返回空结果。
    blocked = res.orders[res.orders["note"].str.contains("T+1", regex=False, na=False)]
    assert not blocked.empty, "应当留下 T+1 守卫的顺延记录"
    assert (blocked["signal_date"] == DATE_STRS[ENTRY_IDX]).any()
    assert (blocked["status"] == "pending").all()
    assert (blocked["exec_date"] == DATE_STRS[ENTRY_IDX]).all()


def test_close_sell_lands_in_same_day_equity(tmp_path, monkeypatch):
    """★ 收盘卖出必须落进**当日**的净值快照（含卖出费用）。

    引擎顺序是「标总资产 → on_bar → 收盘撮合卖单 → 写快照」。若顺序写反
    （先写快照再卖），当日 equity 就会**高估**一笔卖出费用、且持仓快照里
    残留已卖出的标的 —— 净值曲线与 `trades.net_pnl` 会对不上。
    """
    closes = _ramp(10.0, daily=0.007, signal_pct=0.04, tail_daily=0.007)
    _install(tmp_path, monkeypatch, {SLOW_SYM: _frame(SLOW_SYM, closes)}, index_rate=0.003)

    res = _engine().run()
    assert len(res.trades) >= 1
    t = res.trades.iloc[0]
    eq = res.equity.set_index("date")
    exit_day = str(t["exit_date"])
    entry_day = str(t["entry_date"])

    # 期间只有这一笔交易 → 退出日现金 = 买入后现金 + 卖出净得
    cash_entry = float(eq.loc[entry_day, "cash"])
    cash_exit = float(eq.loc[exit_day, "cash"])
    proceeds = float(t["exit_amount"]) - float(t["exit_cost"])
    assert abs(cash_exit - (cash_entry + proceeds)) < 1e-6, (
        f"退出日现金未计入卖出净得：{cash_exit} vs {cash_entry + proceeds}"
    )
    # 且当日快照里该持仓已消失
    assert int(eq.loc[exit_day, "position_count"]) == 0
    assert float(eq.loc[exit_day, "market_value"]) == pytest.approx(0.0, abs=1e-6)
    assert not res.positions[res.positions["date"] == exit_day].shape[0], (
        "退出日的持仓快照不应残留已卖出的标的"
    )


def test_take_profit_uses_gross_return_by_board(market, tmp_path):
    """★ 止盈 v1.4：判据是**毛收益率**（不扣成本）≥ 板块阈值。

    夹具三只都是沪主板（sh.60xxxx）→ 阈值 6%。毛收益 = 卖出价/买入价 − 1。
    """
    res = _engine().run()

    tp = res.trades[res.trades["exit_reason"].str.contains("take_profit", na=False)]
    assert not tp.empty, "快涨标的应当触发止盈"

    for t in tp.itertuples():
        assert t.symbol.startswith("sh.60"), "夹具都是主板标的"
        gross = t.exit_price / t.entry_price - 1
        # v1.6：判定在信号日 T 收盘，成交也在 T 收盘 → 成交毛收益恰为触发值（夹具上升段）
        assert gross >= 0.06 - 1e-9, f"止盈单毛收益 {gross:.4f} 应 ≥ 6%（主板）"


def test_gem_take_profit_threshold_same_as_main(tmp_path, monkeypatch):
    """★ v1.5：止盈**两板块同值 6%**（v1.4 曾是主板 6% / 创业板 10%）。

    同一价格路径下，创业板标的（sz.300xxx）也在 6% 附近止盈 —— 用来证明
    "板块阈值不再区分"真的生效，而不是代码里还留着 10% 的分支。
    """
    closes = _ramp(10.0, daily=0.007, signal_pct=0.04, tail_daily=0.03)
    frames = {"sz.300001": _frame("sz.300001", closes)}
    _install(tmp_path, monkeypatch, frames, index_rate=0.003)

    res = _engine().run()
    tp = res.trades[res.trades["exit_reason"].str.contains("take_profit", na=False)]
    assert not tp.empty
    for t in tp.itertuples():
        gross = t.exit_price / t.entry_price - 1
        assert gross >= 0.06 - 1e-9, f"创业板止盈单毛收益 {gross:.4f} 应 ≥ 6%（与主板同值）"


def test_stop_loss_triggers_for_losing_position(tmp_path, monkeypatch):
    """★ 个股止损：主板深亏单达到 **1.2 ×** 近5日均振幅 → 由 stop_loss 出局（v1.5 口径）。

    夹具振幅恒为 4%（spread=0.02）→ 阈值 ≈ **−4.8%**。一路 −2%/日 深跌会提前触发出局，
    不再扛满 10 个交易日。
    """
    closes = _ramp(10.0, daily=0.007, signal_pct=0.04, tail_daily=-0.02)
    _install(tmp_path, monkeypatch, {SLOW_SYM: _frame(SLOW_SYM, closes)}, index_rate=0.003)

    res = _engine().run()
    assert not res.trades.empty
    sl = res.trades[res.trades["exit_reason"].str.contains("stop_loss", na=False)]
    assert not sl.empty, "深亏单应由个股止损出局"
    for t in sl.itertuples():
        gross = t.exit_price / t.entry_price - 1
        assert gross <= -0.048 + 1e-9, f"止损单毛亏损 {gross:.4f} 应 ≤ −4.8%（1.2×4%）"


def test_mild_loss_does_not_trigger_stop_loss(tmp_path, monkeypatch):
    """★ 止损不得误伤：小幅回撤（未达 1.2×振幅）仍扛满 10 日、由时间止损出局。

    ⚠️ 阈值只有 -4.8%，所以这条路径必须**明显**温和（-0.3%/日 ≈ 10 日 -2.7%），
    否则余量太薄、以后微调振幅参数就会脆断。
    """
    closes = _ramp(10.0, daily=0.007, signal_pct=0.04, tail_daily=-0.003)
    _install(tmp_path, monkeypatch, {SLOW_SYM: _frame(SLOW_SYM, closes)}, index_rate=0.003)

    res = _engine().run()
    assert not res.trades.empty
    assert res.trades[res.trades["exit_reason"].str.contains("stop_loss", na=False)].empty, (
        "轻微阴跌不该触发止损"
    )
    assert not res.trades[res.trades["exit_reason"].str.contains("time_stop", na=False)].empty, (
        "轻微亏损单应由时间止损出局"
    )


# ======================================================================
# 涨跌停无法成交 → 顺延
# ======================================================================
def test_limit_down_sell_is_deferred_not_filled(tmp_path, monkeypatch):
    """★ 跌停时**没有买盘**，必须顺延重试，不能按理想价撮合。

    构造（v1.6 口径）：持有到第 10 天该卖，且**该日收盘跌停** → 当日收盘卖不掉；
    顺延到**次日开盘**才成交（这是"T 日收盘卖"的唯一例外）。
    `orders` 里应留下 pending（顺延）记录，且成交日必须晚于跌停日。
    """
    closes = _ramp(10.0, daily=0.007, signal_pct=0.04, tail_daily=-0.02)
    df = _frame(SLOW_SYM, closes)

    i_signal = TIME_STOP_SIGNAL_IDX                 # 第 10 天：既触发时间止损、也是卖出信号日
    pre = float(df.loc[i_signal - 1, "close"])
    down = round(pre * 0.9, 2)                      # 该日**收盘**跌停 → 尾盘卖不掉
    df.loc[i_signal, ["open", "high", "low", "close"]] = down
    df.loc[i_signal, "preclose"] = pre
    df.loc[i_signal, "pct_chg"] = -10.0
    df.loc[i_signal + 1, "preclose"] = down         # 保持前后一致

    _install(tmp_path, monkeypatch, {SLOW_SYM: df}, index_rate=0.003)
    # 关掉个股止损，让持仓活到第 10 天再卖 —— 本用例只验证"跌停顺延"机制
    res = _engine(params={"use_stop_loss": False}).run()

    deferred = res.orders[res.orders["status"] == "pending"]
    assert not deferred.empty, "收盘跌停当日应当留下未成交（顺延）记录"
    assert deferred["note"].str.contains("跌停").any(), (
        f"顺延原因应当写明跌停，实际 {deferred['note'].tolist()}"
    )

    sells = res.orders[(res.orders["side"] == "sell") & (res.orders["status"] == "filled")]
    assert not sells.empty, "跌停打开后应当最终卖出"
    signal_day = DATE_STRS[i_signal]
    assert (sells["exec_date"] > signal_day).all(), "跌停日不得成交，成交必须晚于它"
    # 顺延路径走的是**开盘价**（不是收盘价）
    assert str(res.trades.iloc[0]["exit_date"]) == DATE_STRS[i_signal + 1]


def test_limit_down_at_hfq_scale_is_still_deferred(tmp_path, monkeypatch):
    """★ 后复权尺度的收盘跌停必须被识别（旧绝对容差漏判的复现用例）。

    2026-09-18 审计发现：真实跌停价取整发生在不复权价上，hfq 复权因子把
    取整错位放大后，绝对元数容差（0.005）漏判 55%。本用例在 hfq 尺度
    （pre=150，模拟复权因子 ×15）构造"收盘跌幅距跌停价只差半分钱"的场景
    —— 旧口径判为可卖（错），比例口径判为跌停顺延（对）。
    """
    closes = _ramp(10.0, daily=0.007, signal_pct=0.04, tail_daily=-0.02)
    df = _frame(SLOW_SYM, closes)

    k = 15.0
    df[["open", "high", "low", "close", "preclose"]] = df[
        ["open", "high", "low", "close", "preclose"]
    ] * k

    i_signal = TIME_STOP_SIGNAL_IDX
    pre = float(df.loc[i_signal - 1, "close"])
    # 真实收盘即跌停，但价格比 round2(pre×0.9) 偏上 2 分钱（复权取整错位的形态；
    # 偏差比例 ≈ 0.011%，仍远小于 0.5% 的比例容差）
    down = pre * 0.9 + 0.02
    assert abs(down - round(pre * 0.9, 2)) > 0.005, "构造必须落在旧绝对容差的漏判区"
    assert abs(down / pre - 1 + 0.10) <= 0.005, "构造必须落在新比例容差内（触及跌停）"
    df.loc[i_signal, ["open", "high", "low", "close"]] = down
    df.loc[i_signal, "preclose"] = pre
    df.loc[i_signal, "pct_chg"] = -10.0
    df.loc[i_signal + 1, "preclose"] = down

    _install(tmp_path, monkeypatch, {SLOW_SYM: df}, index_rate=0.003)
    # 关掉个股止损，让持仓活到第 10 天再卖 —— 本用例只验证"hfq 尺度跌停顺延"
    res = _engine(params={"use_stop_loss": False}).run()

    deferred = res.orders[res.orders["status"] == "pending"]
    assert not deferred.empty, "hfq 尺度的收盘跌停也应顺延，不得按理想价成交"
    assert deferred["note"].str.contains("跌停").any(), (
        f"顺延原因应写明跌停，实际 {deferred['note'].tolist()}"
    )
    signal_day = DATE_STRS[i_signal]
    sells = res.orders[(res.orders["side"] == "sell") & (res.orders["status"] == "filled")]
    assert (sells["exec_date"] > signal_day).all(), "跌停日不得成交"


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
    """闸门 3：横盘标的（今日涨幅不在 1%~上限）任何时候都不该被买入。"""
    res = _engine().run()

    touched = set(res.trades["symbol"]).union(set(res.orders["symbol"]))
    assert FLAT_SYM not in touched, "横盘标的不该被选中"


# ======================================================================
# 择时闸门（v1.4 逐日口径）
# ======================================================================
def _buy_signal_dates(res) -> set[str]:
    buys = res.orders[res.orders["side"] == "buy"]
    return set(buys["signal_date"])


def test_timing_gate_requires_each_of_last_three_days(tmp_path, monkeypatch):
    """★ 闸门 1 逐日口径（v1.4）：近 3 个交易日**任一天**跌破 -1% → 不开仓。

    构造：信号日当天 +0.3% 没问题，但 T-2 那天跌了 -2% → 信号日不应开仓。
    对照基线（index_rate=0.003）下 `test_signal_fires_on_the_designed_day` 会开仓。
    """
    closes = _ramp(10.0, daily=0.007, signal_pct=0.04, tail_daily=0.007)
    idx = 3000.0 * np.cumprod(1 + np.full(N, 0.003))
    idx[SIGNAL_IDX - 2] = idx[SIGNAL_IDX - 3] * 0.98      # T-2：-2%
    idx[SIGNAL_IDX - 1] = idx[SIGNAL_IDX - 2] * 1.003
    idx[SIGNAL_IDX] = idx[SIGNAL_IDX - 1] * 1.003
    _install(
        tmp_path, monkeypatch, {SLOW_SYM: _frame(SLOW_SYM, closes)},
        index_rate=0.003, index_closes=idx,
    )

    res = _engine().run()
    assert DATE_STRS[SIGNAL_IDX] not in _buy_signal_dates(res), (
        "近 3 日内有交易日跌破 -1%，信号日不得开仓"
    )


def test_timing_gate_blocks_when_today_drops(tmp_path, monkeypatch):
    """闸门 1 当日下限：当日跌幅 > 0.5% → 不开仓。"""
    closes = _ramp(10.0, daily=0.007, signal_pct=0.04, tail_daily=0.007)
    idx = 3000.0 * np.cumprod(1 + np.full(N, 0.003))
    idx[SIGNAL_IDX] = idx[SIGNAL_IDX - 1] * 0.99          # 当日 -1%
    _install(
        tmp_path, monkeypatch, {SLOW_SYM: _frame(SLOW_SYM, closes)},
        index_rate=0.003, index_closes=idx,
    )

    res = _engine().run()
    assert DATE_STRS[SIGNAL_IDX] not in _buy_signal_dates(res), "当日跌超 0.5%，不得开仓"


# ======================================================================
# 闸门 3 板块差异化（v1.4）
# ======================================================================
def test_gate3_board_specific_today_ceiling(tmp_path, monkeypatch):
    """★ 闸门 3①：今日 +7% 超主板上限 6%，但仍在创业板上限 11% 内。"""
    closes = _ramp(10.0, daily=0.007, signal_pct=0.07, tail_daily=0.007)
    frames = {
        "sh.600001": _frame("sh.600001", closes),
        "sz.300001": _frame("sz.300001", closes),
    }
    _install(tmp_path, monkeypatch, frames, index_rate=0.003)

    res = _engine().run()
    touched = set(res.orders["symbol"])
    assert "sh.600001" not in touched, "主板今日 +7% 超上限 6%，不应被买"
    assert "sz.300001" in touched, "创业板今日 +7% 在 (1%, 11%) 内，应被买"


def test_gate3_ret10_ceiling_is_board_specific(tmp_path, monkeypatch):
    """★ 闸门 3② 上界：近 10 日累计涨幅偏高时，主板先被拒、创业板仍可通过。

    构造近 10 日累计 ≈ 18%：主板区间 (3%,15%) 拒绝；创业板 (5%,20%) 通过。
    """
    closes = _ramp(10.0, daily=0.015, signal_pct=0.03, tail_daily=0.007)
    frames = {
        "sh.600001": _frame("sh.600001", closes),
        "sz.300001": _frame("sz.300001", closes),
    }
    _install(tmp_path, monkeypatch, frames, index_rate=0.003)

    res = _engine().run()
    touched = set(res.orders["symbol"])
    assert "sh.600001" not in touched, "主板近 10 日累计约 18% > 15% 上界，应被拒"
    assert "sz.300001" in touched, "创业板近 10 日累计约 18% 在 (5%,20%) 内，应被买"


# ======================================================================
# 闸门 5 · 近期无涨停/触板（v1.5）
# ======================================================================
def _frame_touch_limit_up(symbol: str, closes: np.ndarray, touch_idx: int) -> pd.DataFrame:
    """在原价格路径上把**某一天的最高价**顶到涨停价（尾盘回落，收盘不变）。

    这样只触发"触板"，不影响 `close` / `pct_chg` —— 闸门 3 的判定不受干扰，
    可以干净地隔离出闸门 5 的效应。
    """
    df = _frame(symbol, closes)
    pre = float(df.loc[touch_idx, "preclose"])
    df.loc[touch_idx, "high"] = round(pre * 1.10, 2)      # 主板 +10%
    return df


def _signals(res) -> set[str]:
    return set(res.orders[res.orders["side"] == "buy"]["signal_date"])


def test_gate5_blocks_stock_touched_limit_up_inside_window(tmp_path, monkeypatch):
    """★ 闸门 5：窗口（近 10 日，含当日）内触过板 → 该股不出现在任何委托里。"""
    closes = _ramp(10.0, daily=0.007, signal_pct=0.04, tail_daily=0.007)
    df = _frame_touch_limit_up(SLOW_SYM, closes, SIGNAL_IDX - 5)   # T-5，在窗口内
    _install(tmp_path, monkeypatch, {SLOW_SYM: df}, index_rate=0.003)

    res = _engine().run()
    assert DATE_STRS[SIGNAL_IDX] not in _signals(res), "窗口内触过板，信号日不得开仓"
    assert SLOW_SYM not in set(res.orders["symbol"])


def test_gate5_allows_touch_outside_window(tmp_path, monkeypatch):
    """★ 闸门 5 的边界：触板发生在窗口**之外**（T-12）→ 正常买入。"""
    closes = _ramp(10.0, daily=0.007, signal_pct=0.04, tail_daily=0.007)
    df = _frame_touch_limit_up(SLOW_SYM, closes, SIGNAL_IDX - 12)  # 窗口 [T-9, T] 之外
    _install(tmp_path, monkeypatch, {SLOW_SYM: df}, index_rate=0.003)

    res = _engine().run()
    assert DATE_STRS[SIGNAL_IDX] in _signals(res), "窗口外触板不应影响开仓"


def test_gate5_includes_signal_day(tmp_path, monkeypatch):
    """★ 窗口**含当日 T**：当日盘中触板（收盘只涨 4%）也必须拦下。"""
    closes = _ramp(10.0, daily=0.007, signal_pct=0.04, tail_daily=0.007)
    df = _frame_touch_limit_up(SLOW_SYM, closes, SIGNAL_IDX)       # 触板就在 T 日
    _install(tmp_path, monkeypatch, {SLOW_SYM: df}, index_rate=0.003)

    res = _engine().run()
    assert DATE_STRS[SIGNAL_IDX] not in _signals(res), "含当日语义：T 日触板应被拦"


def test_gate5_disabled_when_zero(tmp_path, monkeypatch):
    """★ `no_limit_up_days = 0` → 门关闭，触过板的股照买（这也是网格里的消融档）。"""
    closes = _ramp(10.0, daily=0.007, signal_pct=0.04, tail_daily=0.007)
    df = _frame_touch_limit_up(SLOW_SYM, closes, SIGNAL_IDX - 5)
    _install(tmp_path, monkeypatch, {SLOW_SYM: df}, index_rate=0.003)

    res = _engine(params={"no_limit_up_days": 0}).run()
    assert DATE_STRS[SIGNAL_IDX] in _signals(res), "窗口=0 时不该拦任何标的"


def test_gate5_boundary_is_exactly_ten_days(tmp_path, monkeypatch):
    """★ 窗口边界精确性：T-9 在窗口内（拦），T-10 在窗口外（放行）。"""
    closes = _ramp(10.0, daily=0.007, signal_pct=0.04, tail_daily=0.007)

    df_in = _frame_touch_limit_up(SLOW_SYM, closes, SIGNAL_IDX - 9)
    _install(tmp_path, monkeypatch, {SLOW_SYM: df_in}, index_rate=0.003)
    assert DATE_STRS[SIGNAL_IDX] not in _signals(_engine().run()), "T-9 属于近 10 日，应拦"

    df_out = _frame_touch_limit_up(SLOW_SYM, closes, SIGNAL_IDX - 10)
    _install(tmp_path, monkeypatch, {SLOW_SYM: df_out}, index_rate=0.003)
    assert DATE_STRS[SIGNAL_IDX] in _signals(_engine().run()), "T-10 不属于近 10 日，应放行"


# ======================================================================
# 闸门 4 量比（v1.4）
# ======================================================================
def test_gate4_volume_ratio_filters_candidates(tmp_path, monkeypatch):
    """★ 闸门 4：1 日与 5 日量比都须 > 1.5；缺一项即不通过。"""
    closes = _ramp(10.0, daily=0.007, signal_pct=0.04, tail_daily=0.007)
    frames = {
        "sh.600001": _frame("sh.600001", closes),
        "sh.600002": _frame("sh.600002", closes),
    }
    _install(tmp_path, monkeypatch, frames, index_rate=0.003)
    _install_factors(
        tmp_path, monkeypatch,
        vr1={"sh.600001": 2.0, "sh.600002": 2.0},
        vr5={"sh.600001": 1.8, "sh.600002": 1.2},   # 600002 的 5 日量比不足
    )

    res = _engine().run()
    touched = set(res.orders["symbol"])
    assert "sh.600001" in touched, "两个量比都达标，应被买"
    assert "sh.600002" not in touched, "5 日量比 1.2 < 1.5，不应通过闸门 4"


def test_gate4_degrades_when_factor_missing(tmp_path, monkeypatch):
    """闸门 4 因子缺失 → 降级不启用（不静默拒绝所有候选）。"""
    closes = _ramp(10.0, daily=0.007, signal_pct=0.04, tail_daily=0.007)
    frames = {
        "sh.600001": _frame("sh.600001", closes),
        "sh.600002": _frame("sh.600002", closes),
    }
    _install(tmp_path, monkeypatch, frames, index_rate=0.003)
    # 指向不存在的因子目录 → read_factor 抛 DataNotReadyError → 降级
    monkeypatch.setattr(data_store, "FACTOR_DIR", tmp_path / "no_factor")

    res = _engine().run()
    touched = set(res.orders["symbol"])
    assert {"sh.600001", "sh.600002"} <= touched, "闸门 4 不可用时不应拦下任何候选"


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
