"""指标计算测试。

★ 本文件盯的是**回测报告里那几个"必须对"的数字**：

- **年化交易成本**：本策略持有期短，成本是主要敌人（规格实测 6%~9%）。
  算错了会让人误判策略能不能活。
- **最大单笔亏损**：本策略**不设个股止损**，这是风险敞口的唯一窗口。
- 最大回撤 / 夏普 / 胜率 / 盈亏比 / 换手率的口径必须自洽。

另外还盯一个"接口层面的坑"：`equity_curve` 与 `compute_metrics` 的分母
（初始资金）与年化折算（242 交易日）必须一致，否则同一次回测在
「净值曲线」与「指标卡」上会显示两个不同的年化数字。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.services import metrics as M

INIT = 1_000_000.0


def _equity(nav_path: list[float]) -> pd.DataFrame:
    """按净值路径造每日资产表（cash/market_value 简化）。"""
    dates = pd.bdate_range("2024-01-01", periods=len(nav_path))
    total = np.asarray(nav_path, dtype=float) * INIT
    return pd.DataFrame(
        {
            "date": dates,
            "cash": total * 0.5,
            "market_value": total * 0.5,
            "total_asset": total,
            "position_count": 3,
        }
    )


def _trades(rows: list[dict]) -> pd.DataFrame:
    """按契约造交易记录（缺列自动补）。"""
    base = {
        "symbol": "sh.600000",
        "entry_date": "2024-01-02",
        "entry_price": 10.0,
        "shares": 10_000,
        "entry_amount": 100_000.0,
        "entry_cost": 76.0,
        "exit_date": "2024-01-16",
        "exit_price": 11.0,
        "exit_amount": 110_000.0,
        "exit_cost": 126.0,
        "cost": 202.0,
        "gross_pnl": 10_000.0,
        "net_pnl": 9_798.0,
        "net_return": 0.0979,
        "hold_days": 11,
        "entry_reason": "momentum",
        "exit_reason": "take_profit",
    }
    return pd.DataFrame([{**base, **r} for r in rows])


# ======================================================================
# 净值曲线
# ======================================================================
def test_equity_curve_adds_nav_and_drawdown():
    curve = M.equity_curve(_equity([1.0, 1.1, 0.9, 1.05]), initial_cash=INIT)

    assert list(curve["nav"]) == pytest.approx([1.0, 1.1, 0.9, 1.05])
    assert curve["drawdown"].iloc[0] == pytest.approx(0.0)
    assert curve["drawdown"].iloc[1] == pytest.approx(0.0)
    # 从 1.1 跌到 0.9 → -18.18%
    assert curve["drawdown"].iloc[2] == pytest.approx(0.9 / 1.1 - 1)
    assert curve["drawdown"].iloc[3] < 0


def test_equity_curve_rejects_non_positive_capital():
    with pytest.raises(ValueError):
        M.equity_curve(_equity([1.0]), initial_cash=0)


def test_equity_curve_handles_empty():
    curve = M.equity_curve(pd.DataFrame(), initial_cash=INIT)
    assert curve.empty


# ======================================================================
# 收益与回撤
# ======================================================================
def test_total_and_annual_return():
    # 242 个交易日（正好一年）翻倍 → 年化 = 100%
    nav = list(np.linspace(1.0, 2.0, M.TRADING_DAYS_PER_YEAR))
    m = M.compute_metrics(_equity(nav), pd.DataFrame(), initial_cash=INIT)

    assert m["trading_days"] == M.TRADING_DAYS_PER_YEAR
    assert m["years"] == pytest.approx(1.0)
    assert m["total_return"] == pytest.approx(1.0)
    assert m["annual_return"] == pytest.approx(1.0, rel=1e-6)


def test_max_drawdown_reports_peak_and_trough():
    m = M.compute_metrics(_equity([1.0, 1.5, 0.9, 1.2]), pd.DataFrame(), initial_cash=INIT)

    assert m["max_drawdown"] == pytest.approx(0.9 / 1.5 - 1)
    assert m["max_drawdown_start"] == str(_equity([1.0, 1.5, 0.9, 1.2])["date"].iloc[1])[:10]
    assert m["max_drawdown_end"] == str(_equity([1.0, 1.5, 0.9, 1.2])["date"].iloc[2])[:10]


def test_sharpe_is_none_when_volatility_zero():
    """净值完全不波动 → 夏普无定义，返回 None（前端显示 '—'），不能是 inf。"""
    m = M.compute_metrics(_equity([1.0, 1.0, 1.0]), pd.DataFrame(), initial_cash=INIT)
    assert m["sharpe"] is None
    assert m["annual_volatility"] == pytest.approx(0.0, abs=1e-12)


# ======================================================================
# 交易统计
# ======================================================================
def test_win_rate_payoff_and_expectancy():
    trades = _trades(
        [
            {"net_pnl": 1_000.0, "net_return": 0.10},
            {"net_pnl": 2_000.0, "net_return": 0.20},
            {"net_pnl": -500.0, "net_return": -0.05},
            {"net_pnl": -1_000.0, "net_return": -0.10},
        ]
    )
    m = M.compute_metrics(_equity([1.0, 1.01]), trades, initial_cash=INIT)

    assert m["trade_count"] == 4
    assert m["win_count"] == 2 and m["lose_count"] == 2
    assert m["win_rate"] == pytest.approx(0.5)
    assert m["avg_win"] == pytest.approx(1_500.0)
    assert m["avg_lose"] == pytest.approx(-750.0)
    # 盈亏比 = 平均盈利 / |平均亏损| = 1500 / 750 = 2
    assert m["payoff_ratio"] == pytest.approx(2.0)
    assert m["expectancy"] == pytest.approx(375.0)


def test_max_single_loss_is_the_worst_net_return():
    """★ 规格强制指标：不设个股止损 → 必须能一眼看到最惨的那一笔。"""
    trades = _trades(
        [
            {"net_return": -0.05, "net_pnl": -500.0},
            {"net_return": -0.2787, "net_pnl": -63_329.0},
            {"net_return": 0.12, "net_pnl": 1_200.0},
        ]
    )
    m = M.compute_metrics(_equity([1.0, 1.01]), trades, initial_cash=INIT)

    assert m["max_single_loss"] == pytest.approx(-0.2787)
    assert m["max_single_loss_amount"] == pytest.approx(-63_329.0)
    assert m["max_single_win"] == pytest.approx(0.12)


def test_avg_hold_days():
    trades = _trades([{"hold_days": 5}, {"hold_days": 11}])
    m = M.compute_metrics(_equity([1.0, 1.01]), trades, initial_cash=INIT)
    assert m["avg_hold_days"] == pytest.approx(8.0)


# ======================================================================
# ★ 成本指标
# ======================================================================
def test_annual_cost_ratio_uses_mean_asset_not_initial_cash():
    """★ 年化交易成本的分母是**平均总资产**（净值翻倍后同样的换手侵蚀更小）。

    规格给的是"年成本占总资产比例"，用平均总资产才反映真实侵蚀。
    """
    n = M.TRADING_DAYS_PER_YEAR
    nav = list(np.linspace(1.0, 3.0, n))
    equity = _equity(nav)
    trades = _trades([{"cost": 10_000.0}] * 4)

    m = M.compute_metrics(equity, trades, initial_cash=INIT)

    mean_asset = equity["total_asset"].mean()
    expected = 40_000.0 / mean_asset / (n / M.TRADING_DAYS_PER_YEAR)
    assert m["total_cost"] == pytest.approx(40_000.0)
    assert m["annual_cost_ratio"] == pytest.approx(expected)
    # 反证：若用初始资金作分母，会明显偏大
    assert m["annual_cost_ratio"] < 40_000.0 / INIT


def test_cost_to_gross_profit_ratio():
    trades = _trades(
        [
            {"gross_pnl": 10_000.0, "cost": 1_000.0},
            {"gross_pnl": -4_000.0, "cost": 1_000.0},
        ]
    )
    m = M.compute_metrics(_equity([1.0, 1.01]), trades, initial_cash=INIT)
    # 只统计**盈利笔**的毛收益作分母
    assert m["cost_to_gross_profit"] == pytest.approx(2_000.0 / 10_000.0)


def test_annual_turnover_counts_both_sides():
    n = M.TRADING_DAYS_PER_YEAR
    equity = _equity([1.0] * n)
    trades = _trades([{"entry_amount": 200_000.0}] * 5)

    m = M.compute_metrics(equity, trades, initial_cash=INIT)
    # 5 笔 × 20 万 × 2（买卖双边）/ 平均总资产 100 万 / 1 年 = 200%
    assert m["turnover_annual"] == pytest.approx(2.0)


def test_metrics_are_none_safe_for_missing_columns():
    """缺列时返回 None 而不是抛异常 —— 指标是"尽力而为"的展示层。"""
    m = M.compute_metrics(_equity([1.0, 1.05]), pd.DataFrame([{"symbol": "x"}]), initial_cash=INIT)
    assert m["max_single_loss"] is None
    assert m["annual_cost_ratio"] is None
    assert m["total_return"] == pytest.approx(0.05)


def test_empty_equity_returns_blank_metrics():
    m = M.compute_metrics(pd.DataFrame(), pd.DataFrame(), initial_cash=INIT)
    assert m["trading_days"] == 0
    assert m["total_return"] is None


# ======================================================================
# 展示层
# ======================================================================
def test_format_metrics_renders_placeholders_for_none():
    m = M.compute_metrics(_equity([1.0, 1.05]), pd.DataFrame(), initial_cash=INIT)
    rendered = dict(M.format_metrics(m))

    assert "★ 年化交易成本" in rendered
    assert "★ 最大单笔亏损" in rendered
    assert rendered["总收益率"] == "5.00%"
    # 没有交易 → 相关指标显示占位符而不是 "nan%"
    assert rendered["平均持有交易日"] == "—"
    assert rendered["★ 最大单笔亏损"] == "—"
    assert rendered["★ 年化交易成本"] == "—"
