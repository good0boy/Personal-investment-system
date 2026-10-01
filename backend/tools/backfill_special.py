"""同花顺特色数据回补 —— 涨跌停池 / 板块指数历史 / 热榜 / 龙虎榜 / 个股异动。

落盘位置见 `app/core/paths.py`（LIMIT_POOL_DIR / BOARD_DIR / HOT_LIST_DIR /
DRAGON_TIGER_DIR / ANOMALY_DIR），全部经 `data_store` 写入（架构纪律：
文件 IO 只走 data_store）。

================================ 用法 ================================
    python -m tools.backfill_special --status          # 只看进度，不联网
    python -m tools.backfill_special                   # 全量回补（断点续传）
    python -m tools.backfill_special --dataset limit_pool
    python -m tools.backfill_special --incremental     # 每日增量
    python -m tools.backfill_special --limit 30        # 冒烟：每个数据集只跑 30 项
    python -m tools.backfill_special --retry-failed
    python -m tools.backfill_special --force
    python -m tools.backfill_special --verify 20       # 抽样与源端逐行比对

============================ 各数据的真实边界 ============================
（2026-10-01 逐个日期实测，**不是文档抄来的**）

    数据集            实际可用起点        证据
    ----------------  ------------------  -------------------------------
    涨跌停三池        ≈2020-07-13        20200701→0 行、20200715→87 行
    行业板块指数历史  ≈2021-09/10        2021-07-01→0、2021-10-08→61
    概念板块指数历史  因板块而异(最晚23)  885431.TI 2022H1 有；886042.TI 要到 2023
    热榜历史/龙虎榜   **硬限 1 年**       一年前报 code=1003
    个股异动          **只有当日**        无日期参数

★ 所以本工具的口径是「**按各数据实际起点全量回补**」：能拿多少拿多少，
  拿不到的不假装成功，缺口由 `--status` 如实报出来。

============================= 三个关键实现点 =============================
1. **涨停池必须翻页**：服务端单页上限 200，而热市单日会撞满
   （实测 20241008、20240930 都是 200 行）→ 不翻页就是静默截断。
   撞到页数上限记 `partial`（**不是 ok**），下次运行会重试。
2. **空表分两种**：非交易日**根本不发请求**（先用日历过滤）；
   交易日 0 行记 `empty`（如某天无跌停股），**不写文件**、视为完成、
   以后不再重试 —— 否则每天白打一遍请求。
3. **限速要按聚合算**：`max_calls_per_minute` 是**客户端自限**，
   而服务端按 API Key 计 → N 个通道各 60/min 会聚合出 4 倍速率、直接撞
   4001。所以每通道限速 = `60 // workers`。
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import threading
import time
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

import pandas as pd

from app.core.errors import AppError, DataSourceRateLimited
from app.core.logging import logger, setup_logging
from app.core.paths import BACKFILL_SPECIAL_STATE_FILE, ensure_dirs
from app.repos import db
from app.services import data_store
from app.services.data_source.fuyao_client import (
    DRAGON_TIGER_HOT_MONEY_COLUMNS,
    DRAGON_TIGER_STOCK_COLUMNS,
    FuyaoClient,
)

# ======================================================================
# 配置常量
# ======================================================================
#: 各数据集的实际可用起点（实测；None = 相对今天动态计算）
LIMIT_POOL_START = "2020-07-13"
#: 板块指数历史的**候选起点**（从早到晚，非空即停）。
#:
#: ⚠️ 实测陷阱（2026-10-01）：`index_historical` 在 `start` **早于该板块的数据首日**
#:    时**静默返回 0 行** —— 不是"返回它有的那一段"，而是整段空。
#:    验证：`881121.TI` 传 `2021-09-14` → 0 行；传 `2022-01-01` → 117 行。
#:    所以**不能用一个统一起点**（我第一版用 2021-07-01，结果所有板块都是空的）。
#:    抽样 4 个板块（行业/概念各 2）的首个可用起点都是 `2021-10-08`。
BOARD_START_CANDIDATES: tuple[str, ...] = (
    "2021-10-08",
    "2022-01-04",
    "2022-07-01",
    "2023-01-03",
    "2023-07-03",
    "2024-01-02",
    "2025-01-02",
    "2026-01-05",
)
#: 一年窗口的起点用「今天 - 364 天」（热榜历史 / 龙虎榜 / 排名走势都限一年内）
ONE_YEAR_DAYS = 364

#: 服务端单页上限；撞满即需翻页
FUYAO_PAGE_SIZE = 200
#: 单日最多翻多少页（= 2000 行/日）。撞上限记 partial。
MAX_PAGES_PER_DAY = 10

#: 每多少条落一次状态文件
SAVE_EVERY = 25
#: 每多少条打一次进度
PROGRESS_EVERY = 50

#: 各数据集的**固定列集**。
#: ⚠️ 必须 reindex 到这些列 —— `local_data_service._scan_rows` 刻意不加
#:    `union_by_name`，同一 glob 内 schema 不一致会直接报错。
EXPECTED: dict[str, tuple[str, ...]] = {
    "up": (
        "date", "thscode", "ticker", "name", "is_st", "is_new", "last_price",
        "price_change_ratio_pct", "limit_up_time", "limit_up_reason",
        "continue_day_text", "continue_day_cnt", "seal_money", "max_seal_money",
    ),
    "down": (
        "date", "thscode", "ticker", "name", "last_price",
        "price_change_ratio_pct", "first_limit_time", "last_limit_time",
        "turnover_ratio_pct",
    ),
    "break": (
        "date", "thscode", "ticker", "name", "last_price",
        "price_change_ratio_pct", "open_times", "turnover_ratio_pct", "turnover",
    ),
    "board": ("date", "thscode", "open", "high", "low", "close", "volume", "amount"),
    "board_catalog": ("tag", "thscode", "name", "snapshot_date"),
    "hot_history": ("date", "thscode", "ticker", "name", "rank"),
    "hot_current": (
        "date", "list_type", "period", "thscode", "ticker", "name",
        "rank", "heat", "rank_change", "rank_trend",
    ),
    "hot_rank_trend": ("date", "thscode", "ticker", "rank"),
    "dragon_tiger": ("trade_date", "board_type", *DRAGON_TIGER_STOCK_COLUMNS),
    "dragon_tiger_hot_money": (
        "trade_date", "hot_money_name", "buying", *DRAGON_TIGER_HOT_MONEY_COLUMNS,
    ),
    "anomaly": ("date", "tag_name", "thscode", "stock_name", "analysis_content", "keywords"),
}

#: 板块指数历史的两套列编码与落盘列名的映射
_BAR_RENAME = {
    "date_ms": "date",
    "open_price": "open",
    "high_price": "high",
    "low_price": "low",
    "close_price": "close",
    "volume": "volume",
    "turnover": "amount",
}

DATASETS = ("limit_pool", "board", "hot_list", "dragon_tiger", "anomaly")

#: 回补时用的涨停池列表与对应的客户端方法名
_POOL_METHODS = {
    "up": "limit_up_pool",
    "down": "limit_down_pool",
    "break": "limit_break_pool",
}

#: ★ 三个池的**实测**历史深度不一样（2026-10-01 逐日期二分确认）：
#   · up：从 ≈2020-07-13 起有深历史；
#   · down / break：**只支持一年内**，更早的日期服务端返回
#     `code=1002 Invalid parameter format`（不是 1003，极具迷惑性）。
#:   所以各池的回补起点必须分开 —— 不要"统一起点"（会白打一年量的 1002）。
_POOL_STARTS = {
    "up": LIMIT_POOL_START,
    "down": None,   # None = 运行时取 _one_year_ago()
    "break": None,
}


def _pool_start(pool: str) -> str:
    """某池的回补起点（down/break 的一年窗口按运行日滚动）。"""
    return _POOL_STARTS[pool] or _one_year_ago()


# ======================================================================
# 状态文件（原子写 + 锁）
# ======================================================================
_state_lock = threading.Lock()


def load_state() -> dict[str, Any]:
    if not BACKFILL_SPECIAL_STATE_FILE.exists():
        return {"version": 1, "started_at": None}
    try:
        return json.loads(BACKFILL_SPECIAL_STATE_FILE.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover —— 坏文件不该让整轮跑不动
        logger.warning(f"状态文件解析失败，按空状态继续：{exc}")
        return {"version": 1, "started_at": None}


def save_state(state: dict[str, Any]) -> None:
    """原子写状态文件（照抄 backfill_fundamental 的做法）。"""
    with _state_lock:
        BACKFILL_SPECIAL_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = BACKFILL_SPECIAL_STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        tmp.replace(BACKFILL_SPECIAL_STATE_FILE)


def _bucket(state: dict[str, Any], *path: str) -> dict[str, Any]:
    """取（必要时创建）状态文件的嵌套桶。"""
    node = state
    for key in path:
        node = node.setdefault(key, {})
    return node


def _latest_done(recs: dict[str, Any]) -> str | None:
    """该桶里最后一个**已完成**的键（ok 或 empty 都算完成）。"""
    return max(
        (k for k, v in recs.items() if v.get("status") in ("ok", "empty")), default=None
    )


def _needs_fetch(
    recs: dict[str, Any],
    key: str,
    *,
    force: bool,
    retry_failed: bool,
    incremental: bool = False,
    after: str | None = None,
    retry_partial: bool = False,
) -> bool:
    """某一项是否还需要抓取。

    统一了四个数据集的待办判定，避免每个 runner 各写一遍（写过一遍就够乱了）：
      · `force`                  → 一律重来
      · 从没抓过                 → 要抓
      · `retry_failed` + failed  → 要重试
      · `retry_partial` + partial→ 要重来（**取不全的必须重来，不能当成功**）
      · `incremental` + 新于游标 → 要补
    """
    if force or key not in recs:
        return True
    status = recs[key].get("status")
    if retry_failed and status == "failed":
        return True
    if retry_partial and status == "partial":
        return True
    return bool(incremental and after and key > after)


# ======================================================================
# 每通道独立 client（限速 = 总配额 / 通道数）
# ======================================================================
def make_client(workers: int) -> FuyaoClient:
    """建一个**独立**的 fuyao client，限速按聚合配额切分。

    ★ 为什么要切：`max_calls_per_minute` 只是客户端自限，服务端按 API Key 计。
      4 个通道各 60/min = 聚合 240/min → 必然触发 4001。
      而且熔断器是 per-client 的（`_line_group` 恒 "fuyao"），
      一个通道被熔断，其它通道并不知道 → 只能靠源头把速率压住。
    """
    client = FuyaoClient()
    per_channel = max(1, 60 // max(1, workers))
    client._max_calls_per_minute = per_channel
    client._min_interval = 0.2
    return client


# ======================================================================
# 公共工具
# ======================================================================
def _today() -> str:
    return date.today().strftime("%Y-%m-%d")


def _one_year_ago() -> str:
    return (date.today() - timedelta(days=ONE_YEAR_DAYS)).strftime("%Y-%m-%d")


def _trading_days(start: str, end: str) -> list[str]:
    from app.services.calendar_service import get_calendar  # noqa: PLC0415

    return list(get_calendar().trading_days(start, end))


def normalize(df: pd.DataFrame | None, key: str) -> pd.DataFrame:
    """把源端 DataFrame 规整到固定列集（含空表 → 带列的空表）。

    ⚠️ 这是"同一 glob 内 schema 一致"的唯一防御点 —— 源端列集变化时
       只会多出/少掉列，不会污染已落盘文件。
    """
    cols = EXPECTED[key]
    if df is None or not len(df):
        return pd.DataFrame({c: pd.Series(dtype="object") for c in cols})
    out = df.copy()
    for c in cols:
        if c not in out.columns:
            out[c] = pd.NA
    return out[list(cols)]


def _fetch_paged(
    client: FuyaoClient, method_name: str, day: str
) -> tuple[pd.DataFrame, int, bool]:
    """翻页取全一个池的某一天。

    Returns:
        (df, pages, incomplete) —— `incomplete=True` 表示撞到了页数上限，
        结果可能不完整，调用方**必须**把该日记成 partial 而不是 ok。
    """
    fn: Callable[..., pd.DataFrame] = getattr(client, method_name)
    frames: list[pd.DataFrame] = []
    page = 1
    while page <= MAX_PAGES_PER_DAY:
        part = fn(date_ms=day, page=page, size=FUYAO_PAGE_SIZE)
        if part is None or part.empty:
            break
        frames.append(part)
        if len(part) < FUYAO_PAGE_SIZE:
            return pd.concat(frames, ignore_index=True), page, False
        page += 1
    if not frames:
        return pd.DataFrame(), page - 1, False
    logger.warning(
        f"{method_name} {day} 翻页达到上限 {MAX_PAGES_PER_DAY} 页"
        f"（{MAX_PAGES_PER_DAY * FUYAO_PAGE_SIZE} 行），结果可能不完整"
    )
    return pd.concat(frames, ignore_index=True), MAX_PAGES_PER_DAY, True


def _run_parallel(
    items: list[Any],
    handler: Callable[[Any, FuyaoClient], dict[str, Any]],
    *,
    workers: int,
    state: dict[str, Any],
    label: str,
    on_record: Callable[[Any, dict[str, Any]], None],
) -> dict[str, int]:
    """把一个待办列表切给 N 个线程跑；每 SAVE_EVERY 条落一次状态。

    handler 返回单条的记录 dict（`{"status": ...}`）；抛
    `DataSourceRateLimited` 时本通道等待熔断结束后重试一次。
    """
    stats = {"ok": 0, "empty": 0, "partial": 0, "failed": 0}
    if not items:
        return stats

    stop = threading.Event()
    done = {"n": 0}
    t_start = time.time()
    idx_lock = threading.Lock()
    cursor = {"i": 0}

    def worker() -> None:
        client = make_client(workers)
        while not stop.is_set():
            with idx_lock:
                if cursor["i"] >= len(items):
                    return
                item = items[cursor["i"]]
                cursor["i"] += 1
            try:
                rec = handler(item, client)
            except DataSourceRateLimited:
                # 撞限频：等恢复再试一次（批量任务的正确动作是等待而非逃）
                client.wait_if_limited(max_wait=600.0)
                try:
                    rec = handler(item, client)
                except Exception as exc:
                    rec = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"[:200]}
            except Exception as exc:
                rec = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"[:200]}

            with idx_lock:
                stats[rec.get("status", "failed")] = stats.get(rec.get("status", "failed"), 0) + 1
                on_record(item, rec)
                done["n"] += 1
                n = done["n"]
                if n % SAVE_EVERY == 0:
                    save_state(state)
                if n % PROGRESS_EVERY == 0:
                    elapsed = max(time.time() - t_start, 1e-6)
                    speed = n / elapsed
                    eta = (len(items) - n) / speed if speed else 0
                    logger.info(
                        f"[{label}] {n}/{len(items)}｜{speed:.1f} 项/秒｜"
                        f"已用 {elapsed / 60:.1f} 分｜ETA {eta / 60:.1f} 分"
                    )

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(max(1, workers))]
    for th in threads:
        th.start()
    try:
        while any(th.is_alive() for th in threads):
            for th in threads:
                th.join(timeout=0.5)
    except KeyboardInterrupt:
        logger.warning(f"[{label}] 收到中断，保存断点后退出…")
        stop.set()
        for th in threads:
            th.join(timeout=5)
        save_state(state)
        raise
    save_state(state)
    return stats


# ======================================================================
# 各数据集：待办列表 + 单条处理
# ======================================================================
def _limit_pool_todo(state: dict[str, Any], *, force: bool, limit: int | None,
                     incremental: bool, retry_failed: bool) -> list[tuple[str, str]]:
    todo: list[tuple[str, str]] = []
    for pool in _POOL_METHODS:
        days = _trading_days(_pool_start(pool), _today())
        recs = _bucket(state, "limit_pool", pool)
        after = _latest_done(recs)
        for day in days:
            if _needs_fetch(
                recs, day, force=force, retry_failed=retry_failed,
                incremental=incremental, after=after, retry_partial=True,
            ):
                todo.append((pool, day))
    return todo[:limit] if limit else todo


def run_limit_pool(state: dict[str, Any], *, workers: int, force: bool,
                   limit: int | None, incremental: bool, retry_failed: bool) -> dict[str, int]:
    todo = _limit_pool_todo(
        state, force=force, limit=limit, incremental=incremental, retry_failed=retry_failed
    )
    logger.info(f"[limit_pool] 待办 {len(todo)} 项（池×交易日）")

    def handler(item: tuple[str, str], client: FuyaoClient) -> dict[str, Any]:
        pool, day = item
        raw, pages, incomplete = _fetch_paged(client, _POOL_METHODS[pool], day)
        df = normalize(raw, pool)
        if not len(df):
            return {"status": "empty", "rows": 0}
        df = df.copy()
        df["date"] = pd.Timestamp(day)
        data_store.save_limit_pool(df, pool)
        return {
            "status": "partial" if incomplete else "ok",
            "rows": len(df),
            "pages": pages,
        }

    def record(item: tuple[str, str], rec: dict[str, Any]) -> None:
        pool, day = item
        _bucket(state, "limit_pool", pool)[day] = rec

    return _run_parallel(
        todo, handler, workers=workers, state=state, label="limit_pool", on_record=record
    )


def _board_list(client: FuyaoClient) -> pd.DataFrame:
    """拉全部板块清单（行业 + 概念）并落盘快照。"""
    frames = []
    for tag, api_tag in (("industry", "industry"), ("concept", "cn_concept")):
        df = client.index_list(api_tag)
        if df is None or not len(df):
            continue
        part = df.copy()
        part["tag"] = tag
        frames.append(part)
    if not frames:
        return pd.DataFrame(columns=EXPECTED["board_catalog"])
    out = pd.concat(frames, ignore_index=True)
    out["snapshot_date"] = pd.Timestamp(_today())
    return normalize(out, "board_catalog")


def _board_todo(state: dict[str, Any], catalog: pd.DataFrame, *, force: bool,
                limit: int | None, incremental: bool, retry_failed: bool) -> list[tuple[str, str]]:
    todo: list[tuple[str, str]] = []
    for _, row in catalog.iterrows():
        tag, code = str(row["tag"]), str(row["thscode"])
        recs = _bucket(state, "board", tag)
        # 板块历史是"整段重拉"（接口给的是区间序列），所以增量=重拉该板块
        if _needs_fetch(
            recs, code, force=force, retry_failed=retry_failed, incremental=incremental,
        ):
            todo.append((tag, code))
    return todo[:limit] if limit else todo


def run_board(state: dict[str, Any], *, workers: int, force: bool, limit: int | None,
              incremental: bool, retry_failed: bool) -> dict[str, int]:
    boot = make_client(workers)
    catalog = _board_list(boot)
    if not len(catalog):
        logger.warning("[board] 板块清单为空，跳过")
        return {"ok": 0, "empty": 0, "partial": 0, "failed": 0}
    data_store.save_board_catalog(catalog)
    logger.info(f"[board] 板块清单 {len(catalog)} 个（清单快照已落盘）")

    todo = _board_todo(
        state, catalog, force=force, limit=limit,
        incremental=incremental, retry_failed=retry_failed,
    )
    logger.info(f"[board] 待办 {len(todo)} 个板块")
    end = _today()

    def handler(item: tuple[str, str], client: FuyaoClient) -> dict[str, Any]:
        tag, code = item
        # ★ 逐个候选起点试到非空为止（见 BOARD_START_CANDIDATES 的说明：
        #   起点早于该板块首日时接口**静默返回 0 行**，不能一次定死）
        raw = None
        used_start = ""
        for start in BOARD_START_CANDIDATES:
            raw = client.index_historical(code, start, end)
            if raw is not None and len(raw):
                used_start = start
                break
        if raw is None or not len(raw):
            return {"status": "empty", "rows": 0}
        df = raw.rename(columns=_BAR_RENAME)
        df["thscode"] = code
        df["date"] = pd.to_datetime(df["date"], unit="ms", errors="coerce")
        df = df.dropna(subset=["date"])
        df = normalize(df, "board")
        if not len(df):
            return {"status": "empty", "rows": 0}
        data_store.save_board_history(df, tag, code)
        return {
            "status": "ok",
            "rows": len(df),
            "start_used": used_start,
            "first": df["date"].min().strftime("%Y-%m-%d"),
            "last": df["date"].max().strftime("%Y-%m-%d"),
        }

    def record(item: tuple[str, str], rec: dict[str, Any]) -> None:
        tag, code = item
        _bucket(state, "board", tag)[code] = rec

    return _run_parallel(
        todo, handler, workers=workers, state=state, label="board", on_record=record
    )


def _hot_history_todo(state: dict[str, Any], *, force: bool, limit: int | None,
                      incremental: bool, retry_failed: bool) -> list[str]:
    days = pd.date_range(_one_year_ago(), _today(), freq="D").strftime("%Y-%m-%d").tolist()
    recs = _bucket(state, "hot_list", "history")
    after = _latest_done(recs)
    todo = [
        d for d in days
        if _needs_fetch(
            recs, d, force=force, retry_failed=retry_failed,
            incremental=incremental, after=after,
        )
    ]
    return todo[:limit] if limit else todo


def run_hot_list(state: dict[str, Any], *, workers: int, force: bool, limit: int | None,
                 incremental: bool, retry_failed: bool) -> dict[str, int]:
    todo = _hot_history_todo(
        state, force=force, limit=limit, incremental=incremental, retry_failed=retry_failed
    )
    logger.info(f"[hot_list] 历史榜待办 {len(todo)} 天")

    def handler(day: str, client: FuyaoClient) -> dict[str, Any]:
        raw = client.hot_stock_list_history(day)
        df = normalize(raw, "hot_history")
        if not len(df):
            return {"status": "empty", "rows": 0}
        df = df.copy()
        df["date"] = pd.Timestamp(day)
        data_store.save_hot_list_history(df, day)
        return {"status": "ok", "rows": len(df)}

    def record(day: str, rec: dict[str, Any]) -> None:
        _bucket(state, "hot_list", "history")[day] = rec

    stats = _run_parallel(
        todo, handler, workers=workers, state=state, label="hot_list.history", on_record=record
    )

    # ★ 排名走势由历史榜**本地透视**生成（省约 2000 次接口调用）
    derived = derive_rank_trend(state)
    logger.info(f"[hot_list] 由历史榜派生排名走势：{derived} 个标的")
    return stats


def derive_rank_trend(state: dict[str, Any]) -> int:
    """把 `hot_list/history/*.parquet` 按 thscode 透视成 `rank_trend/*.parquet`。"""
    hist = data_store.read_hot_list_history()
    if not len(hist):
        return 0
    hist = hist[["date", "thscode", "ticker", "rank"]].dropna(subset=["thscode"])
    n = 0
    for code, grp in hist.groupby("thscode"):
        df = normalize(grp.sort_values("date"), "hot_rank_trend")
        data_store.save_hot_rank_trend(df, str(code))
        _bucket(state, "hot_list", "rank_trend")[str(code)] = {
            "status": "ok", "rows": len(df),
            "last": pd.Timestamp(df["date"].max()).strftime("%Y-%m-%d"),
        }
        n += 1
    save_state(state)
    return n


def _dragon_tiger_todo(state: dict[str, Any], *, force: bool, limit: int | None,
                       incremental: bool, retry_failed: bool) -> list[str]:
    days = _trading_days(_one_year_ago(), _today())
    recs = _bucket(state, "dragon_tiger")
    after = _latest_done(recs)
    todo = [
        d for d in days
        if _needs_fetch(
            recs, d, force=force, retry_failed=retry_failed,
            incremental=incremental, after=after,
        )
    ]
    return todo[:limit] if limit else todo


def run_dragon_tiger(state: dict[str, Any], *, workers: int, force: bool,
                     limit: int | None, incremental: bool, retry_failed: bool) -> dict[str, int]:
    todo = _dragon_tiger_todo(
        state, force=force, limit=limit, incremental=incremental, retry_failed=retry_failed
    )
    logger.info(f"[dragon_tiger] 待办 {len(todo)} 个交易日")

    def handler(day: str, client: FuyaoClient) -> dict[str, Any]:
        stock = client.dragon_tiger_list(day, "all")
        hot = client.dragon_tiger_hot_money(day, "hot_money")
        stock = normalize(stock, "dragon_tiger")
        hot = normalize(hot, "dragon_tiger_hot_money")
        if len(stock):
            data_store.save_dragon_tiger(stock, day)
        if len(hot):
            data_store.save_dragon_tiger_hot_money(hot, day)
        if not len(stock) and not len(hot):
            return {"status": "empty", "rows": 0, "hot_money_rows": 0}
        return {
            "status": "ok", "rows": len(stock), "hot_money_rows": len(hot),
        }

    def record(day: str, rec: dict[str, Any]) -> None:
        _bucket(state, "dragon_tiger")[day] = rec

    return _run_parallel(
        todo, handler, workers=workers, state=state, label="dragon_tiger", on_record=record
    )


def run_anomaly(state: dict[str, Any], *, workers: int, force: bool, limit: int | None,
                incremental: bool, retry_failed: bool) -> dict[str, int]:
    """个股异动**只有当日**（无日期参数）→ 只能从今天开始按日累积。"""
    today = _today()
    recs = _bucket(state, "anomaly")
    if not force and recs.get(today, {}).get("status") in ("ok", "empty"):
        logger.info(f"[anomaly] {today} 已抓过，跳过")
        return {"ok": 0, "empty": 0, "partial": 0, "failed": 0}

    def handler(day: str, client: FuyaoClient) -> dict[str, Any]:
        raw = client.anomaly_analysis_list("")
        df = normalize(raw, "anomaly")
        if not len(df):
            return {"status": "empty", "rows": 0}
        df = df.copy()
        if "keyword_list" in df.columns:
            df["keywords"] = df["keyword_list"].map(
                lambda v: "|".join(map(str, v)) if isinstance(v, (list, tuple)) else (v or "")
            )
        df["date"] = pd.Timestamp(day)
        df = normalize(df, "anomaly")
        data_store.save_anomaly(df, day)
        return {"status": "ok", "rows": len(df)}

    def record(day: str, rec: dict[str, Any]) -> None:
        _bucket(state, "anomaly")[day] = rec

    return _run_parallel(
        [today], handler, workers=1, state=state, label="anomaly", on_record=record
    )


# ======================================================================
# --status
# ======================================================================
def print_status(state: dict[str, Any]) -> None:
    print("\n" + "=" * 66)
    print("  同花顺特色数据 · 回补进度")
    print("=" * 66)
    total_calls = 0
    for pool in _POOL_METHODS:
        recs = _bucket(state, "limit_pool", pool)
        counts = _count(recs)
        total_calls += sum(counts.values())
        days = [d for d, r in recs.items() if r.get("status") in ("ok", "empty")]
        span = f"{min(days)} ~ {max(days)}" if days else "—"
        print(f"  limit_pool.{pool:5s} ok={counts['ok']:<5d} empty={counts['empty']:<5d} "
              f"partial={counts['partial']:<3d} failed={counts['failed']:<3d} | {span}")
    for tag in ("industry", "concept"):
        recs = _bucket(state, "board", tag)
        counts = _count(recs)
        total_calls += sum(counts.values())
        print(f"  board.{tag:9s} ok={counts['ok']:<5d} empty={counts['empty']:<5d} "
              f"failed={counts['failed']:<3d} | 共 {len(recs)} 个板块")
    h = _count(_bucket(state, "hot_list", "history"))
    print(f"  hot_list.history ok={h['ok']:<5d} empty={h['empty']:<5d} failed={h['failed']:<3d}")
    rt = _bucket(state, "hot_list", "rank_trend")
    print(f"  hot_list.rank_trend 已派生 {len(rt)} 个标的")
    dt = _count(_bucket(state, "dragon_tiger"))
    print(f"  dragon_tiger     ok={dt['ok']:<5d} empty={dt['empty']:<5d} failed={dt['failed']:<3d}")
    an = _count(_bucket(state, "anomaly"))
    print(f"  anomaly          ok={an['ok']:<5d} empty={an['empty']:<5d} failed={an['failed']:<3d}")
    print(f"\n  状态文件: {BACKFILL_SPECIAL_STATE_FILE}")
    print("=" * 66 + "\n")


def _count(recs: dict[str, Any]) -> dict[str, int]:
    out = {"ok": 0, "empty": 0, "partial": 0, "failed": 0}
    for r in recs.values():
        s = r.get("status", "failed")
        out[s] = out.get(s, 0) + 1
    return out


# ======================================================================
# --verify：抽样与源端逐行比对
# ======================================================================
def run_verify(state: dict[str, Any], n: int) -> int:
    """随机抽 N 个已完成的涨跌停池日期，重拉源端逐行比对。

    Returns:
        比对不一致的项数（0 = 全过）。同时打印明细。
    """
    client = make_client(2)
    recs = _bucket(state, "limit_pool", "up")
    done = [d for d, r in recs.items() if r.get("status") == "ok"]
    if not done:
        print("  （没有已完成的涨停池数据可比对，先跑一次回补）")
        return 0
    sample = random.sample(done, min(n, len(done)))
    bad = 0
    print(f"\n  抽样比对 {len(sample)} 天（涨停池 up）：")
    for day in sorted(sample):
        local = data_store.read_limit_pool("up", day, day)
        remote, _, _ = _fetch_paged(client, _POOL_METHODS["up"], day)
        remote = normalize(remote, "up")
        lset, rset = set(local["thscode"].astype(str)), set(remote["thscode"].astype(str))
        same = lset == rset
        if not same:
            bad += 1
        print(
            f"    {day}: 本地 {len(local):>4d} 行 / 源端 {len(remote):>4d} 行"
            f"  {'✓' if same else '✗ 代码集合不一致'}"
        )
        if not same:
            print(f"        本地多: {sorted(lset - rset)[:5]}  源端多: {sorted(rset - lset)[:5]}")
    print(f"  ==> {len(sample) - bad}/{len(sample)} 一致\n")
    return bad


# ======================================================================
# main
# ======================================================================
_RUNNERS: dict[str, Callable[..., dict[str, int]]] = {
    "limit_pool": run_limit_pool,
    "board": run_board,
    "hot_list": run_hot_list,
    "dragon_tiger": run_dragon_tiger,
    "anomaly": run_anomaly,
}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="同花顺特色数据回补（涨跌停池 / 板块指数历史 / 热榜 / 龙虎榜 / 异动）"
    )
    parser.add_argument("--status", action="store_true", help="只看进度，不联网")
    parser.add_argument("--dataset", default="all",
                        help=f"只跑指定数据集：all|{'|'.join(DATASETS)}")
    parser.add_argument("--incremental", action="store_true", help="每日增量（补到最近交易日）")
    parser.add_argument("--retry-failed", action="store_true", help="只重试失败的项")
    parser.add_argument("--force", action="store_true", help="忽略进度全量重拉")
    parser.add_argument("--limit", type=int, default=None, help="每个数据集只处理前 N 项（冒烟）")
    parser.add_argument("--workers", type=int, default=4, help="并行通道数（聚合限速见模块说明）")
    parser.add_argument("--verify", type=int, default=0, help="抽样 N 项与源端逐行比对")
    args = parser.parse_args()

    setup_logging()
    ensure_dirs()
    if not db.is_initialized():
        print("meta.db 尚未初始化，请先执行：python -m tools.init_db")
        return 2

    if args.dataset != "all" and args.dataset not in _RUNNERS:
        print(f"未知数据集：{args.dataset}（可选 all|{'|'.join(DATASETS)}）")
        return 2

    state = load_state()
    state.setdefault("version", 1)
    state.setdefault("started_at", time.strftime("%Y-%m-%dT%H:%M:%S"))

    if args.status:
        print_status(state)
        return 0
    if args.verify:
        return 1 if run_verify(state, args.verify) else 0

    targets = DATASETS if args.dataset == "all" else (args.dataset,)
    print(f"\n开始回补：{', '.join(targets)}｜通道 {args.workers}｜"
          f"增量={args.incremental} 强制={args.force} 上限={args.limit}\n")

    summary: dict[str, dict[str, int]] = {}
    t0 = time.time()
    for name in targets:
        try:
            summary[name] = _RUNNERS[name](
                state,
                workers=args.workers,
                force=args.force,
                limit=args.limit,
                incremental=args.incremental,
                retry_failed=args.retry_failed,
            )
        except KeyboardInterrupt:
            print("\n已中断，断点已保存。重跑同一条命令即可续传。")
            return 130
        except AppError as exc:
            logger.error(f"[{name}] 失败：{exc.message}")
            summary[name] = {"failed": 1}

    elapsed = time.time() - t0
    print("\n" + "=" * 66)
    print(f"  回补完成｜用时 {elapsed / 60:.1f} 分钟")
    print("=" * 66)
    for name, st in summary.items():
        print(f"  {name:14s} ok={st.get('ok', 0):<6d} empty={st.get('empty', 0):<6d} "
              f"partial={st.get('partial', 0):<3d} failed={st.get('failed', 0):<3d}")
    print()
    print_status(state)
    return 1 if any(st.get("failed") for st in summary.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
