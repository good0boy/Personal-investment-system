"""meta.db 的类型化访问层。

业务层通过这里读写元数据，不直接拼 SQL（除非是复杂查询）。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from app.core.logging import logger
from app.repos import db

# ======================================================================
# 键值元数据
# ======================================================================
#: 约定使用的 meta key
META_KEYS = {
    "quote_last_date": "行情数据最新日期",
    "calendar_last_date": "交易日历最新日期",
    "calendar_synced_at": "交易日历同步时间",
    "universe_synced_at": "股票列表同步时间",
    "stock_count": "股票总数",
    "data_initialized": "数据初始化是否完成（1/0）",
    "schema_version": "schema 版本",
}


def get_meta_value(key: str, default: str | None = None) -> str | None:
    row = db.query_one("SELECT value FROM meta_kv WHERE key = ?", (key,))
    return row["value"] if row else default


def set_meta_value(key: str, value: Any) -> None:
    db.execute(
        "INSERT INTO meta_kv (key, value, updated_at) VALUES (?, ?, datetime('now','localtime')) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, "" if value is None else str(value)),
    )


def set_meta_bulk(items: dict[str, Any]) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    db.executemany(
        "INSERT INTO meta_kv (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        [(k, "" if v is None else str(v), now) for k, v in items.items()],
    )


def get_meta_all() -> dict[str, str]:
    return {r["key"]: r["value"] for r in db.query("SELECT key, value FROM meta_kv")}


# ======================================================================
# 调度任务日志
# ======================================================================
def job_start(job_id: str, run_date: str, trigger_type: str = "cron") -> int:
    """记录任务开始，返回 log id。"""
    db.execute(
        "INSERT INTO job_run_log (job_id, run_date, trigger_type, status) VALUES (?, ?, ?, 'running')",
        (job_id, run_date, trigger_type),
    )
    row = db.query_one("SELECT last_insert_rowid() AS id")
    return int(row["id"]) if row else 0


def job_finish(log_id: int, status: str, duration: float | None = None, message: str = "") -> None:
    db.execute(
        "UPDATE job_run_log SET status = ?, duration = ?, message = ?, "
        "finished_at = datetime('now','localtime') WHERE id = ?",
        (status, duration, message[:2000], log_id),
    )


def job_recent(limit: int = 50, job_id: str | None = None) -> list[dict[str, Any]]:
    if job_id:
        return db.query(
            "SELECT * FROM job_run_log WHERE job_id = ? ORDER BY id DESC LIMIT ?",
            (job_id, limit),
        )
    return db.query("SELECT * FROM job_run_log ORDER BY id DESC LIMIT ?", (limit,))


def job_last_success_date(job_id: str) -> str | None:
    """某任务最后一次成功的逻辑运行日（用于启动补跑）。"""
    row = db.query_one(
        "SELECT run_date FROM job_run_log WHERE job_id = ? AND status = 'success' "
        "ORDER BY run_date DESC LIMIT 1",
        (job_id,),
    )
    return row["run_date"] if row else None


def job_has_run(job_id: str, run_date: str) -> bool:
    """某任务在某逻辑日是否已成功执行过（幂等判断）。"""
    row = db.query_one(
        "SELECT 1 AS x FROM job_run_log WHERE job_id = ? AND run_date = ? "
        "AND status = 'success' LIMIT 1",
        (job_id, run_date),
    )
    return row is not None


# ======================================================================
# 数据源调用日志
# ======================================================================
def log_datasource_call(
    datasource: str,
    func_name: str,
    status: str,
    *,
    params: str = "",
    rows: int | None = None,
    duration: float | None = None,
    error_message: str = "",
) -> None:
    try:
        db.execute(
            "INSERT INTO datasource_log (datasource, func_name, params, status, rows, duration, error_message) "  # noqa: E501
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (datasource, func_name, params[:500], status, rows, duration, error_message[:1000]),
        )
    except Exception as exc:
        logger.debug(f"写入 datasource_log 失败（忽略）：{exc}")


# ======================================================================
# 策略注册表
# ======================================================================
def register_strategy(
    strategy_id: str,
    name: str,
    *,
    impl_path: str = "",
    version: str = "",
    params_schema: dict[str, Any] | None = None,
    description: str = "",
) -> None:
    db.upsert(
        "strategy_registry",
        {
            "id": strategy_id,
            "name": name,
            "impl_path": impl_path,
            "version": version,
            "params_schema": json.dumps(params_schema, ensure_ascii=False) if params_schema else None,
            "description": description,
            "enabled": 1,
        },
        conflict_keys=["id"],
    )


def list_strategies(enabled_only: bool = True) -> list[dict[str, Any]]:
    sql = "SELECT * FROM strategy_registry"
    if enabled_only:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY id"
    return db.query(sql)


# ======================================================================
# 回测运行记录
# ======================================================================
def register_backtest_run(
    run_id: str,
    strategy: str,
    start_date: str,
    end_date: str,
    *,
    tag: str = "",
    is_out_sample: bool = False,
    param: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
) -> None:
    db.upsert(
        "backtest_run",
        {
            "run_id": run_id,
            "strategy": strategy,
            "tag": tag,
            "start_date": start_date,
            "end_date": end_date,
            "status": "running",
            "is_out_sample": 1 if is_out_sample else 0,
            "param_json": json.dumps(param, ensure_ascii=False) if param else None,
            "config_json": json.dumps(config, ensure_ascii=False) if config else None,
        },
        conflict_keys=["run_id"],
    )


def finish_backtest_run(
    run_id: str,
    status: str,
    *,
    metrics: dict[str, Any] | None = None,
    cost_seconds: float | None = None,
    trade_count: int | None = None,
    message: str = "",
) -> None:
    db.execute(
        "UPDATE backtest_run SET status = ?, metrics_json = ?, cost_seconds = ?, "
        "trade_count = ?, message = ?, finished_at = datetime('now','localtime') WHERE run_id = ?",
        (
            status,
            json.dumps(metrics, ensure_ascii=False) if metrics else None,
            cost_seconds,
            trade_count,
            message[:2000],
            run_id,
        ),
    )


def job_reap_zombies(older_than_hours: int = 24) -> int:
    """把卡在 `running` 的过期记录改写为 failed（进程崩溃/断电的收尸）。

    job_start 写入 running、job_finish 改写终态 —— 中间进程没了的话记录
    永远停在 running，前端会一直显示"正在跑"。启动时跑一次。
    """
    db.execute(
        "UPDATE job_run_log SET status = 'failed', message = 'zombie: 进程重启清理', "
        "finished_at = datetime('now','localtime') "
        "WHERE status = 'running' "
        "AND created_at < datetime('now', 'localtime', ?)",
        (f"-{int(older_than_hours)} hours",),
    )
    return db.query_one("SELECT changes() AS n")["n"]  # type: ignore[no-any-return]


def delete_backtest_run(run_id: str) -> None:
    """删除回测运行登记行（产物目录被清理时同步清 DB，避免脏登记残留）。"""
    db.execute("DELETE FROM backtest_run WHERE run_id = ?", (run_id,))


def update_paper_account_strategy(
    account_id: str,
    strategy: str,
    param_json: str | None = None,
) -> None:
    """切换模拟盘策略（含参数）。策略变化触发全量重算，净值历史整体重写。"""
    db.execute(
        "UPDATE paper_account SET strategy = ?, param_json = ? WHERE id = ?",
        (strategy, param_json, account_id),
    )


def list_backtest_runs(
    strategy: str | None = None,
    limit: int = 100,
    status: str | None = None,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM backtest_run WHERE 1=1"
    params: list[Any] = []
    if strategy:
        sql += " AND strategy = ?"
        params.append(strategy)
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    return db.query(sql, params)


# ======================================================================
# 模拟账户
# ======================================================================
def ensure_paper_account(
    account_id: str,
    name: str,
    init_cash: float,
    *,
    strategy: str | None = None,
    start_date: str | None = None,
) -> None:
    db.execute(
        "INSERT OR IGNORE INTO paper_account (id, name, strategy, init_cash, cash, start_date) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (account_id, name, strategy, init_cash, init_cash, start_date),
    )


def list_paper_accounts() -> list[dict[str, Any]]:
    return db.query("SELECT * FROM paper_account ORDER BY created_at")


def get_paper_account(account_id: str) -> dict[str, Any] | None:
    return db.query_one("SELECT * FROM paper_account WHERE id = ?", (account_id,))


def update_paper_run_state(
    account_id: str,
    *,
    last_run_date: str,
    cash: float | None = None,
    status: str = "idle",
) -> None:
    """跑批结束后更新账户状态。

    `last_run_date` 是补跑幂等的依据之一（进程重启后 job_run_log 与
    产物共同判断要不要补）。
    """
    db.execute(
        "UPDATE paper_account SET last_run_date = ?, cash = ?, status = ? WHERE id = ?",
        (last_run_date, cash, status, account_id),
    )


# ======================================================================
# 提醒日志
# ======================================================================
def alert_exists(event_key: str) -> bool:
    """幂等判断：同一事件只推一次。"""
    return db.query_one("SELECT 1 AS x FROM alert_log WHERE event_key = ? LIMIT 1", (event_key,)) is not None


def alert_insert(
    event_key: str,
    action: str,
    title: str,
    content: str,
    *,
    account_id: str | None = None,
    symbol: str | None = None,
    level: str = "info",
    channel: str = "webhook",
) -> int | None:
    """插入提醒记录。已存在则返回 None（去重）。"""
    try:
        with db.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO alert_log "
                "(event_key, account_id, symbol, action, level, title, content, channel, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')",
                (event_key, account_id, symbol, action, level, title, content, channel),
            )
            return int(cur.lastrowid or 0)
    except Exception:
        return None


def alert_mark(
    alert_id: int,
    status: str,
    *,
    error_message: str = "",
    bump_retry: bool = False,
    retry_count: int | None = None,
) -> None:
    """更新提醒记录的状态。

    Args:
        status: pending | sent | failed | skipped
        error_message: 失败原因（截断到 1000 字）
        bump_retry: `retry_count += 1`（保留给旧调用方）
        retry_count: **绝对赋值**。优先于 `bump_retry`。

    ★ 为什么加了 `retry_count` 绝对参数：`bump_retry` 是"相对"语义，
      调用方在重试循环的多个位置各 bump 一次，很容易算错 ——
      实测把 3 次重试记成了 4 次（退避时 +1、末次失败时又 +1）。
      绝对赋值是**幂等**的：调用方心里有一个明确的计数变量，写它就行，
      不存在"多写一次就错"的隐患。
    """
    if retry_count is not None:
        db.execute(
            "UPDATE alert_log SET status = ?, error_message = ?, retry_count = ?, "
            "sent_at = CASE WHEN ? = 'sent' THEN datetime('now','localtime') ELSE sent_at END "
            "WHERE id = ?",
            (status, error_message[:1000], int(retry_count), status, alert_id),
        )
    elif bump_retry:
        db.execute(
            "UPDATE alert_log SET status = ?, error_message = ?, "
            "retry_count = retry_count + 1, sent_at = CASE WHEN ? = 'sent' THEN datetime('now','localtime') ELSE sent_at END "  # noqa: E501
            "WHERE id = ?",
            (status, error_message[:1000], status, alert_id),
        )
    else:
        db.execute(
            "UPDATE alert_log SET status = ?, error_message = ?, "
            "sent_at = CASE WHEN ? = 'sent' THEN datetime('now','localtime') ELSE sent_at END "
            "WHERE id = ?",
            (status, error_message[:1000], status, alert_id),
        )


def list_alerts(limit: int = 100, status: str | None = None) -> list[dict[str, Any]]:
    if status:
        return db.query(
            "SELECT * FROM alert_log WHERE status = ? ORDER BY id DESC LIMIT ?", (status, limit)
        )
    return db.query("SELECT * FROM alert_log ORDER BY id DESC LIMIT ?", (limit,))


# ======================================================================
# 模块1 · 接口目录自检状态
# ======================================================================
# ★ 职责边界：本表只存自检【状态】；接口【定义】的唯一来源是
#   config/catalog/*.yaml。两者不互写（防止重蹈 datasource_capability 死表）。

def catalog_status_upsert(
    item_id: str,
    category: str,
    func_name: str,
    datasource: str,
    status: str,
    *,
    status_message: str = "",
    source_version: str = "",
) -> None:
    db.upsert(
        "catalog_interface",
        {
            "id": item_id,
            "category": category,
            "func_name": func_name,
            "datasource": datasource,
            "status": status,
            "status_message": status_message[:1000],
            "verified_at": datetime.now().isoformat(timespec="seconds"),
            "source_version": source_version,
        },
        conflict_keys=["id"],
    )


def catalog_status_all() -> list[dict[str, Any]]:
    return db.query("SELECT * FROM catalog_interface ORDER BY id")


def catalog_status_delete_removed(valid_ids: set[str]) -> int:
    """清理 YAML 中已删除接口的残留状态行。返回删除行数。"""
    rows = catalog_status_all()
    removed = [r["id"] for r in rows if r["id"] not in valid_ids]
    if not removed:
        return 0
    placeholders = ", ".join("?" for _ in removed)
    return db.execute(f"DELETE FROM catalog_interface WHERE id IN ({placeholders})", removed)


# ======================================================================
# 数据源能力矩阵 —— ⚠️ 不进数据库
# ======================================================================
# 原先这里有 `upsert_capability()` / `list_capabilities()`，操作
# `datasource_capability` 表。该表已于 2026-09-17 删除，**不要恢复**。
#
# 原因：能力矩阵是**配置**（人工维护、需要 diff、需要版本管理），唯一来源是
#       `config/datasource_capabilities/*.yaml`，`/api/v1/datasources`
#       直接读 YAML（技术架构 6.1.6 节明确如此）。
#       原表的两个访问器**从未被任何调用方使用**，表恒为 0 行 ——
#       留着它等于同一份数据两个来源。
#
# 所以：**要加数据源 / 改能力标注，去改 YAML，不要动数据库。**
