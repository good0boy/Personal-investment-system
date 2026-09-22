"""本地数据仓库 —— ★ 全项目唯一允许读写 Parquet / DuckDB 的模块。

纪律（见 技术架构.md 第 4 章）：
    业务层**不得**直接 `pd.read_parquet(...)` 或自己建 DuckDB 连接，
    一律通过本模块。这样日后换存储格式、加分区裁剪、加降采样只改一个文件。

存储布局（见 技术架构.md 5.2）：
    data/raw/quote_daily/{symbol}.parquet         个股日线（后复权）
    data/raw/quote_index/{symbol}.parquet         指数日线
    data/raw/universe/{date}.parquet              每日股票池快照
    data/factor/daily/{name}.parquet              因子
    data/backtest/{strategy}/{run_id}/*.parquet   回测结果（由策略 CLI 写）
    data/meta/trade_calendar.parquet              交易日历

★ 时间锁（READ_UNTIL）：
    回测/模拟盘绝不能读到"未来"数据。所有读取都会经过 `_apply_read_until()`，
    若请求区间超出上界则抛出 ReadUntilViolation。
    **读取上界 = min(显式时间锁, 今天)** —— 即使调用方忘了设锁，也越不过今天。
    交易日历是例外：它是日程表，允许覆盖到未来（见 `read_trade_calendar`）。
"""
from __future__ import annotations

import json
import math
import re
import threading
from bisect import bisect_left
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from app.core.errors import AppError, DataNotReadyError, ReadUntilViolation, StorageError
from app.core.logging import logger
from app.core.paths import (
    BACKTEST_DIR,
    CACHE_DIR,
    DATA_DIR,
    FACTOR_DIR,
    FUNDAMENTAL_DIR,
    META_DB,
    META_DIR,
    PAPER_DIR,
    QUOTE_DAILY_DIR,
    QUOTE_INDEX_DIR,
    SWEEP_DIR,
    TRADE_CALENDAR_PARQUET,
    UNIVERSE_DIR,
)

_PARQUET_COMPRESSION = "zstd"

#: 安全的文件名（symbol -> 文件名）
_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._\-]")

# ======================================================================
# 时间锁
# ======================================================================
_read_until_lock = threading.Lock()
_READ_UNTIL: str | None = None


def set_read_until(value: str | None) -> None:
    """设置全局数据时间锁（YYYY-MM-DD）。None 表示不限制。

    回测启动时设置为回测结束日；模拟盘设置为 today。
    """
    global _READ_UNTIL
    with _read_until_lock:
        _READ_UNTIL = value
    logger.debug(f"数据时间锁已设置为 {value or '不限制'}")


def get_read_until() -> str | None:
    with _read_until_lock:
        return _READ_UNTIL


@contextmanager
def read_until(value: str | None) -> Iterator[None]:
    """临时设置时间锁，退出后恢复原值。

    用法：
        with read_until("2024-12-31"):
            df = read_quotes(...)   # 超出 2024-12-31 的数据会被拒绝
    """
    previous = get_read_until()
    set_read_until(value)
    try:
        yield
    finally:
        set_read_until(previous)


def _upper_bound() -> tuple[str, str]:
    """读取上界 = min(显式时间锁, 今天)。

    ★ 为什么在时间锁之外还要卡"今天"（2026-09-18 新增）：
      交易日历的读取区间被拉到未来（见 `calendar_service.CALENDAR_END`，
      实测数据源给到 **2026-12-31**），日历里从此**存在未来日期**。
      若读取上界只依赖"调用方记得设锁"，
      一个忘了设锁的回测/仿真就能读到尚未发生的数据而**不报任何错** ——
      这正是本模块最怕的那类静默失效。
      今天之后的数据在物理上根本不存在，任何读取都没有正当理由越过它，
      所以把它做成兜底上界：越界一律 ReadUntilViolation，让 bug 大声暴露。

    Returns:
        (today, 显式锁或空串) —— today 一定是上界的一部分。
    """
    today = date.today().strftime("%Y-%m-%d")
    lock = get_read_until()
    if not lock:
        return today, ""
    return today, pd.Timestamp(lock).strftime("%Y-%m-%d")


def _apply_read_until(
    start: str | None, end: str | None
) -> tuple[str | None, str | None]:
    """校验并按时间锁截断/拒绝查询区间。

    ★ 语义（三档，必须严格区分）：

    ==================== ============================== ==========================
    调用形态              行为                            理由
    ==================== ============================== ==========================
    end 显式为 None       收敛为【上界】（隐式截断）      "取全部可用数据"在锁/今天
                                                          生效时的正确含义
    end 显式且 > 上界     **抛 ReadUntilViolation**       调用方在明确索要未来数据，
                                                          这是 bug，必须大声暴露
    end 显式且 <= 上界    原样通过                        —
    ==================== ============================== ==========================
    其中 **上界 = min(显式时间锁, 今天)**。

    ⚠️ **所有对外读取入口都必须无条件调用本函数**，并把返回的 (start, end)
       用于后续查询。历史上正是因为调用点写成 `if end_date: _apply_read_until(...)`，
       导致 `end_date=None` 时整条校验被跳过，实测可读到锁之后 6 个月的数据。

    ⚠️ 交易日历不在本约束内：它是"日程表"而非行情（`read_trade_calendar` 不过锁），
       允许覆盖到未来 —— 否则回测在样本最后一天算 T+1 成交日就会崩。

    Returns:
        (start, end) —— 已按上界收敛后的区间。**调用方必须使用返回值。**
    """
    today_s, lock_s = _upper_bound()
    bound_s = min(today_s, lock_s) if lock_s else today_s

    # --- 情况一：未指定上限 → 收敛为上界，绝不返回上界之后的数据 ---
    if end is None:
        return start, bound_s

    end_s = pd.Timestamp(end).strftime("%Y-%m-%d")

    # --- 情况二：显式越界 → 拒绝（先判"今天"这个硬上界，再判时间锁）---
    if end_s > bound_s:
        if end_s > today_s:
            reason = (
                f"超出今天 {today_s} —— 回测/仿真不得使用未来日期"
                f"（未来行情在物理上还不存在，读到它等于引入未来函数）"
            )
        else:
            reason = f"超出本轮数据时间锁 {lock_s}"
        raise ReadUntilViolation(
            f"请求结束日期 {end_s}：{reason}。",
            detail={
                "requested_end": end_s,
                "read_until": lock_s or None,
                "upper_bound": bound_s,
            },
        )
    return start, end_s


