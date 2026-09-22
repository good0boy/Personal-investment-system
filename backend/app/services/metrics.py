"""回测指标计算 —— 模块 3（回测）与模块 4（模拟盘）共用。

为什么单独一个模块
----------------
同一批指标要在三个地方出现：回测详情页、参数寻优对比表、模拟盘净值页。
如果每处各算一遍，`最大回撤` 这种"看起来谁都会算"的指标迟早出现两种口径
（用净值还是用总资产？含不含当日成本？），然后两个页面显示不同的数字。

★ 本策略必须展示的两个指标（规格说明书 §2.3 / §8 验收标准）
----------------------------------------------------------------
1. **年化交易成本** —— 策略持有期短（≤10 交易日），成本是主要敌人。
   规格测算：平均持有 10 天 → 年成本约 4.9%；实际持有 6~9 天 → **6%~9%**。
   不展示它，就看不出"策略要先赚回 6~9% 才开始有超额收益"。
2. **最大单笔亏损** —— 本策略**不设个股止损**，只有时间止损。
   意味着买入后一路阴跌也必须扛满 10 个交易日，极端单笔可能亏 15%+。
   这个指标是风险敞口的唯一窗口。

口径约定（写死，不要在调用方另立）
--------------------------------
- 年化折算用 **242 个交易日/年**（A 股近年实测均值，与 `calendar_service` 无关）。
- 无风险利率取 **0**（自用系统，不引入 rf 参数；要看超额收益就减基准收益）。
- 夏普用**日收益**的年化：`mean/std × sqrt(242)`；样本标准差（`ddof=1`）。
- 「年化交易成本」分母是**平均总资产**，不是初始资金 —— 成本按每笔成交金额产生，
  净值翻倍后同样的换手会让"占总资产比例"下降，用平均总资产才反映真实侵蚀。
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

#: 年化折算用的交易日数（A 股近年实测约 242）
TRADING_DAYS_PER_YEAR = 242


def _annualise_factor(n_days: int) -> float:
    if n_days <= 0:
        return float("nan")
    return TRADING_DAYS_PER_YEAR / n_days


def _safe_ratio(numerator: float, denominator: float) -> float | None:
    """除零返回 None（而不是 inf/nan）—— 前端对 None 显示 '—'，对 inf 会崩。"""
    if not denominator or math.isnan(denominator) or denominator == 0:
        return None
    return numerator / denominator


def _num_col(df: pd.DataFrame, name: str) -> pd.Series:
    """取数值列并去掉缺失值；**列不存在时返回空 Series**。

    ⚠️ 不能写成 `pd.to_numeric(df.get(name), errors="coerce").dropna()`：
       `DataFrame.get()` 在列缺失时返回 `None`，`to_numeric(None)` 会得到
       一个 **numpy.float64 标量**（不是 Series），接着调 `.dropna()` 直接
       AttributeError。指标是"尽力而为"的展示层，缺列应当给出 None 而不是崩。
    """
    if name not in df.columns:
        return pd.Series(dtype="float64")
    return pd.to_numeric(df[name], errors="coerce").dropna()


# ======================================================================
# 净值曲线
# ======================================================================
def equity_curve(
    equity: pd.DataFrame,
    *,
    initial_cash: float,
    benchmark: pd.Series | None = None,
) -> pd.DataFrame:
    """把每日资产表补齐成可供前端直接画的净值曲线。

    输入要求：列 `date` / `total_asset`（可选 `cash` / `market_value`）。
    输出新增：`nav`（净值）/ `drawdown`（回撤，负数）/ `benchmark_nav`（可选）。
    """
    if equity.empty:
        return pd.DataFrame(
            columns=["date", "total_asset", "cash", "market_value", "nav", "drawdown"]
        )

    df = equity.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)

    if initial_cash <= 0:
        raise ValueError(f"初始资金必须为正，收到 {initial_cash}")

    df["nav"] = df["total_asset"] / float(initial_cash)
    peak = df["nav"].cummax()
    df["drawdown"] = df["nav"] / peak - 1.0

    if benchmark is not None and not benchmark.empty:
        b = pd.Series(benchmark).copy()
        b.index = pd.to_datetime(b.index)
        b = b.sort_index()
        base = float(b.iloc[0]) if float(b.iloc[0]) != 0 else 1.0
        df["benchmark_nav"] = [
            float(b.asof(d)) / base if not pd.isna(b.asof(d)) else None for d in df["date"]
        ]
    return df


# ======================================================================
# 指标
# ======================================================================
def compute_metrics(
    equity: pd.DataFrame,
    trades: pd.DataFrame,
    *,
    initial_cash: float,
) -> dict[str, Any]:
    """核心指标。

    Args:
        equity: 每日资产（至少含 `date` / `total_asset`）
        trades: **已平仓**的交易记录。需要 `net_pnl` / `net_return` /
            `hold_days` / `entry_amount` 列；缺列时相应指标返回 None。
        initial_cash: 初始资金
    """
    out: dict[str, Any] = {
        "initial_cash": float(initial_cash),
        "trading_days": 0,
        "years": None,
        "total_return": None,
        "annual_return": None,
        "max_drawdown": None,
        "max_drawdown_start": None,
        "max_drawdown_end": None,
        "annual_volatility": None,
        "sharpe": None,
        "calmar": None,
        "trade_count": 0,
        "win_count": 0,
        "lose_count": 0,
        "win_rate": None,
        "payoff_ratio": None,
        "expectancy": None,
        "avg_win": None,
        "avg_lose": None,
        "max_single_loss": None,
        "max_single_loss_amount": None,
        "max_single_win": None,
        "avg_hold_days": None,
        # ★ 规格强制展示的两个成本/风险指标
        "total_cost": None,
        "annual_cost_ratio": None,
        "cost_to_gross_profit": None,
        "turnover_annual": None,
        "final_asset": None,
    }

    if equity is None or equity.empty:
        return out

    curve = equity_curve(equity, initial_cash=initial_cash)
    n_days = len(curve)
    out["trading_days"] = int(n_days)
    out["final_asset"] = float(curve["total_asset"].iloc[-1])

    nav = curve["nav"].to_numpy(dtype=float)
    n_years = n_days / TRADING_DAYS_PER_YEAR
    out["years"] = round(n_years, 4)

    # ---- 收益 ----
    total_return = float(nav[-1] - 1.0)
    out["total_return"] = total_return
    if n_years > 0 and nav[-1] > 0:
        out["annual_return"] = float(nav[-1] ** (1.0 / n_years) - 1.0)

    # ---- 回撤 ----
    dd = curve["drawdown"].to_numpy(dtype=float)
    worst = int(np.nanargmin(dd)) if len(dd) else 0
    out["max_drawdown"] = float(dd[worst]) if len(dd) else None
    if len(dd):
        # 回撤起点 = 该点之前净值最高的那一天
        peak_idx = int(np.nanargmax(nav[: worst + 1]))
        out["max_drawdown_start"] = str(curve["date"].iloc[peak_idx])[:10]
        out["max_drawdown_end"] = str(curve["date"].iloc[worst])[:10]

    # ---- 波动与夏普 ----
    rets = pd.Series(nav).pct_change().dropna().to_numpy(dtype=float)
    if len(rets) >= 2:
        std = float(np.std(rets, ddof=1))
        out["annual_volatility"] = std * math.sqrt(TRADING_DAYS_PER_YEAR)
        if std > 0:
            out["sharpe"] = float(
                np.mean(rets) / std * math.sqrt(TRADING_DAYS_PER_YEAR)
            )
    if out["max_drawdown"] not in (None, 0) and out["annual_return"] is not None:
        out["calmar"] = _safe_ratio(out["annual_return"], abs(out["max_drawdown"]))

    # ---- 交易统计 ----
    if trades is None or trades.empty:
        return out

    t = trades.copy()
    closed = t[t.get("exit_date").notna()] if "exit_date" in t.columns else t
    out["trade_count"] = len(closed)
    if closed.empty:
        return out

    pnl = _num_col(closed, "net_pnl")
    ret = _num_col(closed, "net_return")

    if not pnl.empty:
        wins = pnl[pnl > 0]
        loses = pnl[pnl < 0]
        out["win_count"] = len(wins)
        out["lose_count"] = len(loses)
        out["win_rate"] = _safe_ratio(len(wins), len(pnl))
        out["avg_win"] = float(wins.mean()) if not wins.empty else None
        out["avg_lose"] = float(loses.mean()) if not loses.empty else None
        if not wins.empty and not loses.empty and float(loses.mean()) != 0:
            out["payoff_ratio"] = float(wins.mean() / abs(loses.mean()))
        out["expectancy"] = float(pnl.mean())

    if not ret.empty:
        # ★ 最大单笔亏损 —— 本策略不设个股止损，这是风险敞口的唯一窗口
        worst_i = ret.idxmin()
        out["max_single_loss"] = float(ret.loc[worst_i])
        out["max_single_win"] = float(ret.max())
        if "net_pnl" in closed.columns and worst_i in closed.index:
            amount = pd.to_numeric(closed.loc[worst_i, "net_pnl"], errors="coerce")
            if not pd.isna(amount):
                out["max_single_loss_amount"] = float(amount)

    hd = _num_col(closed, "hold_days")
    if not hd.empty:
        out["avg_hold_days"] = float(hd.mean())

    # ---- ★ 成本指标 ----
    cost = _num_col(closed, "cost")
    if not cost.empty:
        total_cost = float(cost.sum())
        out["total_cost"] = total_cost

        avg_asset = float(curve["total_asset"].mean())
        if avg_asset > 0 and n_years > 0:
            # 年化 = 总成本 / 平均总资产 / 年数
            out["annual_cost_ratio"] = total_cost / avg_asset / n_years

        gross = _num_col(closed, "gross_pnl")
        if not gross.empty:
            gross_profit = float(gross[gross > 0].sum())
            # 成本吃掉了多少毛收益（>1 表示毛收益被成本全部吃掉）
            out["cost_to_gross_profit"] = _safe_ratio(total_cost, gross_profit)

    amount = _num_col(closed, "entry_amount")
    if not amount.empty:
        avg_asset = float(curve["total_asset"].mean())
        if avg_asset > 0 and n_years > 0:
            # 年化双边换手（买卖各算一次金额，所以是单边金额×2）
            out["turnover_annual"] = float(amount.sum() * 2.0 / avg_asset / n_years)

    return out


def format_metrics(metrics: dict[str, Any]) -> list[tuple[str, str]]:
    """把指标格式化成 (标签, 显示值) 列表，供 CLI 打印与前端卡片复用。"""
    def pct(v: Any, digits: int = 2) -> str:
        return "—" if v is None else f"{float(v) * 100:.{digits}f}%"

    def num(v: Any, digits: int = 2) -> str:
        return "—" if v is None else f"{float(v):.{digits}f}"

    def money(v: Any) -> str:
        return "—" if v is None else f"¥{float(v):,.2f}"

    return [
        ("回测区间交易日", str(metrics.get("trading_days") or 0)),
        ("总收益率", pct(metrics.get("total_return"))),
        ("年化收益率", pct(metrics.get("annual_return"))),
        ("最大回撤", pct(metrics.get("max_drawdown"))),
        ("年化波动率", pct(metrics.get("annual_volatility"))),
        ("夏普比率", num(metrics.get("sharpe"))),
        ("Calmar", num(metrics.get("calmar"))),
        ("交易次数", str(metrics.get("trade_count") or 0)),
        ("胜率", pct(metrics.get("win_rate"))),
        ("盈亏比", num(metrics.get("payoff_ratio"))),
        ("单笔期望（元）", money(metrics.get("expectancy"))),
        ("★ 最大单笔亏损", pct(metrics.get("max_single_loss"))),
        ("   对应金额", money(metrics.get("max_single_loss_amount"))),
        ("平均持有交易日", num(metrics.get("avg_hold_days"))),
        ("★ 年化交易成本", pct(metrics.get("annual_cost_ratio"))),
        ("   累计交易成本", money(metrics.get("total_cost"))),
        ("   成本 / 毛收益", pct(metrics.get("cost_to_gross_profit"))),
        ("年化换手率", pct(metrics.get("turnover_annual"))),
        ("期末总资产", money(metrics.get("final_asset"))),
    ]
