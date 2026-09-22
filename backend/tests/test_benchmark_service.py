"""P0-3：股票池快照读取与等权基准口径的测试。

★ 核心要钉死的一件事：**基准必须与回测内股票池同源**。
  口径不一致时，"策略跑输基准 X 个百分点"里的 X 是错的 —— 而它不会报错，
  只会让结论看起来更保守或更激进。所以这里对"过滤 vs 不过滤"分别断言具体数值，
  而不是只断言"能跑通"。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.core.errors import AppError
from app.services import benchmark_service, data_store

DATES = ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]
IN_POOL = ("sh.600519", "sz.000001")
OUT_OF_POOL = "sh.600000"


@pytest.fixture
def bench_env(tmp_path, monkeypatch):
    """3 只标的的合成行情 + 只含其中 2 只的股票池快照。

    收益刻意不对称，便于手算：
      · 池内两只：每日 +2%
      · 池外一只：每日 +10%（"理论上不该持有"的标的）
    过滤后日收益应当**恰好** +2%，不过滤是 (2+2+10)/3 = +4.667%。
    """
    qdir = tmp_path / "quote_daily"
    udir = tmp_path / "universe"
    qdir.mkdir(parents=True, exist_ok=True)
    udir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(data_store, "QUOTE_DAILY_DIR", qdir)
    monkeypatch.setattr(data_store, "UNIVERSE_DIR", udir)

    specs = [(s, 1.02) for s in IN_POOL] + [(OUT_OF_POOL, 1.10)]
    for sym, step in specs:
        # 让 preclose 严格等于上一日 close，这样"日收益 = close/preclose − 1"
        # 恰好等于 `step − 1`，手算与断言才能对上。
        # 同时 close 逐日按 step 复利 → 买入持有区间收益 = step^(n−1) − 1。
        closes = [100.0 * step**i for i in range(len(DATES))]
        # 首日的 preclose 也要让"日收益"等于 step−1（否则首日会是 0%，
        # 累计净值少乘一步 —— 这种差一格的错最容易在断言里被放过）
        prec = [100.0 / step, *closes[:-1]]
        df = pd.DataFrame(
            {
                "date": pd.to_datetime(DATES),
                "symbol": sym,
                "open": closes,
                "high": closes,
                "low": closes,
                "close": closes,
                "preclose": prec,
                "volume": 1_000_000.0,
                "amount": 1.0e8,
                "turn": 1.0,
                "pct_chg": (step - 1.0) * 100,
                "trade_status": 1,
                "is_st": 0,
            }
        )
        data_store.write_parquet(df, qdir / f"{sym}.parquet")

    for d in DATES:
        data_store.save_universe(d, list(IN_POOL))
    return qdir, udir


def test_read_universe_range_returns_pairs(bench_env):
    out = data_store.read_universe_range(DATES[0], DATES[-1])
    assert len(out) == len(DATES) * len(IN_POOL)
    assert set(out["symbol"]) == set(IN_POOL)


def test_read_universe_range_respects_time_lock(bench_env):
    with data_store.read_until(DATES[1]):
        out = data_store.read_universe_range()
    assert set(pd.to_datetime(out["date"]).dt.strftime("%Y-%m-%d")) <= set(DATES[:2])


def test_read_universe_range_empty_when_no_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(data_store, "UNIVERSE_DIR", tmp_path / "nope")
    out = data_store.read_universe_range("2024-01-01", "2024-12-31")
    assert out.empty


def test_equal_weight_daily_uses_snapshot(bench_env):
    """对齐口径必须**排除**池外标的 —— 数值级断言，不接受"大概对"。"""
    daily = benchmark_service.universe_equal_weight_daily(DATES[0], DATES[-1], use_universe=True)
    assert len(daily) == len(DATES)
    assert set(daily["n_symbols"]) == {2}
    assert np.allclose(daily["ew_return"], 1.02 - 1.0)

    raw = benchmark_service.universe_equal_weight_daily(DATES[0], DATES[-1], use_universe=False)
    assert set(raw["n_symbols"]) == {3}
    assert np.allclose(raw["ew_return"], (1.02 + 1.02 + 1.10) / 3 - 1.0)


def test_equal_weight_daily_nav_is_cumulative(bench_env):
    daily = benchmark_service.universe_equal_weight_daily(DATES[0], DATES[-1], use_universe=True)
    assert np.allclose(daily["ew_nav"], (1.02) ** np.arange(1, len(DATES) + 1))


def test_equal_weight_daily_raises_without_snapshot(bench_env, monkeypatch):
    monkeypatch.setattr(data_store, "UNIVERSE_DIR", bench_env[0].parent / "nope")
    with pytest.raises(AppError) as ei:
        benchmark_service.universe_equal_weight_daily(DATES[0], DATES[-1], use_universe=True)
    assert "backfill_universe" in ei.value.message, "报错要指向该跑什么命令"


def test_buy_hold_universe_filter(bench_env):
    """买入持有口径：每只股票 c_last/c_first，再等权平均。

    4 个交易日、每日 ×1.02 → 区间收益 = 1.02^3 − 1（3 步，不是 4 步）。
    """
    aligned = benchmark_service.equal_weight_buy_hold(DATES[0], DATES[-1], use_universe=True)
    raw = benchmark_service.equal_weight_buy_hold(DATES[0], DATES[-1], use_universe=False)
    step = len(DATES) - 1
    assert aligned["n"] == 2
    assert np.isclose(aligned["mean"], 1.02**step - 1)
    assert raw["n"] == 3
    assert np.isclose(raw["mean"], (1.02**step * 2 + 1.10**step) / 3 - 1)


def test_summarize_fields(bench_env):
    daily = benchmark_service.universe_equal_weight_daily(DATES[0], DATES[-1], use_universe=True)
    s = benchmark_service.summarize(daily)
    assert s["days"] == len(DATES)
    assert np.isclose(s["cum_return"], 1.02**4 - 1)
    assert np.isclose(s["n_symbols_avg"], 2.0)
    assert s["max_drawdown"] <= 0


def test_pair_keys_never_collide_on_missing_symbol_code(bench_env):
    """编码缺失的标的（不在快照里）不得撞上任何合法复合键。

    这是本模块最容易出的静默错：`code` 若只按快照建，行情里池外标的 reindex
    后会得到 NaN，转 int64 变成垃圾值；一旦它恰好等于某个合法键，
    本该剔除的行就会被留下 —— 基准因此偏高，且不报错。
    """
    q = data_store.read_quotes(None, DATES[0], DATES[-1], columns=["date", "symbol"])
    snap = data_store.read_universe_range(DATES[0], DATES[-1])
    code = benchmark_service._sym_codes(q["symbol"], snap["symbol"])
    assert code.notna().all(), "并集编码不允许出现缺失"

    keys_q = benchmark_service._pair_keys(q["date"], q["symbol"], code)
    keys_s = np.unique(benchmark_service._pair_keys(snap["date"], snap["symbol"], code))
    assert len(keys_q) == len(q)
    assert set(keys_s).issubset(set(keys_q.tolist()))
    # 池外标的的键必须不在快照键集合里
    out_key = benchmark_service._pair_keys(
        pd.Series([pd.Timestamp(DATES[0])]), pd.Series([OUT_OF_POOL]), code
    )
    assert out_key[0] not in keys_s