def _guard_day(day: str, *, what: str) -> None:
    """对"按单日读取"的派生数据做上界检查。

    股票池快照是从行情派生的（"某日哪些股票可交易"隐含了当时是否停牌/ST），
    因此同样属于未来数据，必须过上界。
    """
    today_s, lock_s = _upper_bound()
    bound_s = min(today_s, lock_s) if lock_s else today_s
    day_s = pd.Timestamp(day).strftime("%Y-%m-%d")
    if day_s > bound_s:
        if day_s > today_s:
            reason = f"超出今天 {today_s}（回测/仿真不得使用未来日期）"
        else:
            reason = f"超出本轮数据时间锁 {lock_s}"
        raise ReadUntilViolation(
            f"请求日期 {day_s}：{reason}（{what}）。",
            detail={"requested_day": day_s, "read_until": lock_s or None, "what": what},
        )


# ======================================================================
# 路径工具
# ======================================================================
def _safe_name(symbol: str) -> str:
    return _UNSAFE_RE.sub("_", str(symbol).strip())


def _quote_path(symbol: str, is_index: bool = False) -> Path:
    base = QUOTE_INDEX_DIR if is_index else QUOTE_DAILY_DIR
    return base / f"{_safe_name(symbol)}.parquet"


def _glob_pattern(base: Path) -> str:
    """转成 DuckDB 可用的 POSIX 风格 glob。"""
    return str(base / "*.parquet").replace("\\", "/")


def quote_glob(*, is_index: bool = False) -> str:
    """行情 Parquet 文件集的 glob（**唯一实现**）。

    对外公开的理由：`factors/analyze.py` 等旁路工具要直接对行情文件集跑聚合 SQL，
    各自拼一遍 `"data/raw/quote_daily/*.parquet"` 在换目录/换 DATA_ROOT 时会静默失效。
    """
    base = QUOTE_INDEX_DIR if is_index else QUOTE_DAILY_DIR
    return _glob_pattern(base)


# ======================================================================
# DuckDB 连接
# ======================================================================
_duck_lock = threading.Lock()
_duck_conn: duckdb.DuckDBPyConnection | None = None


def get_duck() -> duckdb.DuckDBPyConnection:
    """获取 DuckDB 连接（单例 + 互斥锁，DuckDB 连接非线程安全）。"""
    global _duck_conn
    if _duck_conn is None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _duck_conn = duckdb.connect(database=":memory:")
        _duck_conn.execute("SET threads TO 4")
        _duck_conn.execute("PRAGMA enable_object_cache")
    return _duck_conn


def close_duck() -> None:
    global _duck_conn
    if _duck_conn is not None:
        try:
            _duck_conn.close()
        finally:
            _duck_conn = None


def query_duck(sql: str, params: Sequence[Any] | None = None) -> pd.DataFrame:
    """执行 DuckDB SQL（只读用途）。"""
    try:
        with _duck_lock:
            conn = get_duck()
            return conn.execute(sql, params or []).df()
    except duckdb.Error as exc:
        raise StorageError(f"DuckDB 查询失败：{exc}", detail={"sql": sql[:500]}) from exc


def query_parquet(glob_pattern: str, sql: str, params: Sequence[Any] | None = None) -> pd.DataFrame:
    """对 Parquet 文件集跑 SQL，`t` 为表别名。

    用法：
        query_parquet("data/raw/quote_daily/*.parquet",
                      "SELECT symbol, avg(close) FROM t WHERE date >= ? GROUP BY symbol",
                      ["2024-01-01"])
    """
    pattern = glob_pattern.replace("\\", "/")
    wrapped = f"WITH t AS (SELECT * FROM read_parquet('{pattern}')) {sql}"
    return query_duck(wrapped, params)


# ======================================================================
# Parquet 读写
# ======================================================================
def write_parquet(
    df: pd.DataFrame,
    path: Path,
    *,
    dedup_keys: Sequence[str] | None = None,
    allow_empty: bool = False,
) -> dict[str, Any]:
    """原子写入 Parquet。

    先用 .tmp 写，再 os.replace，避免中途失败留下半截文件。

    Args:
        allow_empty: 空表也写文件（0 行带列结构）。默认 False 保持旧行为
            （跳过）。**需要区分"真的 0 行"与"没落盘"的消费者必须传 True**
            —— 模拟盘 0 笔交易是正常状态，trades.parquet 缺失会被误读成
            "没跑过"（开发进度 §2.4 设计要点 8 的同类问题）。
    """
    if df is None or (df.empty and not allow_empty):
        return {"path": str(path), "rows": 0, "written": False}

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")

    out = df.copy()
    if dedup_keys:
        keys = [k for k in dedup_keys if k in out.columns]
        if keys:
            out = out.drop_duplicates(subset=keys, keep="last")

    try:
        out.to_parquet(tmp, engine="pyarrow", compression=_PARQUET_COMPRESSION, index=False)
        tmp.replace(path)
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        raise StorageError(f"写入 Parquet 失败：{path} —— {exc}") from exc

    return {
        "path": str(path),
        "rows": len(out),
        "written": True,
        "size_bytes": path.stat().st_size,
    }


