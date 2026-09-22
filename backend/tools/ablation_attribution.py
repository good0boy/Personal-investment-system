"""消融实验的逐笔归因 —— 拆开"每笔边际"与"交易次数"。

用途：判断策略亏在哪 —— 是每笔都亏（入场无边际），还是次数太多（成本/换手）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[2]

# tag → run_id（in_sample 全市场）
RUNS = {
    "基准 v1.2 (10,0.10)": "20260918-170311-5273a1dc",
    "消融·关时间止损": "ablate-notimestop",
    "消融·关大盘清仓": "ablate-nopanic",
    "消融·两者都关": "ablate-both",
    "样本外 v1.2": "20260918-171651-b65bfdcf",
}

# tag 里带冒号/空格的不便做目录名匹配，这里只按 tag 字符串精确找


def find_by_tag(tag: str) -> str | None:
    """按 tag 找 run_id（tag 精确匹配，取最新）。"""
    hits = []
    for cfg in (ROOT / "data" / "backtest" / "momentum").glob("*/config.json"):
        d = json.loads(cfg.read_text(encoding="utf-8"))
        if d["_run"].get("tag") == tag:
            hits.append((cfg.parent.name, cfg.stat().st_mtime))
    if not hits:
        return None
    return max(hits, key=lambda x: x[1])[0]


def main() -> None:
    rows = []
    for label, key in RUNS.items():
        # key 若本身是已存在的 run_id 就直接用，否则按 tag 找
        rid = key if (ROOT / "data" / "backtest" / "momentum" / key).is_dir() else find_by_tag(key)
        if rid is None:
            rows.append({"配置": label, "备注": "未找到产物"})
            continue
        d = ROOT / "data" / "backtest" / "momentum" / rid
        m = json.loads((d / "metrics.json").read_text(encoding="utf-8"))
        t = pd.read_parquet(d / "trades.parquet")

        # 逐笔口径：以开仓金额为分母
        t = t.assign(
            net_pct=t["net_pnl"] / t["entry_amount"],
            gross_pct=t["gross_pnl"] / t["entry_amount"],
        )
        rows.append({
            "配置": label,
            "笔数": len(t),
            "年化%": round(m["annual_return"] * 100, 2),
            "每笔净%": round(t["net_pct"].mean() * 100, 3),
            "每笔毛%": round(t["gross_pct"].mean() * 100, 3),
            "毛/笔×笔数": round(t["gross_pct"].mean() * len(t) * 100, 1),
            "毛胜率%": round((t["gross_pct"] > 0).mean() * 100, 1),
            "净胜率%": round((t["net_pct"] > 0).mean() * 100, 1),
            "持有日": round(t["hold_days"].mean(), 1),
            "成本%年": round(m["annual_cost_ratio"] * 100, 2),
        })

    df = pd.DataFrame(rows)
    out = df.to_string(index=False)
    print(out)
    (ROOT / "data" / "_ablation_report.txt").write_text(out, encoding="utf-8")


if __name__ == "__main__":
    main()
