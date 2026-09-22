"""模块 1 · 接口目录服务 —— 「目录即数据，试跑即文档」。

职责边界（避免双份真相，与 datasource_capability 死表的教训一致）
----------------------------------------------------------------
- 接口【定义】（名称/入参/出参/示例参数）的唯一来源是
  `config/catalog/*.yaml`（人工维护，可 diff、可版本管理）。**本服务不把它们写库。**
- 自检【状态】（ok/broken/上次验证时间）的唯一来源是 `meta.db.catalog_interface`。
  YAML 不写状态。
- API 返回 = YAML 定义 ⊕ DB 状态。

试跑（try_run）是全系统唯一"接受任意参数并触发真实网络调用"的入口，
因此必须过四道闸：
    1. 参数白名单 —— 只允许该接口 YAML 里声明的参数名
    2. 类型转换与校验 —— 按 YAML 声明的 type 转换；symbol 类参数按
       symbol_format 校验格式（akshare 的代码格式有 5 种，见附录 A.0）
    3. 数据源路由 —— akshare 走 `akshare_client.call()`（自带函数存在性
       与签名校验）；baostock 走**方法白名单**（绝不提供任意调用能力）
    4. 限频与缓存 —— 同 IP 10 次/分钟；结果缓存 TTL 300s（重复点击 <200ms）
"""
from __future__ import annotations

import json
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import yaml

from app.core.config import settings
from app.core.errors import (
    AppError,
    DataSourceError,
    DataSourceRateLimited,
    InvalidParamError,
    NotFoundError,
)
from app.core.logging import logger
from app.core.paths import CATALOG_CATEGORY_FILES
from app.services.data_source import akshare_client, baostock_client
from app.services.data_source.base import TTLCache


# ======================================================================
# 数据结构
# ======================================================================
@dataclass
class CatalogItem:
    """一个接口的完整定义（来自 YAML）。"""

    id: str
    name: str
    category: str
    sub_category: str = ""
    datasource: str = "akshare"           # akshare | baostock
    func_name: str = ""
    description: str = ""
    symbol_format: str | None = None      # plain|suffix|sh_prefix|lower_prefix|report_period|chinese|baostock
    params: list[dict[str, Any]] = field(default_factory=list)
    returns: list[dict[str, Any]] = field(default_factory=list)
    sample_params: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    doc_url: str = ""
    #: 出参列名是否经本环境真实调用核对（false = 依据文档整理）
    returns_verified: bool = True
    returns_note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "category": self.category,
            "sub_category": self.sub_category,
            "datasource": self.datasource,
            "func_name": self.func_name,
            "description": self.description,
            "symbol_format": self.symbol_format,
            "params": self.params,
            "returns": self.returns,
            "sample_params": self.sample_params,
            "sample_code": gen_sample_code(self),
            "tags": self.tags,
            "doc_url": self.doc_url,
            "returns_verified": self.returns_verified,
            "returns_note": self.returns_note,
        }


#: 三大类元数据（key 顺序即 Tab 顺序）
CATEGORIES: dict[str, dict[str, str]] = {
    "fundamental": {"label": "基本面", "icon": "Coin"},
    "quote_realtime": {"label": "实时行情", "icon": "Odometer"},
    "quote_history": {"label": "历史行情", "icon": "TrendCharts"},
}

# ======================================================================
# YAML 加载（mtime 缓存 —— 文件没变就不重新解析）
# ======================================================================
_cache: dict[str, Any] = {"signature": None, "items": None}
_cache_lock = threading.Lock()

_SYMBOL_PATTERNS: dict[str, str] = {
    "plain": r"^\d{6}$",                        # 600519
    "suffix": r"^\d{6}\.(SH|SZ|BJ)$",           # 600519.SH
    "sh_prefix": r"^(SH|SZ)\d{6}$",             # SH600519
    "lower_prefix": r"^(sh|sz)\d{6}$",          # sh600519
    "report_period": r"^(最新|\d{8})$",          # 20230930 / 最新
    "baostock": r"^(sh|sz)\.\d{6}$",            # sh.600519
}