def merge_parquet(df: pd.DataFrame, path: Path, keys: Sequence[str]) -> dict[str, Any]:
    """增量合并写入：读旧文件 → 按 keys 去重 → 写回。"""
    if path.exists():
        try:
            old = pd.read_parquet(path)
            df = pd.concat([old, df], ignore_index=True)
        except Exception as exc:
            logger.warning(f"读取旧 Parquet 失败，将整体覆盖：{path} —— {exc}")
    return write_parquet(df, path, dedup_keys=keys)


# ---------- 行情 ----------
def save_quotes(df: pd.DataFrame, *, is_index: bool = False, merge: bool = True) -> dict[str, Any]:
    """保存行情（按 symbol 分文件）。"""
    if df is None or df.empty:
        return {"written": 0, "files": 0}

    results = []
    group_col = "symbol"
    if group_col not in df.columns:
        raise StorageError("行情 DataFrame 缺少 symbol 列")

    for symbol, g in df.groupby(group_col, sort=False):
        path = _quote_path(str(symbol), is_index=is_index)
        if merge:
            results.append(merge_parquet(g, path, keys=("date",)))
        else:
            results.append(write_parquet(g, path))

    total_rows = sum(r.get("rows", 0) for r in results)
    logger.info(
        f"行情落盘：{len(results)} 个文件，合计 {total_rows} 行"
        f"（{'指数' if is_index else '个股'}）"
    )
    return {"written": total_rows, "files": len(results)}


# ---------- 基本面（tools/backfill_fundamental.py 落盘，因子/P5 读取入口在此扩展）----------
def fundamental_path(dataset: str, symbol: str | None = None) -> Path:
    """基本面数据文件路径。

    逐只类：fundamental/{dataset}/{symbol}.parquet；
    全市场批量类（symbol=None）：fundamental/{dataset}/all.parquet。
    """
    base = FUNDAMENTAL_DIR / _safe_name(dataset)
    return base / ("all.parquet" if symbol is None else f"{_safe_name(symbol)}.parquet")


def save_fundamental(df: pd.DataFrame, dataset: str, *, symbol: str | None = None) -> dict[str, Any]:
    """保存基本面数据。

    逐只类按 [report_date, notice_date] 去重（同一报告期重发公告以新覆盖旧）；
    批量类整体按 [symbol, report_date] 去重后合并进 all.parquet。
    """
    if df is None or df.empty:
        return {"written": 0}
    path = fundamental_path(dataset, symbol)
    keys = ("symbol", "report_date") if symbol is None else ("report_date", "notice_date")
    return merge_parquet(df, path, keys=keys)


#: `notice_date`（公告日）缺失时的保守回退天数。
#: 与 `tools/backfill_fundamental.py` 的落盘约定一致：读不到 NOTICE_DATE 就按
#: **报告期 + 90 天**估计 —— 宁晚不早。宁可少用一点信息，也绝不引入未来函数。
FUNDAMENTAL_NOTICE_FALLBACK_DAYS = 90

#: 基本面数据的三个标识列（任何 dataset 都有，由回补工具统一生成）
FUNDAMENTAL_ID_COLUMNS: tuple[str, ...] = ("symbol", "report_date", "notice_date")

#: 逐只类数据集 = 一个 symbol 一个 parquet；批量类 = 一个 all.parquet
_FUNDAMENTAL_BATCH_DATASETS = ("yjbb", "yjyg", "gdhs", "fhps", "fund_hold")


def fundamental_glob(dataset: str) -> str:
    """某个逐只类数据集的 Parquet 文件集 glob（**唯一实现**）。

    对外公开的理由与 `quote_glob()` 相同：换目录 / 换 `DATA_ROOT` 时，
    各处自己拼一遍 `"data/raw/fundamental/x/*.parquet"` 必然静默失效。
    """
    return _glob_pattern(FUNDAMENTAL_DIR / _safe_name(dataset))


