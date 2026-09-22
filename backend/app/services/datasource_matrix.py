"""数据源能力矩阵 —— 从 YAML 读取，不做任何硬编码。

设计要点（技术架构 6.1.6 节）
-----------------------------
1. **能力矩阵是配置**：唯一来源是 `config/datasource_capabilities/*.yaml`。
   要加数据源、改能力标注，只改 YAML，不改代码，更不要动数据库
   （原 `datasource_capability` 表已删，理由见 sql/schema.sql）。
2. **`_` 开头的文件是元数据，不是数据源**：`_capabilities.yaml` 定义能力项的
   中文名与分组。它必须被跳过，否则页面上会多出一个名叫 "_capabilities" 的"数据源"。
3. **实测与未实测必须区分显示**：`verified: false` 的源（依据官方文档整理，
   未在本环境真实调用）会整列标灰。把抄来的文档当成实测结论展示，
   比不展示更危险 —— 将来换源时会踩空。
4. **`groups[].items` 必须写全**：`_capability_keys_in_order()` 靠它决定能力项
   顺序，前端也靠它把能力项映射到分组标签。漏写 items 不会报错，只会让
   排序静默退化成字母序、前端分组全变"其他"（2026-09-18 修过一次）。

⚠️ **改完 `_capabilities.yaml` 必须重启后端才生效**（已知行为，主人 2026-09-18
   决定不修）：下面的 `signature` 只统计**数据源文件**（`_` 前缀被跳过），
   所以这个元数据文件的 mtime 不在签名里 —— 缓存不会失效，接口会继续返回
   旧的表头/分组/排序。看到"改了 YAML 但页面没变"时，先重启再排查。

排序规则：按「关键能力覆盖数」降序。这样主源（字段最全的）自然排在前面，
不需要在 YAML 里手工维护顺序，加新源也不用改这里。
"""
from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.core.logging import logger
from app.core.paths import CAPABILITY_META_FILE, DATASOURCE_CAPABILITY_DIR

#: 实测结论的保鲜期：超过这个天数提示复测
VERIFY_STALE_DAYS = 90

#: 能力矩阵的内存缓存（按文件 mtime 失效）
_cache: dict[str, Any] = {"signature": None, "data": None}


def _read_yaml(path: Path) -> dict[str, Any] | None:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning(f"解析数据源能力文件失败 {path.name}：{exc}")
        return None
    return raw if isinstance(raw, dict) else None


def _load_meta() -> dict[str, Any]:
    """读能力项元数据（中文名、说明、分组）。文件缺失时退化为空结构。"""
    if not CAPABILITY_META_FILE.exists():
        logger.warning(f"能力项元数据缺失：{CAPABILITY_META_FILE.name}（表头将退化为英文 key）")
        return {"groups": [], "capabilities": {}}

    data = _read_yaml(CAPABILITY_META_FILE) or {}
    return {
        "groups": data.get("groups") or [],
        "capabilities": data.get("capabilities") or {},
    }


def _capability_keys_in_order(meta: dict[str, Any], sources: list[dict[str, Any]]) -> list[str]:
    """能力项展示顺序：先按 groups 声明的顺序，再把没进分组的追加在后面。

    为什么不让前端自己排：YAML 里 `capabilities` 是 dict，展示顺序应当由
    人工按重要性决定（关键能力靠前），而不是由字母序决定。
    """
    available: set[str] = set()
    for s in sources:
        available.update((s.get("capabilities") or {}).keys())

    ordered: list[str] = []
    for g in meta["groups"]:
        for key in g.get("items") or []:
            if key in available and key not in ordered:
                ordered.append(key)
    for key in sorted(available):
        if key not in ordered:
            ordered.append(key)
    return ordered


def _is_stale(verified_at: str | None) -> bool:
    if not verified_at:
        return False
    try:
        d = date.fromisoformat(str(verified_at)[:10])
    except ValueError:
        return False
    return (date.today() - d) > timedelta(days=VERIFY_STALE_DAYS)


def _build_source(raw: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
    caps: dict[str, Any] = raw.get("capabilities") or {}

    # 关键能力覆盖：这几项决定了它能否用于本项目的回测
    critical_total = [
        k for k, v in meta["capabilities"].items() if v.get("critical")
    ]
    critical_hit = [
        k for k in critical_total
        if (caps.get(k) or {}).get("supported")
    ]

    verified = bool(raw.get("verified", False))
    verified_at = raw.get("verified_at")

    return {
        "name": raw.get("name"),
        "display_name": raw.get("display_name") or raw.get("name"),
        "role": raw.get("role", ""),
        "package": raw.get("package", ""),
        "version_pin": raw.get("version_pin", ""),
        "version_note": raw.get("version_note", ""),
        "homepage": raw.get("homepage", ""),
        "license": raw.get("license", ""),
        "symbol_format": raw.get("symbol_format", ""),
        "symbol_note": raw.get("symbol_note", ""),
        "stability": raw.get("stability", {}) or {},
        "pitfalls": raw.get("pitfalls", []) or [],
        "capabilities": caps,
        # ---- 实测状态 ----
        "verified": verified,
        "verified_at": verified_at,
        "verified_by": raw.get("verified_by"),
        "verified_note": raw.get("verified_note"),
        "stale": _is_stale(verified_at) if verified else False,
        # ---- 汇总指标 ----
        "supported_count": sum(1 for v in caps.values() if (v or {}).get("supported")),
        "capability_count": len(caps),
        "critical_keys": critical_total,
        "critical_hit": critical_hit,
        "critical_count": len(critical_hit),
        "critical_total": len(critical_total),
        "critical_ratio": round(len(critical_hit) / len(critical_total), 3) if critical_total else 0.0,
    }


def load_matrix(*, force: bool = False) -> dict[str, Any]:
    """加载能力矩阵（含 mtime 缓存）。

    Returns:
        {capability_meta, capability_keys, sources, source_count,
         verified_count, critical_total}
    """
    files = sorted(
        f for f in DATASOURCE_CAPABILITY_DIR.glob("*.yaml")
        if not f.name.startswith("_")           # ★ 元数据文件不是数据源
    )
    signature = tuple((f.name, f.stat().st_mtime_ns) for f in files)

    if not force and _cache["data"] is not None and _cache["signature"] == signature:
        return _cache["data"]

    meta = _load_meta()
    sources: list[dict[str, Any]] = []
    for f in files:
        raw = _read_yaml(f)
        if not raw:
            continue
        sources.append(_build_source(raw, meta))

    # 排序：关键能力覆盖数降序 → 已实测优先 → 名称
    sources.sort(
        key=lambda s: (-s["critical_count"], not s["verified"], s["name"] or "")
    )

    data = {
        "capability_meta": meta,
        "capability_keys": _capability_keys_in_order(meta, sources),
        "sources": sources,
        "source_count": len(sources),
        "verified_count": sum(1 for s in sources if s["verified"]),
        "critical_total": len([k for k, v in meta["capabilities"].items() if v.get("critical")]),
        "verify_stale_days": VERIFY_STALE_DAYS,
    }

    _cache["signature"] = signature
    _cache["data"] = data
    return data


def clear_cache() -> None:
    """清空缓存（测试或手动刷新时用）。"""
    _cache["signature"] = None
    _cache["data"] = None


@lru_cache(maxsize=1)
def _noop() -> None:  # pragma: no cover - 占位以保证 lru_cache 导入不被判未使用
    return None
