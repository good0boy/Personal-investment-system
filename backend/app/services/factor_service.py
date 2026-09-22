"""模块 2 · 因子库的**只读**服务层 —— 扫盘 + 清单为真相源。

本模块是「文件契约」的消费端，与 `backtest_service` / `sweep_service` 同构：

    定义  ←  factors/registry.yaml      （人工维护，本模块只读）
    状态  ←  data/factor/_manifest.json  （factors/calc.py 写）
    明细  ←  data/factor/{category}/{name}.parquet
    分析  ←  data/factor/analysis/{name}.json  （factors/analyze.py 写）

**不写数据库、不在请求里做计算**（架构纪律 4/5）。前端每一次请求都只是
读几个小文件或跑一条聚合 SQL，页面响应是毫秒级。

★ 为什么 `factors.*` 是延迟导入：
  `factors/calc.py` 与 `factors/analyze.py` 是 CLI 侧入口，导入链上会拉起
  pandas/numpy 乃至 `strategies.engine`。服务层只在真正需要时才导入，
  避免"打开后端就顺手把 CLI 依赖全拉起来"。
  （同 `backtest_service` 延迟导入 `strategies.registry` 的处置。）
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any

import pandas as pd

from app.core.errors import AppError, DataNotReadyError, InvalidParamError, NotFoundError
from app.core.jsonutils import json_safe
from app.core.logging import logger
from app.core.paths import relative_to_project
from app.services import data_store

#: 净值/序列降采样后的最大点数（与回测服务保持一致）
DEFAULT_MAX_POINTS = 2000

#: 扫盘缓存 TTL（秒）—— 与 backtest/sweep 服务一致
_SCAN_TTL = 15.0
_scan_lock = threading.Lock()
_scan_cache: dict[str, Any] = {"at": 0.0, "by_name": {}}

#: 分析产物摘要缓存（列表页轻量版）—— 同 TTL，避免每个列表请求都重读 JSON
_analysis_lock = threading.Lock()
_analysis_cache: dict[str, Any] = {"at": 0.0, "loaded": False, "by_name": {}}

#: 单次请求允许的最大标的数（多标的叠图，超过就画不清了）
MAX_SYMBOLS_PER_QUERY = 8

#: 截面分布直方图的默认分箱数
DEFAULT_BINS = 40


# ======================================================================
# 定义 / 清单读取
# ======================================================================
def _loader() -> Any:
    from factors import loader  # noqa: PLC0415

    return loader


def _manifest() -> Any:
    from factors import manifest  # noqa: PLC0415

    return manifest


def _manifest_entries() -> dict[str, Any]:
    """带 TTL 缓存的清单读取。

    ⚠️ 只缓存**清单**（一个小 JSON），不缓存明细：
       明细走 DuckDB 直接查文件，改了 parquet 立刻生效。
    """
    now = time.monotonic()
    with _scan_lock:
        if _scan_cache["by_name"] and (now - _scan_cache["at"]) < _SCAN_TTL:
            return _scan_cache["by_name"]
    entries = _manifest().read_manifest()["factors"]
    with _scan_lock:
        _scan_cache["at"] = now
        _scan_cache["by_name"] = entries
    return entries


def invalidate_cache() -> None:
    """清扫盘缓存（测试用）。"""
    with _scan_lock:
        _scan_cache["at"] = 0.0
        _scan_cache["by_name"] = {}
    with _analysis_lock:
        _analysis_cache["at"] = 0.0
        _analysis_cache["loaded"] = False
        _analysis_cache["by_name"] = {}


def _status_of(defn: Any, entries: dict[str, Any]) -> dict[str, Any]:
    """把「YAML 定义」与「清单状态」合成前端的展示模型。

    三态区分（**不能混为一谈**）：
      · `calculated`  —— 产物文件在 + 清单有条目
      · `orphan`      —— 清单有条目但**文件不在**（被手工删了/换机器拷漏了）
                         → 必须显式报出来，否则前端显示"已计算"而点进去读不到数据
      · `not_calculated` —— 没算过
    """
    file_path = _manifest().factor_file(defn)
    has_file = file_path.exists()
    entry = entries.get(defn.name)

    state = "not_calculated"
    if entry and has_file:
        state = "calculated"
    elif entry and not has_file:
        state = "orphan"
    elif not entry and has_file:
        # 文件在、清单没记（清单被删/被换机器拷漏）→ 文件为准，但状态未知
        state = "unindexed"

    warnings: list[str] = []
    if state == "orphan":
        warnings.append("清单里有记录但产物文件不存在（可能被手工删除或未随目录一起拷贝）")
    if state == "unindexed":
        warnings.append(
            "产物文件存在但清单里没有记录，行数/覆盖区间未知；"
            "请重跑 python -m factors.calc --force"
        )

    analysis_file = analysis_path(defn.name)
    return {
        **defn.to_dict(),
        "state": state,
        "calculated": state in ("calculated", "unindexed"),
        "rows": (entry or {}).get("rows"),
        "non_null": (entry or {}).get("non_null"),
        "nan_ratio": (entry or {}).get("nan_ratio"),
        "symbols": (entry or {}).get("symbols"),
        "date_start": (entry or {}).get("interval", {}).get("start"),
        "date_end": (entry or {}).get("interval", {}).get("end"),
        "computed_at": (entry or {}).get("computed_at") or "",
        "seconds": (entry or {}).get("seconds"),
        "size_bytes": (entry or {}).get("size_bytes"),
        "file": relative_to_project(file_path),
        "debug_symbols": (entry or {}).get("debug_symbols"),
        "warnings": warnings + list((entry or {}).get("warnings") or []),
        "has_analysis": analysis_file.exists(),
        # 列表页轻量摘要（TTL 缓存，O(1) 查询无 IO）；详情接口会用完整版覆盖它
        "analysis_summary": _analysis_summaries().get(defn.name) or {},
    }


def analysis_path(name: str, horizon: int = 1) -> Any:
    from factors import analyze  # noqa: PLC0415

    return analyze.analysis_path(name, horizon)


def _scan_analysis_files() -> dict[str, dict[str, Any]]:
    """扫分析产物目录，读全部 JSON（按产物内 name 归键；损坏文件跳过并留痕）。

    ⚠️ 只收 `settings.horizon == 1` 的产物：h5/h10 等持有期扫描产物与 h1 同名
    （产物内 name 相同），混进来会把列表摘要口径从 h1 悄悄顶掉。
    """
    from factors import analyze  # noqa: PLC0415

    out: dict[str, dict[str, Any]] = {}
    for path in sorted(analyze.FACTOR_ANALYSIS_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(f"分析产物 {path.name} 读取失败，列表摘要跳过：{exc}")
            continue
        if not (isinstance(data, dict) and data.get("name")):
            continue
        if int((data.get("settings") or {}).get("horizon") or 1) != 1:
            continue
        out[str(data["name"])] = data
    return out


def _analysis_summaries() -> dict[str, dict[str, Any]]:
    """列表页用的**轻量**分析摘要（TTL 缓存）。

    ⚠️ 与 `get_factor` 的完整版摘要不同：这里不带 group_summary / long_short /
    monotonicity（列表用不上，塞进去只是白费带宽）。前端列表只消费
    `analysis_summary.ic.mean / .ic.ir / .settings.horizon` 三个数字。
    """
    now = time.monotonic()
    with _analysis_lock:
        if _analysis_cache["loaded"] and (now - _analysis_cache["at"]) < _SCAN_TTL:
            return _analysis_cache["by_name"]

    out: dict[str, dict[str, Any]] = {}
    for name, data in _scan_analysis_files().items():
        ic = data.get("ic") or {}
        out[name] = {
            "generated_at": data.get("generated_at") or "",
            "interval": data.get("interval") or {},
            "settings": data.get("settings") or {},
            "ic": {k: v for k, v in ic.items() if k != "series"},
            "warnings": data.get("warnings") or [],
        }

    with _analysis_lock:
        _analysis_cache["at"] = now
        _analysis_cache["loaded"] = True
        _analysis_cache["by_name"] = out
    return out


# ======================================================================
# 列表
# ======================================================================
def list_factors(category: str | None = None) -> dict[str, Any]:
    """因子列表：分类元数据 + 每个因子的定义与状态。

    ★ 分类里 `status != ready` 的分类（minute / fundamental）**照样返回**，
      并带上 `note` 说明为什么没有因子 —— 前端据此显示「待开发 / 数据源未接入」。
      直接把它们藏起来会让人以为"系统漏了这两类"。
    """
    loader = _loader()
    entries = _manifest_entries()
    cats = loader.categories()
    if category and loader.category_of(category) is None:
        raise InvalidParamError(
            f"未知分类：{category}",
            detail={"available": [c.key for c in cats]},
        )

    factors_all: list[dict[str, Any]] = []
    for defn in loader.list_factors(include_disabled=True):
        try:
            factors_all.append(_status_of(defn, entries))
        except Exception as exc:
            logger.warning(f"因子 {defn.name} 状态读取失败，降级展示：{exc}")
            factors_all.append({**defn.to_dict(), "state": "error", "warnings": [str(exc)]})

    factors = (
        factors_all if not category else [f for f in factors_all if f["category"] == category]
    )

    cat_payload = []
    for c in cats:
        n_all = len(loader.list_factors(c.key, include_disabled=True))
        # ⚠️ 计数一律基于**未按 category 过滤**的 `factors_all`。
        #    用过滤后的列表会让"选 daily 时另外两类的已计算数显示为 0"，
        #    看起来像"数据丢了"。这类"过滤后被下游误用"是统计口径漂移的常见来源。
        n_done = len([f for f in factors_all if f["category"] == c.key and f.get("calculated")])
        cat_payload.append({**c.to_dict(), "factor_count": n_all, "calculated_count": n_done})

    manifest_meta = _manifest().read_manifest()
    return {
        "categories": cat_payload,
        "factors": factors,
        "total": len(factors),
        "calculated": len([f for f in factors if f.get("calculated")]),
        "manifest_updated_at": manifest_meta.get("updated_at") or "",
        "registry_file": relative_to_project(loader.FACTOR_REGISTRY_FILE),
        "notes": [
            "因子【定义】的唯一来源是 factors/registry.yaml；【状态】来自 data/factor/_manifest.json。",
            "因子由 CLI 计算（python -m factors.calc），前端只读产物，不触发计算。",
        ],
    }


# ======================================================================
# 详情
# ======================================================================
def get_factor(name: str) -> dict[str, Any]:
    """因子详情：定义 + 状态 + 分析摘要（分层/IC 的结论数字）。"""
    loader = _loader()
    defn = loader.get_factor(name)  # 未注册 → NotFoundError
    entries = _manifest_entries()
    payload = _status_of(defn, entries)

    summary: dict[str, Any] = {}
    apath = analysis_path(defn.name)
    if apath.exists():
        data = _read_analysis(defn.name)
        summary = {
            "generated_at": data.get("generated_at") or "",
            "interval": data.get("interval") or {},
            "settings": data.get("settings") or {},
            "ic": {k: v for k, v in (data.get("ic") or {}).items() if k != "series"},
            "group_summary": (data.get("layered") or {}).get("group_summary") or [],
            "monotonicity": (data.get("layered") or {}).get("monotonicity"),
            "long_short": (data.get("layered") or {}).get("long_short") or {},
            "warnings": data.get("warnings") or [],
        }
    payload["analysis_summary"] = summary
    return payload


# ======================================================================
# 时序
# ======================================================================
def get_series(
    name: str,
    *,
    symbols: list[str] | None = None,
    start: str | None = None,
    end: str | None = None,
    max_points: int = DEFAULT_MAX_POINTS,
) -> dict[str, Any]:
    """因子时序。

    两种模式（**默认为全市场截面聚合**）：
      · 不给 symbols → 返回每日的 `mean / median / q25 / q75 / count`
        （"因子在全市场上的整体走势"，也是唯一能看出**因子整体漂移**的视角）
      · 给 symbols   → 返回这些标的各自的因子值（多标的对比）

    ⚠️ 为什么默认是聚合而不是"第一只股票"：
      单只股票的因子折线**看着有信息量，实际没有**（它只是价格走势的影子）。
      真正有信息的是"截面分布随时间怎么移动"。
    """
    loader = _loader()
    defn = loader.get_factor(name)
    if symbols and len(symbols) > MAX_SYMBOLS_PER_QUERY:
        raise InvalidParamError(
            f"一次最多对比 {MAX_SYMBOLS_PER_QUERY} 只标的，收到 {len(symbols)} 只"
        )

    try:
        if symbols:
            df = data_store.read_factor(
                defn.name, start, end, list(symbols), defn.category
            )
            if df.empty:
                raise DataNotReadyError(
                    f"因子 {defn.name} 在 {start or '起点不限'} ~ {end or '今天'} "
                    f"区间内没有数据 —— 请确认区间与标的代码格式（如 sh.600519）"
                )
            dates = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
            wide = pd.DataFrame({"date": dates, "symbol": df["symbol"].astype(str),
                                 "value": pd.to_numeric(df["value"], errors="coerce")})
            pivot = wide.pivot_table(index="date", columns="symbol", values="value", aggfunc="last")
            pivot = pivot.sort_index().reset_index()
            n_raw = len(pivot)
            down = data_store.downsample_series(
                pivot, x_col="date", y_cols=list(pivot.columns[1:2]), max_points=max_points
            )
            cols = list(pivot.columns[1:])
            return json_safe(
                {
                    "name": defn.name,
                    "cname": defn.cname,
                    "category": defn.category,
                    "direction": defn.direction,
                    "mode": "symbols",
                    "unit": defn.unit,
                    "dates": [str(v) for v in down["date"].tolist()],
                    "series": {c: [None if pd.isna(v) else float(v) for v in down[c]] for c in cols},
                    "downsampled_from": int(n_raw),
                    "symbols": cols,
                }
            )

        agg = data_store.read_factor_aggregate(defn.name, start, end, category=defn.category)
        if agg.empty:
            raise DataNotReadyError(
                f"因子 {defn.name} 在 {start or '起点不限'} ~ {end or '今天'} 区间内没有数据"
            )
        n_raw = len(agg)
        down = data_store.downsample_series(agg, x_col="date", y_cols=["mean"], max_points=max_points)
        return json_safe(
            {
                "name": defn.name,
                "cname": defn.cname,
                "category": defn.category,
                "direction": defn.direction,
                "mode": "cross_section_aggregate",
                "unit": defn.unit,
                "dates": pd.to_datetime(down["date"]).dt.strftime("%Y-%m-%d").tolist(),
                "series": {
                    c: [None if pd.isna(v) else float(v) for v in down[c]]
                    for c in ("mean", "median", "q25", "q75")
                },
                "count": [int(v) if not pd.isna(v) else 0 for v in down["count"]],
                "downsampled_from": int(n_raw),
                "mode_note": "未指定标的 → 展示全市场每日截面聚合（均值/中位数/25~75 分位）",
            }
        )
    except FileNotFoundError as exc:  # pragma: no cover - 极端竞态
        raise DataNotReadyError(f"因子 {defn.name} 的产物文件读不到：{exc}") from exc


# ======================================================================
# 截面
# ======================================================================
def get_cross_section(
    name: str,
    *,
    day: str | None = None,
    bins: int = DEFAULT_BINS,
    top_n: int = 10,
) -> dict[str, Any]:
    """某交易日的因子截面：直方图 + 分位数 + 极值标的。

    默认取**该因子产物的最后一天**（而不是"今天"）—— 今天是周末/节假日或
    数据还没回补时，按今天查会返回空表，前端看起来像"因子坏了"。
    """
    if bins < 5 or bins > 200:
        raise InvalidParamError(f"bins 应在 5~200 之间，收到 {bins}")

    loader = _loader()
    defn = loader.get_factor(name)
    entries = _manifest_entries()
    entry = entries.get(defn.name) or {}

    if day is None:
        day = (entry.get("interval") or {}).get("end")
    if day is None:
        agg = data_store.read_factor_aggregate(defn.name, category=defn.category)
        if agg.empty:
            raise DataNotReadyError(f"因子 {defn.name} 没有可用产物，请先执行 python -m factors.calc")
        day = pd.Timestamp(agg["date"].max()).strftime("%Y-%m-%d")

    df = data_store.read_factor(defn.name, day, day, category=defn.category)
    if df.empty:
        raise DataNotReadyError(
            f"因子 {defn.name} 在 {day} 没有数据 —— 该日可能非交易日，或超出产物覆盖区间"
            f"（{entry.get('interval', {}).get('start')} ~ {entry.get('interval', {}).get('end')}）"
        )

    values = pd.to_numeric(df["value"], errors="coerce").dropna()
    if values.empty:
        raise DataNotReadyError(f"因子 {defn.name} 在 {day} 的值全部为空")

    sym = df.loc[values.index, "symbol"].astype(str)
    q = values.quantile([0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99])

    # 直方图范围取 1%~99% 分位：因子尾部常有极端值（如 20 日动量遇到重组股），
    # 直接按 min~max 分箱会让 99% 的样本挤在第一个箱子里面目全非。
    lo, hi = float(q.loc[0.01]), float(q.loc[0.99])
    if not (hi > lo):
        lo, hi = float(values.min()), float(values.max())
        if not (hi > lo):
            hi = lo + 1.0
    clipped = values.clip(lo, hi)
    width = (hi - lo) / bins
    counts, _edges = _histogram(clipped.to_numpy(), lo, hi, bins)

    ordered = values.sort_values()
    bottom = [
        {"symbol": str(sym.loc[i]), "value": float(ordered.iloc[k])}
        for k, i in enumerate(ordered.index[:top_n])
    ]
    top = [
        {"symbol": str(sym.loc[i]), "value": float(ordered.iloc[-1 - k])}
        for k, i in enumerate(ordered.index[::-1][:top_n])
    ]

    return json_safe(
        {
            "name": defn.name,
            "cname": defn.cname,
            "category": defn.category,
            "direction": defn.direction,
            "day": day,
            "unit": defn.unit,
            "count": int(values.size),
            "stats": {
                "mean": float(values.mean()),
                "std": float(values.std(ddof=1)) if values.size > 1 else 0.0,
                "min": float(values.min()),
                "max": float(values.max()),
                "median": float(values.median()),
            },
            "quantiles": {f"p{int(k * 100):02d}": float(v) for k, v in q.items()},
            "histogram": {
                "bins": [round(float(lo + i * width), 8) for i in range(bins + 1)],
                "counts": counts,
                "clip_note": (
                    f"直方图区间取 1%~99% 分位 [{lo:.6g}, {hi:.6g}]，"
                    f"超出该范围的 {int((values < lo).sum() + (values > hi).sum())} 个样本已并入两端"
                ),
            },
            "extremes": {"bottom": bottom, "top": top},
        }
    )


def _histogram(arr: Any, lo: float, hi: float, bins: int) -> tuple[list[int], list[float]]:
    """等宽分箱（numpy 的 histogram 只返回计数，边界自己算以便前端对齐）。"""
    import numpy as np  # noqa: PLC0415

    counts, edges = np.histogram(arr, bins=bins, range=(lo, hi))
    return [int(x) for x in counts], [float(x) for x in edges]


# ======================================================================
# 分层 / IC（读离线分析产物）
# ======================================================================
def _read_analysis(name: str) -> dict[str, Any]:
    path = analysis_path(name)
    if not path.exists():
        raise NotFoundError(
            f"因子 {name} 还没有做有效性分析。请先执行："
            f"python -m factors.analyze --name {name}",
            detail={"factor": name, "expected_file": relative_to_project(path)},
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise AppError(f"因子 {name} 的分析产物损坏：{exc}") from exc
    if not isinstance(data, dict):
        raise AppError(f"因子 {name} 的分析产物结构异常（不是 JSON 对象）")
    return data


def get_layered(name: str) -> dict[str, Any]:
    """分层回测结果（分组累计净值 + 换手 + 成本估算）。"""
    loader = _loader()
    defn = loader.get_factor(name)
    data = _read_analysis(defn.name)
    layered = data.get("layered") or {}
    return json_safe(
        {
            "name": defn.name,
            "cname": defn.cname,
            "direction": defn.direction,
            "interval": data.get("interval") or {},
            "settings": data.get("settings") or {},
            "group_summary": layered.get("group_summary") or [],
            "monotonicity": layered.get("monotonicity"),
            "long_short": layered.get("long_short") or {},
            "dates": layered.get("dates") or [],
            "nav": layered.get("nav") or [],
            "nav_columns": layered.get("nav_columns") or [],
            "warnings": data.get("warnings") or [],
            "notes": data.get("notes") or [],
        }
    )


def get_ic(name: str, *, max_points: int = DEFAULT_MAX_POINTS) -> dict[str, Any]:
    """RankIC 序列 + 摘要（IC 均值 / ICIR / t 值 / 胜率）。"""
    loader = _loader()
    defn = loader.get_factor(name)
    data = _read_analysis(defn.name)
    ic = data.get("ic") or {}
    series = ic.get("series") or []

    if len(series) > max_points:
        # IC 是**逐日**序列，降采样会掩盖"连续多日为负"这类形态；
        # 所以不用 LTTB，改用等间隔抽样并**保留正负号信息**（前端画柱状图）。
        step = max(1, len(series) // max_points)
        series = series[::step]

    return json_safe(
        {
            "name": defn.name,
            "cname": defn.cname,
            "direction": defn.direction,
            "interval": data.get("interval") or {},
            "settings": data.get("settings") or {},
            "summary": {k: v for k, v in ic.items() if k != "series"},
            "series": series,
            "warnings": data.get("warnings") or [],
            "notes": data.get("notes") or [],
        }
    )


# ======================================================================
# 标的检索
# ======================================================================
def search_symbols(q: str | None = None, *, limit: int = 50) -> list[str]:
    """本地已有日线数据的标的（按代码子串过滤，供前端选择器用）。"""
    if limit < 1 or limit > 500:
        raise InvalidParamError(f"limit 应在 1~500 之间，收到 {limit}")
    all_syms = data_store.list_quote_symbols()
    key = (q or "").strip().lower()
    if not key:
        return all_syms[:limit]
    hits = [s for s in all_syms if key in s.lower()]
    return hits[:limit]


# ======================================================================
# 自检
# ======================================================================
def check(*, with_smoke: bool = True) -> dict[str, Any]:
    """因子定义/实现自检（导入 + 签名 + 冒烟调用），并叠加落盘状态。

    这是**唯一允许在请求里跑实现代码**的入口（只在显式自检时调用，且跑的是
    40 行合成数据、毫秒级）。用途与模块 1 的 `check_catalog` 对标：
    让"YAML 改错了""实现签名对不上"在页面上标红，而不是等到跑计算时才炸。
    """
    loader = _loader()
    rows = loader.check(with_smoke=with_smoke)
    entries = _manifest_entries()
    for r in rows:
        entry = entries.get(r["name"]) or {}
        r["state"] = "calculated" if entry else "not_calculated"
        r["date_start"] = (entry.get("interval") or {}).get("start")
        r["date_end"] = (entry.get("interval") or {}).get("end")
        r["rows"] = entry.get("rows")
        r["has_analysis"] = analysis_path(r["name"]).exists()
    bad = [r for r in rows if r["status"] != "ok"]
    return {
        "total": len(rows),
        "ok": len(rows) - len(bad),
        "broken": len(bad),
        "items": rows,
        "checked_at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


__all__ = [
    "DEFAULT_BINS",
    "DEFAULT_MAX_POINTS",
    "MAX_SYMBOLS_PER_QUERY",
    "analysis_path",
    "check",
    "get_cross_section",
    "get_factor",
    "get_ic",
    "get_layered",
    "get_series",
    "invalidate_cache",
    "list_factors",
    "search_symbols",
]