def read_fundamental(
    dataset: str,
    symbols: Sequence[str] | None = None,
    *,
    columns: Sequence[str] | None = None,
) -> pd.DataFrame:
    """读取基本面数据 —— **因子 / 提醒模块的唯一读取入口**。

    ★ 为什么必须走这里（而不是 `pd.read_parquet`）：
      本函数承担**未来函数防线**。财务数据是按**报告期**组织的
      （2024-12-31 的报表、2025-03-29 才公告），若按 `report_date` 对齐，
      回测在 2025-01 就能"看到"要到 2025-03 才公开的业绩 —— 而这**不会报错**，
      只会让因子 IC 虚高、让策略回测凭空多出一段不存在的收益。
      所以这里做两件事：
        1. `notice_date`（公告日）缺失时回退 `report_date + 90 天`（宁晚不早）；
        2. 按 `min(时间锁, 今天)` **截断 notice_date** —— 未公告的财报读不出来。
      调用方再用 `notice_date <= 交易日` 做 as-of 对齐即可（见 factors/impl/fundamental.py）。

    Args:
        dataset: 数据集名，如 `financial_indicator` / `balance_sheet` / `yjbb`
        symbols: 指定标的；None = 全部（走 glob，DuckDB 并行扫描，比逐文件快得多）
        columns: 需要的数据列（标识列 `symbol/report_date/notice_date` 会自动补上）

    Returns:
        长表，含 `symbol` / `report_date` / `notice_date` 三个标识列（均为已解析日期）。
        数据集目录不存在或为空时返回空表（不抛异常 —— "没有数据"由调用方决定语义）。
    """
    ds = _safe_name(dataset)
    if symbols is not None and len(symbols) == 0:
        # 与 read_quotes 同一条纪律：显式空列表 = 没有标的可读，**不可退化成全量**
        return _empty_fundamental_frame(columns)

    want = list(FUNDAMENTAL_ID_COLUMNS)
    for c in columns or ():
        if c not in want:
            want.append(c)

    if symbols is not None and ds not in _FUNDAMENTAL_BATCH_DATASETS:
        # 逐只类且点名了标的 → 只读这几个文件（列裁剪 + 文件裁剪，最快）
        paths = [fundamental_path(ds, s) for s in symbols]
        paths = [p for p in paths if p.exists()]
        if not paths:
            return _empty_fundamental_frame(columns)
        pattern = "[" + ",".join(f"'{p.as_posix()}'" for p in paths) + "]"
    else:
        # 其余情况都走 glob：全量读，或批量类（single all.parquet）
        base_dir = FUNDAMENTAL_DIR / ds
        # ★ 先确认"目录里确实有 parquet 文件"再查。
        #   否则 DuckDB 会抛裸的 `IO Error: No files found that match the pattern ...`
        #   —— 一条把用户指向 SQL 而不是指向"你还没回补数据"的报错
        #   （与 read_quotes 路径上 tools/factors 的同类修正是同一条经验）。
        if not base_dir.exists() or not any(base_dir.glob("*.parquet")):
            return _empty_fundamental_frame(columns)
        pattern = f"'{fundamental_glob(ds)}'"

    # ★ `union_by_name=true`：逐只文件是"列白名单 ∩ 源返回列"，理论上不同标的的
    #   列集可能不同（源接口对某些股票少返一列）。不加这个开关，DuckDB 会在
    #   某个文件缺列时直接报错；加了则按列名合并、缺的补 NULL。
    #   成本几乎为零，收益是"不会因为一只股票的脏数据炸掉整个因子计算"。
    select_cols = ", ".join(f'"{c}"' for c in want)
    sql = (
        f"SELECT {select_cols} FROM read_parquet({pattern}, union_by_name=true)"
    )
    try:
        df = query_duck(sql)
    except StorageError as exc:
        # 把"列名写错"翻译成可执行的提示。列名来自 registry.yaml 的 params.field，
        # 写错一个字母就走到这里 —— 裸 Binder Error 说了等于没说。
        available = _fundamental_columns(ds)
        missing = [c for c in want if c not in available]
        if missing and available:
            raise AppError(
                f"基本面数据集 {ds} 里没有字段 {missing}。"
                f"可用字段（{len(available)} 个）示例：{available[:12]}"
                f"（完整白名单见 tools/backfill_fundamental.py 的 WHITELISTS）"
            ) from exc
        raise
    if df.empty:
        return _empty_fundamental_frame(columns)

    df = df.copy()
    df["symbol"] = df["symbol"].astype(str)
    df["report_date"] = pd.to_datetime(df["report_date"], errors="coerce")
    df["notice_date"] = pd.to_datetime(df["notice_date"], errors="coerce")
    # 公告日缺失 → 报告期 + 90 天（宁晚不早）
    missing_notice = df["notice_date"].isna()
    if missing_notice.any():
        df.loc[missing_notice, "notice_date"] = (
            df.loc[missing_notice, "report_date"]
            + pd.to_timedelta(FUNDAMENTAL_NOTICE_FALLBACK_DAYS, unit="D")
        )
    # 报告期本身就缺失的行没有对齐意义，直接丢掉
    df = df[df["report_date"].notna() & df["notice_date"].notna()]

    # ★ 未来函数防线：按 min(时间锁, 今天) 截断公告日。
    #   注意截断的是 **notice_date**，不是 report_date —— 后者会把
    #   "早已公告的上期报表"也一起误杀。
    today, lock = _upper_bound()
    upper = min(today, lock) if lock else today
    dropped = int((df["notice_date"] > pd.Timestamp(upper)).sum())
    if dropped:
        df = df[df["notice_date"] <= pd.Timestamp(upper)]
        logger.debug(f"基本面 {ds}：按时间锁 {upper} 截断掉 {dropped} 行未公告数据")

    return df.sort_values(["symbol", "notice_date"], kind="stable").reset_index(drop=True)


def _empty_fundamental_frame(columns: Sequence[str] | None) -> pd.DataFrame:
    """空结果的标准形态（保住列，让下游的 `df["x"]` 不炸 KeyError）。"""
    cols = list(FUNDAMENTAL_ID_COLUMNS)
    for c in columns or ():
        if c not in cols:
            cols.append(c)
    return pd.DataFrame({c: pd.Series(dtype="object") for c in cols})


def _fundamental_columns(dataset: str) -> list[str]:
    """某个数据集里实际存在的列名（只读元信息，不读数据）。"""
    ds = _safe_name(dataset)
    base_dir = FUNDAMENTAL_DIR / ds
    if not base_dir.exists() or not any(base_dir.glob("*.parquet")):
        return []
    try:
        df = query_duck(
            f"SELECT * FROM read_parquet('{fundamental_glob(ds)}', union_by_name=true) LIMIT 0"
        )
    except StorageError:
        return []
    return list(df.columns)


