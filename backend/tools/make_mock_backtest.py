"""生成 **mock 回测产物** —— 前端并行开发用。

为什么需要它
------------
前端不该等后端把全市场数据拉完（1.5~3 小时）才能开发净值图。
这个工具按**真实契约**（`strategies/base.py` 的 `EQUITY_COLUMNS` 等）写一批
假数据到 `data/backtest/{strategy}/{run_id}/`，前端对着它开发；
真实回测跑完后把目录一换即可。

★ 它是契约的**回归样本**，不是随手造的假数字：
  · 交易成本走 `services/cost_model.py`（同一个实现，同一套费率）
  · 指标走 `services/metrics.py`（`compute_metrics`，与真实回测同口径）
  · 净值与交易**自洽**：`期末总资产 - 初始资金 == 累计已实现净盈亏 + 浮动`
    所以"期末资产"和"总净盈亏"能对上，交叉检查不会露馅
  前端按这份数据调好的图表，换真实数据不会错位。

四个 run 覆盖四种边界形态
------------------------
    mock-normal-a   1115 个交易日（与规格一致），正常盈亏
    mock-long       2020-06-01 至今（约 1650 天），跨成本档位、跨牛熊
    mock-empty      200 个交易日、**零成交** → 验证"空表也带列名"的契约
    mock-debug      40 个交易日 + `debug_symbols` 非空 → 详情页必须标红警告

前端能画第 3 种，真实数据就一定画得出来 —— "空/边界"是前端白屏最常见的来源。
（LTTB 降采样路径请用 `?max_points=200` 触发：真实回测只有 1115 天，
  连"更长区间"都凑不到 2000 点。）

用法
----
    cd backend
    python -m tools.make_mock_backtest              # 生成/覆盖 4 个 mock run
    python -m tools.make_mock_backtest --clean      # 删除全部 mock run
    python -m tools.make_mock_backtest --seed 7     # 换一份随机形态
    python -m tools.make_mock_backtest --no-db      # 不写 meta.db（默认会写）

⚠️ mock run 的 `run_id` 一律以 `mock-` 开头，便于一眼识别、批量删除。
   真实 run_id 是 `{YYYYMMDD-HHMMSS}-{hash8}`，不会撞。
"""
from __future__ import annotations

import argparse
import itertools
import random
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from app.core.logging import logger, setup_logging
from app.core.paths import ensure_dirs, relative_to_project
from app.repos import meta_repo
from app.services import backtest_service, cost_model
from app.services import metrics as metrics_mod
from strategies.base import (
    EQUITY_COLUMNS,
    ORDER_COLUMNS,
    POSITION_COLUMNS,
    run_layout,
    write_run,
)

STRATEGY = "momentum"
MOCK_PREFIX = "mock-"

#: 可用标的（真实代码，便于前端按惯例着色/展示）
SYMBOLS = [
    "sh.600519", "sz.000001", "sz.300750", "sh.601318", "sz.000858",
    "sh.600036", "sz.002594", "sh.601899", "sz.000333", "sh.600276",
]
#: 卖出原因：故意混入多值（`time_stop|take_profit`）以验证前端的多值解析
EXIT_REASONS = ["time_stop", "take_profit", "stop_loss", "time_stop|take_profit", "market_panic"]

INITIAL_CASH = 1_000_000.0


# ======================================================================
# 时间与序列
# ======================================================================
def _trading_days(start: date, end: date) -> list[str]:
    """粗造交易日：跳过周末。

    mock 不需要真实日历（形态够用即可），也**刻意不读** `trade_calendar.parquet` ——
    否则没同步过日历的环境连 mock 都生不出来。
    """
    out: list[str] = []
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            out.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)
    return out


def _diff_prefix(n: int, spans: list[tuple[int, int, float]]) -> list[float]:
    """用差分数组算"每个时点上有多少值在生效"（O(n + spans)）。

    直接对每个时点遍历全部 span 是 O(n×m) —— 2600 天 × 400 笔 = 100 万次，
    mock 还不至于卡，但没必要。
    """
    diff = [0.0] * (n + 1)
    for a, b, v in spans:
        diff[a] += v
        diff[b] -= v
    return list(itertools.accumulate(diff[:n]))


