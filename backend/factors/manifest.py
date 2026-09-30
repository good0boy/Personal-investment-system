"""因子产物清单（`data/factor/_manifest.json`）—— 因子【状态】的唯一来源。

为什么是文件而不是数据库
----------------------
与模块 3 的回测产物、参数寻优产物同一条纪律：**磁盘为唯一真相源**。
`factor_registry` 表（原设计含 impl_path / params_json）已于 2026-09-20 删除 ——
那些是【定义】类字段，与 `registry.yaml` 天然重复，留着必然出现
"改了 DB 却不知道 YAML 才是生效的那份"（`datasource_capability` 死表的原样翻版）。

本文件只存**状态**：算了没、多少行、覆盖到哪天、什么参数、多久算完。
定义一个字都不存 —— 定义全部来自 `factors/registry.yaml`。

为什么不能"靠扫 parquet 现算状态"
--------------------------------
`rows` / `non_null` / `date_start` / `date_end` 都要真扫一遍数据。
单因子是全市场 ~4800 只 × 1600 天 ≈ 700 万行，列表页 15 个因子就要扫 7000 万行
—— 页面秒开变成十几秒。清单文件是 O(1)。清单缺失时由读取方降级为"文件存在但状态未知"，
**不假装它是完整的**。
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from app.core.logging import logger
from app.core.paths import FACTOR_MANIFEST_FILE

MANIFEST_VERSION = 1

_write_lock = threading.Lock()


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def factor_file(defn_or_name: Any, category: str | None = None) -> Path:
    """因子产物路径。入参可以是 FactorDef（推荐）或 (name, category)。

    ★ 路径拼接**不在这里实现** —— 走 `data_store.factor_path()` 单一实现。
      两处各拼一遍必然出现"清单指向 A、实际写到 B"的静默错位。
    """
    from app.services.data_store import factor_path  # noqa: PLC0415 - 避免循环导入

    if category is None:
        name = getattr(defn_or_name, "name", None) or str(defn_or_name)
        category = getattr(defn_or_name, "category", None)
        if category is None:
            raise ValueError("只给 name 时必须同时给 category")
    else:
        name = str(defn_or_name)
    return factor_path(name, str(category))


def read_manifest() -> dict[str, Any]:
    """读清单。文件不存在/损坏 → 返回空壳（**不抛异常**）。

    为什么容错：清单丢了不该让整张因子列表页崩掉 —— parquet 还在，
    读者可以降级成"文件存在但状态未知"。这与回测产物读半截 JSON 的处置一致。
    """
    path = FACTOR_MANIFEST_FILE
    if not path.exists():
        return _empty()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(f"因子清单解析失败，按空清单处理：{path}（{exc}）")
        return _empty()
    if not isinstance(data, dict) or not isinstance(data.get("factors"), dict):
        logger.warning(f"因子清单结构异常，按空清单处理：{path}")
        return _empty()
    return data


def _empty() -> dict[str, Any]:
    return {"version": MANIFEST_VERSION, "updated_at": "", "factors": {}}


def write_manifest(data: dict[str, Any]) -> None:
    """原子写清单（tmp + os.replace，避免半截文件）。"""
    data["version"] = MANIFEST_VERSION
    data["updated_at"] = _now()
    path = FACTOR_MANIFEST_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with _write_lock:
        try:
            tmp.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(tmp, path)
        except OSError as exc:  # pragma: no cover - 磁盘问题
            tmp.unlink(missing_ok=True)
            logger.error(f"因子清单写入失败：{path} —— {exc}")


def upsert(name: str, entry: dict[str, Any]) -> None:
    """写入/更新单个因子的状态条目（保留其它条目）。

    ⚠️ **必须是合并而不是整体覆盖**：`calc --name ret_3` 只算了一个因子，
    整体覆盖会把其余 10 个因子的状态抹掉 —— 表现为"列表页突然全部显示未计算"，
    而文件明明都在。这类"部分运行破坏全局状态"的 bug 最难排查。
    """
    data = read_manifest()
    data["factors"][str(name)] = entry
    write_manifest(data)


def remove(name: str) -> bool:
    """删除某因子的状态条目（产物文件不存在时，状态必须跟着走，否则谎报算过）。"""
    data = read_manifest()
    if str(name) not in data["factors"]:
        return False
    data["factors"].pop(str(name), None)
    write_manifest(data)
    return True


def entry_of(name: str) -> dict[str, Any] | None:
    return read_manifest()["factors"].get(str(name))


def stale_reasons(
    defn: Any,
    entry: dict[str, Any] | None,
    *,
    start: str | None,
    end: str | None,
) -> list[str]:
    """判断某因子是否需要重算，返回中文原因列表（空 = 可复用已有产物）。

    判据全部是"**用户看得懂且能反驳**"的：文件在不在、参数变没变、区间盖不盖得住。
    刻意不去比 parquet 的 mtime 与行情 mtime —— 那会让每次行情回补后
    "所有因子都过期"，而实际上大部分因子的区间根本不受影响。
    """
    reasons: list[str] = []
    if entry is None:
        reasons.append("清单里没有该因子的记录")
        return reasons

    if not factor_file(defn).exists():
        reasons.append("产物文件不存在")
        return reasons

    if str(entry.get("params_key") or "") != defn.params_key:
        reasons.append(
            f"参数已变更（清单 {entry.get('params_key') or '<无>'} → "
            f"当前 {defn.params_key or '<无>'}）"
        )

    interval = entry.get("interval") or {}
    if end and (interval.get("end") or "") < str(end):
        reasons.append(f"已有产物只覆盖到 {interval.get('end') or '<未知>'}，需要到 {end}")
    if start and (interval.get("start") or "") > str(start):
        reasons.append(f"已有产物从 {interval.get('start') or '<未知>'} 开始，需要从 {start}")
    return reasons


__all__ = [
    "MANIFEST_VERSION",
    "entry_of",
    "factor_file",
    "read_manifest",
    "remove",
    "stale_reasons",
    "upsert",
    "write_manifest",
]
