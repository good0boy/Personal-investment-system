"""等权基准对照报告 —— `python -m tools.benchmark_ew`。

产出：`data/_benchmark_ew_report.txt`

为什么要有这份报告（研究文档 §1「产物缺口」第 2 条的收尾）
--------------------------------------------------------
项目此前所有"策略跑输基准 X 个百分点"里的基准，都是拿 `quote_daily` **全量**凑的，
而回测内的股票池剔除了 ST / 停牌 / 新股 / 一字涨跌停 / 非目标板块。
本工具把**两种口径并排打印**，差值就是当初那个口径偏差的量级 ——
以后再引用基准数字，就知道该用哪一列，也知道偏差有多大。

⚠️ 本工具只算**基准**，不评价策略。要对照策略请把本报告的年化与
`data/backtest/.../metrics.json` 里的 `annual_return` 放在一起看
（同区间、且都用"年化"这一列 —— 买入持有口径与日频策略不可直接比）。

用法
----
    cd backend
    python -m tools.benchmark_ew
    python -m tools.benchmark_ew --start 2020-06-01 --end 2024-12-31
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings
from app.core.errors import AppError
from app.core.logging import logger, setup_logging
from app.core.paths import DATA_DIR, relative_to_project
from app.services import benchmark_service, data_store

REPORT_PATH = DATA_DIR / "_benchmark_ew_report.txt"


def _fmt(v: float, nd: int = 2) -> str:
    if v != v:  # NaN
        return "n/a"
    return f"{v:+.{nd}%}"


def _windows(start: str | None, end: str | None) -> list[tuple[str, str, str]]:
    if start and end:
        return [("指定区间", start, end)]
    bt = settings.backtest
    return [
        ("样本内", bt.in_sample_start, bt.in_sample_end),
        ("样本外", bt.out_sample_start, bt.out_sample_end),
    ]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m tools.benchmark_ew",
        description="全市场等权基准对照（股票池快照口径 vs 全市场不过滤口径）",
    )
    p.add_argument("--start", default=None, help="起始日（默认用 config 的样本内/样本外）")
    p.add_argument("--end", default=None, help="结束日")
    args = p.parse_args(argv)

    setup_logging()
    if args.end:
        data_store.set_read_until(args.end)

    days = data_store.universe_snapshot_days()
    lines: list[str] = []
    lines.append("=" * 92)
    lines.append("  全市场等权基准对照报告")
    lines.append("=" * 92)
    lines.append(f"  生成时间：{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S')}")
    snapshot_desc = (
        f"（{days[0]} ~ {days[-1]}）" if days else "（**空**，请先 python -m tools.backfill_universe）"
    )
    lines.append(f"  股票池快照：{len(days)} 天{snapshot_desc}")
    lines.append("")
    lines.append("  口径说明：")
    lines.append("    · 对齐口径  = 只持有当日股票池快照内的标的（与回测内股票池一致）")
    lines.append("    · 无过滤    = 全市场所有有行情的标的（**旧做法**，仅供对照）")
    lines.append("    · 日再平衡  = 每日在池内等权，逐日复利（与日频策略可比的口径）")
    lines.append("    · 买入持有  = 区间内每只股票 c1/c0 的等权平均（与早期研究数字同源）")
    lines.append("")

    failures: list[str] = []
    for label, s, e in _windows(args.start, args.end):
        lines.append("-" * 92)
        lines.append(f"  【{label}】{s} ~ {e}")
        lines.append("-" * 92)
        lines.append(
            f"    {'口径':<12}{'日再平衡年化':>14}{'日再平衡累计':>14}{'最大回撤':>12}"
            f"{'日均只数':>10}{'买入持有均值':>14}"
        )
        for tag, use_uni in (("对齐口径", True), ("无过滤", False)):
            try:
                daily = benchmark_service.universe_equal_weight_daily(s, e, use_universe=use_uni)
                sry = benchmark_service.summarize(daily)
                bh = benchmark_service.equal_weight_buy_hold(s, e, use_universe=use_uni)
                lines.append(
                    f"    {tag:<12}{_fmt(sry['annual_return']):>14}{_fmt(sry['cum_return']):>14}"
                    f"{_fmt(sry['max_drawdown']):>12}{sry['n_symbols_avg']:>10.0f}"
                    f"{_fmt(bh['mean']):>14}"
                )
            except AppError as exc:
                failures.append(f"{label}/{tag}: {exc.message}")
                lines.append(f"    {tag:<12}  ⚠️ 无法计算：{exc.message}")
        lines.append("")

    lines.append("=" * 92)
    lines.append("  怎么读这份报告")
    lines.append("=" * 92)
    lines.append("  1. 引用基准数字时一律用**对齐口径**那一行 —— 它与回测内股票池同源。")
    lines.append("  2. 「无过滤 − 对齐」的差 = 旧结论里的口径偏差；差越小，历史结论越可信。")
    lines.append("  3. 与策略比，只比「日再平衡年化」这一列（买入持有口径与日频策略不可比）。")
    lines.append("=" * 92)

    text = "\n".join(lines) + "\n"
    print()
    # ★ 先打印、后落盘：落盘那一步曾经在本项目制造过"算完了却崩在最后一行"的假失败
    print(text)
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(text, encoding="utf-8")
    logger.info(f"报告已落盘：{relative_to_project(REPORT_PATH)}")

    for f in failures:
        logger.warning(f"部分窗口无法计算：{f}")
    return 2 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