# ======================================================================
# 交易
# ======================================================================
def _make_trades(rng: random.Random, days: list[str], n_trades: int) -> list[dict[str, Any]]:
    """造已平仓交易（成本用 cost_model 真算）。"""
    out: list[dict[str, Any]] = []
    n = len(days)
    for _ in range(n_trades):
        hold = rng.randint(3, 10)
        entry_i = rng.randint(20, max(21, n - hold - 2))
        exit_i = min(entry_i + hold, n - 1)
        if exit_i <= entry_i:
            continue

        entry_date = days[entry_i]
        exit_date = days[exit_i]
        entry_price = round(rng.uniform(6.0, 60.0), 2)

        # 单票不超过总资产 20%，留点余量（其余资金在别的票或现金上）
        target = INITIAL_CASH * rng.uniform(0.10, 0.19)
        shares = cost_model.lots_for_amount(target, entry_price) or 100

        # 赢单毛涨 13%~35%（净收益 >10% 才触发止盈）；输单毛跌 1%~25%
        gross = (
            rng.uniform(0.13, 0.35) if rng.random() < 0.42 else rng.uniform(-0.25, -0.01)
        )
        exit_price = round(entry_price * (1.0 + gross), 2)

        entry_amount = entry_price * shares
        exit_amount = exit_price * shares
        entry_cost = cost_model.buy_cost(entry_amount).total
        exit_cost = cost_model.sell_cost(exit_amount, exit_date).total
        cost = entry_cost + exit_cost
        gross_pnl = exit_amount - entry_amount
        net_pnl = gross_pnl - cost

        out.append(
            {
                "symbol": rng.choice(SYMBOLS),
                "entry_date": entry_date,
                "entry_price": entry_price,
                "shares": shares,
                "entry_amount": round(entry_amount, 2),
                "entry_cost": round(entry_cost, 2),
                "exit_date": exit_date,
                "exit_price": exit_price,
                "exit_amount": round(exit_amount, 2),
                "exit_cost": round(exit_cost, 2),
                "cost": round(cost, 2),
                "gross_pnl": round(gross_pnl, 2),
                "net_pnl": round(net_pnl, 2),
                "net_return": net_pnl / (entry_amount + entry_cost),
                "hold_days": exit_i - entry_i + 1,
                "entry_reason": "momentum_top",
                "exit_reason": rng.choice(EXIT_REASONS),
            }
        )
    return out


# ======================================================================
# 净值曲线（与交易自洽）
# ======================================================================
def _build_equity(
    rng: random.Random, days: list[str], trades: list[dict[str, Any]]
) -> pd.DataFrame:
    """净值曲线。

    ★ 自洽性：`total_asset = 初始资金 + 累计已实现净盈亏 + 未平仓浮动`。
      这样详情页上"期末总资产"与"累计净盈亏"能对上，
      而且"年化交易成本"（分母取平均总资产）算出来也是可信的量级。
    """
    n = len(days)
    idx = {d: i for i, d in enumerate(days)}

    # 已实现盈亏：按平仓日入账
    realized_at = [0.0] * n
    for t in trades:
        i = idx.get(t["exit_date"])
        if i is not None:
            realized_at[i] += float(t["net_pnl"])
    realized_cum = list(itertools.accumulate(realized_at))

    # 持仓成本基准与持仓只数：交易在 [entry_i, exit_i) 区间内生效
    basis_spans: list[tuple[int, int, float]] = []
    count_spans: list[tuple[int, int, float]] = []
    for t in trades:
        a = idx.get(t["entry_date"])
        if a is None:
            continue
        b = idx.get(t["exit_date"], n)
        basis_spans.append((a, b, float(t["entry_amount"])))
        count_spans.append((a, b, 1.0))
    open_basis = _diff_prefix(n, basis_spans)
    pos_count = [round(v) for v in _diff_prefix(n, count_spans)]

    # 未平仓浮动：相对持仓成本的小幅随机游走（均值回归，避免飘走）
    unrealized: list[float] = []
    cur = 0.0
    for i in range(n):
        cur = cur * 0.92 + rng.gauss(0.0, 0.012) * max(open_basis[i], 1.0)
        cap = 0.15 * max(open_basis[i], 1.0)
        unrealized.append(max(-cap, min(cap, cur)))

    navs: list[float] = []
    for i in range(n):
        total = INITIAL_CASH + realized_cum[i] + unrealized[i]
        navs.append(total / INITIAL_CASH)

    peak = 0.0
    drawdown: list[float] = []
    for v in navs:
        peak = max(peak, v)
        drawdown.append(v / peak - 1.0 if peak else 0.0)

    # 基准：独立随机游走（上证指数形态），同起点归一化到 1.0
    bench = [1.0]
    for _ in range(n - 1):
        bench.append(max(0.2, bench[-1] * (1.0 + rng.gauss(0.0002, 0.011))))

    rows = []
    for i, day in enumerate(days):
        total = INITIAL_CASH + realized_cum[i] + unrealized[i]
        market_value = open_basis[i] + unrealized[i]
        rows.append(
            {
                "date": day,
                "cash": round(total - market_value, 2),
                "market_value": round(market_value, 2),
                "total_asset": round(total, 2),
                "position_count": pos_count[i],
                "nav": navs[i],
                "drawdown": drawdown[i],
                "benchmark_nav": bench[i],
            }
        )
    return pd.DataFrame(rows, columns=list(EQUITY_COLUMNS))