def fundamental_dataset_stats(dataset: str) -> dict[str, Any]:
    """某个数据集的规模统计（供能力矩阵 / 诊断用；不读数据列，只读元信息）。"""
    ds = _safe_name(dataset)
    if ds in _FUNDAMENTAL_BATCH_DATASETS:
        p = fundamental_path(ds, None)
        if not p.exists():
            return {"dataset": ds, "files": 0, "rows": 0, "path": ""}
        df = query_duck(
            f"SELECT count(*) AS n FROM read_parquet('{p.as_posix()}')"
        )
        return {"dataset": ds, "files": 1, "rows": int(df["n"].iloc[0]), "path": p.as_posix()}
    base_dir = FUNDAMENTAL_DIR / ds
    if not base_dir.exists():
        return {"dataset": ds, "files": 0, "rows": 0, "path": ""}
    files = list(base_dir.glob("*.parquet"))
    if not files:
        return {"dataset": ds, "files": 0, "rows": 0, "path": base_dir.as_posix()}
    df = query_duck(
        f"SELECT count(*) AS n FROM read_parquet('{fundamental_glob(ds)}', union_by_name=true)"
    )
    return {
        "dataset": ds,
        "files": len(files),
        "rows": int(df["n"].iloc[0]),
        "path": base_dir.as_posix(),
    }


def read_quotes(
    symbols: str | Sequence[str] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    *,
    is_index: bool = False,
    columns: Sequence[str] | None = None,
) -> pd.DataFrame:
    """读取行情。

    Args:
        symbols: 单个或列表；None = 全市场（走 glob，较慢）；
                 **空列表 = 无标的可读，直接返回空**（不可退化成全市场！）
        start_date / end_date: 闭区间，YYYY-MM-DD；end 省略时受时间锁收敛
        columns: 需要的列（列裁剪，强烈建议指定以提速）

    Raises:
        ReadUntilViolation: 显式请求的区间超出全局数据时间锁
    """
    # ★ 时间锁校验：这里是读取入口，必须【无条件】过锁并用返回值覆盖区间。
    #   写成 `if end_date: ...` 是历史 bug —— end_date=None 时校验会被整个跳过。
    start_date, end_date = _apply_read_until(start_date, end_date)

    base = QUOTE_INDEX_DIR if is_index else QUOTE_DAILY_DIR

    select_cols = ", ".join(columns) if columns else "*"
    conditions: list[str] = []
    params: list[Any] = []

    if start_date:
        conditions.append("date >= ?")
        params.append(pd.Timestamp(start_date))
    if end_date:
        conditions.append("date <= ?")
        params.append(pd.Timestamp(end_date))

    if isinstance(symbols, str):
        symbols = [symbols]

    # ★ 显式空列表 → 返回空。历史 bug：空列表落入 else 分支变成"读全市场"，
    #   在候选池为空的日子里会从"今天不该买"变成"今天买一切"。
    if symbols is not None and len(symbols) == 0:
        logger.debug("read_quotes 收到空标的列表，返回空结果（不退化为全市场）")
        return pd.DataFrame(columns=list(columns) if columns else None)

    if symbols is not None:
        # 单文件精确读取（最快路径）
        files = [str(_quote_path(s, is_index=is_index)).replace("\\", "/") for s in symbols]
        existing = [f for f in files if Path(f).exists()]
        if not existing:
            logger.debug(f"请求的行情文件均不存在：{symbols[:3]}...")
            return pd.DataFrame(columns=list(columns) if columns else None)
        file_list = ", ".join(f"'{f}'" for f in existing)
        source = f"read_parquet([{file_list}])"
    else:
        source = f"read_parquet('{_glob_pattern(base)}')"

    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    sql = f"SELECT {select_cols} FROM {source}{where}"

    with _duck_lock:
        try:
            conn = get_duck()
            df = conn.execute(sql, params).df()
        except duckdb.Error as exc:
            raise StorageError(f"读取行情失败：{exc}", detail={"sql": sql[:400]}) from exc

    return df


def load_symbol_history(
    symbol: str,
    end_date: str | None = None,
    *,
    is_index: bool = False,
    columns: Sequence[str] | None = None,
) -> pd.DataFrame:
    """读单只标的的全部历史（截至 end_date，受时间锁约束）。"""
    if end_date:
        _apply_read_until("1900-01-01", end_date)
    return read_quotes(symbol, None, end_date, is_index=is_index, columns=columns)


# ---------- 交易日历 ----------
def save_trade_calendar(df: pd.DataFrame) -> dict[str, Any]:
    """保存交易日历（含非交易日标记）。"""
    return write_parquet(df, TRADE_CALENDAR_PARQUET, dedup_keys=("date",))


def read_trade_calendar(start_date: str | None = None, end_date: str | None = None) -> pd.DataFrame:
    """读交易日历（含非交易日标记）。

    ★ **刻意不过时间锁，也不受"今天"上界约束** —— 这是本模块唯一的读取例外。

    理由：交易日历是**日程表**（交易所提前公布的排期），不是行情数据。
    它必须能看到未来，否则回测在样本最后一个交易日算"T+1 成交日"就会崩：
        next_trading_day("2024-12-31")  # 需要 2025-01-02 存在
    日历本身不含任何价格/成交信息，读到未来日期不会引入未来函数；
    真正的未来函数风险在行情/因子/股票池 —— 那些仍然无条件过上界。
    """
    if not TRADE_CALENDAR_PARQUET.exists():
        raise DataNotReadyError(
            "交易日历尚未初始化，请先执行：python -m tools.sync_trade_calendar"
        )
    df = pd.read_parquet(TRADE_CALENDAR_PARQUET)
    if start_date:
        df = df[df["date"] >= pd.Timestamp(start_date)]
    if end_date:
        df = df[df["date"] <= pd.Timestamp(end_date)]
    return df.reset_index(drop=True)


