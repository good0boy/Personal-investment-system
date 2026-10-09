"""模块 3 · 回测结果的读取服务（**严格只读**）。

架构定位
--------
本模块是「文件契约」的**消费端**：`data/backtest/{strategy}/{run_id}/`
由策略 CLI 写入，这里只负责读出来给前端。它**不做任何计算密集的事**
（不跑回测、不做参数寻优），符合架构纪律 5「只读展示优先」。

真相源是**磁盘目录**，不是数据库
------------------------------
`meta.db` 的 `backtest_run` 表是**登记簿**，它会因为进程被杀留下
`status='running'` 的脏记录，也会因为手工拷贝目录而缺记录。
而 `data/backtest/...` 下的文件是**产物本身**——目录在，结果就在。
所以：扫盘为准，DB 只用来补 `tag` / `cost_seconds` 这类"过程信息"。

三个必须的容错
------------
1. **JSON 里的 NaN/Infinity**：Python 能读回来，浏览器 `JSON.parse` 会抛
   `SyntaxError` —— 一个 NaN 让整页白屏。所有响应都过 `json_safe()`。
2. **半截文件**：进程被杀时可能留下写了一半的 JSON。读失败按缺失处理，
   不能让一个坏 run 把整个列表页拖崩。
3. **调试模式的 run**：`strategies/run.py --symbols sh.600519` 只跑了 1 只标的，
   截面选股结果**没有可比性**。它跑出来的年化收益可能很漂亮，
   拿它评估策略就是自欺。所以凡是 `debug_symbols` 非空的 run 都要**醒目标记**。
"""
from __future__ import annotations

import math
import threading
import time
from typing import Any

import pandas as pd

from app.core.errors import NotFoundError
from app.core.jsonutils import json_safe
from app.core.logging import logger
from app.repos import meta_repo
from app.services import data_store

# ======================================================================
# 常量
# ======================================================================
STATUS_SUCCESS = "success"
STATUS_RUNNING = "running"
STATUS_FAILED = "failed"
STATUS_INCOMPLETE = "incomplete"

#: 列表页允许的排序字段 → 从哪个字典取值
SORTABLE: dict[str, str] = {
    "created_at": "created_at",
    "finished_at": "finished_at",
    "total_return": "total_return",
    "annual_return": "annual_return",
    "max_drawdown": "max_drawdown",
    "sharpe": "sharpe",
    "win_rate": "win_rate",
    "trade_count": "trade_count",
    "annual_cost_ratio": "annual_cost_ratio",
    "max_single_loss": "max_single_loss",
}

#: 前端净值图最多要多少个点（LTTB 之后）
DEFAULT_MAX_POINTS = 2000

#: mock 产物的 run_id 前缀（由 `tools/make_mock_backtest.py` 生成）。
#: ★ 定义在这里而不是散落两处：mock 工具从本模块 import，保证前缀永远一致 ——
#:   前缀一旦对不上，列表页就会把假数据当成真实回测结果展示。
MOCK_PREFIX = "mock-"

#: 扫盘缓存 TTL（秒）。回测是分钟级任务，15 秒缓存足以吃掉"翻页/筛选"的重复扫盘。
_SCAN_TTL = 15.0
_scan_cache: dict[str, Any] = {"at": 0.0, "rows": []}
_scan_lock = threading.Lock()


