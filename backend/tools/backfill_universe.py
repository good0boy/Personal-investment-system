"""每日股票池（Universe）快照回补 —— `python -m tools.backfill_universe`。

为什么需要它（研究文档 §1「产物缺口」第 2 条，挂了很久）
-------------------------------------------------------
`data/raw/universe/` 一直是**空的**：股票池每天都由 `tools/universe.build_universe()`
现算，但**从不落盘**。后果是"基准"没法严格对齐 ——

    回测内的股票池 = 剔除 ST / 停牌 / 新股(<90 交易日) / 一字涨跌停 / 非目标板块
    而研究用的"全市场等权基准"只能用 `quote_daily` 全量凑

两者口径不同，于是"策略跑输基准 X 个百分点"这句话的**分母是错的**。
而「没有基准不动结论」是本项目头号分析纪律 —— 基准口径不严 = 纪律打折。

本工具把逐日股票池落成快照：`data/raw/universe/{date}.parquet`（列 date/symbol）。
之后基准计算改读快照（见 `app/services/benchmark_service.py`），两边口径就统一了。

设计要点（与 `tools/backfill_quotes.py` 同一先例）
--------------------------------------------------
1. **文件即状态，不需要断点续传的状态文件**：一天一个 parquet，已存在就跳过。
   这比维护一个 json 状态更稳 —— 与模块 3「磁盘产物为唯一真相源」是同一条纪律。
2. **`first_trade_date` 必须来自全量历史**，不能从"当前加载的矩阵"推断。
   项目已经在这上面栽过一次（研究文档 §4 第 33 条：用矩阵内日期当交易日历算上市
   天数，把 2020 年前上市的老股票全判成新股 → 整池清空）。这里一次性算全量。
3. **单日失败不中断整批**：记下来继续，最后汇总。
4. **耗时**：每天一次"读全市场当日行情 + 建池"，实测约 0.1~0.3s/天，
   1600 天量级约 5~10 分钟。

用法
----
    cd backend
    python -m tools.backfill_universe --status                  # 只看进度（不联网、不写盘）
    python -m tools.backfill_universe                           # 全量续传（跳过已存在的日期）
    python -m tools.backfill_universe --start 2025-01-01        # 只补某段
    python -m tools.backfill_universe --force                   # 重算并覆盖
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.errors import AppError  # noqa: E402
from app.core.logging import logger, setup_logging  # noqa: E402
from app.core.paths import UNIVERSE_DIR, relative_to_project  # noqa: E402
from app.services import data_store  # noqa: E402
from app.services.calendar_service import calendar  # noqa: E402
from tools.universe import build_universe  # noqa: E402


def existing_days() -> set[str]:
    """已落盘的快照日期集合（文件名即日期）。"""
    if not UNIVERSE_DIR.exists():
        return set()
    out: set[str] = set()
    for p in UNIVERSE_DIR.glob("*.parquet"):
        out.add(p.stem)
    return out


def all_first_trade_dates() -> dict[str, str]:
    """一次性从**全量行情**推导每只标的的首个交易日。

    ★ 必须是全量：拿子区间推导会把区间之前就上市的老股票误判成"新股"
      （研究文档 §4 第 33 条）。
    """
    glob = data_store.quote_glob()
    df = data_store.query_parquet(
        glob,
        "SELECT symbol, min(date) AS first_date FROM t GROUP BY symbol",
    )
    if df.empty:
        raise AppError(
            "行情为空，无法推导上市日期。请先执行：python -m tools.backfill_quotes"
        )
    return {
        str(r.symbol): pd.Timestamp(r.first_date).strftime("%Y-%m-%d")
        for r in df.itertuples(index=False)
    }


#: 建池需要的行情列（列裁剪：少读 3 列能省 1/4 时间）
_QUOTE_COLUMNS = (
    "date", "symbol", "open", "high", "low", "close",
    "preclose", "amount", "pct_chg", "trade_status", "is_st",
)


def load_all_quotes(start: str, end: str) -> pd.DataFrame:
    """把区间内全市场行情**一次性**读进内存。

    ★ 为什么不在循环里逐日 `build_universe_from_store`：
      那个函数每天都走一次 `read_quotes(全市场 glob)` —— DuckDB 要为**每一天**
      打开全部 4792 个 parquet 文件，实测约 5 秒/天，1600 天就是 2 小时以上。
      一次性读进来（约 7 秒）再按日切片，总耗时降到分钟级。
      代价是内存里多放一份 700 万行长表（约 0.6~1 GB）—— 对单机脚本完全可接受。
    """
    q = data_store.read_quotes(
        symbols=None, start_date=start, end_date=end, columns=list(_QUOTE_COLUMNS)
    )
    if q.empty:
        raise AppError(f"行情为空：{start} ~ {end}。请先执行 python -m tools.backfill_quotes")
    q = q.copy()
    q["date"] = pd.to_datetime(q["date"])
    return q


def trading_days(start: str | None, end: str | None) -> list[str]:
    from app.services.calendar_service import calendar  # noqa: PLC0415

    # `calendar.trading_days()` 的 end 不接受 None（归一化时会抛"日期为空"），
    # 所以这里把"到今天"显式化。
    end_s = end or date.today().strftime("%Y-%m-%d")
    days = calendar.trading_days(start, end_s)
    if not days:
        raise AppError(f"交易日历在 {start or '最早'} ~ {end_s} 内没有任何交易日")
    return list(days)


def print_status() -> None:
    days = sorted(existing_days())
    print()
    print("=" * 84)
    print("  股票池快照进度")
    print("=" * 84)
    print(f"  目录    : {relative_to_project(UNIVERSE_DIR)}")
    print(f"  已落盘  : {len(days)} 天")
    if days:
        print(f"  覆盖区间: {days[0]} ~ {days[-1]}")
        # 抽一天看规模，确认不是空文件
        sample = UNIVERSE_DIR / f"{days[-1]}.parquet"
        n = len(pd.read_parquet(sample))
        print(f"  最新一天: {days[-1]}，{n} 只")
    else:
        print("  （尚未回补）请执行：python -m tools.backfill_universe")
    print("=" * 84)
    print()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m tools.backfill_universe",
        description="每日股票池（Universe）快照回补（断点=文件存在即跳过）",
    )
    p.add_argument("--start", default="2020-01-01", help="起始交易日（默认 2020-01-01）")
    p.add_argument("--end", default=None, help="结束交易日（默认今天）")
    p.add_argument("--force", action="store_true", help="重算并覆盖已存在的快照")
    p.add_argument("--status", action="store_true", help="只看进度后退出")
    p.add_argument("--limit", type=int, default=0, help="只处理前 N 天（调试用）")
    args = p.parse_args(argv)

    setup_logging()
    if args.status:
        print_status()
        return 0

    # 时间锁：显式给了终点就上锁，越界会抛 ReadUntilViolation（让 bug 大声）
    if args.end:
        data_store.set_read_until(args.end)

    days = trading_days(args.start, args.end)
    done = existing_days()
    todo = [d for d in days if args.force or d not in done]
    if args.limit:
        todo = todo[: args.limit]

    print()
    print("=" * 96)
    print(
        f"  股票池快照回补｜区间 {days[0]} ~ {days[-1]}（{len(days)} 交易日）"
        f"｜已存在 {len(days) - len([d for d in days if d not in done])}｜待处理 {len(todo)}"
    )
    print("=" * 96)

    if not todo:
        logger.info("全部交易日都已有快照，无需回补（--force 可强制重算）")
        print_status()
        return 0

    all_start = days[0]
    all_end = days[-1]
    logger.info(f"读取全量行情 {all_start} ~ {all_end}（一次读盘，循环内按日切片）…")
    t0 = time.monotonic()
    quotes = load_all_quotes(all_start, all_end)
    logger.info(
        f"行情就绪：{len(quotes):,} 行 × {quotes['symbol'].nunique()} 只"
        f"（{time.monotonic() - t0:.1f}s）"
    )

    logger.info("推导全量上市日期（全库一句 SQL，★ 不能用区间内行情推导）…")
    t0 = time.monotonic()
    ftd = all_first_trade_dates()
    logger.info(f"上市日期就绪：{len(ftd)} 只（{time.monotonic() - t0:.1f}s）")

    # 按日分组一次（避免 1600 次全表比对），再逐日建池
    by_day = {d.strftime("%Y-%m-%d"): sub for d, sub in quotes.groupby("date", sort=True)}

    ok = 0
    empty: list[str] = []
    missing: list[str] = []
    failed: list[tuple[str, str]] = []
    t_all = time.monotonic()

    for i, day in enumerate(todo, 1):
        try:
            sub = by_day.get(day)
            if sub is None or sub.empty:
                # 日历说这天是交易日，行情里却没有 —— 多为数据没回补到那么新。
                # 不落盘（免得把"缺数据"伪装成"空池"），只记下来。
                missing.append(day)
            else:
                res = build_universe(
                    day,
                    sub,
                    first_trade_date=ftd,
                    calendar_days_fn=calendar.trading_days,
                    verbose=False,
                )
                if not res.symbols:
                    # 空池也落盘（0 行带列结构）—— "当天确实没有可投标的"与
                    # "这天没跑过"必须能区分（与模拟盘 0 笔交易必须落盘同一个道理）
                    data_store.save_universe(day, [], meta={"kept": 0, "empty": True})
                    empty.append(day)
                else:
                    data_store.save_universe(
                        day, res.symbols, meta={"kept": len(res.symbols), "empty": False}
                    )
                    ok += 1
        except Exception as exc:  # noqa: BLE001 - 单日失败不中断整批
            failed.append((day, f"{type(exc).__name__}: {exc}"))
            logger.warning(f"{day} 股票池构建失败：{exc}")

        if i % 100 == 0 or i == len(todo):
            speed = i / max(time.monotonic() - t_all, 1e-6)
            eta = (len(todo) - i) / max(speed, 1e-6)
            print(
                f"  [{i:>5}/{len(todo)}] {day}｜写入 {ok}｜空池 {len(empty)}"
                f"｜缺行情 {len(missing)}｜失败 {len(failed)}"
                f"｜{speed:.1f} 天/秒｜剩余约 {eta / 60:.1f} 分钟"
            )

    elapsed = time.monotonic() - t_all
    print("-" * 96)
    print(
        f"  完成：写入 {ok}｜空池 {len(empty)}｜缺行情 {len(missing)}｜失败 {len(failed)}"
        f"｜耗时 {elapsed / 60:.1f} 分钟｜目录 {relative_to_project(UNIVERSE_DIR)}"
    )
    if empty:
        print(
            f"    空池日期（已如实落盘 0 行；多为 2020 上半年「行情不足 90 交易日」期）："
            f"{empty[0]} ~ {empty[-1]} 共 {len(empty)} 天"
        )
    if missing:
        print(f"    缺行情日期（未落盘）：{missing[0]} ~ {missing[-1]} 共 {len(missing)} 天")
    for day, reason in failed[:10]:
        print(f"    ❌ {day}: {reason}")
    print("=" * 96)
    print()

    # 有失败就给非零退出码（沿用 factors.calc 的约定：3 = 有失败，1 留给"结果完好但打印崩了"）
    return 3 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
