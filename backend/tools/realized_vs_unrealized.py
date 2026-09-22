"""已实现 vs 未实现盈亏 —— 防止"用未实现浮亏/浮盈掩盖真相"。

关键：净值曲线含浮盈浮亏。若只看已平仓交易，可能得出与净值相反的结论
（消融组「两者都关」就是这种情形）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[2]

RUNS = {
    "基准 v1.2 (10,0.10) 样本内": "20260918-170311-5273a1dc",
    "消融·关时间止损": "ablate-notimestop",
    "消融·关大盘清仓": "ablate-nopanic",
    "消融·两者都关": "ablate-both",
    "样本外 v1.2": "20260918-171651-b65bfdcf",
}


def main() -> None:
    rows = []
    for label, key in RUNS.items():
        d = ROOT / "data" / "backtest" / "momentum" / key
        if not d.is_dir():  # 按 tag 找
            for cfg in (ROOT / "data" / "backtest" / "momentum").glob("*/config.json"):
                if json.loads(cfg.read_text(encoding="utf-8"))["_run"].get("tag") == key:
                    d = cfg.parent
                    break
        m = json.loads((d / "metrics.json").read_text(encoding="utf-8"))
        t = pd.read_parquet(d / "trades.parquet")

        realized = float(t["net_pnl"].sum())
        final = float(m["final_asset"])
        unrealized = final - 1_000_000 - realized

        # 期末是否还有持仓
        pos = pd.read_parquet(d / "positions.parquet")
        last_day = pos["date"].max() if "date" in pos.columns else None
        open_at_end = int((pos["date"] == last_day).sum()) if last_day is not None else -1

        rows.append({
            "配置": label,
            "笔数": len(t),
            "已实现": round(realized),
            "期末浮盈亏": round(unrealized),
            "期末资产": round(final),
            "总盈亏": round(final - 1_000_000),
            "期末持仓数": open_at_end,
        })

    df = pd.DataFrame(rows)
    txt = df.to_string(index=False)
    print(txt)
    (ROOT / "data" / "_realized_report.txt").write_text(txt, encoding="utf-8")


if __name__ == "__main__":
    main()
