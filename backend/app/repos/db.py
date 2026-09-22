"""SQLite 连接管理（data/meta/meta.db）。

要点：
- WAL 模式，允许读写并发（本地单机足够）
- 连接按线程隔离（FastAPI 的线程池 + APScheduler 会跨线程）
- 统一事务上下文管理器
- 统一把 sqlite3 异常转成 DatabaseError
"""
from __future__ import annotations

import contextlib
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from app.core.errors import DatabaseError
from app.core.logging import logger
from app.core.paths import META_DB, SQL_DIR

_SCHEMA_FILE: Path = SQL_DIR / "schema.sql"

#: 每线程一个连接
_local = threading.local()
_init_lock = threading.Lock()


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")   # 10s，避免并发写直接报错
    conn.execute("PRAGMA temp_store = MEMORY")


def get_connection() -> sqlite3.Connection:
    """获取当前线程的连接（惰性创建）。"""
    conn: sqlite3.Connection | None = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.execute("SELECT 1")
            return conn
        except sqlite3.Error:
            logger.debug("检测到失效的 SQLite 连接，重建中")

    META_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(META_DB), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    _apply_pragmas(conn)
    _local.conn = conn
    return conn


def close_connection() -> None:
    """关闭当前线程的连接。"""
    conn: sqlite3.Connection | None = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        finally:
            _local.conn = None


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """事务上下文：正常提交，异常回滚。"""
    conn = get_connection()
    try:
        conn.execute("BEGIN")
    except sqlite3.OperationalError:
        # 已在事务中（嵌套调用），直接复用
        yield conn
        return
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        # ROLLBACK 失败会掩盖原始异常，因此吞掉（但保留原始异常抛出）
        with contextlib.suppress(sqlite3.Error):
            conn.execute("ROLLBACK")
        raise


# ======================================================================
# 查询辅助
# ======================================================================
def query(sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> list[dict[str, Any]]:
    """执行查询，返回 dict 列表。"""
    try:
        conn = get_connection()
        cur = conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]
    except sqlite3.Error as exc:
        raise DatabaseError(f"查询失败：{exc}", detail={"sql": sql[:300]}) from exc


def query_one(sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> dict[str, Any] | None:
    """返回单行。"""
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> int:
    """执行写操作，返回受影响行数。"""
    try:
        with transaction() as conn:
            cur = conn.execute(sql, params)
            return cur.rowcount
    except sqlite3.IntegrityError as exc:
        raise DatabaseError(f"数据约束冲突：{exc}", detail={"sql": sql[:300]}) from exc
    except sqlite3.Error as exc:
        raise DatabaseError(f"写入失败：{exc}", detail={"sql": sql[:300]}) from exc


def executemany(sql: str, seq: Sequence[Sequence[Any]]) -> int:
    """批量写入。"""
    try:
        with transaction() as conn:
            cur = conn.executemany(sql, seq)
            return cur.rowcount
    except sqlite3.Error as exc:
        raise DatabaseError(f"批量写入失败：{exc}", detail={"sql": sql[:300]}) from exc


def upsert(table: str, data: dict[str, Any], conflict_keys: Sequence[str]) -> None:
    """INSERT ... ON CONFLICT DO UPDATE（SQLite 3.24+）。"""
    cols = list(data.keys())
    placeholders = ", ".join("?" for _ in cols)
    updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c not in conflict_keys)
    sql = (
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT ({', '.join(conflict_keys)}) DO UPDATE SET {updates}"
        if updates
        else f"INSERT OR IGNORE INTO {table} ({', '.join(cols)}) VALUES ({placeholders})"
    )
    execute(sql, [data[c] for c in cols])


# ======================================================================
# 初始化
# ======================================================================
def init_schema() -> dict[str, Any]:
    """建表（幂等）。返回统计信息。"""
    global _init_lock
    if not _SCHEMA_FILE.exists():
        raise DatabaseError(f"schema 文件不存在：{_SCHEMA_FILE}")

    sql_text = _SCHEMA_FILE.read_text(encoding="utf-8")
    conn = get_connection()

    with _init_lock:
        try:
            conn.executescript(sql_text)
        except sqlite3.Error as exc:
            raise DatabaseError(f"建表失败：{exc}") from exc

    tables = [
        r["name"]
        for r in query(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        )
    ]
    logger.info(f"meta.db 就绪，共 {len(tables)} 张表")
    return {"db_path": str(META_DB), "tables": tables, "table_count": len(tables)}


def table_row_counts() -> dict[str, int]:
    """各表行数（供系统状态页展示）。"""
    out: dict[str, int] = {}
    for r in query(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ):
        name = r["name"]
        try:
            row = query_one(f"SELECT COUNT(*) AS c FROM {name}")
            out[name] = int(row["c"]) if row else 0
        except DatabaseError:
            out[name] = -1
    return out


def db_size_bytes() -> int:
    """数据库文件大小（含 WAL）。"""
    total = 0
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(META_DB) + suffix)
        if p.exists():
            total += p.stat().st_size
    return total


def is_initialized() -> bool:
    """是否已完成建表。"""
    try:
        row = query_one(
            "SELECT COUNT(*) AS c FROM sqlite_master WHERE type='table' AND name='meta_kv'"
        )
        return bool(row and row["c"])
    except DatabaseError:
        return False