# ---------- 股票池快照 ----------
def save_universe(day: str, symbols: Iterable[str], meta: dict[str, Any] | None = None) -> dict[str, Any]:
    """保存某日股票池快照。"""
    syms = list(symbols)
    df = pd.DataFrame({"date": [pd.Timestamp(day)] * len(syms), "symbol": syms})
    if meta:
        for k, v in meta.items():
            df[k] = v
    path = UNIVERSE_DIR / f"{_safe_name(day)}.parquet"
    return write_parquet(df, path)


def read_universe(day: str) -> list[str]:
    _guard_day(day, what="股票池快照")
    path = UNIVERSE_DIR / f"{_safe_name(day)}.parquet"
    if not path.exists():
        return []
    df = pd.read_parquet(path)
    return df["symbol"].astype(str).tolist()


def universe_snapshot_days() -> list[str]:
    """已有快照的日期列表（升序）。"""
    if not UNIVERSE_DIR.exists():
        return []
    return sorted(p.stem for p in UNIVERSE_DIR.glob("*.parquet"))


def read_universe_range(
    start_date: str | None = None,
    end_date: str | None = None,
) -> pd.DataFrame:
    """读取区间内全部股票池快照 → 列 `[date, symbol]`。

    ★ 为什么要有它（而不是循环 `read_universe`）：需要"整段区间"的场景
      （基准计算、口径对照）逐日读会开 1600 个文件、每天再单独读一次行情，
      实测比"一次 glob + 一次 join"慢一两个数量级。
      `union_by_name=true` 是保险：快照文件带 `kept`/`empty` 元数据列，
      元数据键若有增删，不同日期的列集可能不一致。

    时间锁：与其它读取入口一致（`min(显式锁, 今天)`）。
    """
    start_date, end_date = _apply_read_until(start_date, end_date)
    if not universe_snapshot_days():
        return pd.DataFrame({"date": pd.Series(dtype="datetime64[ns]"), "symbol": pd.Series(dtype="object")})

    conditions: list[str] = []
    params: list[Any] = []
    if start_date:
        conditions.append("date >= ?")
        params.append(pd.Timestamp(start_date))
    if end_date:
        conditions.append("date <= ?")
        params.append(pd.Timestamp(end_date))
    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    sql = (
        f"SELECT date, symbol FROM read_parquet('{_glob_pattern(UNIVERSE_DIR)}'"
        f", union_by_name=true){where}"
    )
    df = query_duck(sql, params)
    if df.empty:
        return pd.DataFrame({"date": pd.Series(dtype="datetime64[ns]"), "symbol": pd.Series(dtype="object")})
    df["date"] = pd.to_datetime(df["date"])
    df["symbol"] = df["symbol"].astype(str)
    return df


# ---------- 因子 ----------
def factor_path(name: str, category: str = "daily") -> Path:
    """因子产物路径 —— **唯一实现**。

    刻意对外公开：`factors/manifest.py`（产物清单）与 `factors/calc.py` 都要算这个路径，
    各自拼一遍必然出现"清单指向 A 文件、实际写到 B 文件"的静默错位。
    """
    return FACTOR_DIR / _safe_name(category) / f"{_safe_name(name)}.parquet"


def save_factor(name: str, df: pd.DataFrame, category: str = "daily") -> dict[str, Any]:
    """保存因子（列：date, symbol, value）。"""
    return write_parquet(df, factor_path(name, category), dedup_keys=("date", "symbol"))


def read_factor(
    name: str,
    start_date: str | None = None,
    end_date: str | None = None,
    symbols: Sequence[str] | None = None,
    category: str = "daily",
) -> pd.DataFrame:
    # ★ 时间锁校验（因子同样是行情衍生数据）—— 无条件过锁，并用返回值覆盖区间
    start_date, end_date = _apply_read_until(start_date, end_date)

    path = factor_path(name, category)
    if not path.exists():
        raise DataNotReadyError(f"因子 {name} 尚未计算，文件不存在：{path}")

    conditions, params = [], []
    if start_date:
        conditions.append("date >= ?")
        params.append(pd.Timestamp(start_date))
    if end_date:
        conditions.append("date <= ?")
        params.append(pd.Timestamp(end_date))
    if symbols:
        marks = ", ".join("?" for _ in symbols)
        conditions.append(f"symbol IN ({marks})")
        params.extend(symbols)

    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    sql = f"SELECT * FROM read_parquet('{path.as_posix()}'){where}"
    return query_duck(sql, params)


def read_factor_aggregate(
    name: str,
    start_date: str | None = None,
    end_date: str | None = None,
    *,
    category: str = "daily",
    symbols: Sequence[str] | None = None,
) -> pd.DataFrame:
    """按交易日聚合因子截面：`[date, count, mean, median, q25, q75, std]`。

    为什么需要它（而不是把全量因子读进内存再 groupby）：
      单因子全量约 700 万行、30~60 MB。前端要画"因子全市场走势"时，
      读全量再聚合是纯浪费 —— 4800 行结果就够画图了。
      这条 SQL 全部在 DuckDB 里做完，返回的是几百/一千多行的小表。

    ⚠️ 只在**这个函数**里写聚合 SQL：`read_factor` 是明细的唯一入口，
       聚合口径也只有这一处 —— 否则"均值算不算 NaN"、"分位数用哪种算法"
       会在前后端各写一遍然后不一致。
    """
    start_date, end_date = _apply_read_until(start_date, end_date)

    path = factor_path(name, category)
    if not path.exists():
        raise DataNotReadyError(f"因子 {name} 尚未计算，文件不存在：{path}")

    conditions, params = [], []
    if start_date:
        conditions.append("date >= ?")
        params.append(pd.Timestamp(start_date))
    if end_date:
        conditions.append("date <= ?")
        params.append(pd.Timestamp(end_date))
    if symbols:
        marks = ", ".join("?" for _ in symbols)
        conditions.append(f"symbol IN ({marks})")
        params.extend(symbols)

    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    sql = (
        "SELECT date, count(value) AS count, avg(value) AS mean, "
        "median(value) AS median, "
        "quantile_cont(value, 0.25) AS q25, quantile_cont(value, 0.75) AS q75, "
        "stddev_samp(value) AS std "
        f"FROM read_parquet('{path.as_posix()}'){where} "
        "GROUP BY date ORDER BY date"
    )
    return query_duck(sql, params)