#: 视为"股票代码"的参数名（只有这些名按 symbol_format 校验）
SYMBOL_PARAM_NAMES = {"symbol", "stock", "code", "ts_code"}


def _parse_item(raw: dict[str, Any], category: str) -> CatalogItem:
    item_id = str(raw.get("id") or "").strip()
    if not item_id:
        raise ValueError(f"{category} 分类下存在缺少 id 的接口定义")
    return CatalogItem(
        id=item_id,
        name=str(raw.get("name") or item_id),
        category=raw.get("category") or category,
        sub_category=str(raw.get("sub_category") or ""),
        datasource=str(raw.get("datasource") or "akshare").lower(),
        func_name=str(raw.get("func_name") or raw.get("akshare_func") or ""),
        description=str(raw.get("description") or ""),
        symbol_format=raw.get("symbol_format"),
        params=list(raw.get("params") or []),
        returns=list(raw.get("returns") or []),
        sample_params=dict(raw.get("sample_params") or {}),
        tags=[str(t) for t in (raw.get("tags") or [])],
        doc_url=str(raw.get("doc_url") or ""),
        returns_verified=bool(raw.get("returns_verified", True)),
        returns_note=str(raw.get("returns_note") or ""),
    )


def load_items(*, force: bool = False) -> list[CatalogItem]:
    """加载全部接口定义（按 YAML mtime 缓存）。"""
    files = sorted(CATALOG_CATEGORY_FILES.values())
    signature = tuple(
        (f.name, f.stat().st_mtime_ns if f.exists() else -1) for f in files
    )
    with _cache_lock:
        if not force and _cache["items"] is not None and _cache["signature"] == signature:
            return _cache["items"]  # type: ignore[return-value]

        items: list[CatalogItem] = []
        seen: set[str] = set()
        for path in files:
            if not path.exists():
                logger.warning(f"接口目录文件缺失：{path.name}（跳过）")
                continue
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
            if not isinstance(raw, list):
                logger.error(f"接口目录文件格式错误（应为列表）：{path.name}")
                continue
            for entry in raw:
                item = _parse_item(entry, path.stem)
                if item.id in seen:
                    logger.error(f"接口 id 重复，后者被忽略：{item.id}")
                    continue
                seen.add(item.id)
                items.append(item)

        _cache["signature"] = signature
        _cache["items"] = items
        return items


def clear_cache() -> None:
    with _cache_lock:
        _cache["signature"] = None
        _cache["items"] = None


def get_item(item_id: str) -> CatalogItem:
    for it in load_items():
        if it.id == item_id:
            return it
    raise NotFoundError(f"接口不存在：{item_id}")


def tree() -> dict[str, Any]:
    """分类树：category → sub_category → 接口摘要。"""
    items = load_items()
    categories: list[dict[str, Any]] = []
    for key, meta in CATEGORIES.items():
        group = [it for it in items if it.category == key]
        subs: dict[str, list[dict[str, Any]]] = {}
        for it in group:
            subs.setdefault(it.sub_category or "其他", []).append(
                {"id": it.id, "name": it.name, "datasource": it.datasource}
            )
        categories.append(
            {
                "key": key,
                "label": meta["label"],
                "icon": meta["icon"],
                "count": len(group),
                "sub_groups": [
                    {"key": k, "items": v} for k, v in subs.items()
                ],
            }
        )
    unknown = [it for it in items if it.category not in CATEGORIES]
    if unknown:
        logger.warning(f"{len(unknown)} 个接口的 category 不在三大类中：{[i.id for i in unknown][:5]}")
    return {"categories": categories, "total": len(items)}


def gen_sample_code(item: CatalogItem) -> str:
    """按数据源生成可直接运行的示例代码（不手写，避免过时）。"""
    args = ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in item.sample_params.items())
    if item.datasource == "baostock":
        return (
            "from app.services.data_source import baostock_client\n\n"
            f"df = baostock_client.{item.func_name}({args})\nprint(df.head())"
        )
    return f"import akshare as ak\n\ndf = ak.{item.func_name}({args})\nprint(df.head())"


