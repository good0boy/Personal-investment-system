"""验证 pct_chg（baostock 原生涨跌幅）与 hfq close/preclose 隐含涨跌幅是否同口径。

若两者一致（差异仅在浮点噪声级），则开仓条件①（pct_chg）与条件②③④（hfq 相除）
口径统一；若除权日出现系统性偏差，说明 pct_chg 与 hfq 口径分裂，需要修正。
"""
import json
import random
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(r"E:\个人文件\股票投资系统搭建")
QDIR = ROOT / "data" / "raw" / "quote_daily"
OUT = ROOT / "data" / "_pctchg_check_report.json"

random.seed(7)
files = [f for f in sorted(QDIR.glob("*.parquet"))
         if not f.name.startswith(("sh.000", "sz.399"))]
sample = random.sample([str(f) for f in files], 300)

diffs_all = []
diffs_big = []   # 差异 > 0.5% 的（疑似除权日口径分裂）
bad_pre = 0      # preclose 与前日 close 断裂 > 20%（疑似 preclose 未复权）

for fs in sample:
    try:
        df = pd.read_parquet(fs)
    except Exception:
        continue
    if df.empty:
        continue
    df = df[(df["date"] >= "2020-06-01") & (df["date"] <= "2024-12-31")]
    for col in ("close", "preclose", "pct_chg"):
        if col not in df.columns or df[col].isna().all():
            break
    else:
        c = pd.to_numeric(df["close"], errors="coerce")
        pre = pd.to_numeric(df["preclose"], errors="coerce")
        pct = pd.to_numeric(df["pct_chg"], errors="coerce")
        implied = (c / pre - 1.0) * 100.0
        d = (pct - implied).abs()
        ok = np.isfinite(d)
        diffs_all.extend(d[ok].tolist())
        big = ok & (d > 0.5)
        if big.any():
            sub = df.loc[big, ["date", "close", "preclose", "pct_chg"]].copy()
            sub["implied"] = implied[big].round(4)
            sub["diff"] = d[big].round(4)
            diffs_big.append({
                "symbol": fs.stem,
                "rows": sub.head(5).to_dict("records"),
                "count": int(big.sum()),
            })
        # preclose 连续性：除权日 hfq preclose 应当 ≈ 前日 close（复权序列连续）
        prev_c = c.shift(1)
        ratio = (pre / prev_c).abs()
        brk = np.isfinite(ratio) & ((ratio < 0.8) | (ratio > 1.2))
        bad_pre += int(brk.sum())

arr = np.array(diffs_all)
report = {
    "sample_symbols": len(sample),
    "n_days": int(arr.size),
    "pct_diff_median": float(np.median(arr)),
    "pct_diff_p99": float(np.percentile(arr, 99)),
    "pct_diff_max": float(arr.max()),
    "n_diff_gt_0pct5": int((arr > 0.5).sum()),
    "preclose_break_rows": bad_pre,
    "diff_big_symbols": sorted(diffs_big, key=lambda x: -x["count"])[:10],
}
OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str),
               encoding="utf-8")
print(json.dumps({k: v for k, v in report.items() if k != "diff_big_symbols"},
                 ensure_ascii=False, indent=2))
print("worst symbols:", json.dumps(report["diff_big_symbols"][:5],
                                   ensure_ascii=False, default=str))