# ======================================================================
# 持仓快照 / 委托流水
# ======================================================================
def _build_positions(days: list[str], trades: list[dict[str, Any]]) -> pd.DataFrame:
    """每日持仓明细（只在有持仓的日子出行）。"""
    idx = {d: i for i, d in enumerate(days)}
    rows: list[dict[str, Any]] = []
    for t in trades:
        a = idx.get(t["entry_date"])
        if a is None:
            continue
        b = idx.get(t["exit_date"], len(days))
        for i in range(a, min(b, len(days))):
            rows.append(
                {
                    "date": days[i],
                    "symbol": t["symbol"],
                    "shares": t["shares"],
                    "entry_date": t["entry_date"],
                    "entry_price": t["entry_price"],
                    "close": t["exit_price"],
                    "market_value": round(t["exit_price"] * t["shares"], 2),
                    "unrealized_pnl": round(
                        (t["exit_price"] - t["entry_price"]) * t["shares"], 2
                    ),
                    "hold_days": i - a + 1,
                }
            )
    return pd.DataFrame(rows, columns=list(POSITION_COLUMNS))


def _build_orders(rng: random.Random, trades: list[dict[str, Any]]) -> pd.DataFrame:
    """委托流水：成交记录 + 少量**顺延**（跌停卖不掉）。

    ★ 故意留 pending：`orders` 是排查"跌停卖不掉"的唯一凭证，
      而顺延记录在净值曲线上完全看不出来。前端要有地方展示它。
    """
    rows: list[dict[str, Any]] = []
    for i, t in enumerate(trades):
        rows.append(
            {
                "signal_date": t["entry_date"],
                "exec_date": t["entry_date"],
                "symbol": t["symbol"],
                "side": "buy",
                "shares": t["shares"],
                "price": t["entry_price"],
                "amount": t["entry_amount"],
                "status": "filled",
                "reason": "momentum_top",
                "note": "",
            }
        )
        # 少数卖单会先撞上跌停 → 顺延一日
        if i % 11 == 5:
            rows.append(
                {
                    "signal_date": t["exit_date"],
                    "exec_date": t["exit_date"],
                    "symbol": t["symbol"],
                    "side": "sell",
                    "shares": t["shares"],
                    "price": None,
                    "amount": None,
                    "status": "pending",
                    "reason": t["exit_reason"],
                    "note": "一字跌停无买盘，顺延至下一交易日重试",
                }
            )
        rows.append(
            {
                "signal_date": t["exit_date"],
                "exec_date": t["exit_date"],
                "symbol": t["symbol"],
                "side": "sell",
                "shares": t["shares"],
                "price": t["exit_price"],
                "amount": t["exit_amount"],
                "status": "filled",
                "reason": t["exit_reason"],
                "note": "",
            }
        )
    return pd.DataFrame(rows, columns=list(ORDER_COLUMNS))


