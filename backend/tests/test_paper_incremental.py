"""增量撮合一致性对拍（2026-10-10 方案的上线硬门槛）。

对拍设计（方案 §4）：
    同一份合成数据，
    A. 全量：BacktestEngine(start=D0, end=Dn, preheat=PREHEAT_START) 一次跑完；
    B. 增量：全量跑到 Dk（冷启动，export_state）→ 之后逐日 PaperEngine.run_one_day()。
    断言 5 类产物（equity/trades/positions/orders + 期末状态）**逐位一致**
    （assert_frame_equal check_exact=True）。

复用 test_quality_reversal 的合成数据夹具（200 个交易日、4 只标的、
因子文件全日期）—— 那套夹具已被 20+ 个引擎行为测试钉过。
"""
from __future__ import annotations

import sys
from itertools import pairwise
from pathlib import Path

import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from strategies.engine import BacktestEngine  # noqa: E402
from strategies.paper_engine import PaperEngine  # noqa: E402
from strategies.registry import get_strategy  # noqa: E402
from tests.test_quality_reversal import (  # noqa: E402
    BT_START,
    DATE_STRS,
    _carrier,
    _install,
    _install_factors,
    _params,
)

N_DAYS = len(DATE_STRS)
DK_IDX = N_DAYS - 30        # 冷启动终点（倒数第 30 日）—— 留 29 日给增量段
DN_IDX = N_DAYS - 1         # 对拍终点
DK, DN = DATE_STRS[DK_IDX], DATE_STRS[DN_IDX]
PREHEAT_START = DATE_STRS[0]  # 全量口径的预热起点（与 runner 同语义的"足够早"）


@pytest.fixture
def synth_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """合成行情 + 因子（走 test_quality_reversal 的成熟夹具）。"""
    from app.services import data_store

    _install(tmp_path, monkeypatch, drift={"sh.600003": 0.004, "sh.600004": -0.006})
    _install_factors(_carrier())
    return data_store


def _mk_engine(mode: str, run_date: str, state: dict | None):
    """按生产口径构造引擎：每个"交易日"新建策略实例（与 paper_service 一致）。"""
    # ★ rebalance_every 必须用**生产值 5**：原测试用 1，而任何数都能整除 1，
    #   于是"建仓节奏"这条分支在对拍里恒成立、从未被真正验证过 ——
    #   增量模式 `di % 1 == 0` 恒真（每天都建仓）这个 bug 就这样躲过了对拍门。
    strategy = get_strategy(
        "quality_reversal", _params(reversal_pct=0.75, rebalance_every=5)
    )
    common = {
        "initial_cash": 1_000_000.0,
        "max_positions": 5,
        "max_weight_per_stock": 0.20,
        "benchmark": "sh.000001",
    }
    if mode == "full":
        if run_date == BT_START:
            return BacktestEngine(
                strategy, start=run_date, end=run_date,
                preheat_start=PREHEAT_START, **common,
            )
        raise ValueError("仅冷启动日使用 full")
    return PaperEngine(
        strategy, run_date=run_date, state=state,
        listed_days_min=90, no_rebuy_days=60, delist_confirm_days=3,
        # 建仓节奏锚点与全量一致（生产传 PAPER_START_DATE，这里传 BT_START）
        rebalance_anchor=BT_START,
        trading_days=list(DATE_STRS),
        **common,
    )


