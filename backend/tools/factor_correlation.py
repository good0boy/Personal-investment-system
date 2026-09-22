"""模块 2 · 因子相关性矩阵 —— 多因子合成（combo）前的必修课。

口径（与 factors.analyze 完全一致，禁止另写加载器）：
  行情矩阵 = strategies.engine.MarketData；可交易池 = build_investable_masks；
  因子矩阵 = load_factor_matrix（散射对齐）。

计算方法：**逐日截面 Spearman 秩相关的时序平均**。
  对每个交易日：取 11 个因子的截面值（池内 + 完整个案），各自转平均秩
  （pandas rank(method="average)，与 analyze._average_rank 同口径），
  秩上算 Pearson（== Spearman），得 11×11 矩阵；对日序做 nan 安全平均。

为什么用截面秩相关而不是全表原始值相关：
  因子量纲不同（换手率是百分数、动量是小数），原始值相关会被量纲与
  共同的时间趋势污染；截面秩相关回答的是"今天谁和谁选出同样的股票"，
  这正是合成因子前真正要问的问题。

产物：
  1. data/factor/analysis/factor_correlation.json（研究产物；无 name 键，
     factor_service._scan_analysis_files 会自动跳过，不污染列表摘要）
  2. docs/因子相关性矩阵.html（ECharts 热力图单文件，可直接浏览器打开）
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from loguru import logger

from app.core.errors import AppError
from app.core.paths import relative_to_project
from factors import loader, manifest
from factors.analyze import (
    FACTOR_ANALYSIS_DIR,
    build_investable_masks,
    load_factor_matrix,
    load_market,
)

#: 平均有效股票数低于该值的交易日跳过（截面秩相关噪声太大）
DEFAULT_MIN_SYMBOLS = 200

#: 相关性热力图发散色带（负 = 蓝，正 = 红；RdBu 反转）
_DIVERGING_COLORS = [
    "#313695", "#4575b4", "#74add1", "#abd9e9", "#e0f3f8", "#ffffbf",
    "#fee090", "#fdae61", "#f46d43", "#d73027", "#a50026",
]

_METHOD_NOTES = (
    "逐日截面 Spearman 秩相关（平均秩口径，与 IC 一致）对日序做平均",
    "样本 = 当日可交易池 ∩ 11 个因子全部非 NaN 的完整个案（listwise）",
    "秩上 Pearson == Spearman；并列值取平均名次",
    "相关 ≠ 因果；高相关只说明选股重叠，合成前还需看各自 IC 方向",
)


def run(*, start: str, end: str, min_symbols: int) -> dict[str, Any]:
    """算平均截面秩相关矩阵并落盘。返回产物 dict。"""
    defns = {d.name: d for d in loader.list_factors("daily")}
    calculated = set(manifest.read_manifest()["factors"])
    names = sorted(n for n in calculated if n in defns)
    if len(names) < 2:
        raise AppError(f"已计算的 daily 因子不足 2 个（现在 {len(names)}），先跑 factors.calc")

    t0 = time.monotonic()
    market = load_market(start, end, None)
    masks, universe_info = build_investable_masks(market, start, end)
    logger.info(
        f"行情矩阵 {len(market.dates)} 日 × {len(market.symbols)} 只"
        f"｜股票池就绪（{time.monotonic() - t0:.1f}s）"
    )

    k = len(names)
    mats: dict[str, np.ndarray] = {}
    for n in names:
        mats[n] = load_factor_matrix(n, defns[n].category, market).astype("float32")

    corr_sum = np.zeros((k, k), dtype="float64")
    corr_cnt = np.zeros((k, k), dtype="int64")
    n_used = 0
    n_skipped = 0
    valid_counts: list[int] = []
    for di in range(len(market.dates)):
        block = np.stack([mats[n][di] for n in names])
        ok = masks[di] & np.isfinite(block).all(axis=0)
        m = int(ok.sum())
        if m < min_symbols:
            n_skipped += 1
            continue
        ranks = pd.DataFrame(block[:, ok].T).rank(method="average").to_numpy().T
        with np.errstate(divide="ignore", invalid="ignore"):
            c = np.corrcoef(ranks)
        fin = np.isfinite(c)
        corr_sum[fin] += c[fin]
        corr_cnt[fin] += 1
        valid_counts.append(m)
        n_used += 1

    avg = np.full((k, k), np.nan)
    has = corr_cnt > 0
    avg[has] = corr_sum[has] / corr_cnt[has]

    pairs = [
        {"a": names[i], "b": names[j], "rho": round(float(avg[i, j]), 4)}
        for i in range(k) for j in range(i + 1, k) if np.isfinite(avg[i, j])
    ]
    pairs.sort(key=lambda p: -abs(p["rho"]))

    result: dict[str, Any] = {
        "kind": "factor_correlation",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "interval": {"start": start, "end": end},
        "method": list(_METHOD_NOTES),
        "universe": universe_info,
        "min_symbols": min_symbols,
        "names": names,
        "cnames": {n: defns[n].cname for n in names},
        "matrix": [[None if not np.isfinite(v) else round(float(v), 4) for v in row] for row in avg],
        "days_used": n_used,
        "days_skipped": n_skipped,
        "avg_valid_symbols": round(float(np.mean(valid_counts)), 1) if valid_counts else 0.0,
        "pairs": pairs,
    }

    out = FACTOR_ANALYSIS_DIR / "factor_correlation.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(out)

    _print_report(result)
    return result


def _print_report(res: dict[str, Any]) -> None:
    names = res["names"]
    print(f"\n  平均截面秩相关（{res['days_used']} 个交易日，"
          f"日均有效 {res['avg_valid_symbols']} 只）｜|rho| ≥ 0.7 视为冗余：\n")
    header = "        " + "".join(f"{n[:7]:>9s}" for n in names)
    print(header)
    for i, row in enumerate(res["matrix"]):
        cells = "".join(
            f"{v:>9.2f}" if v is not None else f"{'—':>9s}"
            for v in ([None] * i + row[i:])
        )
        print(f"{names[i][:7]:>7s}  {cells}")
    print("\n  按绝对值排序的全部因子对：")
    for p in res["pairs"][:12]:
        flag = " ⚠️" if abs(p["rho"]) >= 0.7 else ""
        print(f"    {p['a']:<15s} × {p['b']:<15s}  {p['rho']:+.3f}{flag}")
    print(f"\n  产物：{relative_to_project(FACTOR_ANALYSIS_DIR / 'factor_correlation.json')}")


# ======================================================================
# ECharts 热力图单文件
# ======================================================================
def write_html(res: dict[str, Any], path: Path) -> None:
    """生成自包含的 ECharts 相关性热力图 HTML（浏览器直接打开）。"""
    names = res["names"]
    cnames = res["cnames"]
    mat = res["matrix"]
    labels = [f"{n}\n{cnames.get(n, '')}" for n in names]
    data = [
        [j, i, mat[i][j]]
        for i in range(len(names)) for j in range(len(names))
        if mat[i][j] is not None
    ]
    top_pairs = res["pairs"][:20]
    pair_rows = "".join(
        f"<tr><td>{p['a']}</td><td>{p['b']}</td>"
        f"<td class=\"{'warn' if abs(p['rho']) >= 0.7 else ''}\">{p['rho']:+.3f}</td></tr>"
        for p in top_pairs
    )
    notes = "".join(f"<li>{n}</li>" for n in res["method"])

    payload = json.dumps(
        {"labels": labels, "data": data, "colors": _DIVERGING_COLORS},
        ensure_ascii=False,
    )
    html = _HTML_TEMPLATE.replace("__PAYLOAD__", payload).replace("__PAIR_ROWS__", pair_rows)
    html = html.replace("__NOTES__", notes).replace(
        "__META__", f"{res['days_used']} 个交易日｜日均有效 {res['avg_valid_symbols']} 只"
        f"｜区间 {res['interval']['start']} ~ {res['interval']['end']}"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    logger.info(f"热力图已生成：{path}")


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>因子相关性矩阵 · 截面秩相关</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js"></script>
<style>
  body { font-family: "Microsoft YaHei", system-ui, sans-serif; margin: 24px;
         background: #fafafa; color: #222; }
  h1 { font-size: 20px; margin: 0 0 4px; }
  .meta { color: #666; font-size: 13px; margin-bottom: 12px; }
  #chart { width: 100%; max-width: 980px; height: 760px; background: #fff;
           border: 1px solid #e5e5e5; border-radius: 8px; }
  .notes { font-size: 12px; color: #555; margin: 14px 0; max-width: 960px; }
  table { border-collapse: collapse; font-size: 13px; background: #fff; }
  th, td { border: 1px solid #e0e0e0; padding: 5px 14px; text-align: left; }
  th { background: #f0f0f0; }
  td.warn { color: #c0392b; font-weight: 600; }
  h2 { font-size: 15px; margin: 20px 0 8px; }
</style>
</head>
<body>
<h1>因子相关性矩阵 · 逐日截面秩相关的时序平均</h1>
<div class="meta">__META__</div>
<div id="chart"></div>
<h2>口径说明</h2>
<ul class="notes">__NOTES__</ul>
<h2>绝对相关最高的 20 对（|rho| ≥ 0.7 标红 = 合成前需二选一或做正交化）</h2>
<table>
<tr><th>因子 A</th><th>因子 B</th><th>平均 ρ</th></tr>
__PAIR_ROWS__
</table>
<script>
const P = __PAYLOAD__;
const chart = echarts.init(document.getElementById("chart"));
chart.setOption({
  tooltip: {
    position: "top",
    formatter: p => `${P.labels[p.value[1]].split("\\n")[0]} × ` +
      `${P.labels[p.value[0]].split("\\n")[0]}<br>ρ = ${Number(p.value[2]).toFixed(3)}`
  },
  grid: { left: 130, bottom: 110, right: 120, top: 20 },
  xAxis: { type: "category", data: P.labels, splitArea: { show: true },
           axisLabel: { fontSize: 10, interval: 0, rotate: 45 } },
  yAxis: { type: "category", data: P.labels, splitArea: { show: true },
           axisLabel: { fontSize: 10, interval: 0 } },
  visualMap: {
    min: -1, max: 1, calculable: true, orient: "vertical",
    right: 8, top: "center", precision: 2,
    inRange: { color: P.colors }
  },
  series: [{
    type: "heatmap", data: P.data,
    label: { show: true, fontSize: 9,
             formatter: p => Number(p.value[2]).toFixed(2) },
    emphasis: { itemStyle: { shadowBlur: 6, shadowColor: "rgba(0,0,0,0.4)" } }
  }]
});
window.addEventListener("resize", () => chart.resize());
</script>
</body>
</html>
"""