# ======================================================================
# 试跑
# ======================================================================
#: 试跑限流：同 IP 每分钟最多 10 次（规格 S1.3）
TRY_RATE_LIMIT_PER_MIN = 10
_try_rate: dict[str, deque[float]] = {}
_try_rate_lock = threading.Lock()

#: 试跑结果缓存（与 akshare 内部缓存分开 —— 这里的 key 带接口 id）
_try_cache = TTLCache(ttl=float(settings.datasource.try_cache_ttl), maxsize=256)

#: baostock 方法白名单 —— 目录页能试跑的 baostock 能力**只有这些**
_BAOSTOCK_METHODS: dict[str, tuple[str, str]] = {
    # func_name -> (签名说明, 需要转换的 symbol 参数名)
    "fetch_daily": ("symbol: sh.600519; start_date/end_date: YYYY-MM-DD; adjust: hfq|qfq|none", "symbol"),
    "fetch_index_daily": ("symbol: sh.000001; start_date/end_date: YYYY-MM-DD", "symbol"),
    "fetch_trade_dates": ("start_date/end_date: YYYY-MM-DD", None),
    "fetch_all_stock": ("day: YYYY-MM-DD（可空=最近交易日）", None),
    "fetch_index_constituents": ("index: hs300|zz500|sz50|zz1000; day: YYYY-MM-DD（可空）", None),
    "fetch_stock_industry": ("（无参数）", None),
}

_MAX_ROWS = 100
_MAX_COLS = 30


def _enforce_rate_limit(client_host: str) -> None:
    """同 IP 每分钟最多 TRY_RATE_LIMIT_PER_MIN 次。"""
    now = time.monotonic()
    with _try_rate_lock:
        dq = _try_rate.setdefault(client_host or "local", deque())
        while dq and dq[0] <= now - 60.0:
            dq.popleft()
        if len(dq) >= TRY_RATE_LIMIT_PER_MIN:
            wait = int(60 - (now - dq[0])) + 1
            raise DataSourceRateLimited(
                f"试跑太频繁（每分钟最多 {TRY_RATE_LIMIT_PER_MIN} 次），请 {wait} 秒后再试",
                detail={"retry_after_seconds": wait},
            )
        dq.append(now)


def _coerce_param(item: CatalogItem, p: dict[str, Any], value: Any) -> Any:
    """按 YAML 声明的类型转换参数值。"""
    ptype = str(p.get("type") or "str").lower()
    declared_name = str(p.get("name") or "")
    if value is None or value == "":
        # 空值：有默认值则用默认值，否则原样传（让数据源报错）
        if p.get("default") not in (None, ""):
            return p["default"]
        return value

    if ptype in ("int", "integer"):
        try:
            return int(float(value))
        except (TypeError, ValueError) as exc:
            raise InvalidParamError(
                f"参数 {declared_name} 应为整数，收到 {value!r}"
            ) from exc
    if ptype in ("float", "number"):
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise InvalidParamError(
                f"参数 {declared_name} 应为数字，收到 {value!r}"
            ) from exc
    if ptype == "bool":
        if isinstance(value, bool):
            return value
        if str(value).lower() in ("true", "1", "yes", "是"):
            return True
        if str(value).lower() in ("false", "0", "no", "否"):
            return False
        raise InvalidParamError(f"参数 {declared_name} 应为布尔值，收到 {value!r}")
    return str(value)


def _validate_symbol(item: CatalogItem, name: str, value: Any) -> None:
    """按接口声明的 symbol_format 校验代码格式。

    akshare 各接口的代码格式有 5 种（附录 A.0），混用会静默出错，
    在入口处拦住比让用户对着东财的报错猜要友好得多。
    """
    if name not in SYMBOL_PARAM_NAMES or not item.symbol_format:
        return
    pattern = _SYMBOL_PATTERNS.get(item.symbol_format)
    if pattern is None:      # chinese_name / 其他自由格式：不校验
        return
    v = str(value).strip()
    if not re.match(pattern, v):
        example = {
            "plain": "600519",
            "suffix": "600519.SH",
            "sh_prefix": "SH600519",
            "lower_prefix": "sh600519",
            "report_period": "20230930 或 最新",
            "baostock": "sh.600519",
        }.get(item.symbol_format, "")
        raise InvalidParamError(
            f"参数 {name} 的格式应为 {item.symbol_format}（示例：{example}），收到 {value!r}"
        )


