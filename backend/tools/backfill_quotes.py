"""全市场日线回补（阶段 2 的第一个任务）。

为什么必须有它
--------------
回测/模拟盘的行情**只能来自本地**（`data_store` 是唯一入口，业务层不联网）。
所以跑策略之前，得先把 2020-01-01 至今的全市场日线拉下来。
规模约 5000 只 × 1600 交易日 ≈ 800 万行，一次 1~3 小时 —— 中途断是常态，
**断点续传不是可选项**。

本工具的设计要点
----------------
1. **标的清单来自历史日期**：`query_all_stock(day)` 支持历史日期（实测
   2020-01-02 返回 4306 只、2022-01-04 返回 5147 只，且 600069/600074/600086/600090
   等已退市股票都在名单里）。按**每个季度的首个交易日**采样再取并集，
   就能覆盖"曾经交易过"的全部标的 —— 这是消除幸存者偏差的前提。
   ⚠️ 不能用"当前名单"，那样退市股会全部缺失。
2. **断点续传**：状态写 `data/meta/backfill_state.json`，每只一条记录，
   每 N 只落一次盘。Ctrl-C / 断电后重跑即续上。
3. **单只失败不中断整批**：失败只记 error 继续下一只，最后统一汇总，
   `--retry-failed` 可只重试失败的。
4. **不写 `data_store` 之外的地方**：行情一律经 `data_store.save_quotes()`，
   它内部做 merge（按 date 去重，新值覆盖旧值），所以重复跑是安全的。

⚠️ **回补运行期间不要在别的进程里调 baostock。**
   baostock 的登录是**按账号**的，另一个进程 login/logout 会把本进程的会话踢掉，
   表现为突然全部请求报 `10001001 用户未登录`（2026-09-18 实测踩到）。
   回测/读 parquet 不受影响，只有需要联网取数的操作（catalog 自检等）要避开。

用法
----
    cd backend
    python -m tools.backfill_quotes --status           # 只看进度（不联网）
    python -m tools.backfill_quotes --discover         # 只刷新标的清单
    python -m tools.backfill_quotes --limit 5          # 冒烟：只拉 5 只
    python -m tools.backfill_quotes                    # 全量续传
    python -m tools.backfill_quotes --retry-failed     # 只重试失败的
    python -m tools.backfill_quotes --incremental      # 增量：补齐已成功但落后的
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.errors import AppError
from app.core.logging import logger, setup_logging
from app.core.paths import BACKFILL_STATE_FILE, ensure_dirs
from app.services import calendar_service, data_store
from app.services.data_source import baostock_client
from tools.universe import is_target_board_symbol

#: 回补起点 —— 与策略规格的预热窗口一致（2020-01-01 起日线）
START_DATE = "2020-01-01"

#: 必须回补的指数（上证指数：择时闸门 + 大盘急跌清仓信号源）
INDEX_SYMBOLS: tuple[str, ...] = ("sh.000001",)

#: 每处理多少只落一次盘（状态文件写入是原子替换，代价很低）
SAVE_EVERY = 10
#: 每处理多少只打一行进度
PROGRESS_EVERY = 25


# ======================================================================
# 状态文件（断点续传的载体）
# ======================================================================
def load_state() -> dict[str, Any]:
    if not BACKFILL_STATE_FILE.exists():
        return {"version": 1, "symbols": {}, "discovered_at": None}
    try:
        data = json.loads(BACKFILL_STATE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logger.warning(f"状态文件损坏，将重建：{BACKFILL_STATE_FILE}")
        return {"version": 1, "symbols": {}, "discovered_at": None}
    data.setdefault("symbols", {})
    return data


def save_state(state: dict[str, Any]) -> None:
    """原子写：先写临时文件再 replace，避免断电留下半截 JSON。"""
    BACKFILL_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = BACKFILL_STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(BACKFILL_STATE_FILE)


# ======================================================================
# 标的清单
# ======================================================================
def _quarter_sample_days(start: str, end: str) -> list[str]:
    """每个季度的首个交易日 + 区间最后一天。

    取季度粒度是个折中：更密的采样能覆盖"上市后不到一年就退市"的极端标的，
    但每次采样都是一次网络请求。季度粒度把这种漏网窗口压到 3 个月以内，
    成本只有约 27 次请求。
    """
    days = calendar_service.calendar.trading_days(start, end)
    if not days:
        raise AppError(f"交易日历在 {start} ~ {end} 内没有任何交易日，请先同步日历")

    picked: list[str] = []
    seen: set[tuple[str, int]] = set()
    for d in days:
        key = (d[:4], (int(d[5:7]) - 1) // 3)
        if key not in seen:
            seen.add(key)
            picked.append(d)
    if days[-1] not in picked:
        picked.append(days[-1])
    return picked


def discover_symbols(end: str) -> list[str]:
    """全部"曾经交易过"的目标板块标的（含已退市）。

    ⚠️ 用**历史日期**的名单取并集。用当前名单会漏掉所有退市股 ——
       那正是幸存者偏差的来源。
    """
    sample_days = _quarter_sample_days(START_DATE, end)
    logger.info(f"标的探测：{len(sample_days)} 个采样日（{sample_days[0]} ~ {sample_days[-1]}）")

    all_codes: set[str] = set()
    for i, d in enumerate(sample_days, 1):
        t0 = time.perf_counter()
        try:
            df = baostock_client.fetch_all_stock(d)
        except Exception as exc:
            logger.warning(f"采样日 {d} 取名单失败（跳过）：{exc}")
            continue
        codes = [str(c) for c in df["code"].tolist()]
        all_codes.update(codes)
        logger.info(
            f"  采样 {i}/{len(sample_days)}（{d}）返回 {len(codes)} 个，"
            f"累计 {len(all_codes)}，本 Call 耗时 {time.perf_counter() - t0:.1f}s"
        )

    targets = sorted(c for c in all_codes if is_target_board_symbol(c))
    dropped = len(all_codes) - len(targets)
    logger.info(
        f"标的探测完成：原始 {len(all_codes)} 个代码 → "
        f"目标板块 {len(targets)} 只（剔除指数/基金/科创/北交/B股等 {dropped} 个）"
    )
    return targets


# ======================================================================
# 单只回补
# ======================================================================
def _fetch_one(symbol: str, start: str, end: str, *, is_index: bool) -> Any:
    if is_index:
        return baostock_client.fetch_index_daily(symbol, start, end)
    return baostock_client.fetch_daily(symbol, start, end, adjust="hfq")


def _do_one(
    symbol: str,
    rec: dict[str, Any],
    start: str,
    end: str,
    *,
    is_index: bool,
) -> str:
    """回补一只，就地更新 rec。返回状态字符串。"""
    t0 = time.perf_counter()
    try:
        df = _fetch_one(symbol, start, end, is_index=is_index)
    except Exception as exc:
        rec.update(
            status="failed",
            error=f"{type(exc).__name__}: {str(exc)[:200]}",
            attempts=int(rec.get("attempts") or 0) + 1,
            updated_at=datetime.now().isoformat(timespec="seconds"),
        )
        return "failed"

    if df is None or df.empty:
        rec.update(
            status="empty",
            rows=0,
            error="",
            updated_at=datetime.now().isoformat(timespec="seconds"),
        )
        return "empty"

    try:
        data_store.save_quotes(df, is_index=is_index, merge=True)
    except Exception as exc:
        rec.update(
            status="failed",
            error=f"落盘失败 {type(exc).__name__}: {str(exc)[:200]}",
            attempts=int(rec.get("attempts") or 0) + 1,
            updated_at=datetime.now().isoformat(timespec="seconds"),
        )
        return "failed"

    dates = df["date"]
    rec.update(
        status="ok",
        rows=len(df),
        first_date=str(dates.min())[:10],
        last_date=str(dates.max())[:10],
        error="",
        is_index=is_index,
        elapsed_ms=round((time.perf_counter() - t0) * 1000, 1),
        updated_at=datetime.now().isoformat(timespec="seconds"),
    )
    return "ok"


# ======================================================================
# 主流程
# ======================================================================
def _is_done(rec: dict[str, Any], *, latest_trading_day: str | None, incremental: bool) -> bool:
    if rec.get("status") != "ok":
        return False
    if not incremental:
        return True
    # 增量模式：已成功但落后于最新交易日 → 需要补齐
    return str(rec.get("last_date") or "") >= str(latest_trading_day or "")


def _print_status(state: dict[str, Any], end: str) -> None:
    syms = state.get("symbols") or {}
    buckets: dict[str, int] = {}
    rows = 0
    for rec in syms.values():
        buckets[rec.get("status", "pending")] = buckets.get(rec.get("status", "pending"), 0) + 1
        rows += int(rec.get("rows") or 0)
    print()
    print("=" * 62)
    print("  全市场日线回补 · 进度")
    print("=" * 62)
    print(f"  区间终点    : {end}")
    print(f"  清单发现于  : {state.get('discovered_at')}")
    print(f"  清单规模    : {len(syms)} 只")
    for k in ("ok", "failed", "empty", "pending"):
        if buckets.get(k):
            print(f"    {k:8s}: {buckets[k]}")
    print(f"  已落盘行数  : {rows:,}")
    print(f"  状态文件    : {BACKFILL_STATE_FILE}")
    failed = [s for s, r in syms.items() if r.get("status") == "failed"]
    if failed:
        print(f"  失败样例    : {failed[:5]}")
    print("=" * 62)


def main() -> int:
    parser = argparse.ArgumentParser(description="全市场日线回补（断点续传）")
    parser.add_argument("--status", action="store_true", help="只看进度，不联网")
    parser.add_argument("--discover", action="store_true", help="只刷新标的清单")
    parser.add_argument("--limit", type=int, default=None, help="只处理前 N 只（冒烟用）")
    parser.add_argument("--only", default=None, help="只处理指定代码，逗号分隔")
    parser.add_argument("--retry-failed", action="store_true", help="只重试失败的")
    parser.add_argument("--incremental", action="store_true", help="增量补齐落后的标的")
    parser.add_argument("--force", action="store_true", help="忽略进度全量重拉（慎用）")
    parser.add_argument("--end", default=None, help="区间终点，默认今天")
    args = parser.parse_args()

    setup_logging()
    ensure_dirs()

    end = args.end or date.today().strftime("%Y-%m-%d")
    state = load_state()

    if args.status:
        _print_status(state, end)
        return 0

    latest_trading_day = calendar_service.calendar.latest_trading_day()

    # ---- 1) 标的清单（缺失或 --discover 时才联网探测）----
    if args.only:
        symbols = [s.strip() for s in args.only.split(",") if s.strip()]
    elif not state.get("symbols") or args.discover:
        symbols = discover_symbols(end)
        state["symbols"] = {s: state["symbols"].get(s, {}) for s in symbols}
        state["discovered_at"] = datetime.now().isoformat(timespec="seconds")
        state["universe_size"] = len(symbols)
        save_state(state)
        if args.discover:
            _print_status(state, end)
            return 0
    else:
        raw = sorted(state["symbols"].keys())
        keep = set(INDEX_SYMBOLS)
        symbols = [s for s in raw if s in keep or is_target_board_symbol(s)]
        if len(symbols) != len(raw):
            # ★ 清单自愈：板块判定规则更新后，旧清单里可能残留不符规则的代码。
            #   实测 2026-09-18：修掉"只看 6 位码"的 bug 后，旧清单里有 223 个
            #   `sh.000xxx` 上证指数需要剔除。不自动清理的话，它们会被当成
            #   深主板股票拉取并落进 quote_daily/，污染回测候选池。
            dropped = sorted(set(raw) - set(symbols))
            logger.warning(
                f"清单里有 {len(dropped)} 个代码不符合当前板块规则，已剔除"
                f"（例如 {dropped[:3]}）—— 通常是板块判定规则更新过"
            )
            for s in dropped:
                state["symbols"].pop(s, None)
            state["universe_size"] = len(symbols)
            save_state(state)
        logger.info(f"沿用已有清单（{len(symbols)} 只）；要重扫加 --discover")

    # ---- 2) 待办筛选 ----
    todo: list[str] = []
    for s in symbols:
        rec = state["symbols"].setdefault(s, {})
        if args.force:
            todo.append(s)
            continue
        if args.retry_failed and rec.get("status") != "failed":
            continue
        if _is_done(rec, latest_trading_day=latest_trading_day, incremental=args.incremental):
            continue
        todo.append(s)

    # 指数单独补（不在板块过滤的标的里）
    index_todo = [
        s for s in INDEX_SYMBOLS
        if args.force or not _is_done(
            state["symbols"].setdefault(s, {}),
            latest_trading_day=latest_trading_day,
            incremental=args.incremental,
        )
    ]

    if args.limit:
        todo = todo[: args.limit]
        index_todo = index_todo[: args.limit]

    if not todo and not index_todo:
        logger.info("没有待办标的 —— 已全部回补完成（要重拉加 --force）")
        _print_status(state, end)
        return 0

    logger.info(
        f"本次待办：个股 {len(todo)} 只 + 指数 {len(index_todo)} 个"
        f"（清单 {len(symbols)} 只 / 最新交易日 {latest_trading_day}）"
    )

    # ---- 3) 逐只回补（断点续传）----
    failed: list[str] = []
    n_done = 0
    t_start = time.perf_counter()

    for symbol in index_todo:
        rec = state["symbols"].setdefault(symbol, {})
        st = _do_one(symbol, rec, START_DATE, end, is_index=True)
        if st == "failed":
            failed.append(symbol)
        save_state(state)

    for i, symbol in enumerate(todo, 1):
        rec = state["symbols"].setdefault(symbol, {})
        st = _do_one(symbol, rec, START_DATE, end, is_index=False)
        n_done += 1
        if st == "failed":
            failed.append(symbol)

        if i % SAVE_EVERY == 0:
            save_state(state)
        if i % PROGRESS_EVERY == 0 or i == len(todo):
            done = sum(1 for r in state["symbols"].values() if r.get("status") == "ok")
            rows = sum(int(r.get("rows") or 0) for r in state["symbols"].values())
            spent = time.perf_counter() - t_start
            speed = n_done / spent if spent > 0 else 0
            eta = (len(todo) - i) / speed / 60 if speed > 0 else 0
            logger.info(
                f"进度 {i}/{len(todo)}（{i / len(todo) * 100:.1f}%）｜ "
                f"累计 ok={done} 行={rows:,}｜ "
                f"{speed:.2f} 只/秒 用时 {spent / 60:.1f}min 预计还需 {eta:.0f}min"
            )

    save_state(state)

    # ---- 4) 汇总 ----
    spent = time.perf_counter() - t_start
    logger.info(
        f"回补结束：处理 {n_done} 只，失败 {len(failed)} 只，"
        f"用时 {spent / 60:.1f} 分钟"
    )
    _print_status(state, end)
    if failed:
        print(f"  失败 {len(failed)} 只，重试：python -m tools.backfill_quotes --retry-failed")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
