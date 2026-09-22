"""样本内 vs 样本外归因对比 —— 一次性诊断脚本，跑完可删。

结论用途：判断 v1.2 规则的失效是「结构性」还是「样本内过拟合」。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

IN_SAMPLE = "data/backtest/momentum/20260918-170311-5273a1dc"  # full-market-v1
OUT_SAMPLE = "data/backtest/momentum/20260918-171651-b65bfdcf"  # v1.2-out
ROOT = Path(__file__).resolve().parents[2]


def load(rel: str) -> tuple[dict, pd.DataFrame]:
    d = ROOT / rel
    m = json.loads((d / "metrics.json").read_text(encoding="utf-8"))
    t = pd.read_parquet(d / "trades.parquet")
    return m, t


def primary(reason: str) -> str:
    """exit_reason 可能是 `a|b` 组合，取第一个（引擎的判定顺序）。"""
    return str(reason).split("|")[0]


def attribution(t: pd.DataFrame) -> pd.DataFrame:
    t = t.copy()
    t["primary"] = t["exit_reason"].map(primary)
    g = t.groupby("primary").agg(
        笔数=("net_pnl", "size"),
        累计盈亏=("net_pnl", "sum"),
        平均收益=("net_return", "mean"),
        胜率=("net_return", lambda s: (s > 0).mean()),
        平均持有=("hold_days", "mean"),
    )
    g["占比"] = g["笔数"] / len(t)
    return g.sort_values("累计盈亏")


def main() -> None:
    out: list[str] = []
    for label, rel in (("样本内 2020-06 ~ 2024-12", IN_SAMPLE), ("样本外 2025-01 ~ 2026-09", OUT_SAMPLE)):
        m, t = load(rel)
        out.append(f"\n{'=' * 78}\n{label}\n{'=' * 78}")
        out.append(
            f"区间交易日 {m['trading_days']}｜交易 {m['trade_count']} 笔｜"
            f"总收益 {m['total_return']:.2%}｜年化 {m['annual_return']:.2%}｜"
            f"最大回撤 {m['max_drawdown']:.2%}｜夏普 {m['sharpe']:.2f}"
        )
        out.append(
            f"胜率 {m['win_rate']:.2%}｜盈亏比 {m['payoff_ratio']:.2f}｜"
            f"平均持有 {m['avg_hold_days']:.2f} 日｜年化换手 {m['turnover_annual']:.0f} 倍｜"
            f"年化成本 {m['annual_cost_ratio']:.2%}"
        )
        out.append(
            f"平均盈利 {m['avg_win']:,.0f}｜平均亏损 {m['avg_lose']:,.0f}｜期望 {m['expectancy']:,.0f}/笔"
        )
        out.append("")
        out.append(attribution(t).to_string(float_format=lambda x: f"{x:,.4f}"))

    # 入场跳空：样本内/外都要看，这是根因 1
    out.append(f"\n{'=' * 78}\n入场跳空（T+1 开盘 vs 信号日 T 收盘）\n{'=' * 78}")
    for label, rel in (("样本内", IN_SAMPLE), ("样本外", OUT_SAMPLE)):
        _, t = load(rel)
        # entry_price 已是 T+1 开盘；用 net_return 反推不了 T 收盘，这里只看入场后表现
        out.append(
            f"{label}：入场金额均值 {t['entry_amount'].mean():,.0f}｜"
            f"成本均值 {t['cost'].mean():,.0f}/笔｜"
            f"亏损笔占比 {(t['net_return'] <= 0).mean():.2%}"
        )

    Path(ROOT / "data" / "_attr_report.txt").write_text("\n".join(out), encoding="utf-8")
    print("\n".join(out))


if __name__ == "__main__":
    main()
