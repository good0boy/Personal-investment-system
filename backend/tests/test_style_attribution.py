"""风格归因工具（`tools.style_attribution`）的单测。

只测**纯函数**（百分位秩 / 长短组合 / OLS），不碰真实数据与网络 ——
研究型 CLI 工具平时没有测试，但回归数学错了会让结论整体作废，所以这里钉住。
"""
from __future__ import annotations

import numpy as np
import pytest

from tools.style_attribution import _ls_return, _ols, _pct_rank


# ======================================================================
# 百分位秩
# ======================================================================
def test_pct_rank_average_ties() -> None:
    """并列值取平均名次；pct = 名次 / 个数（与 `Series.rank(pct=True)` 同口径）。"""
    r = _pct_rank(np.array([1.0, 2.0, 2.0, 4.0]))
    assert r[0] == pytest.approx(0.25)
    assert r[1] == pytest.approx(0.625)      # (2+3)/2 = 2.5 → 2.5/4
    assert r[2] == pytest.approx(0.625)
    assert r[3] == pytest.approx(1.0)


def test_pct_rank_nan_is_nan() -> None:
    r = _pct_rank(np.array([1.0, np.nan, 3.0]))
    assert np.isnan(r[1])
    assert r[0] == pytest.approx(0.5)
    assert r[2] == pytest.approx(1.0)


# ======================================================================
# 长短组合
# ======================================================================
def test_ls_return_direction_flips_sign() -> None:
    """`direction` 只决定哪一端做多 —— 换方向应当恰好反号。"""
    rank = _pct_rank(np.arange(100.0))       # 0.01 … 1.00
    ret = np.arange(100.0) * 0.01            # 因子值越大、收益越高
    long_low = _ls_return(rank, ret, 0.3, +1)    # 做多"值小"端
    long_high = _ls_return(rank, ret, 0.3, -1)   # 做多"值大"端
    assert long_low == pytest.approx(-long_high)
    assert long_low < 0


def test_ls_return_too_few_names_returns_nan() -> None:
    """分组内样本太少（<5）→ 返回 NaN，不硬算。"""
    rank = _pct_rank(np.arange(6.0))
    ret = np.arange(6.0) * 0.01
    assert np.isnan(_ls_return(rank, ret, 0.05, +1))


# ======================================================================
# OLS
# ======================================================================
def test_ols_recovers_known_beta() -> None:
    rng = np.random.default_rng(0)
    n = 500
    x = rng.normal(size=(n, 2))
    y = 0.001 + 1.5 * x[:, 0] - 0.7 * x[:, 1] + rng.normal(scale=0.01, size=n)
    res = _ols(y, x, ["a", "b"])
    assert res["names"] == ["const", "a", "b"]
    assert res["beta"][0] == pytest.approx(0.001, abs=0.002)
    assert res["beta"][1] == pytest.approx(1.5, abs=0.05)
    assert res["beta"][2] == pytest.approx(-0.7, abs=0.05)
    assert res["t"][1] > 10           # 斜率应高度显著
    assert res["r2"] > 0.9


def test_ols_alpha_insignificant_on_pure_noise() -> None:
    """纯噪声下常数项不应显著 —— 这是"没有 alpha"的判据本身。"""
    rng = np.random.default_rng(1)
    n = 800
    x = rng.normal(size=(n, 3))
    y = rng.normal(scale=0.01, size=n)
    res = _ols(y, x, ["a", "b", "c"])
    assert abs(res["t"][0]) < 1.96
    assert res["n"] == n