def test_incremental_matches_full_bitwise(synth_env) -> None:
    """★ 对拍门：全量一次跑 vs 冷启动+逐日增量，5 类产物逐位一致。

    equity/trades/positions/orders 逐位（check_exact=True）；
    state 用"再跑一天应等价"的间接校验（见 test_state_roundtrip_continues）。

    ★ `rebalance_every=5`（生产值）：用 1 的话"建仓节奏"分支恒成立、等于没测，
      增量每天建仓的 bug 会直接漏过（2026-10-11 阻塞级事故的根因）。
    """
    params = _params(reversal_pct=0.75, rebalance_every=5)

    # ---- A. 全量 ----
    full = BacktestEngine(
        get_strategy("quality_reversal", params),
        start=BT_START, end=DN, preheat_start=PREHEAT_START,
        initial_cash=1_000_000.0, max_positions=5, max_weight_per_stock=0.20,
        benchmark="sh.000001", listed_days_min=90, no_rebuy_days=60,
        delist_confirm_days=3,
        # 显式注入合成交易日历（不依赖全局 calendar 单例，避免别的用例换日历）
        trading_days=list(DATE_STRS),
    )
    ref = full.run()

    # ---- B. 冷启动（全量跑到 DK）+ 逐日增量 ----
    cold = BacktestEngine(
        get_strategy("quality_reversal", params),
        start=BT_START, end=DK, preheat_start=PREHEAT_START,
        initial_cash=1_000_000.0, max_positions=5, max_weight_per_stock=0.20,
        benchmark="sh.000001", listed_days_min=90, no_rebuy_days=60,
        delist_confirm_days=3,
        # 显式注入合成交易日历（不依赖全局 calendar 单例，避免别的用例换日历）
        trading_days=list(DATE_STRS),
    )
    cold_result = cold.run()
    state = cold.export_state()

    inc_equity = [cold_result.equity]
    inc_trades = [cold_result.trades]
    inc_positions = [cold_result.positions]
    inc_orders = [cold_result.orders]

    for day in DATE_STRS[DK_IDX + 1 : DN_IDX + 1]:
        pe = _mk_engine("incremental", day, state)
        r = pe.run_one_day()
        state = pe.export_state()
        inc_equity.append(r.equity)
        inc_trades.append(r.trades)
        inc_positions.append(r.positions)
        inc_orders.append(r.orders)

    from strategies.base import (
        EQUITY_COLUMNS,
        ORDER_COLUMNS,
        POSITION_COLUMNS,
        TRADE_COLUMNS,
        ensure_contract,
    )

    # ensure_contract 统一列契约与 dtype（引擎原始 equity 无 nav 列，
    # concat 会引入 nullable dtype 的 <NA> —— 与全量的 NaN 只是表示差异）
    inc = {
        "equity": ensure_contract(pd.concat(inc_equity, ignore_index=True), EQUITY_COLUMNS),
        "trades": ensure_contract(pd.concat(inc_trades, ignore_index=True), TRADE_COLUMNS),
        "positions": ensure_contract(pd.concat(inc_positions, ignore_index=True), POSITION_COLUMNS),
        "orders": ensure_contract(pd.concat(inc_orders, ignore_index=True), ORDER_COLUMNS),
    }
    ref_dfs = {
        "equity": ensure_contract(ref.equity, EQUITY_COLUMNS),
        "trades": ensure_contract(ref.trades, TRADE_COLUMNS),
        "positions": ensure_contract(ref.positions, POSITION_COLUMNS),
        "orders": ensure_contract(ref.orders, ORDER_COLUMNS),
    }

    # orders.shares 的 dtype 是"值的函数"而非撮合差异：全是成交行时是 int64，
    # 混入未成交行（None→NaN）后是 float64。比较前统一成 float64。
    for d in (inc["orders"], ref_dfs["orders"]):
        d["shares"] = pd.to_numeric(d["shares"], errors="coerce").astype("float64")

    # equity 的 nav/drawdown/benchmark_nav 是 metrics 层补的列（引擎侧全 NA），
    # object-NA 与 float-NaN 的表示差异不属于撮合结果 —— 单独按"是否全 NA"校验。
    NA_ONLY = {"equity": ("nav", "drawdown", "benchmark_nav")}
    for name in ("equity", "trades", "positions", "orders"):
        a, b = ref_dfs[name], inc[name]
        assert len(a) == len(b), f"{name} 行数不一致：全量 {len(a)} vs 增量 {len(b)}"
        drop = list(NA_ONLY.get(name, ()))
        try:
            assert_frame_equal(
                a.drop(columns=drop).reset_index(drop=True),
                b.drop(columns=drop).reset_index(drop=True),
                check_exact=True,
            )
            for c in drop:
                assert a[c].isna().all() and b[c].isna().all(), f"{name}.{c} 应全为 NA"
        except AssertionError as exc:
            pytest.fail(f"★ 对拍失败（{name}）：增量与全量存在逐位差异\n{exc}")

    # ---- 建仓节奏的"非退化"断言（逐位一致之上再钉一层语义）----
    # 逐位一致若发生在"两边都不建仓"或"两边每天都建仓"上，依然算通过，
    # 所以必须显式验证建仓日确实是"每 5 个交易日一次"、且两边落在同一天。
    def _entry_days(df: pd.DataFrame) -> list[str]:
        if "side" not in df.columns or df.empty:
            return []
        return sorted({str(d)[:10] for d in df.loc[df["side"] == "buy", "signal_date"]})

    ref_days, inc_days = _entry_days(ref_dfs["orders"]), _entry_days(inc["orders"])
    assert ref_days == inc_days, (
        f"建仓日不一致：全量 {ref_days} vs 增量 {inc_days}"
    )
    # 相邻建仓日的间隔必须 >= rebalance_every（每日建仓 = 间隔 1 = 回归）
    dates_sorted = sorted(set(full.days))
    pos = {d: i for i, d in enumerate(dates_sorted)}
    gaps = [pos[b] - pos[a] for a, b in pairwise(ref_days) if b in pos]
    assert all(g >= 5 for g in gaps), f"建仓间隔应 >= 5 个交易日，实际 {gaps}"


