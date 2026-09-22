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
    render_summary,
)

CFG = {"strategy": "momentum", "objective": "annual_return",
       "grid": {"hold_days_max": [10, 20], "take_profit_net": [0.05, 0.1]}}
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
            rows.append({"hold_days_max": h, "take_profit_net": tp,
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
                                   series="take_profit_net",
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
                                   series="take_profit_net",
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
