"""闸门 5 的底座：「当日最高价触及涨停价」矩阵 + 近 N 日窗口查询。

★ 本文件盯的是**两份实现不能漂移**：

  `MarketData._build_limit_up_touched`（向量化，供回测热路径）与
  `tools.universe.price_limit_pct` + `at_limit_up`（标量，单一实现）
  必须**逐格一致**（含 ST ±5% / 创业板 2020-08-24 前后 10%↔20%）。

本项目最危险的失效就是这类"看着算了、其实口径不同"（历史上绝对元数容差
在复权尺度上漏判过半）。所以这里把两者钉在一起。
"""
from __future__ import annotations

import numpy as np
import pytest

from strategies.engine import MarketData
from tools.universe import at_limit_up, limit_up_price, price_limit_pct

#: 覆盖三种限幅分档：创业板 2020-08-24 之前 / 之后、ST、普通主板
DATES = ["2019-06-03", "2020-08-21", "2020-08-24", "2024-01-02"]
SYMBOLS = ["sh.600000", "sz.300001"]
PRECLOSE = 10.0


@pytest.mark.parametrize("is_st_value", [0, 1])
def test_vectorized_matches_scalar_at_limits(is_st_value: int) -> None:
    """★ 一致性：把 high 恰好放在**涨停价**上，两边都必须判为触板。"""
    n_d, n_s = len(DATES), len(SYMBOLS)
    preclose = np.full((n_d, n_s), PRECLOSE, dtype=np.float64)
    high = np.zeros((n_d, n_s), dtype=np.float64)
    for di, day in enumerate(DATES):
        for si, sym in enumerate(SYMBOLS):
            limit = price_limit_pct(sym, day, is_st_value)
            high[di, si] = limit_up_price(PRECLOSE, limit)

    got = MarketData._build_limit_up_touched(
        dates=DATES, symbols=SYMBOLS, high=high, preclose=preclose,
        is_st=np.full((n_d, n_s), is_st_value, dtype=np.int8),
        has_data=np.ones((n_d, n_s), dtype=bool),
    )
    for di, day in enumerate(DATES):
        for si, sym in enumerate(SYMBOLS):
            limit = price_limit_pct(sym, day, is_st_value)
            want = at_limit_up(high[di, si], PRECLOSE, limit)
            assert bool(got[di, si]) is want, (
                f"{sym}@{day} is_st={is_st_value}：向量化={got[di, si]} 标量={want}"
            )
            assert want is True, "构造必须落在涨停价上，否则这条用例没验到东西"


def test_vectorized_matches_scalar_below_limit() -> None:
    """★ 一致性：high 明显低于涨停价 → 两边都判 False（不能全 True 蒙过去）。"""
    preclose = np.full((len(DATES), len(SYMBOLS)), PRECLOSE, dtype=np.float64)
    high = preclose * 1.02          # 只涨 2%，远低于任何限幅

    got = MarketData._build_limit_up_touched(
        dates=DATES, symbols=SYMBOLS, high=high, preclose=preclose,
        is_st=np.zeros_like(preclose, dtype=np.int8),
        has_data=np.ones_like(preclose, dtype=bool),
    )
    assert not got.any(), "2% 的日内高点不该被判成触板"


def test_st_uses_five_percent_limit() -> None:
    """ST 股 ±5%：涨 5% 即触板；非 ST 同样幅度不算。"""
    preclose = np.full((1, 2), PRECLOSE, dtype=np.float64)
    high = preclose * 1.05
    got = MarketData._build_limit_up_touched(
        dates=["2024-01-02"], symbols=SYMBOLS, high=high, preclose=preclose,
        is_st=np.array([[1, 0]], dtype=np.int8),
        has_data=np.ones((1, 2), dtype=bool),
    )
    assert bool(got[0, 0]) is True, "sh.600000 标记为 ST：+5% 应触板"
    assert bool(got[0, 1]) is False, "sz.300001 非 ST：+5% 不是触板（创业板 20%）"