# ======================================================================
# 小工具
# ======================================================================
def human_bytes(n: int | float | None) -> str:
    if not n:
        return "0 B"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _finite(value: Any) -> float | None:
    """转成有限 float，否则 None（前端对 null 显示 '—'）。"""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _col_to_list(series: Any) -> list[Any]:
    """列 → JSON 安全的列表（NaN → None）。

    ⚠️ 不能 `series.tolist()` 了事：pandas 的 NaN 经 `json.dumps` 会写成
       非法字面量 `NaN`（见 `app/core/jsonutils.py` 的说明）。
    ⚠️ 也不能用 `pd.isna(v)` 做通用判断 —— 它遇到 list/ndarray 会返回**数组**，
       在 `or` / `if` 里直接抛 "truth value of an array is ambiguous"。
       这里按类型分派，不依赖 pandas 的通用判空。
    """
    out: list[Any] = []
    for v in series:
        if v is None:
            out.append(None)
        elif isinstance(v, bool):
            out.append(v)
        elif isinstance(v, float):
            out.append(v if math.isfinite(v) else None)
        elif isinstance(v, (int, str)):
            out.append(v)
        elif hasattr(v, "year"):  # datetime / date / Timestamp
            out.append(f"{v.year:04d}-{v.month:02d}-{v.day:02d}")
        else:
            # numpy 标量等：先试数值，再退化成字符串
            try:
                fv = float(v)
            except (TypeError, ValueError):
                out.append(str(v))
            else:
                out.append(fv if math.isfinite(fv) else None)
    return out


def _num_series(df: pd.DataFrame, name: str) -> pd.Series:
    """取数值列；**列不存在或全空时返回空 Series**（不要返回标量或 None）。

    与 `metrics._num_col` 同一个理由：`DataFrame.get()` 缺列时返回 None，
    `pd.to_numeric(None)` 得到的是标量，接着比较/过滤就炸。
    """
    if name not in df.columns:
        return pd.Series(dtype="float64")
    return pd.to_numeric(df[name], errors="coerce").dropna()