def list_quote_symbols() -> list[str]:
    """本地已有日线文件的标的列表（按文件名，**不读 Parquet**）。

    供前端做标的检索用：4792 个文件名的目录列举是毫秒级，
    而 `SELECT DISTINCT symbol` 要扫 700 万行。
    """
    if not QUOTE_DAILY_DIR.exists():
        return []
    return sorted(p.stem for p in QUOTE_DAILY_DIR.glob("*.parquet") if not p.stem.startswith("_"))


# ---------- 回测结果（只读，由策略 CLI 写入）----------
# ★ 为什么这里**不过时间锁**：回测结果是历史产物（已经跑完、已经落盘），
#   读它不会「泄漏未来数据」——真正需要防未来的是**行情**（回测引擎的输入）。
#   给结果读取加锁反而会让"回测跑到 2025，前端看 2025 的净值"直接报错。
def run_dir(strategy: str, run_id: str) -> Path:
    return BACKTEST_DIR / _safe_name(strategy) / _safe_name(run_id)


def list_runs(strategy: str) -> list[str]:
    d = BACKTEST_DIR / _safe_name(strategy)
    if not d.exists():
        return []
    return sorted((p.name for p in d.iterdir() if p.is_dir()), reverse=True)


def list_all_runs() -> list[tuple[str, str]]:
    """扫描**全部**回测产物目录，返回 `[(strategy, run_id), ...]`。

    ★ 以磁盘目录为真相源（文件契约是策略与前端唯一的耦合点）：
      meta.db 的 `backtest_run` 表可能因为进程被杀而留下 status='running' 的
      脏记录，也可能因为手工拷贝目录而缺失记录。**目录在 = 结果在。**
      唯一拿不到的字段是 `tag`（它在 DB 里），由 service 层做一次 join 补齐。
    """
    if not BACKTEST_DIR.exists():
        return []
    out: list[tuple[str, str]] = []
    for sdir in BACKTEST_DIR.iterdir():
        if not sdir.is_dir():
            continue
        for rdir in sdir.iterdir():
            if rdir.is_dir():
                out.append((sdir.name, rdir.name))
    return out


def _read_run_parquet(strategy: str, run_id: str, name: str) -> pd.DataFrame:
    path = run_dir(strategy, run_id) / f"{name}.parquet"
    if not path.exists():
        return pd.DataFrame()
    return pd.read_parquet(path)


def read_equity(strategy: str, run_id: str) -> pd.DataFrame:
    path = run_dir(strategy, run_id) / "equity.parquet"
    if not path.exists():
        raise DataNotReadyError(f"回测净值文件不存在：{path}")
    return pd.read_parquet(path)


def read_trades(strategy: str, run_id: str) -> pd.DataFrame:
    return _read_run_parquet(strategy, run_id, "trades")


def read_positions(strategy: str, run_id: str) -> pd.DataFrame:
    return _read_run_parquet(strategy, run_id, "positions")


def read_orders(strategy: str, run_id: str) -> pd.DataFrame:
    return _read_run_parquet(strategy, run_id, "orders")


def read_run_json(strategy: str, run_id: str, name: str) -> dict[str, Any] | None:
    """读回测目录里的 JSON（`config` / `metrics`）。不存在返回 None。"""
    path = run_dir(strategy, run_id) / f"{name}.json"
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        # 半截文件（进程被杀时正在写）不该让整个列表页崩掉
        logger.warning(f"回测文件解析失败，按缺失处理：{path}（{exc}）")
        return None
    return data if isinstance(data, dict) else None


def run_artifact_sizes(strategy: str, run_id: str) -> dict[str, int]:
    """各产物文件字节数（详情页展示"结果有多大"，也便于发现空文件）。"""
    d = run_dir(strategy, run_id)
    if not d.exists():
        return {}
    return {p.name: p.stat().st_size for p in sorted(d.iterdir()) if p.is_file()}


def delete_run(strategy: str, run_id: str) -> bool:
    """删除一个回测产物目录。**仅供 mock 数据清理与显式维护使用。**

    ⚠️ 不做递归删除的"顺手清理"：只删 `data/backtest/{strategy}/{run_id}/`
       这一层，且要求路径确实是 BACKTEST_DIR 下的两层目录（防路径穿越）。
    """
    import shutil  # noqa: PLC0415

    d = run_dir(strategy, run_id)
    if not d.is_dir() or BACKTEST_DIR not in d.parents:
        return False
    shutil.rmtree(d)
    logger.info(f"已删除回测产物目录：{d}")
    return True


