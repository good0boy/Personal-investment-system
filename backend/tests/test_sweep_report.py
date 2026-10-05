"""参数寻优的报告渲染测试。

★ 本文件盯的是**报告里的结论有没有真的被呈现出来**，而不是指标算得对不对
（指标口径在 test_metrics.py）。

两个真实踩过的坑，各锁一条：

1. **同表两种口径**：目标指标 `annual_return` 走了 `_fmt_num` → 显示 `-0.05`，
   而紧邻的 `neighbor_mean`（同一个指标）走 `_fmt_pct` → 显示 `-8.20%`。
   同一张表两种口径，读的人会把 `-0.05` 当成"只亏 5 分钱"而不是 5%。

2. **算了却不呈现**：`analyse_sensitivity()` 跑完把 `sens` 一路传进
   `render_summary`，但函数体里**从未引用** —— 报告只有"看相邻列的变化幅度"
   这句空话。纪律 2 写着"强制敏感性分析"，代码也算了，结论却没给读者看到。
   这类"看着像做了"的失效最危险。
"""
from __future__ import annotations

import pandas as pd
import pytest

from strategies.sweep import (
    PCT_METRICS,
    _fmt_metric,
    analyse_sensitivity,
    best_single,
    pick_plateau,
    render_summary,
)

CFG = {"strategy": "momentum", "objective": "annual_return",
       "grid": {"hold_days_max": [10, 20], "take_profit_gross_main": [0.05, 0.1]}}
INTERVAL = {"name": "in_sample", "start": "2020-06-01", "end": "2024-12-31",
            "preheat_start": "2020-01-01", "is_out_sample": False}


# ----------------------------------------------------------------------
# 口径统一
# ----------------------------------------------------------------------
@pytest.mark.parametrize("name", sorted(PCT_METRICS))
def test_pct_metrics_render_as_percent(name: str) -> None:
    """所有百分比类指标必须以 `%` 结尾 —— 不能裸出小数。"""
    assert _fmt_metric(name, 0.0812).endswith("%")


def test_annual_return_and_neighbor_mean_same_scale() -> None:
    """★ 同一个指标的两个列必须同口径。

    这就是"同表两种口径"的最小复现：`annual_return` 与 `neighbor_mean`
    本质是同一个东西（前者是本组、后者是邻域），展示尺度必须一致。
    """
    a = _fmt_metric("annual_return", 0.0812)
    b = _fmt_metric("neighbor_mean", 0.0812)
    assert a == b == "8.12%"


def test_count_metrics_render_as_integer() -> None:
    assert _fmt_metric("trade_count", 500) == "500"
    assert _fmt_metric("trade_count", None) == "—"
    assert _fmt_metric("avg_hold_days", 12.7) == "12"


def test_ratio_metrics_render_as_number() -> None:
    """夏普这类无量纲比率不加 `%`。"""
    out = _fmt_metric("sharpe", 0.53)
    assert out == "0.53" and not out.endswith("%")


def test_nan_renders_as_dash() -> None:
    assert _fmt_metric("annual_return", float("nan")) == "—"
    assert _fmt_metric("sharpe", None) == "—"


# ----------------------------------------------------------------------
# 敏感性分析必须被渲染
# ----------------------------------------------------------------------
def _grid_df() -> pd.DataFrame:
    rows = []
    for h in (10, 20):
        for tp in (0.05, 0.1):
            # 10 档：两档都不错；20 档：整体塌掉 → 该轴"很敏感"
            ret = 0.18 if h == 10 else 0.05
            rows.append({"hold_days_max": h, "take_profit_gross_main": tp,
                         "annual_return": ret, "max_drawdown": -0.3,
                         "annual_cost_ratio": 0.05, "sharpe": 0.5,
                         "trade_count": 100, "failed": False})
    return pd.DataFrame(rows)


def test_sensitivity_section_is_present_in_summary() -> None:
    """★ 核心断言：敏感性结论必须真的出现在报告里。

    修复前 `sens` 传进来却没人用，报告里搜不到"敏感性分析"这一节。
    """
    df = _grid_df()
    df, sens = analyse_sensitivity(df, x="hold_days_max",
                                   series="take_profit_gross_main",
                                   objective="annual_return")
    text = render_summary(
        sweep_id="t-1", cfg=CFG, interval=INTERVAL, df=df, sens=sens,
        objective="annual_return", plateau=None, single=None,
        combos=[{}, {}], elapsed=1.0, debug_symbols=None,
    )
    assert "## 敏感性分析" in text
    # 透视表：x 的两个取值都应作为表头出现
    assert ("`10`" in text) and ("`20`" in text)
    # 必须给出"挪一格的变化幅度"这个可直接判断的依据
    assert "变化幅度" in text
    assert "很敏感" in text or "偏敏感" in text or "平稳" in text


def test_summary_has_no_raw_decimal_for_pct_objective() -> None:
    """★ 报告里不该出现 `-0.05` 这种裸小数（口径统一后应带 `%`）。"""
    df = _grid_df()
    df, sens = analyse_sensitivity(df, x="hold_days_max",
                                   series="take_profit_gross_main",
                                   objective="annual_return")
    text = render_summary(
        sweep_id="t-2", cfg=CFG, interval=INTERVAL, df=df, sens=sens,
        objective="annual_return", plateau=None, single=None,
        combos=[{}, {}], elapsed=1.0, debug_symbols=None,
    )
    table = text.split("## 全网格结果")[1].split("##")[0]
    assert "| 5.00% |" in table          # 20 档的 annual_return
    assert "| 18.00% |" in table