def _date_str(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return pd.Timestamp(value).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return str(value)[:10]


def _db_map() -> dict[str, dict[str, Any]]:
    """meta.db 里的登记记录（key = run_id）。读失败返回空表，不影响列表展示。"""
    try:
        rows = meta_repo.list_backtest_runs(limit=2000)
    except Exception as exc:
        logger.warning(f"读取 backtest_run 表失败，列表将不含注册信息：{exc}")
        return {}
    return {str(r.get("run_id")): r for r in rows}


# ======================================================================
# 策略列表
# ======================================================================
def list_strategies() -> list[dict[str, Any]]:
    """已注册策略。

    两个来源合并：
      · **代码注册表**（`strategies.registry`）—— 能跑的策略，含默认参数与说明
      · **meta.db `strategy_registry`** —— 历史登记信息（版本/实现路径）

    代码注册表为准（它才是"现在能跑什么"），DB 只补字段。
    """
    from strategies import registry  # noqa: PLC0415 - 延迟导入：避免 CLI 依赖 FastAPI

    db_rows = {str(r.get("id")): r for r in meta_repo.list_strategies(enabled_only=False)}
    out: list[dict[str, Any]] = []

    code_names: set[str] = set()
    for name in registry.strategy_names():
        info = registry.describe(name)
        reg = db_rows.get(name, {})
        code_names.add(name)
        out.append(
            {
                "name": name,
                "display_name": reg.get("name") or name,
                "version": info.get("version") or reg.get("version") or "",
                "description": info.get("description") or reg.get("description") or "",
                "impl_path": reg.get("impl_path") or f"strategies/impl/{name}.py",
                "in_code": True,
                "params": info.get("params") or {},
            }
        )

    # DB 里登记但代码里已删掉的策略：仍然展示（它的历史结果还能看），但标注不可再跑
    for name, reg in db_rows.items():
        if name in code_names:
            continue
        out.append(
            {
                "name": name,
                "display_name": reg.get("name") or name,
                "version": reg.get("version") or "",
                "description": reg.get("description") or "",
                "impl_path": reg.get("impl_path") or "",
                "in_code": False,
                "params": {},
            }
        )
    return out


# ======================================================================
# 运行列表（扫盘 + DB 补登记信息）
# ======================================================================
def _scan_one(strategy: str, run_id: str, db_row: dict[str, Any] | None) -> dict[str, Any]:
    cfg = data_store.read_run_json(strategy, run_id, "config") or {}
    met = data_store.read_run_json(strategy, run_id, "metrics")

    run_meta = cfg.get("_run") or {}
    interval = cfg.get("interval") or {}
    strategy_cfg = cfg.get("strategy") or {}
    capital = cfg.get("capital") or {}
    debug_symbols = cfg.get("debug_symbols")

    # ★ 有 metrics.json = 跑完了；没有 = 运行中/被中断。DB 的 status 只作为兜底。
    if met is not None:
        status = STATUS_SUCCESS
    elif db_row:
        status = str(db_row.get("status") or STATUS_INCOMPLETE)
    else:
        status = STATUS_INCOMPLETE

    metrics = json_safe(met or {})
    return {
        "strategy": strategy,
        "run_id": run_id,
        "tag": run_meta.get("tag") or (db_row or {}).get("tag") or "",
        "status": status,
        "has_metrics": met is not None,
        "is_out_sample": bool(interval.get("is_out_sample")),
        # ★ mock 数据必须被标出来：它是给前端开发用的假数据，
        #   产物结构与真实回测一模一样，混在列表里分不清就会有人拿它下结论。
        "is_mock": run_id.startswith(MOCK_PREFIX),
        # ★ 参数寻优的子运行也必须被标出来。
        #   原因：一次 sweep 会在 data/backtest/ 下留 12 个 run（每组一个），
        #   它们与真实回测的产物结构完全一样 —— 不标的话回测列表页会被
        #   "sweep 的 12 个格子"淹没，而它们其实是同一批数据的 12 种参数。
        #   非空 = 这个 run 属于某个 sweep，且值就是那个 sweep_id。
        "sweep_id": run_meta.get("sweep_id") or None,
        "combo_index": run_meta.get("combo_index"),
        # ★ 明细表（trades/positions/orders）是否落盘。
        #   sweep 的子运行为了省磁盘刻意不写这三张表，但 `metrics.json` 里
        #   `trade_count` 照样是 392 —— 详情页若不看这个标记，就会显示
        #   "交易数 392"却一笔都列不出来（本项目的"看着成功其实残缺"坑）。
        #   None 视为 True：老产物没有这个字段，而它们都写了明细表。
        "detail_written": run_meta.get("detail_written") is not False,
        "interval_name": interval.get("name") or "",
        "start_date": _date_str(interval.get("start")) or (db_row or {}).get("start_date"),
        "end_date": _date_str(interval.get("end")) or (db_row or {}).get("end_date"),
        "preheat_start": _date_str(interval.get("preheat_start")),
        "strategy_version": strategy_cfg.get("version") or "",
        "params": strategy_cfg.get("params") or {},
        "initial_cash": _finite(capital.get("initial_cash")),
        "max_positions": capital.get("max_positions"),
        "benchmark": strategy_cfg.get("benchmark"),
        # ★ 完成时间优先取**产物**里的 `_run.finished_at`，DB 的 `created_at` 只兜底：
        #   产物是真相源；DB 的 created_at 在重新登记/换机器时会变，
        #   而且默认值可能是 UTC，会出现"完成时间与 run_id 里的时间对不上"。
        "created_at": run_meta.get("finished_at") or (db_row or {}).get("created_at") or "",
        "finished_at": run_meta.get("finished_at") or (db_row or {}).get("finished_at") or "",
        "cost_seconds": _finite((db_row or {}).get("cost_seconds")),
        # 指标（前端列表页直接展示这几列）
        "trading_days": metrics.get("trading_days"),
        "total_return": metrics.get("total_return"),
        "annual_return": metrics.get("annual_return"),
        "max_drawdown": metrics.get("max_drawdown"),
        "sharpe": metrics.get("sharpe"),
        "win_rate": metrics.get("win_rate"),
        "trade_count": metrics.get("trade_count"),
        "annual_cost_ratio": metrics.get("annual_cost_ratio"),
        "max_single_loss": metrics.get("max_single_loss"),
        # ★ 调试模式标记：只跑了几只标的的 run，结果没有可比性
        "debug_symbols": debug_symbols or None,
    }


def scan_runs(*, force: bool = False) -> list[dict[str, Any]]:
    """扫描全部回测产物，返回 run 摘要列表（带 TTL 缓存）。"""
    now = time.monotonic()
    with _scan_lock:
        if not force and _scan_cache["rows"] and (now - _scan_cache["at"]) < _SCAN_TTL:
            return list(_scan_cache["rows"])

    db = _db_map()
    rows: list[dict[str, Any]] = []
    for strategy, run_id in data_store.list_all_runs():
        # ★ 产物不完整自愈（2026-10-09 smoke:11 事件）：metrics.json 已写完
        #   （= 跑批正常结束）但 equity.parquet 缺失 → 详情页读净值必然 503，
        #   留在列表里只会误导。判定为坏档：删目录 + 清 DB 登记行，并不再展示。
        #   ⚠️ 正在跑的 run（metrics 尚未写）不在此列，不会被误删。
        rd = data_store.run_dir(strategy, run_id)
        if (
            (rd / "config.json").exists()
            and (rd / "metrics.json").exists()
            and not (rd / "equity.parquet").exists()
        ):
            logger.warning(
                f"回测 {strategy}/{run_id} 缺少 equity.parquet（产物不完整），已自动删除并从列表移除"
            )
            data_store.delete_run(strategy, run_id)
            meta_repo.delete_backtest_run(run_id)
            continue
        try:
            rows.append(_scan_one(strategy, run_id, db.get(run_id)))
        except Exception as exc:
            logger.warning(f"回测 {strategy}/{run_id} 摘要读取失败，已跳过：{exc}")
    rows.sort(key=lambda r: (r.get("created_at") or "", r.get("run_id") or ""), reverse=True)

    with _scan_lock:
        _scan_cache["at"] = now
        _scan_cache["rows"] = rows
    return list(rows)


def invalidate_cache() -> None:
    """清空扫盘缓存（mock 数据生成后、或手工删了目录时调用）。"""
    with _scan_lock:
        _scan_cache["at"] = 0.0
        _scan_cache["rows"] = []


def list_runs(
    *,
    strategy: str | None = None,
    tag: str | None = None,
    status: str | None = None,
    sort: str = "created_at",
    desc: bool = True,
    limit: int = 200,
    exclude_sweep: bool = True,
) -> dict[str, Any]:
    """回测运行列表（筛选 + 排序 + 截断）。

    ★ `exclude_sweep=True`（默认）会滤掉**参数寻优的子运行**。
      为什么默认滤掉：一次 12 组的 sweep 会在 `data/backtest/` 下留 12 个 run，
      它们的产物结构与真实回测一模一样。全列出来的话，回测列表页立刻被
      这些"格子"淹没，而真正手工跑的那几次全被挤到后面。
      需要看它们时传 `exclude_sweep=False`（前端有开关）。
    """
    rows = scan_runs()

    if exclude_sweep:
        rows = [r for r in rows if not r.get("sweep_id")]
    if strategy:
        rows = [r for r in rows if r["strategy"] == strategy]
    if tag:
        rows = [r for r in rows if tag in (r.get("tag") or "")]
    if status:
        rows = [r for r in rows if r["status"] == status]

    key = SORTABLE.get(sort, "created_at")

    # ★ None 必须永远排最后，且**不受 desc 影响** —— 否则"没跑完/没指标"的 run
    #   会霸占第一行，看起来像是"收益最好"。做法：先升序（None 落尾），
    #   再把非空段反转，最后接上 None 段。
    def _sort_key(row: dict[str, Any]) -> tuple[int, Any]:
        v = row.get(key)
        if v is None or v == "":
            return (1, "")
        if isinstance(v, (int, float)):
            return (0, float(v))
        return (0, str(v))

    rows.sort(key=_sort_key)
    if desc:
        head = [r for r in rows if r.get(key) not in (None, "")]
        tail = [r for r in rows if r.get(key) in (None, "")]
        head.reverse()
        rows = head + tail

    total = len(rows)
    return {
        "items": rows[:limit],
        "total": total,
        "truncated": total > limit,
        "strategies": list_strategies(),
        "sort": sort,
        "desc": desc,
    }


# ======================================================================
# 月度收益（净值曲线 → 年 × 月 矩阵）
# ======================================================================
def monthly_returns(equity: pd.DataFrame) -> list[dict[str, Any]]:
    """从净值列算月度收益率。

    口径：**每月最后一个交易日**的净值 / 上月末净值 - 1。
    第一个月用 `nav - 1`（回测起点净值恒为 1），否则首月收益会永远缺失。
    """
    if equity is None or equity.empty or "nav" not in equity.columns:
        return []
    df = equity[["date", "nav"]].copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["nav"] = pd.to_numeric(df["nav"], errors="coerce")
    df = df.dropna().sort_values("date")
    if df.empty:
        return []

    monthly = df.groupby(df["date"].dt.to_period("M"))["nav"].last()
    prev = monthly.shift(1)
    if len(monthly):
        prev.iloc[0] = 1.0  # 起点净值为 1
    rets = monthly / prev - 1.0

    out: list[dict[str, Any]] = []
    for period, value in rets.items():
        v = _finite(value)
        out.append({"year": int(period.year), "month": int(period.month), "return": v})
    return out


# ======================================================================
# 运行详情
# ======================================================================
def _find_run(strategy: str, run_id: str) -> dict[str, Any]:
    rows = scan_runs()
    for r in rows:
        if r["strategy"] == strategy and r["run_id"] == run_id:
            return r
    # 缓存可能是 15 秒前的：目录刚被 mock 生成时再强制扫一次
    for r in scan_runs(force=True):
        if r["strategy"] == strategy and r["run_id"] == run_id:
            return r
    raise NotFoundError(
        f"找不到回测 {strategy}/{run_id}。可用运行请查 GET /api/v1/backtest/runs",
        detail={"strategy": strategy, "run_id": run_id},
    )


def run_notes(summary: dict[str, Any], metrics: dict[str, Any]) -> list[dict[str, str]]:
    """数据健康提示 —— 只读页面上最值得被看到的那几句话。

    ★ 这个函数存在的理由：一个只跑了 3 只标的的 run 和一个跑全市场的 run，
      产物结构完全一样、指标栏位完全一样。不显式区分，就会有人拿前者下结论。
    """
    notes: list[dict[str, str]] = []
    if summary.get("is_mock"):
        notes.append(
            {
                "level": "danger",
                "text": (
                    "模拟数据（mock）：由 tools/make_mock_backtest.py 生成的假产物，"
                    "仅用于前端联调，没有任何真实数据支撑，不要据此判断策略。"
                ),
            }
        )
    if summary.get("debug_symbols"):
        n = len(summary["debug_symbols"])
        notes.append(
            {
                "level": "danger",
                # 不用 markdown 强调符号：前端是纯文本渲染（避免为此开 v-html），
                # 强调交给样式 —— danger 级本身就是红色加粗。
                "text": (
                    f"调试运行：只用了 {n} 只标的（{', '.join(summary['debug_symbols'][:5])}"
                    f"{'…' if n > 5 else ''}）。截面选股被严重削弱，"
                    f"这些指标不能用于评估策略。"
                ),
            }
        )
    if summary.get("is_out_sample"):
        notes.append(
            {
                "level": "warning",
                "text": "样本外区间：规则冻结前不应反复查看（会变成事后挑参数）。",
            }
        )
    if not summary.get("has_metrics"):
        notes.append(
            {"level": "warning", "text": "缺少 metrics.json —— 运行中，或被中断。"}
        )
    # ★ 来自参数寻优的子运行：它只关心指标，没写明细表。
    #   不提示的话，详情页会显示"交易数 392"却在交易记录里列不出任何东西 ——
    #   用户会以为数据坏了。这里直接把原因说清楚。
    if summary.get("sweep_id"):
        notes.append(
            {
                "level": "info",
                # 不用 markdown 强调符号：前端是纯文本渲染（见本函数开头的约定）
                "text": (
                    f"本次运行是参数寻优「{summary['sweep_id']}」的第 "
                    f"{summary.get('combo_index')} 组。"
                    f"寻优只保留指标与净值，未落盘交易/持仓/委托明细"
                    f"（12 组各存一份纯属浪费磁盘）。"
                    f"需要看明细请按这套参数单独跑一次单次回测。"
                ),
            }
        )
    if summary.get("detail_written") is False and not summary.get("sweep_id"):
        notes.append(
            {
                "level": "warning",
                "text": "本次运行未落盘交易/持仓/委托明细表 —— 交易记录页会是空的。",
            }
        )
    days = metrics.get("trading_days") or 0
    if summary.get("has_metrics") and days and days < 100:
        notes.append(
            {"level": "warning", "text": f"区间只有 {days} 个交易日，指标波动很大。"}
        )
    if metrics.get("trade_count") == 0:
        notes.append({"level": "info", "text": "本次运行没有产生任何成交。"})
    return notes


def get_run(strategy: str, run_id: str) -> dict[str, Any]:
    """运行详情：配置快照 + 指标 + 月度收益 + 产物清单。"""
    summary = _find_run(strategy, run_id)
    cfg = data_store.read_run_json(strategy, run_id, "config") or {}
    metrics = json_safe(data_store.read_run_json(strategy, run_id, "metrics") or {})

    equity = pd.DataFrame()
    rows_count = (cfg.get("_run") or {}).get("rows") or {}
    if summary.get("has_metrics"):
        try:
            equity = data_store.read_equity(strategy, run_id)
        except Exception as exc:
            logger.warning(f"净值读取失败（详情页仍返回其余信息）：{exc}")

    sizes = data_store.run_artifact_sizes(strategy, run_id)
    files = [
        {"name": name, "bytes": size, "human": human_bytes(size)}
        for name, size in sizes.items()
    ]

    return {
        **summary,
        "config": json_safe(cfg),
        "metrics": metrics,
        "monthly_returns": monthly_returns(equity),
        "files": files,
        "rows": {
            "equity": rows_count.get("equity") or (len(equity) if not equity.empty else 0),
            "trades": rows_count.get("trades"),
            "positions": rows_count.get("positions"),
            "orders": rows_count.get("orders"),
        },
        "notes": run_notes(summary, metrics),
    }


# ======================================================================
# 净值曲线（LTTB 降采样）
# ======================================================================
#: 净值曲线上要传给前端的列（列式返回，比行式紧凑一半以上）
_EQUITY_FIELDS: tuple[str, ...] = (
    "nav",
    "benchmark_nav",
    "drawdown",
    "cash",
    "market_value",
    "position_count",
)


def get_equity(
    strategy: str, run_id: str, *, max_points: int = DEFAULT_MAX_POINTS
) -> dict[str, Any]:
    """净值曲线（服务端 LTTB 降采样）。

    ★ 1115 个交易日就要 1115 个点；模拟盘分钟级会到十万级。不降采样前端必卡。
      LTTB 保留极值形态（回撤尖峰不会被抹平），优于等间隔抽样。
    """
    _find_run(strategy, run_id)  # 不存在就抛 NotFoundError
    df = data_store.read_equity(strategy, run_id)
    if df.empty:
        return {"count": 0, "downsampled_from": 0, "downsampled": False, "series": {}}

    df = df.sort_values("date").reset_index(drop=True)
    n_total = len(df)

    if n_total > max_points:
        df = data_store.downsample_series(
            df, x_col="date", y_cols=("nav",), max_points=max_points
        )

    series: dict[str, list[Any]] = {"date": [str(d)[:10] for d in df["date"]]}
    for col in _EQUITY_FIELDS:
        if col in df.columns:
            series[col] = _col_to_list(df[col])

    return {
        "count": len(df),
        "downsampled_from": n_total,
        "downsampled": n_total > len(df),
        "start_date": series["date"][0] if series["date"] else None,
        "end_date": series["date"][-1] if series["date"] else None,
        "series": series,
    }


# ======================================================================
# 交易记录（筛选 + 分页）
# ======================================================================
_TRADE_ITEM_FIELDS: tuple[str, ...] = (
    "symbol",
    "entry_date",
    "entry_price",
    "shares",
    "entry_amount",
    "entry_cost",
    "exit_date",
    "exit_price",
    "exit_amount",
    "exit_cost",
    "cost",
    "gross_pnl",
    "net_pnl",
    "net_return",
    "hold_days",
    "entry_reason",
    "exit_reason",
)


def _matches_reasons(value: Any, wanted: list[str]) -> bool:
    """`exit_reason` 可能是 `time_stop|take_profit` 这样的多值串。"""
    if not wanted:
        return True
    text = str(value or "")
    parts = [p.strip() for p in text.split("|") if p.strip()]
    return any(w in parts for w in wanted)


def get_trades(
    strategy: str,
    run_id: str,
    *,
    page: int = 1,
    page_size: int = 50,
    symbol: str | None = None,
    exit_reasons: list[str] | None = None,
    win: str | None = None,          # win | lose | all
    start: str | None = None,
    end: str | None = None,
    sort: str = "entry_date",
    desc: bool = True,
) -> dict[str, Any]:
    """已平仓交易记录：筛选 + 分页 + 筛选后统计。

    `summary` 统计的是**筛选后的全量**，不是当前页 —— 否则翻页时数字会变。
    """
    _find_run(strategy, run_id)
    df = data_store.read_trades(strategy, run_id)
    if df.empty:
        return {
            "items": [],
            "total": 0,
            "page": page,
            "page_size": page_size,
            "pages": 0,
            "summary": {"trade_count": 0, "net_pnl": None, "win_rate": None},
            "filters": {"exit_reasons": []},
        }

    dt = df.copy()
    for col in ("entry_date", "exit_date"):
        if col in dt.columns:
            dt[col] = pd.to_datetime(dt[col], errors="coerce")

    if symbol:
        dt = dt[dt["symbol"].astype(str).str.contains(symbol.strip(), case=False, na=False)]
    if exit_reasons:
        dt = dt[dt["exit_reason"].map(lambda v: _matches_reasons(v, exit_reasons))]
    if start and "entry_date" in dt.columns:
        dt = dt[dt["entry_date"] >= pd.Timestamp(start)]
    if end and "entry_date" in dt.columns:
        dt = dt[dt["entry_date"] <= pd.Timestamp(end)]
    if win in ("win", "lose"):
        pnl_all = (
            pd.to_numeric(dt["net_pnl"], errors="coerce")
            if "net_pnl" in dt.columns
            else pd.Series(index=dt.index, dtype="float64")
        )
        dt = dt[pnl_all > 0] if win == "win" else dt[pnl_all < 0]

    # 全量筛选结果里的可选值（供前端渲染筛选器）
    all_reasons: list[str] = []
    for v in df.get("exit_reason", pd.Series(dtype="object")).dropna().unique():
        for p in str(v).split("|"):
            p = p.strip()
            if p and p not in all_reasons:
                all_reasons.append(p)
    all_reasons.sort()

    total = len(dt)
    if sort in dt.columns:
        dt = dt.sort_values(sort, ascending=not desc, na_position="last")

    page = max(1, page)
    page_size = max(1, min(page_size, 500))
    start_i = (page - 1) * page_size
    chunk = dt.iloc[start_i : start_i + page_size]

    items: list[dict[str, Any]] = []
    for row in chunk.to_dict("records"):
        item: dict[str, Any] = {}
        for col in _TRADE_ITEM_FIELDS:
            v = row.get(col)
            if isinstance(v, float):
                item[col] = v if math.isfinite(v) else None
            elif hasattr(v, "isoformat"):
                item[col] = _date_str(v)
            elif pd.isna(v):
                item[col] = None
            else:
                item[col] = v
        items.append(item)

    pnl = _num_series(dt, "net_pnl")
    rets = _num_series(dt, "net_return")
    costs = _num_series(dt, "cost")
    summary = {
        "trade_count": total,
        "net_pnl": _finite(pnl.sum()) if not pnl.empty else None,
        "win_rate": _finite((pnl > 0).sum() / len(pnl)) if not pnl.empty else None,
        "avg_net_return": _finite(rets.mean()) if not rets.empty else None,
        "total_cost": _finite(costs.sum()) if not costs.empty else None,
    }

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": math.ceil(total / page_size) if total else 0,
        "summary": summary,
        "filters": {"exit_reasons": all_reasons},
    }


