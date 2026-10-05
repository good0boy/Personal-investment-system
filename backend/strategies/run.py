"""回测 CLI —— `python -m strategies.run`。

为什么是 CLI 不是 Web 按钮
------------------------
回测要跑 1115 个交易日 × 全市场，属于计算密集任务。放进 HTTP 请求链路里会
把服务卡死（见 `技术架构.md` 判断一 + 架构纪律 5）。所以：
**CLI 跑 → 结果落盘到 `data/backtest/{strategy}/{run_id}/` → 前端只读文件。**
前端与策略彻底解耦：改策略不用动系统，系统挂了不影响跑策略。

本文件只负责"解析命令行 + 打印报告"；真正的执行流水线在 `strategies/runner.py`
（参数寻优 `sweep.py` 复用的是同一份，避免两处逻辑走偏）。

用法
----
    cd backend
    python -m strategies.run                                  # 默认 momentum，样本内
    python -m strategies.run --tag baseline
    python -m strategies.run --start 2020-06-01 --end 2024-12-31
    python -m strategies.run --out-sample                     # 样本外（谨慎：只跑一次）
    python -m strategies.run --param hold_days_max=20 --param take_profit_gross_main=0.09
    python -m strategies.run --list                           # 看有哪些策略
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings
from app.core.errors import AppError
from app.core.logging import logger, setup_logging
from app.core.paths import ensure_dirs, relative_to_project
from app.repos import db
from app.services import metrics as metrics_mod
from strategies import registry
from strategies.runner import PREHEAT_START, run_backtest


def _parse_params(pairs: list[str] | None) -> dict[str, Any]:
    """`--param k=v` → dict，并按字面量推断类型。

    ⚠️ 一定要做类型转换：`--param hold_days_max=20` 若传成字符串 "20"，
       与 int 比较时 Python 会 TypeError；而 `take_profit_gross_main=0.09` 传成字符串
       则会**静默变成字符串比较**，止盈判定永远不触发。两种都很难查。
    """
    out: dict[str, Any] = {}
    for raw in pairs or []:
        if "=" not in raw:
            raise AppError(f"--param 需要 k=v 形式，收到：{raw!r}")
        k, v = raw.split("=", 1)
        k, v = k.strip(), v.strip()
        if not k:
            raise AppError(f"--param 的键不能为空：{raw!r}")
        low = v.lower()
        if low in ("true", "false"):
            out[k] = low == "true"
        else:
            try:
                out[k] = int(v)
            except ValueError:
                try:
                    out[k] = float(v)
                except ValueError:
                    out[k] = v
    return out


def _resolve_interval(args: argparse.Namespace) -> tuple[str, str, bool, str]:
    """决定回测区间。返回 (start, end, is_out_sample, interval_name)。"""
    bt = settings.backtest
    if args.start or args.end:
        start = args.start or bt.in_sample_start
        end = args.end or bt.in_sample_end
        is_out = not (bt.in_sample_start <= start and end <= bt.in_sample_end)
        return start, end, is_out, "custom"
    if args.out_sample:
        return bt.out_sample_start, bt.out_sample_end, True, "out_sample"
    return bt.in_sample_start, bt.in_sample_end, False, "in_sample"


def main() -> int:
    parser = argparse.ArgumentParser(description="策略回测 CLI")
    parser.add_argument("--strategy", default="momentum", help="策略名（--list 可看全部）")
    parser.add_argument("--tag", default="", help="标签，便于在列表页区分（如 baseline）")
    parser.add_argument("--start", default=None, help="覆盖回测起点 YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="覆盖回测终点 YYYY-MM-DD")
    parser.add_argument("--out-sample", action="store_true", help="跑样本外区间（只跑一次）")
    parser.add_argument("--param", action="append", default=None, help="策略参数 k=v，可重复")
    parser.add_argument(
        "--symbols", default=None,
        help="只跑指定标的（逗号分隔，如 sh.600519,sz.000001）—— 调试用，不必等全量回补完",
    )
    parser.add_argument("--list", action="store_true", help="列出已注册策略")
    args = parser.parse_args()

    setup_logging()
    ensure_dirs()

    if args.list:
        print("已注册策略：")
        for n in registry.strategy_names():
            d = registry.describe(n)
            print(f"  {n:12s} v{d['version']:<6s} {d['description']}")
        return 0

    if not db.is_initialized():
        logger.error("meta.db 尚未初始化，请先执行：python -m tools.init_db")
        return 2

    start, end, is_out, interval_name = _resolve_interval(args)
    params = _parse_params(args.param)
    strategy = registry.get_strategy(args.strategy, params)

    symbols: list[str] | None = None
    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    logger.info("=" * 66)
    logger.info(f"回测开始：{strategy.name} v{strategy.version}｜{interval_name}")
    logger.info(f"  区间      : {start} ~ {end}（预热自 {PREHEAT_START}）")
    logger.info(f"  参数      : {strategy.params}")
    logger.info("=" * 66)

    if symbols:
        logger.warning(
            f"⚠️ 调试模式：只跑 {len(symbols)} 只标的 → {symbols}。"
            f"截面选股结果**没有可比性**，不要用它评估策略。"
        )

    try:
        summary = run_backtest(
            args.strategy,
            start=start,
            end=end,
            interval_name=interval_name,
            is_out_sample=is_out,
            params=params,
            tag=args.tag,
            symbols=symbols,
        )
    except (AppError, ValueError) as exc:
        logger.error(f"回测失败：{exc}")
        return 3
    except Exception as exc:  # pragma: no cover - 兜底
        logger.exception(f"回测失败：{exc}")
        return 3

    metrics = summary["metrics"]
    print()
    print("=" * 66)
    print(f"  回测完成｜{summary['strategy']} v{summary['strategy_version']}｜{interval_name}")
    print(
        f"  区间 {start} ~ {end}｜耗时 {summary['cost_seconds']}s"
        f"｜run_id {summary['run_id']}"
    )
    print("=" * 66)
    for label, value in metrics_mod.format_metrics(metrics):
        print(f"  {label:<16s}: {value}")
    print("=" * 66)
    print(f"  结果目录：{relative_to_project(summary['root'])}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
