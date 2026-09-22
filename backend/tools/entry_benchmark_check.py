"""入场跳空 + 选股能力验证 —— 判断策略是否"跑输随机"。

三件事：
1. 入场跳空：信号日 T 收盘 → T+1 开盘，实际让掉多少（验证"追强反被反噬"）
2. 选股能力：策略选出的股票，在其持有期内相对全市场等权基准的超额
3. 随机基准：同期全市场等权 buy&hold 的表现
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[2]
RUN_IN = "data/backtest/momentum/20260918-170311-5273a1dc"
RUN_OUT = "data/backtest/momentum/20260918-171651-b65bfdcf"


def load_quotes_dir() -> dict[str, pd.DataFrame]:
    """一次读入全部日线（只取 date/open/close/pctChg）。"""
    out: dict[str, pd.DataFrame] = {}
    for p in (ROOT / "data" / "raw" / "quote_daily").glob("*.parquet"):
        try:
            d = pd.read_parquet(p, columns=["date", "symbol", "open", "close", "pct_chg"])
        except Exception:
            continue
        d["date"] = pd.to_datetime(d["date"])
        out[p.stem] = d.sort_values("date").reset_index(drop=True)
    return out


def entry_gap(quotes: dict[str, pd.DataFrame], trades: pd.DataFrame) -> pd.DataFrame:
    """对每笔交易算：T 日收盘 → T+1(入场日) 开盘 的跳空幅度。"""
    rows = []
    for t in trades.itertuples():
        q = quotes.get(t.symbol)
        if q is None:
            continue
        m = q["date"] == pd.Timestamp(t.entry_date)
        if not m.any():
            continue
        idx = int(q.index[m][0])
        if idx == 0:
            continue
        prev_close = float(q["close"].iloc[idx - 1])
        open_px = float(q["open"].iloc[idx])
        if prev_close <= 0:
            continue
        rows.append({
            "symbol": t.symbol,
            "entry_date": t.entry_date,
            "prev_close": prev_close,
            "entry_open": open_px,
            "gap": open_px / prev_close - 1,
            "net_return": t.net_return,
        })
    return pd.DataFrame(rows)


def equal_weight_benchmark(quotes: dict[str, pd.DataFrame], start: str, end: str) -> dict[str, float]:
    """全市场等权 buy&hold：区间内每只股票区间收益的等权平均。"""
    rets = []
    for _sym, q in quotes.items():
        w = q[(q["date"] >= start) & (q["date"] <= end)]
        if len(w) < 2:
            continue
        c0, c1 = float(w["close"].iloc[0]), float(w["close"].iloc[-1])
        if c0 > 0:
            rets.append(c1 / c0 - 1)
    s = pd.Series(rets)
    n = s.size
    return {"n": n, "mean": float(s.mean()), "median": float(s.median()),
            "win_rate": float((s > 0).mean())}


def main() -> None:
    out: list[str] = []
    quotes = load_quotes_dir()
    out.append(f"读入 {len(quotes)} 只标的日线\n")

    # ---------- 1. 入场跳空 ----------
    out.append("=" * 84)
    out.append("1. 入场跳空（信号日 T 收盘 → 入场日 T+1 开盘）")
    out.append("=" * 84)
    for label, rel in (("样本内", RUN_IN), ("样本外", RUN_OUT)):
        trades = pd.read_parquet(ROOT / rel / "trades.parquet")
        g = entry_gap(quotes, trades)
        if g.empty:
            out.append(f"{label}：无法匹配入场日行情")
            continue
        out.append(
            f"{label}：{len(g)} 笔可匹配｜跳空均值 {g['gap'].mean():+.3%}｜"
            f"中位数 {g['gap'].median():+.3%}｜低开占比 {(g['gap'] < 0).mean():.1%}｜"
            f"跳空≤-1% 占 {(g['gap'] <= -0.01).mean():.1%}"
        )
        # 跳空与最终净收益的关系
        corr = g["gap"].corr(g["net_return"])
        good = g[g["gap"] < 0]["net_return"].mean()
        bad = g[g["gap"] >= 0]["net_return"].mean()
        out.append(
            f"       跳空 vs 最终净收益 相关系数 {corr:+.3f}｜"
            f"低开组平均净收益 {good:+.2%}｜高开组 {bad:+.2%}"
        )

    # ---------- 2. 等权基准 ----------
    out.append("")
    out.append("=" * 84)
    out.append("2. 全市场等权买入持有基准（同期）")
    out.append("=" * 84)
    for label, s, e in (("样本内 2020-06-01~2024-12-31", "2020-06-01", "2024-12-31"),
                        ("样本外 2025-01-01~2026-09-16", "2025-01-01", "2026-09-16")):
        b = equal_weight_benchmark(quotes, s, e)
        out.append(
            f"{label}：{b['n']} 只｜等权平均区间收益 {b['mean']:+.2%}｜"
            f"中位数 {b['median']:+.2%}｜上涨股票占比 {b['win_rate']:.1%}"
        )

    # ---------- 3. 策略 vs 基准 ----------
    out.append("")
    out.append("=" * 84)
    out.append("3. 策略 vs 基准")
    out.append("=" * 84)
    for label, rel, s, e in (
        ("样本内", RUN_IN, "2020-06-01", "2024-12-31"),
        ("样本外", RUN_OUT, "2025-01-01", "2026-09-16"),
    ):
        m = json.loads((ROOT / rel / "metrics.json").read_text(encoding="utf-8"))
        b = equal_weight_benchmark(quotes, s, e)
        # 同样年化（用交易日数换算）
        n_years = m["trading_days"] / 243
        bench_ann = (1 + b["mean"]) ** (1 / n_years) - 1
        out.append(
            f"{label}：策略年化 {m['annual_return']:+.2%}｜"
            f"等权基准年化 {bench_ann:+.2%}｜"
            f"超额 {m['annual_return'] - bench_ann:+.2%}"
        )

    txt = "\n".join(out)
    print(txt)
    (ROOT / "data" / "_entry_benchmark_report.txt").write_text(txt, encoding="utf-8")


if __name__ == "__main__":
    main()