def test_gem_limit_switches_on_20200824() -> None:
    """创业板限幅分档：2020-08-21 为 ±10%（+10% 触板），2020-08-24 起 ±20%（不触板）。"""
    preclose = np.full((2, 1), PRECLOSE, dtype=np.float64)
    high = preclose * 1.10
    got = MarketData._build_limit_up_touched(
        dates=["2020-08-21", "2020-08-24"], symbols=["sz.300001"],
        high=high, preclose=preclose,
        is_st=np.zeros((2, 1), dtype=np.int8),
        has_data=np.ones((2, 1), dtype=bool),
    )
    assert bool(got[0, 0]) is True, "2020-08-21 创业板仍是 ±10% → +10% 触板"
    assert bool(got[1, 0]) is False, "2020-08-24 起创业板 ±20% → +10% 不触板"


def test_recent_limit_up_touched_window_boundaries() -> None:
    """★ 窗口 `[di-n+1, di]`（含当日）：边界日必须精确，不能差一天。"""
    # 5 个交易日、1 只标的；只在第 3 天（下标 2）触板
    touched = np.zeros((5, 1), dtype=bool)
    touched[2, 0] = True
    md = _make_market(np.cumsum(touched, axis=0, dtype=np.int16), touched)

    # di=4, n=3 → 窗口 [2,4] 含下标 2 → True
    assert md.recent_limit_up_touched("sh.600000", 4, 3) is True
    # di=4, n=2 → 窗口 [3,4] 不含下标 2 → False
    assert md.recent_limit_up_touched("sh.600000", 4, 2) is False
    # di=2, n=1 → 窗口 [2,2] 就是触板当日 → True（"含当日"的关键断言）
    assert md.recent_limit_up_touched("sh.600000", 2, 1) is True
    # di=1, n=1 → 前一日没触板 → False
    assert md.recent_limit_up_touched("sh.600000", 1, 1) is False


def test_recent_limit_up_touched_n_zero_disables() -> None:
    """`n = 0` = 关闭该门（恒 False），不管历史有没有触板。"""
    touched = np.ones((3, 1), dtype=bool)
    md = _make_market(np.cumsum(touched, axis=0, dtype=np.int16), touched)
    assert md.recent_limit_up_touched("sh.600000", 2, 0) is False
    assert md.recent_limit_up_touched("sh.600000", 2, -1) is False
    assert md.recent_limit_up_touched("sh.600000", 2, 3) is True


def test_recent_limit_up_touched_unknown_symbol() -> None:
    touched = np.ones((3, 1), dtype=bool)
    md = _make_market(np.cumsum(touched, axis=0, dtype=np.int16), touched)
    assert md.recent_limit_up_touched("sz.300999", 2, 3) is False


# ----------------------------------------------------------------------
def _make_market(prefix_wo_header: np.ndarray, touched: np.ndarray) -> MarketData:
    """直接构造 MarketData 做纯函数测试（不读盘）。

    `limit_up_prefix` 比 `limit_up_touched` 多一行（第 0 行全 0）。
    """
    n_d, n_s = touched.shape
    prefix = np.zeros((n_d + 1, n_s), dtype=np.int16)
    prefix[1:] = prefix_wo_header
    zeros = np.zeros((n_d, n_s), dtype=np.float64)
    return MarketData(
        dates=[f"2024-01-0{i + 2}" for i in range(n_d)],
        date_index={f"2024-01-0{i + 2}": i for i in range(n_d)},
        symbols=["sh.600000"],
        symbol_index={"sh.600000": 0},
        open=zeros, high=zeros, low=zeros, close=zeros, preclose=zeros,
        pct_chg=zeros.astype(np.float32),
        trade_status=np.ones((n_d, n_s), dtype=np.int8),
        is_st=np.zeros((n_d, n_s), dtype=np.int8),
        has_data=np.ones((n_d, n_s), dtype=bool),
        limit_up_touched=touched,
        limit_up_prefix=prefix,
    )