# ======================================================================
# 降采样（前端图表性能）
# ======================================================================
def lttb_downsample(points: Sequence[tuple[float, float]], n_out: int) -> list[tuple[float, float]]:
    """Largest-Triangle-Three-Buckets 降采样。

    保留曲线的视觉形态（极值点），比等间隔抽样好得多。
    净值序列（含回撤尖峰）必须用它，否则前端会丢关键信息。
    """
    n = len(points)
    if n_out >= n or n_out < 3:
        return list(points)

    sampled: list[tuple[float, float]] = []
    every = (n - 2) / (n_out - 2)
    a = 0
    sampled.append(points[0])

    for i in range(n_out - 2):
        avg_start = int(math.floor((i + 1) * every)) + 1  # noqa: RUF046
        avg_end = int(math.floor((i + 2) * every)) + 1  # noqa: RUF046
        avg_end = min(avg_end, n)
        if avg_start >= avg_end:
            avg_start = min(avg_start, n - 1)
            avg_end = avg_start + 1

        avg_x = sum(points[j][0] for j in range(avg_start, avg_end)) / (avg_end - avg_start)
        avg_y = sum(points[j][1] for j in range(avg_start, avg_end)) / (avg_end - avg_start)

        range_start = int(math.floor(i * every)) + 1  # noqa: RUF046
        range_end = int(math.floor((i + 1) * every)) + 1  # noqa: RUF046
        range_end = min(range_end, n)

        ax, ay = points[a]
        max_area = -1.0
        chosen = range_start
        for j in range(range_start, range_end):
            area = abs(
                (ax - avg_x) * (points[j][1] - ay) - (ax - points[j][0]) * (avg_y - ay)
            )
            if area > max_area:
                max_area = area
                chosen = j

        sampled.append(points[chosen])
        a = chosen

    sampled.append(points[-1])
    return sampled


def downsample_series(
    df: pd.DataFrame,
    x_col: str = "date",
    y_cols: Sequence[str] = ("nav",),
    max_points: int = 2000,
) -> pd.DataFrame:
    """对时间序列 DataFrame 做 LTTB 降采样（保留给定主列的形态）。

    ⚠️ `lttb_downsample` 返回的是**被选中的点本身** `(x, y)`，不是位置下标。
       早期版本写了 `idx = [p[0] for p in ...]` 然后 `df.iloc[idx]` ——
       把 x（`pd.Timestamp.astype("int64")` 得到的**纳秒数 ≈ 1.6e18**）
       当行号用，直接 `IndexError: positional indexers are out-of-bounds`。

       这个 bug 能活很久，是因为**这条路径此前没有任何调用方**：
       单元测试只测了 `lttb_downsample` 的返回值，没人测过 `downsample_series`
       的端到端结果（典型的"写了但从未跑过"）。

       所以这里显式把 x 映射回下标，并顺手保证下标**单调递增且唯一**
       （LTTB 的桶本身不重叠，映射后天然有序；`iloc` 允许重复但会重复出行）。
    """
    if df is None or df.empty or len(df) <= max_points:
        return df

    y_main = y_cols[0] if y_cols and y_cols[0] in df.columns else None
    if y_main is None:
        return df.iloc[:: max(1, len(df) // max_points)].reset_index(drop=True)

    xs = pd.to_datetime(df[x_col]).astype("int64").to_numpy(dtype=float)
    ys = pd.to_numeric(df[y_main], errors="coerce").fillna(0).to_numpy(dtype=float)

    chosen = lttb_downsample(list(zip(xs, ys, strict=True)), max_points)

    x_list = xs.tolist()
    idx: list[int] = []
    for x, _ in chosen:
        # 采样点的 x 必然来自 xs，bisect 命中第一个等值位置
        j = bisect_left(x_list, x)
        if j >= len(x_list):
            j = len(x_list) - 1
        if not idx or j != idx[-1]:
            idx.append(j)

    out = df.iloc[idx].reset_index(drop=True)
    out.attrs["downsampled_from"] = len(df)
    return out


# ======================================================================
# 存储统计
# ======================================================================
def storage_stats() -> dict[str, Any]:
    """数据目录统计（供系统状态页展示）。"""
    def _dir_stat(p: Path) -> dict[str, Any]:
        if not p.exists():
            return {"files": 0, "bytes": 0}
        files = list(p.rglob("*.parquet"))
        return {"files": len(files), "bytes": sum(f.stat().st_size for f in files)}

    def _human(n: int) -> str:
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if n < 1024:
                return f"{n:.1f} {unit}"
            n /= 1024  # type: ignore[assignment]
        return f"{n:.1f} PB"

    sections = {
        "quote_daily": _dir_stat(QUOTE_DAILY_DIR),
        "quote_index": _dir_stat(QUOTE_INDEX_DIR),
        "universe": _dir_stat(UNIVERSE_DIR),
        "factor": _dir_stat(FACTOR_DIR),
        "backtest": _dir_stat(BACKTEST_DIR),
        "sweep": _dir_stat(SWEEP_DIR),
        "paper": _dir_stat(PAPER_DIR),
        "meta": _dir_stat(META_DIR),
        "cache": _dir_stat(CACHE_DIR),
    }
    total_bytes = sum(s["bytes"] for s in sections.values())
    for s in sections.values():
        s["human"] = _human(s["bytes"])

    # meta.db 不是 parquet，单独统计（否则系统状态页会漏掉它）
    meta_db_bytes = META_DB.stat().st_size if META_DB.exists() else 0

    return {
        "data_root": str(DATA_DIR),
        "sections": sections,
        "total_bytes": total_bytes,
        "total_human": _human(total_bytes),
        "meta_db_bytes": meta_db_bytes,
        "meta_db_human": _human(meta_db_bytes),
        "trade_calendar_ready": TRADE_CALENDAR_PARQUET.exists(),
    }
