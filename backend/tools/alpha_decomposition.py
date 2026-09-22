"""边际分解：策略的 alpha 究竟消失在哪个环节。

对每笔交易，把总收益拆成两段：
  ① 隔夜段：信号日 T 收盘 → 入场日 T+1 开盘
  ② 持有段：入场日 T+1 开盘 → 卖出日开盘

引擎实际按 ①+② 成交（T+1 开盘买入）；这里额外算"假如能按 T 日收盘成交"的收益，
用于判断 alpha 是否被「信号生成时点 与 成交时点 之间的缺口」吃掉。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[2]
RUN_IN = "data/backtest/momentum/20260918-170311-5273a1dc"
RUN_OUT = "data/backtest/momentum/20260918-171651-b65bfdcf"


def main() -> None:
    out: list[str] = []
    for label, rel in (("样本内", RUN_IN), ("样本外", RUN_OUT)):
        trades = pd.read_parquet(ROOT / rel / "trades.parquet")
        gaps, holds, closes = [], [], []
        for t in trades.itertuples():
            p = ROOT / "data" / "raw" / "quote_daily" / f"{t.symbol}.parquet"
            if not p.exists():
                continue
            q = pd.read_parquet(p, columns=["date", "open", "close"])
            q["date"] = pd.to_datetime(q["date"])
            m = q["date"] == pd.Timestamp(t.entry_date)
            if not m.any():
                continue
            i = int(q.index[m][0])
            if i == 0:
                continue
            prev_close = float(q["close"].iloc[i - 1])   # 信号日 T 收盘
            entry_open = float(q["open"].iloc[i])        # 入场日 T+1 开盘
            exit_px = float(t.exit_price)
            if prev_close <= 0 or entry_open <= 0:
                continue
            gaps.append(entry_open / prev_close - 1)              # ① 隔夜
            holds.append(exit_px / entry_open - 1)                # ② 持有（引擎实际口径）
            closes.append(exit_px / prev_close - 1)               # ①+② 按 T 收盘买入

        g, h, c = pd.Series(gaps), pd.Series(holds), pd.Series(closes)
        out.append(f"\n{'=' * 80}\n{label}（{len(g)} 笔）\n{'=' * 80}")
        out.append(f"① 隔夜跳空   T收→T+1开  平均 {g.mean():+.3%}  中位 {g.median():+.3%}")
        out.append(
            f"② 持有段     T+1开→卖   平均 {h.mean():+.3%}  中位 {h.median():+.3%}  （引擎实际拿到的）"
        )
        out.append(
            f"①*② 合计     T收→卖     平均 {c.mean():+.3%}  中位 {c.median():+.3%}  （假如按 T 收盘成交）"
        )
        out.append("    → 逐笔往返成本率约 -0.202%（含滑点），扣后：")
        out.append(
            f"       引擎口径净 {h.mean() - 0.00202:+.3%}/笔 ｜ 收盘口径净 {c.mean() - 0.00202:+.3%}/笔"
        )

    txt = "\n".join(out)
    print(txt)
    (ROOT / "data" / "_alpha_decomp_report.txt").write_text(txt, encoding="utf-8")


if __name__ == "__main__":
    main()