# ======================================================================
# 委托流水（排查"跌停卖不掉"的唯一凭证）
# ======================================================================
def get_orders(
    strategy: str, run_id: str, *, limit: int = 500, status: str | None = None
) -> dict[str, Any]:
    _find_run(strategy, run_id)
    df = data_store.read_orders(strategy, run_id)
    if df.empty:
        return {"items": [], "total": 0}
    if status:
        df = df[df["status"].astype(str) == status]
    total = len(df)
    chunk = df.tail(limit) if total > limit else df
    return {
        "items": json_safe(chunk.to_dict("records")),
        "total": total,
        "shown": len(chunk),
        "columns": list(chunk.columns),
    }


# ======================================================================
# 多 run 净值对比（P1）
# ======================================================================
MAX_COMPARE = 6


def compare_runs(refs: list[tuple[str, str]], *, max_points: int = 2000) -> dict[str, Any]:
    """把多份回测的净值曲线对齐到同一时间轴上。

    对齐规则：取所有 run 日期的**并集**，各自 `asof` 填充（区间外为 None，
    前端 ECharts 会断线，正好表达"这个 run 没覆盖那段区间"）。
    不要用交集 —— 那会让"全区间基线 vs 半年期新版本"的对比变成只有半年。

    ★ ffill 只在**各自的区间内**生效，不能延到区间之后：
      否则一个跑到 2024-01 就结束的 run，会在图上被延长成一条水平线到
      并集末尾，看起来像"那段时间收益为 0"——比不画更误导。
    """
    if not refs:
        return {"dates": [], "series": [], "count": 0}
    refs = refs[:MAX_COMPARE]

    frames: list[tuple[str, pd.Series]] = []
    series_out: list[dict[str, Any]] = []
    for strategy, run_id in refs:
        summary = _find_run(strategy, run_id)
        try:
            df = data_store.read_equity(strategy, run_id)
        except Exception as exc:
            logger.warning(f"对比时跳过 {strategy}/{run_id}：{exc}")
            continue
        if df.empty:
            continue
        s = pd.Series(
            pd.to_numeric(df["nav"], errors="coerce").to_numpy(dtype=float),
            index=pd.to_datetime(df["date"]),
        ).sort_index()
        label = f"{strategy}/{run_id}" + (f"（{summary['tag']}）" if summary.get("tag") else "")
        frames.append((label, s))
        series_out.append(
            {
                "strategy": strategy,
                "run_id": run_id,
                "tag": summary.get("tag") or "",
                "label": label,
                "debug": bool(summary.get("debug_symbols")),
                "total_return": summary.get("total_return"),
                "max_drawdown": summary.get("max_drawdown"),
            }
        )

    if not frames:
        return {"dates": [], "series": [], "count": 0}

    union = pd.DatetimeIndex(sorted(set().union(*[set(s.index) for _, s in frames])))
    if len(union) > max_points:
        step = math.ceil(len(union) / max_points)
        kept = list(union[::step])
        if kept[-1] != union[-1]:
            kept.append(union[-1])
        union = pd.DatetimeIndex(kept)

    for i, (_, s) in enumerate(frames):
        if i >= len(series_out):
            break
        aligned = s.reindex(union).ffill()
        # 只在 [首日, 末日] 区间内保留；区间外留 None 让前端断线
        aligned = aligned.where((union >= s.index[0]) & (union <= s.index[-1]))
        series_out[i]["nav"] = _col_to_list(aligned)
        series_out[i]["start_date"] = s.index[0].strftime("%Y-%m-%d")
        series_out[i]["end_date"] = s.index[-1].strftime("%Y-%m-%d")

    return {
        "dates": [d.strftime("%Y-%m-%d") for d in union],
        "series": series_out,
        "count": len(series_out),
    }
