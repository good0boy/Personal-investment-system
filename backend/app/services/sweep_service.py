"""参数寻优（sweep）的**只读**服务层 —— 扫盘为真相源。

与 `backtest_service` 的关系
---------------------------
回测产物：`data/backtest/{strategy}/{run_id}/`
寻优产物：`data/sweep/{sweep_id}/`

一个 sweep = 一次网格扫描，每组参数跑一次完整回测，所以 sweep 目录里存的是
**索引**（组 → run_id）而不是回测产物本身：净值/交易仍在 backtest 目录下，
两处不重复存同一份数据（否则迟早不一致）。

产物契约（`data/sweep/{sweep_id}/`）
----------------------------------
- `grid.json`     参数网格定义 + 区间 + 目标指标
- `results.parquet` 一行一组：参数 + 指标 + run_id + 敏感性标记
- `runs.json`      组序号 → run_id（轻量，供前端跳转）
- `summary.md`     自动生成的结论（含"高原 vs 尖峰"判断）

★ 为什么 sweep 结果也要**落盘成文件**而不是只写 DB：
  与模块 3 同一条理由 —— CLI 手跑的结果、换机器拷过来的结果都要能被前端看到，
  不需要"登记"这一步。DB 只做兜底。
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any

import pandas as pd

from app.core.errors import NotFoundError
from app.core.jsonutils import json_safe
from app.core.logging import logger
from app.core.paths import SWEEP_DIR

#: 扫盘缓存 TTL（秒）—— 与 backtest_service 一致
_SCAN_TTL = 15.0
_scan_lock = threading.Lock()
_scan_cache: dict[str, Any] = {"at": 0.0, "rows": []}

#: sweep 目录下必需的文件（缺任一 → 视为未完成）
REQUIRED_FILES = ("grid.json", "results.parquet")


def _read_json(path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(f"sweep 文件读取失败 {path}：{exc}")
        return None


def sweep_dir(sweep_id: str):
    """sweep 目录。**拒绝路径穿越** —— sweep_id 来自 URL。"""
    sid = str(sweep_id or "").strip()
    if not sid or "/" in sid or "\\" in sid or sid in (".", ".."):
        raise NotFoundError(f"非法的 sweep_id：{sweep_id!r}")
    root = (SWEEP_DIR / sid).resolve()
    if SWEEP_DIR.resolve() not in root.parents:
        raise NotFoundError(f"非法的 sweep_id：{sweep_id!r}")
    return root


def list_sweeps(*, force: bool = False) -> list[dict[str, Any]]:
    """扫描全部 sweep 产物，返回摘要列表（按开始时间倒序，带 TTL 缓存）。"""
    now = time.monotonic()
    with _scan_lock:
        if not force and _scan_cache["rows"] and (now - _scan_cache["at"]) < _SCAN_TTL:
            return list(_scan_cache["rows"])

    rows: list[dict[str, Any]] = []
    if SWEEP_DIR.exists():
        for d in sorted(SWEEP_DIR.iterdir()):
            if not d.is_dir():
                continue
            grid = _read_json(d / "grid.json")
            if grid is None:
                continue
            missing = [f for f in REQUIRED_FILES if not (d / f).exists()]
            n_combos = len(grid.get("combos") or [])
            summary = _load_results(d)
            rows.append(
                {
                    "sweep_id": d.name,
                    "strategy": grid.get("strategy") or "",
                    "tag": (grid.get("meta") or {}).get("tag") or "",
                    "description": grid.get("description") or "",
                    "objective": grid.get("objective") or "annual_return",
                    "interval_name": (grid.get("interval") or {}).get("name") or "",
                    "start_date": (grid.get("interval") or {}).get("start"),
                    "end_date": (grid.get("interval") or {}).get("end"),
                    "combo_count": n_combos,
                    "done_count": 0 if summary is None else len(summary),
                    "is_complete": not missing and summary is not None
                    and len(summary) >= n_combos,
                    "missing_files": missing,
                    "debug_symbols": (grid.get("meta") or {}).get("symbols"),
                    "created_at": (grid.get("meta") or {}).get("created_at") or "",
                }
            )

    rows.sort(key=lambda r: (r.get("created_at") or "", r.get("sweep_id") or ""), reverse=True)
    with _scan_lock:
        _scan_cache["at"] = now
        _scan_cache["rows"] = rows
    return list(rows)


def invalidate_cache() -> None:
    with _scan_lock:
        _scan_cache["at"] = 0.0
        _scan_cache["rows"] = []


def _load_results(d) -> pd.DataFrame | None:
    p = d / "results.parquet"
    if not p.exists():
        return None
    try:
        return pd.read_parquet(p)
    except Exception as exc:  # pragma: no cover - 损坏产物
        logger.warning(f"sweep 结果读取失败 {p}：{exc}")
        return None


# ======================================================================
# 详情
# ======================================================================
def get_sweep(sweep_id: str) -> dict[str, Any]:
    """sweep 详情：网格定义 + 每组指标 + 敏感性分析 + 结论。"""
    d = sweep_dir(sweep_id)
    if not d.exists():
        raise NotFoundError(
            f"找不到参数寻优 {sweep_id}。可用任务请查 GET /api/v1/sweep/list",
            detail={"sweep_id": sweep_id},
        )
    grid = _read_json(d / "grid.json")
    if grid is None:
        raise NotFoundError(f"sweep {sweep_id} 缺少 grid.json（未完成或已损坏）")

    results = _load_results(d)
    items: list[dict[str, Any]] = []
    if results is not None and not results.empty:
        # NaN → None，否则前端 JSON.parse 崩（与回测接口同一条纪律）
        items = [json_safe(r) for r in results.to_dict(orient="records")]

    summary_md = ""
    sm = d / "summary.md"
    if sm.exists():
        summary_md = sm.read_text(encoding="utf-8")

    runs = _read_json(d / "runs.json") or {}

    return {
        "sweep_id": d.name,
        "strategy": grid.get("strategy") or "",
        "description": grid.get("description") or "",
        "objective": grid.get("objective") or "annual_return",
        "params_grid": grid.get("params") or {},
        "param_names": grid.get("param_names") or sorted(grid.get("params") or {}),
        "sensitivity_axes": grid.get("sensitivity") or {},
        "combo_count": len(grid.get("combos") or []),
        "interval": grid.get("interval") or {},
        "meta": grid.get("meta") or {},
        "items": items,
        "runs": runs,
        "summary_md": summary_md,
        "grid": grid,
    }