def test_state_roundtrip_continues(synth_env) -> None:
    """★ 状态 round-trip：两段增量之间（跨夜落盘/重读）结果必须连续一致。

    模拟"状态经过 JSON 序列化再读回"（生产路径 paper_service 的 state.json），
    断言序列化 round-trip 不改变后续撮合结果。
    """
    import json

    params = _params(reversal_pct=0.75)

    cold = BacktestEngine(
        get_strategy("quality_reversal", params),
        start=BT_START, end=DK, preheat_start=PREHEAT_START,
        initial_cash=1_000_000.0, max_positions=5, max_weight_per_stock=0.20,
        benchmark="sh.000001", listed_days_min=90, no_rebuy_days=60,
        delist_confirm_days=3,
        # 显式注入合成交易日历（不依赖全局 calendar 单例，避免别的用例换日历）
        trading_days=list(DATE_STRS),
    )
    cold.run()
    state = cold.export_state()

    day1, day2 = DATE_STRS[DK_IDX + 1], DATE_STRS[DK_IDX + 2]

    # 路径 1：state 在内存中直接传递
    pe1 = _mk_engine("incremental", day1, state)
    pe1.run_one_day()
    st1 = pe1.export_state()

    # 路径 2：state 先过 JSON round-trip（生产语义）
    st_rt = json.loads(json.dumps(state))
    pe2 = _mk_engine("incremental", day1, st_rt)
    pe2.run_one_day()
    st2 = pe2.export_state()

    assert st1["cash"] == st2["cash"], "JSON round-trip 后资金链必须逐位一致"
    assert st1["positions"] == st2["positions"]
    assert st1["pending"] == st2["pending"]
    assert st1["last_sell_date"] == st2["last_sell_date"]

    # 次日（day2）从两条路径的 state 出发，撮合结果一致
    p_a = _mk_engine("incremental", day2, st1)
    r_a = p_a.run_one_day()
    p_b = _mk_engine("incremental", day2, st2)
    r_b = p_b.run_one_day()
    assert_frame_equal(r_a.equity, r_b.equity, check_exact=True)
    assert_frame_equal(r_a.trades, r_b.trades, check_exact=True)