# ======================================================================
# 单个 run
# ======================================================================
def _config(
    *,
    start: str,
    end: str,
    preheat_start: str,
    interval_name: str,
    is_out_sample: bool,
    debug_symbols: list[str] | None,
) -> dict[str, Any]:
    """config.json 内容（结构与 `strategies/run.py` 一致）。"""
    return {
        "strategy": {
            "name": STRATEGY,
            "version": "1.0",
            "benchmark": "sh.000001",
            "params": {
                "hold_days_max": 10,
                "take_profit_gross_main": 0.06,
                "take_profit_gross_gem": 0.10,
                "index_panic_pct": -0.015,
                "max_positions": 5,
            },
        },
        "interval": {
            "name": interval_name,
            "start": start,
            "end": end,
            "preheat_start": preheat_start,
            "is_out_sample": is_out_sample,
        },
        "capital": {
            "initial_cash": INITIAL_CASH,
            "max_positions": 5,
            "max_weight_per_stock": 0.20,
        },
        "universe": {
            "boards": ["sh_main", "sz_main", "gem"],
            "listed_days_min": 90,
            "no_rebuy_days": 60,
        },
        "cost": {
            "commission_rate": 0.00025,
            "commission_min": 5.0,
            "transfer_fee_rate": 0.00001,
            "slippage_bp": 5.0,
        },
        "data": {"adjust": "hfq", "source": "baostock"},
        "debug_symbols": debug_symbols,
        "description": "【MOCK 数据】由 tools/make_mock_backtest.py 生成，供前端开发使用",
    }


def _emit(
    *,
    run_id: str,
    tag: str,
    start: date,
    end: date,
    n_trades: int,
    rng: random.Random,
    debug_symbols: list[str] | None = None,
    interval_name: str = "in_sample",
    is_out_sample: bool = False,
    finished_at: str,
    register_db: bool,
) -> dict[str, Any]:
    days = _trading_days(start, end)
    trades = _make_trades(rng, days, n_trades)
    equity = _build_equity(rng, days, trades)
    trades_df = pd.DataFrame(trades)
    positions = _build_positions(days, trades)
    orders = _build_orders(rng, trades)

    metrics = metrics_mod.compute_metrics(equity, trades_df, initial_cash=INITIAL_CASH)

    cfg = _config(
        start=days[0],
        end=days[-1],
        preheat_start=(start - timedelta(days=150)).strftime("%Y-%m-%d"),
        interval_name=interval_name,
        is_out_sample=is_out_sample,
        debug_symbols=debug_symbols,
    )
    layout = run_layout(STRATEGY, run_id)
    write_run(
        layout,
        config=cfg,
        equity=equity,
        trades=trades_df,
        positions=positions,
        orders=orders,
        metrics=metrics,
        tag=tag,
        started_at=finished_at,
        finished_at=finished_at,
    )

    if register_db:
        try:
            meta_repo.register_backtest_run(
                run_id, STRATEGY, days[0], days[-1],
                tag=tag, is_out_sample=is_out_sample,
                param=cfg["strategy"]["params"], config=cfg,
            )
            meta_repo.finish_backtest_run(
                run_id, "success", metrics=metrics, cost_seconds=42.0,
                trade_count=int(metrics.get("trade_count") or 0),
                message="MOCK 数据",
            )
        except Exception as exc:
            logger.warning(f"写入 meta.db 失败（mock 数据仍可用）：{exc}")

    return {
        "run_id": run_id,
        "tag": tag,
        "days": len(days),
        "trades": len(trades_df),
        "total_return": metrics.get("total_return"),
        "path": relative_to_project(layout.root),
    }