# ======================================================================
# CLI
# ======================================================================
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m tools.factor_correlation",
        description="模块 2 · 因子截面秩相关矩阵（合成 combo 前的冗余体检）",
    )
    p.add_argument("--start", default=None, help="起始日（默认 config.data.start_date）")
    p.add_argument("--end", default=None, help="结束日（默认今天）")
    p.add_argument(
        "--min-symbols", type=int, default=DEFAULT_MIN_SYMBOLS,
        help=f"日均有效股票数低于该值的交易日跳过（默认 {DEFAULT_MIN_SYMBOLS}）",
    )
    p.add_argument(
        "--html-out", default=None,
        help="热力图输出路径（默认 docs/因子相关性矩阵.html）",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    from app.core.config import settings  # noqa: PLC0415

    args = build_parser().parse_args(argv)
    start = args.start or settings.data.start_date
    end = args.end or datetime.now().strftime("%Y-%m-%d")

    try:
        res = run(start=start, end=end, min_symbols=args.min_symbols)
    except AppError as exc:
        logger.error(f"相关性分析失败：{exc.message}")
        return 2

    html_path = Path(args.html_out) if args.html_out else (
        Path(__file__).resolve().parents[2] / "docs" / "因子相关性矩阵.html"
    )
    write_html(res, html_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