def test_sensitivity_degrades_when_axis_unavailable() -> None:
    """没有可用轴时优雅降级，不能抛异常把整个报告搞挂。"""
    df = _grid_df()
    text = render_summary(
        sweep_id="t-3", cfg=CFG, interval=INTERVAL, df=df,
        sens={"available": False}, objective="annual_return",
        plateau=None, single=None, combos=[{}], elapsed=1.0, debug_symbols=None,
    )
    assert "本节省略" in text


def test_debug_mode_is_flagged_loudly() -> None:
    """调试模式的报告必须显著标注"结论不可用"。"""
    df = _grid_df()
    txt = render_summary(
        sweep_id="t-4", cfg=CFG, interval=INTERVAL, df=df,
        sens={"available": False}, objective="annual_return",
        plateau=None, single=None, combos=[{}], elapsed=1.0,
        debug_symbols=["sh.600000", "sz.000001"],
    )
    assert "⚠️ 调试模式" in txt
    assert "不可用于评估策略" in txt


# ----------------------------------------------------------------------
# 联合维度（v1.4）与全负目标指标
# ----------------------------------------------------------------------
JOINT_CFG = {
    "strategy": "momentum",
    "objective": "annual_return",
    "grid": {
        "hold_days_max": [10],
        "take_profit": [
            {"take_profit_gross_main": 0.06, "take_profit_gross_gem": 0.10},
            {"take_profit_gross_main": 0.12, "take_profit_gross_gem": 0.15},
        ],
    },
}


def _joint_df() -> pd.DataFrame:
    def row(tp_main: float, tp_gem: float, own: float) -> dict:
        return {
            "hold_days_max": 10,
            "take_profit_gross_main": tp_main,
            "take_profit_gross_gem": tp_gem,
            "annual_return": own,
            "max_drawdown": -0.5,
            "annual_cost_ratio": 0.03,
            "sharpe": -0.5,
            "trade_count": 200,
            "failed": False,
            "is_spike": False,
        }

    return pd.DataFrame([row(0.06, 0.10, -0.095), row(0.12, 0.15, -0.047)])


def test_joint_dimension_columns_appear_in_summary() -> None:
    """★ 联合维度展开后的参数名必须出现在报告里（v1.4-sweep 踩到）。

    修复前用 `cfg["grid"]` 的原始键取列 —— 而 `take_profit` 在结果表里根本不存在
    （它展开成 `_main`/`_gem`）→ 推荐组合与全网格表**丢掉止盈档位**，
    且"只差止盈档"的两组会被误判成**完全一致**。
    """
    df = _joint_df()
    df, sens = analyse_sensitivity(
        df, x="hold_days_max", series="take_profit_gross_main", objective="annual_return"
    )
    plateau = pick_plateau(df, "annual_return")
    single = best_single(df, "annual_return")
    text = render_summary(
        sweep_id="t-joint", cfg=JOINT_CFG, interval=INTERVAL, df=df, sens=sens,
        objective="annual_return", plateau=plateau, single=single,
        combos=[{}, {}], elapsed=1.0, debug_symbols=None,
    )
    head = text.split("## 全网格结果")[0]
    # 展开后的止盈参数名必须出现（推荐组合里就要带上）
    assert "take_profit_gross_main" in head
    assert "take_profit_gross_gem" in head
    # 全网格表也要有这两个参数列
    table = text.split("## 全网格结果", 1)[1].split("\n## ", 1)[0]
    assert "take_profit_gross_main" in table and "take_profit_gross_gem" in table
    # 单点最优与高原只差止盈档 → 必须如实并列，**不能**谎报"一致"
    assert "单点最优为" in head
    assert "一致" not in head


def test_summary_warns_when_all_objective_values_negative() -> None:
    """★ 目标指标全为负时必须提示"尖峰判定口径不适用"。

    「尖峰」判据是『邻居均值 ÷ 本组值』的比值，隐含"收益为正"；全负时它永不触发，
    "0 个尖峰"会被误读成"没有噪声点"。
    """
    df = _joint_df()
    df, sens = analyse_sensitivity(
        df, x="hold_days_max", series="take_profit_gross_main", objective="annual_return"
    )
    text = render_summary(
        sweep_id="t-neg", cfg=JOINT_CFG, interval=INTERVAL, df=df, sens=sens,
        objective="annual_return", plateau=None, single=None,
        combos=[{}, {}], elapsed=1.0, debug_symbols=None,
    )
    assert "全为负" in text


def test_neighbor_mean_averages_cells_with_multiple_rows() -> None:
    """★ 邻域均值必须按**格子均值**算，不能"后写覆盖"。

    真实网格里每个 (x, series) 格子通常含多行（其余维度也在网格中）。
    修复前是 `lookup[key] = value`，只留每格最后一行 → 高原/尖峰判定失真，
    而 `pick_plateau` 正是按 neighbor_mean 选参的。
    """
    df = pd.DataFrame([
        {"hold_days_max": 10, "annual_return": 0.10},
        {"hold_days_max": 10, "annual_return": 0.20},
        {"hold_days_max": 20, "annual_return": 0.30},
        {"hold_days_max": 20, "annual_return": 0.40},
    ])
    out, _ = analyse_sensitivity(
        df, x="hold_days_max", series=None, objective="annual_return"
    )
    at10 = out.loc[out["hold_days_max"] == 10, "neighbor_mean"].tolist()
    at20 = out.loc[out["hold_days_max"] == 20, "neighbor_mean"].tolist()
    # x=10 的邻居格是 x=20：均值 (0.30+0.40)/2 = 0.35（而非最后一行 0.40）
    assert at10 == [pytest.approx(0.35), pytest.approx(0.35)]
    # x=20 的邻居格是 x=10：均值 (0.10+0.20)/2 = 0.15（而非最后一行 0.20）
    assert at20 == [pytest.approx(0.15), pytest.approx(0.15)]