def _run_akshare(item: CatalogItem, params: dict[str, Any]) -> Any:
    return akshare_client.call(item.func_name, params)


def _run_baostock(item: CatalogItem, params: dict[str, Any]) -> Any:
    """baostock 白名单执行器。

    为什么不用"任意函数调用"：baostock 的方法有串行锁与会话语义，
    暴露任意调用能力没有收益，只有风险。目录页声明了哪些方法可试跑，
    就只允许那些。
    """
    if item.func_name not in _BAOSTOCK_METHODS:
        raise InvalidParamError(
            f"baostock 方法 {item.func_name} 不在目录白名单中（可试跑：{sorted(_BAOSTOCK_METHODS)}）"
        )
    fn = getattr(baostock_client, item.func_name)
    return fn(**params)


def try_run(
    item_id: str,
    params: dict[str, Any] | None,
    *,
    client_host: str = "",
    force: bool = False,
    skip_rate_limit: bool = False,
) -> dict[str, Any]:
    """真实执行一个接口并返回截断后的结果。

    Args:
        force: 跳过缓存（前端"强制刷新"开关）
        skip_rate_limit: 跳过"同 IP 每分钟 10 次"的试跑限流。
            ★ 只允许**后端自己发起**的调用传 True（当前唯一使用者是
              `tools/check_catalog.py` 的抽样试跑）。

            为什么必须留这个口子：自检最多抽样 20 个接口（API 的 `le=20`），
            而试跑限流是 10 次/分钟 —— 第 11 个开始会被**自己的限流**拦下，
            报"试跑太频繁"。看日志像接口坏了，实际是自检撞上了面向
            前端的防滥用闸门。两个机制受众不同，不该互相干扰。

            不能反过来把"限流"只在 client_host 为空时跳过：那样 API 路径
            一旦拿不到 `request.client`（某些代理/测试客户端下会为 None），
            限流就被静默绕过。必须由调用方**显式声明**。
    """
    item = get_item(item_id)
    params = dict(params or {})

    # 1) 参数白名单
    declared = {str(p["name"]): p for p in item.params}
    unknown = sorted(set(params) - set(declared))
    if unknown:
        raise InvalidParamError(
            f"不接受参数 {unknown}。该接口声明的参数：{sorted(declared) or '（无）'}",
            detail={"unknown": unknown, "accepted": sorted(declared)},
        )

    # 2) 类型转换 + symbol 校验
    clean: dict[str, Any] = {}
    for name, value in params.items():
        p = declared[name]
        v = _coerce_param(item, p, value)
        _validate_symbol(item, name, v)
        clean[name] = v

    # 3) 缓存命中直接返回（命中不计入限流 —— 只对真实网络调用限流）
    cache_key = f"try:{item.id}:{json.dumps(clean, sort_keys=True, ensure_ascii=False, default=str)}"
    if not force:
        from app.services.data_source.base import MISS  # noqa: PLC0415

        hit = _try_cache.get(cache_key)
        if hit is not MISS:
            result = dict(hit)
            result["cached"] = True
            return result

    if not skip_rate_limit:
        _enforce_rate_limit(client_host)

    # 4) 路由执行
    t0 = time.perf_counter()
    runner = _run_baostock if item.datasource == "baostock" else _run_akshare
    try:
        raw = runner(item, clean)
    except AppError:
        raise
    except Exception as exc:
        # ★ 第三方异常（KeyError/pandas/requests…）在服务边界统一转成业务异常。
        #   实测案例：akshare 的 stock_dividend_cninfo 对无分红数据的代码
        #   内部 KeyError 崩溃 —— 不转换的话它穿透成 500，用户只看到
        #   "服务内部错误"，无法知道是接口本身的问题。
        raise DataSourceError(
            f"数据源调用失败（{item.func_name}）：{type(exc).__name__}: {str(exc)[:200]}",
            detail={"item": item.id, "func_name": item.func_name},
        ) from exc
    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)

    # 5) 截断与序列化
    result = _frame_to_payload(raw, elapsed_ms)
    _try_cache.set(cache_key, result)
    result["cached"] = False
    return result


