"""审计：hfq 价格上的涨跌停判定是否漏判（引擎 _buyable/_sellable/_is_limit_down 的口径）。

真相判据（不受复权影响）：开盘涨幅比 o_ratio = open/preclose - 1。
复权因子在同一天对 open/preclose 等比缩放，比值 = 真实比值。
真实开盘涨停 ⇔ round2(pre_raw×(1+limit)) == open_raw
           ⇔ |o_ratio - limit| <= 0.005/pre_raw
pre_raw 未知，但 0.005/pre_raw 的上限 = 0.005/2元 = 0.25%（A 股最低价约 1~2 元）。
取 0.3% 比例容差作为"真实开盘涨/跌停"的近似真相，宁可多抓不放过。
引擎判据：|open_hfq - round2(pre_hfq×(1±limit))| < 0.005（绝对 5 厘米容差）。
"""
import json
import random
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(r"E:\个人文件\股票投资系统搭建")
QDIR = ROOT / "data" / "raw" / "quote_daily"
OUT = ROOT / "data" / "_limit_check_report.json"

sys.path.insert(0, str(ROOT / "backend"))

from tools.universe import limit_down_price, limit_up_price, price_limit_pct

START, END = "2020-06-01", "2024-12-31"
RATIO_TOL = 0.003      # 真相比例容差 0.3%
ENGINE_TOL = 0.005     # 引擎绝对容差（元）
SAMPLE = 400

# 覆盖复权因子大的（高分红）与低价股
PICK = ["sh.600519", "sh.600036", "sz.000858", "sh.600309", "sz.000002",
        "sh.601398", "sh.600028", "sz.000651", "sh.600887", "sz.002415"]

all_files = sorted(QDIR.glob("*.parquet"))
stock_files = [f for f in all_files if not f.name.startswith(("sh.000", "sz.399"))]
random.seed(42)
sample = PICK + list(random.sample([str(f) for f in stock_files], SAMPLE))
sample = [Path(s) for s in dict.fromkeys(str(p) for p in sample)]

stats = {
    "days_total": 0,
    "truth_open_limit_up": 0,
    "truth_open_limit_down": 0,
    "truth_close_limit_down": 0,
    "leak_up": 0,          # 真涨停但引擎判非涨停 → 买入被放行（偏乐观）
    "leak_down": 0,        # 真跌停但引擎判非跌停 → 卖出被放行（偏乐观）
    "leak_close_down": 0,  # 收盘跌停但 _is_limit_down 判非跌停 → 少触发卖出信号
    "false_up": 0,         # 引擎判涨停但真值不是
    "false_down": 0,
    "leak_up_examples": [],
    "leak_down_examples": [],
    "leak_close_down_examples": [],
    "engine_hit_up": 0,
    "engine_hit_down": 0,
}

for f in sample:
    sym = f.stem
    try:
        df = pd.read_parquet(f)
    except Exception:
        continue
    if df.empty:
        continue
    df = df[(df["date"] >= START) & (df["date"] <= END)]
    need = {"open", "high", "low", "close", "preclose", "pct_chg"}
    if not need.issubset(df.columns) or df.empty:
        continue
    o = pd.to_numeric(df["open"], errors="coerce")
    h = pd.to_numeric(df["high"], errors="coerce")
    lo = pd.to_numeric(df["low"], errors="coerce")
    c = pd.to_numeric(df["close"], errors="coerce")
    pre = pd.to_numeric(df["preclose"], errors="coerce")
    is_st = pd.to_numeric(df.get("is_st", 0), errors="coerce").fillna(0)
    dates = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d").tolist()

    for i in range(len(df)):
        if not (np.isfinite(o.iloc[i]) and np.isfinite(pre.iloc[i]) and pre.iloc[i] > 0):
            continue
        stats["days_total"] += 1
        limit = price_limit_pct(sym, dates[i], int(is_st.iloc[i]))
        o_ratio = o.iloc[i] / pre.iloc[i] - 1.0

        t_up = abs(o_ratio - limit) <= RATIO_TOL
        t_dn = abs(o_ratio + limit) <= RATIO_TOL
        e_up = abs(o.iloc[i] - limit_up_price(pre.iloc[i], limit)) < ENGINE_TOL
        e_dn = abs(o.iloc[i] - limit_down_price(pre.iloc[i], limit)) < ENGINE_TOL

        if t_up:
            stats["truth_open_limit_up"] += 1
            if e_up:
                stats["engine_hit_up"] += 1
            else:
                stats["leak_up"] += 1
                if len(stats["leak_up_examples"]) < 15:
                    stats["leak_up_examples"].append({
                        "symbol": sym, "date": dates[i],
                        "open": round(float(o.iloc[i]), 4),
                        "preclose": round(float(pre.iloc[i]), 4),
                        "o_ratio": round(o_ratio, 5), "limit": limit,
                        "engine_limit_price": round(limit_up_price(pre.iloc[i], limit), 4),
                        "diff": round(abs(o.iloc[i] - limit_up_price(pre.iloc[i], limit)), 4),
                    })
        if t_dn:
            stats["truth_open_limit_down"] += 1
            if e_dn:
                stats["engine_hit_down"] += 1
            else:
                stats["leak_down"] += 1
                if len(stats["leak_down_examples"]) < 15:
                    stats["leak_down_examples"].append({
                        "symbol": sym, "date": dates[i],
                        "open": round(float(o.iloc[i]), 4),
                        "preclose": round(float(pre.iloc[i]), 4),
                        "o_ratio": round(o_ratio, 5), "limit": limit,
                        "engine_limit_price": round(limit_down_price(pre.iloc[i], limit), 4),
                        "diff": round(abs(o.iloc[i] - limit_down_price(pre.iloc[i], limit)), 4),
                    })
        if e_up and not t_up:
            stats["false_up"] += 1
        if e_dn and not t_dn:
            stats["false_down"] += 1

        # 收盘跌停口径（momentum._is_limit_down 的判定：close == limit_down_price）
        if np.isfinite(c.iloc[i]):
            c_ratio = c.iloc[i] / pre.iloc[i] - 1.0
            t_cdn = abs(c_ratio + limit) <= RATIO_TOL
            e_cdn = abs(c.iloc[i] - limit_down_price(pre.iloc[i], limit)) < ENGINE_TOL
            if t_cdn:
                stats["truth_close_limit_down"] += 1
                if e_cdn:
                    pass
                else:
                    stats["leak_close_down"] += 1
                    if len(stats["leak_close_down_examples"]) < 10:
                        stats["leak_close_down_examples"].append({
                            "symbol": sym, "date": dates[i],
                            "close": round(float(c.iloc[i]), 4),
                            "preclose": round(float(pre.iloc[i]), 4),
                            "c_ratio": round(c_ratio, 5),
                            "diff": round(abs(c.iloc[i] - limit_down_price(pre.iloc[i], limit)), 4),
                        })

report = {
    "sample_symbols": len(sample),
    "ratio_tol": RATIO_TOL,
    "engine_tol": ENGINE_TOL,
    **stats,
}
OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({k: v for k, v in report.items() if "examples" not in k},
                 ensure_ascii=False, indent=2))
print("examples_up:", json.dumps(stats["leak_up_examples"][:5], ensure_ascii=False))
print("examples_down:", json.dumps(stats["leak_down_examples"][:5], ensure_ascii=False))