# ======================================================================
# 清理
# ======================================================================
def _clean() -> int:
    """删除全部 `mock-` 前缀的回测产物（含 meta.db 登记）。"""
    from app.services import data_store  # noqa: PLC0415

    removed = 0
    for strategy, run_id in data_store.list_all_runs():
        if not run_id.startswith(MOCK_PREFIX):
            continue
        if data_store.delete_run(strategy, run_id):
            removed += 1

    try:
        from app.repos import db  # noqa: PLC0415

        if db.is_initialized():
            n = db.execute(
                "DELETE FROM backtest_run WHERE run_id LIKE ?", (f"{MOCK_PREFIX}%",)
            )
            logger.info(f"meta.db 中清理 MOCK 登记 {n} 条")
    except Exception as exc:
        logger.warning(f"清理 meta.db 登记失败（不影响目录清理）：{exc}")

    backtest_service.invalidate_cache()
    return removed


# ======================================================================
# 入口
# ======================================================================
def main() -> int:
    parser = argparse.ArgumentParser(description="生成 mock 回测产物（前端开发用）")
    parser.add_argument("--clean", action="store_true", help="删除全部 mock run 后退出")
    parser.add_argument("--seed", type=int, default=20260918, help="随机种子（保证可复现）")
    parser.add_argument("--no-db", action="store_true", help="不写 meta.db 登记")
    args = parser.parse_args()

    setup_logging()

    if args.clean:
        n = _clean()
        print(f"已删除 {n} 个 mock run。")
        return 0

    ensure_dirs()
    rng = random.Random(args.seed)
    register = not args.no_db

    specs = [
        {
            "run_id": f"{MOCK_PREFIX}normal-a",
            "tag": "baseline",
            "start": date(2020, 6, 1),
            "end": date(2024, 12, 31),
            "n_trades": 342,
            "finished_at": "2026-09-15T20:10:00",
        },
        {
            # ⚠️ 起点不能再往前：`cost.stamp_tax` 的分段只覆盖 2020-06-01 起，
            #    更早的日期会（正确地）抛 AppError —— 成本模型"绝不静默取 0"。
            #    2020-06-01 ~ 今 约 1650 个交易日，仍不足以触发 2000 点降采样；
            #    降采样路径请用 `?max_points=200` 触发（真实回测同样只用 1115 天）。
            "run_id": f"{MOCK_PREFIX}long",
            "tag": "long-window",
            "start": date(2020, 6, 1),
            "end": date(2026, 9, 17),
            "n_trades": 806,
            "finished_at": "2026-09-16T09:35:00",
        },
        {
            "run_id": f"{MOCK_PREFIX}empty",
            "tag": "no-signal",
            "start": date(2023, 1, 2),
            "end": date(2023, 10, 31),
            "n_trades": 0,
            "finished_at": "2026-09-16T21:05:00",
        },
        {
            "run_id": f"{MOCK_PREFIX}debug",
            "tag": "smoke-3symbols",
            "start": date(2026, 7, 1),
            "end": date(2026, 8, 31),
            "n_trades": 14,
            "debug_symbols": ["sh.600519", "sz.000001", "sz.300750"],
            "interval_name": "custom",
            "finished_at": "2026-09-17T11:02:00",
        },
    ]

    print()
    print("=" * 74)
    print("  生成 MOCK 回测产物（前端开发用）")
    print("=" * 74)
    for spec in specs:
        info = _emit(rng=rng, register_db=register, **spec)
        ret = info["total_return"]
        ret_s = f"{ret * 100:7.2f}%" if ret is not None else "      —"
        print(
            f"  {info['run_id']:<16s} {info['days']:>5d} 天 {info['trades']:>4d} 笔 "
            f"总收益 {ret_s}  {info['path']}"
        )
    print("=" * 74)
    print()
    print("  前端现在可以对着这些数据开发：")
    print("    GET /api/v1/backtest/runs")
    print("    GET /api/v1/backtest/runs/momentum/mock-normal-a")
    print("    GET /api/v1/backtest/runs/momentum/mock-normal-a/equity")
    print("    GET /api/v1/backtest/runs/momentum/mock-normal-a/trades")
    print()
    print("  清理：python -m tools.make_mock_backtest --clean")
    print()
    logger.info(f"已生成 {len(specs)} 个 mock run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