def _frame_to_payload(raw: Any, elapsed_ms: float) -> dict[str, Any]:
    """DataFrame / list → 前端友好的 JSON。"""
    import pandas as pd  # noqa: PLC0415

    if raw is None:
        return {"columns": [], "rows": [], "shape": [0, 0], "elapsed_ms": elapsed_ms,
                "note": "接口返回 None", "truncated": {"rows": False, "cols": False}}
    if isinstance(raw, list):
        payload_rows = [str(x)[:120] for x in raw[:_MAX_ROWS]]
        return {
            "columns": [{"name": "value", "dtype": "object"}],
            "rows": [[v] for v in payload_rows],
            "shape": [len(raw), 1],
            "elapsed_ms": elapsed_ms,
            "truncated": {"rows": len(raw) > _MAX_ROWS, "cols": False},
        }

    if not isinstance(raw, pd.DataFrame):  # pragma: no cover —— 防御
        raw = pd.DataFrame(raw)

    total_rows, total_cols = int(raw.shape[0]), int(raw.shape[1])
    show_cols = list(raw.columns[:_MAX_COLS])
    sub = raw[show_cols].head(_MAX_ROWS)

    columns = [
        {"name": str(c), "dtype": str(raw[c].dtype)} for c in show_cols
    ]
    rows: list[list[str]] = []
    for _, row in sub.iterrows():
        rows.append(["" if pd.isna(v) else str(v)[:120] for v in row.tolist()])

    note = ""
    if total_cols > _MAX_COLS:
        note = f"共 {total_cols} 列，仅展示前 {_MAX_COLS} 列"
    elif total_rows > _MAX_ROWS:
        note = f"共 {total_rows} 行，仅展示前 {_MAX_ROWS} 行"

    return {
        "columns": columns,
        "rows": rows,
        "shape": [total_rows, total_cols],
        "elapsed_ms": elapsed_ms,
        "note": note,
        "truncated": {"rows": total_rows > _MAX_ROWS, "cols": total_cols > _MAX_COLS},
    }


# ======================================================================
# 自检状态（DB 读写）
# ======================================================================
def merge_status(items: list[CatalogItem]) -> list[dict[str, Any]]:
    """YAML 定义 ⊕ DB 自检状态。"""
    from app.repos import meta_repo  # noqa: PLC0415

    status_map: dict[str, dict[str, Any]] = {}
    try:
        for row in meta_repo.catalog_status_all():
            status_map[row["id"]] = row
    except AppError as exc:
        logger.warning(f"读取自检状态失败（按 unchecked 处理）：{exc.message}")

    out = []
    for it in items:
        d = it.to_dict()
        s = status_map.get(it.id)
        d["status"] = (s or {}).get("status", "unchecked")
        d["status_message"] = (s or {}).get("status_message")
        d["verified_at"] = (s or {}).get("verified_at")
        out.append(d)
    return out


def check_summary() -> dict[str, Any]:
    """自检状态汇总（供前端角标与 /check/status）。"""
    from app.repos import meta_repo  # noqa: PLC0415

    items = load_items()
    try:
        rows = meta_repo.catalog_status_all()
    except AppError:
        rows = []
    status_map = {r["id"]: r for r in rows}

    ok = broken = missing = unchecked = 0
    for it in items:
        s = (status_map.get(it.id) or {}).get("status", "unchecked")
        if s == "ok":
            ok += 1
        elif s == "broken":
            broken += 1
        elif s == "missing":
            missing += 1
        else:
            unchecked += 1

    stale_ids = [it.id for it in items if it.id not in status_map]
    removed_ids = [rid for rid in status_map if rid not in {i.id for i in items}]

    return {
        "total": len(items),
        "ok": ok,
        "broken": broken,
        "missing": missing,
        "unchecked": unchecked,
        "last_checked_at": max((r.get("verified_at") or "" for r in rows), default=None) or None,
        "stale_count": len(stale_ids),
        "removed_count": len(removed_ids),
    }
