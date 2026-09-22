"""因子注册表加载器 —— 读 `factors/registry.yaml`，动态解析实现并做契约校验。

为什么要有这一层（而不是让 calc / service 各自读 YAML）
-----------------------------------------------------
1. **单一入口**：YAML 的解析、校验、缓存只在一处，三处读三份迟早漂移。
2. **契约可校验**：`impl` 是字符串路径，写错了只有在真正调用的那一刻才炸。
   本模块把"能否 import""签名能不能接住 params""返回是不是和输入等长的 Series"
   全部前移到 `check()`，让错误在**自检阶段**暴露，而不是跑到一半。
3. **一个坏因子不该拖垮整张列表页**：`load_registry()` 对**结构性错误**
   （重名、未知分类、direction 非法）直接抛 `ConfigError`（这是配置写错了，
   必须立刻看见）；而"实现能否加载"这类问题降级为 `check()` 的逐项状态，
   列表页照样能显示这名因子并把它标红（与模块 1 自检标红同构）。

★ 定义与状态的边界（同模块 1 纪律）：
    定义 → 本模块（来自 YAML）
    状态 → `data/factor/_manifest.json` + parquet 文件（来自 calc.py）
    **不写数据库**。原 `factor_registry` 表已删除，见 sql/schema.sql 第 3 节。
"""
from __future__ import annotations

import importlib
import inspect
import re
import threading
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml

from app.core.errors import ConfigError, NotFoundError
from app.core.paths import FACTOR_REGISTRY_FILE

#: 因子名格式：小写字母开头，只允许小写字母/数字/下划线。同时是产物文件名
NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

#: 合法的 direction
DIRECTIONS = (-1, 0, 1)

#: 分类状态
CATEGORY_STATUSES = ("ready", "planned", "blocked")

#: 分类顺序（UI 展示顺序；未列出的分类排最后）
CATEGORY_ORDER = ("daily", "combo", "fundamental", "minute")


# ======================================================================
# 数据结构
# ======================================================================
@dataclass(frozen=True)
class CategoryDef:
    key: str
    cname: str
    status: str
    desc: str = ""
    note: str = ""

    @property
    def available(self) -> bool:
        return self.status == "ready"

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "cname": self.cname,
            "status": self.status,
            "available": self.available,
            "desc": self.desc,
            "note": self.note,
        }


@dataclass(frozen=True)
class FactorDef:
    """一个因子的定义（全部来自 YAML，无一处来自代码或数据库）。"""

    name: str
    cname: str
    category: str
    direction: int
    impl: str
    params: dict[str, Any] = field(default_factory=dict)
    data_deps: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    #: 量纲说明（前端标在坐标轴上）。**定义在 YAML，不在服务/前端按因子名硬编码。**
    unit: str = ""
    desc: str = ""
    enabled: bool = True

    @property
    def impl_module(self) -> str:
        module, _, _func = self.impl.rpartition(".")
        return module

    @property
    def impl_func(self) -> str:
        return self.impl.rpartition(".")[2]

    @property
    def params_key(self) -> str:
        """参数指纹（用于判断"同一个因子换了参数"是否需要重算）。"""
        if not self.params:
            return ""
        items = sorted((str(k), self.params[k]) for k in self.params)
        return ",".join(f"{k}={v}" for k, v in items)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "cname": self.cname,
            "category": self.category,
            "direction": self.direction,
            "impl": self.impl,
            "params": self.params,
            "params_key": self.params_key,
            "data_deps": list(self.data_deps),
            "tags": list(self.tags),
            "unit": self.unit,
            "desc": self.desc,
            "enabled": self.enabled,
        }


# ======================================================================
# 加载与缓存（按 mtime，改 YAML 不用重启）
# ======================================================================
_cache_lock = threading.Lock()
_cache: dict[str, Any] = {"key": None, "registry": None}


def _file_key(path: Path) -> tuple[str, int, int] | None:
    if not path.exists():
        return None
    st = path.stat()
    return (str(path), st.st_mtime_ns, st.st_size)


def load_registry(*, force: bool = False) -> dict[str, Any]:
    """读取并校验注册表。返回 `{"categories": [...], "factors": [...], "version": n}`。

    Raises:
        ConfigError: 文件缺失，或存在**结构性错误**（重名 / 未知分类 / direction 非法 …）
    """
    path = FACTOR_REGISTRY_FILE
    key = _file_key(path)
    if key is None:
        raise ConfigError(
            f"因子注册表不存在：{path}",
            detail={"path": str(path)},
        )

    with _cache_lock:
        if not force and _cache["key"] == key and _cache["registry"] is not None:
            return _cache["registry"]

    registry = _parse_registry(path)

    with _cache_lock:
        _cache["key"] = key
        _cache["registry"] = registry
    return registry


def _parse_registry(path: Path) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"因子注册表 YAML 解析失败：{path} —— {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"因子注册表顶层必须是映射（dict），收到 {type(raw).__name__}")

    # ---------- 分类 ----------
    categories: list[CategoryDef] = []
    seen_cat: set[str] = set()
    for i, item in enumerate(raw.get("categories") or []):
        if not isinstance(item, dict) or not item.get("key"):
            raise ConfigError(f"categories[{i}] 缺少 key 字段")
        key = str(item["key"]).strip()
        if key in seen_cat:
            raise ConfigError(f"分类 key 重复：{key}")
        status = str(item.get("status") or "planned").strip()
        if status not in CATEGORY_STATUSES:
            raise ConfigError(
                f"分类 {key} 的 status 非法：{status!r}（可选 {CATEGORY_STATUSES}）"
            )
        seen_cat.add(key)
        categories.append(
            CategoryDef(
                key=key,
                cname=str(item.get("cname") or key),
                status=status,
                desc=str(item.get("desc") or "").strip(),
                note=str(item.get("note") or "").strip(),
            )
        )
    if not categories:
        raise ConfigError("因子注册表没有任何 category 定义")

    def _cat_rank(c: CategoryDef) -> tuple[int, str]:
        try:
            return (CATEGORY_ORDER.index(c.key), "")
        except ValueError:
            return (len(CATEGORY_ORDER), c.key)

    categories.sort(key=_cat_rank)
    known_cats = {c.key for c in categories}

    # ---------- 因子 ----------
    factors: list[FactorDef] = []
    seen_name: set[str] = set()
    for i, item in enumerate(raw.get("factors") or []):
        where = f"factors[{i}]"
        if not isinstance(item, dict):
            raise ConfigError(f"{where} 必须是映射")
        name = str(item.get("name") or "").strip()
        if not name:
            raise ConfigError(f"{where} 缺少 name")
        if not NAME_RE.match(name):
            raise ConfigError(
                f"因子名非法：{name!r} —— 必须匹配 {NAME_RE.pattern}"
                f"（小写字母开头，只含小写字母/数字/下划线）"
            )
        if name in seen_name:
            raise ConfigError(f"因子名重复：{name}")
        seen_name.add(name)

        category = str(item.get("category") or "").strip()
        if category not in known_cats:
            raise ConfigError(
                f"因子 {name} 的 category 未在 categories 中声明：{category!r}"
                f"（已知 {sorted(known_cats)}）"
            )

        direction = item.get("direction", 0)
        if isinstance(direction, bool) or direction not in DIRECTIONS:
            raise ConfigError(
                f"因子 {name} 的 direction 非法：{direction!r}（可选 {DIRECTIONS}）"
            )

        impl = str(item.get("impl") or "").strip()
        if not impl or "." not in impl:
            raise ConfigError(
                f"因子 {name} 的 impl 非法：{impl!r} —— 需要 `<模块>.<函数>` 形式，"
                f"如 factors.impl.daily.ret_n"
            )

        params = item.get("params") or {}
        if not isinstance(params, dict):
            raise ConfigError(f"因子 {name} 的 params 必须是映射，收到 {type(params).__name__}")

        factors.append(
            FactorDef(
                name=name,
                cname=str(item.get("cname") or name),
                category=category,
                direction=int(direction),
                impl=impl,
                params=dict(params),
                data_deps=tuple(str(x) for x in (item.get("data_deps") or ())),
                tags=tuple(str(x) for x in (item.get("tags") or ())),
                unit=str(item.get("unit") or "").strip(),
                desc=str(item.get("desc") or "").strip(),
                enabled=bool(item.get("enabled", True)),
            )
        )

    if not factors:
        raise ConfigError("因子注册表没有任何 factor 定义")

    return {
        "version": int(raw.get("version") or 1),
        "path": str(path),
        "categories": categories,
        "factors": factors,
        "by_name": {f.name: f for f in factors},
    }


def invalidate_cache() -> None:
    """清掉缓存（测试与显式刷新用）。"""
    with _cache_lock:
        _cache["key"] = None
        _cache["registry"] = None


# ======================================================================
# 查询
# ======================================================================
def categories() -> list[CategoryDef]:
    return list(load_registry()["categories"])


def list_factors(
    category: str | None = None, *, include_disabled: bool = False
) -> list[FactorDef]:
    out = [
        f
        for f in load_registry()["factors"]
        if (include_disabled or f.enabled) and (category is None or f.category == category)
    ]
    return out


def get_factor(name: str) -> FactorDef:
    """按名取因子定义。

    Raises:
        NotFoundError: 该名字未注册（API 层要映射成 404，不能给 500 ——
                       "你问的因子不存在"和"服务器配置坏了"是两回事）
    """
    key = str(name or "").strip()
    factor = load_registry()["by_name"].get(key)
    if factor is None:
        known = sorted(load_registry()["by_name"])
        raise NotFoundError(
            f"未注册的因子：{name!r}（已知 {len(known)} 个：{known[:8]}{'…' if len(known) > 8 else ''}）",
            detail={"available": known},
        )
    return factor


def category_of(key: str) -> CategoryDef | None:
    for c in categories():
        if c.key == key:
            return c
    return None


def available_categories() -> list[str]:
    """status == ready 的分类（只有这些能计算）。"""
    return [c.key for c in categories() if c.available]


# ======================================================================
# 实现解析与契约校验
# ======================================================================
def resolve_impl(defn: FactorDef) -> Callable[..., pd.Series]:
    """按 `impl` 动态导入实现函数。

    Raises:
        ConfigError: 模块不存在 / 属性不存在 / 不是可调用对象
    """
    module_name, func_name = defn.impl_module, defn.impl_func
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ConfigError(
            f"因子 {defn.name} 的实现模块无法导入：{module_name} —— {exc}",
            detail={"factor": defn.name, "impl": defn.impl},
        ) from exc
    fn = getattr(module, func_name, None)
    if fn is None:
        raise ConfigError(
            f"因子 {defn.name} 的实现函数不存在：{module_name}.{func_name}",
            detail={"factor": defn.name, "impl": defn.impl},
        )
    if not callable(fn):
        raise ConfigError(
            f"因子 {defn.name} 的 impl 不是可调用对象：{defn.impl}（{type(fn).__name__}）"
        )
    return fn


def signature_error(defn: FactorDef, fn: Callable[..., Any]) -> str | None:
    """校验签名能否接住 YAML 声明的 params。合规返回 None，否则返回中文原因。

    规则：
      1. 至少有一个位置参数（行情表 `q`）
      2. YAML 里的每个参数名都能被函数接住（显式命名，或函数带 `**kwargs`）
      3. 除第一个参数外，**没有**无默认值的必填参数漏在 YAML 之外
         —— 否则调用时必然 TypeError，而那是在跑了几分钟之后才炸的
    """
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError) as exc:  # pragma: no cover - 内建/C 函数
        return f"无法获取签名：{exc}"

    params = list(sig.parameters.values())
    if not params:
        return "函数没有任何参数，至少要接受行情表 `q`"

    first = params[0]
    if first.kind not in (first.POSITIONAL_ONLY, first.POSITIONAL_OR_KEYWORD):
        return f"第一个参数 {first.name!r} 必须是位置参数（kind={first.kind.name}）"

    positional_names = {
        p.name
        for p in params[1:]
        if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
    }
    has_var_kw = any(p.kind == p.VAR_KEYWORD for p in params)

    unknown = [k for k in defn.params if k not in positional_names and not has_var_kw]
    if unknown:
        return (
            f"YAML 声明的参数 {unknown} 不在函数签名中"
            f"（函数接受 {sorted(positional_names) or ['<无>']}）"
        )

    required = [
        p.name
        for p in params[1:]
        if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
        and p.default is inspect.Parameter.empty
    ]
    missing = [k for k in required if k not in defn.params]
    if missing:
        return f"函数有必填参数 {missing}，但 YAML 的 params 里没有提供"

    return None


def smoke_frame(n_dates: int = 40, n_symbols: int = 3) -> pd.DataFrame:
    """构造一个**最小但真实形态**的合成行情帧，用于冒烟调用实现。

    ⚠️ 为什么不复用 tests 的夹具：单元测试夹具服务于"验证数值"，
       这里是"验证能不能跑通"，必须自包含、且能被 CLI 直接调用。
    """
    dates = pd.bdate_range("2024-01-01", periods=n_dates)
    rows = []
    for si in range(n_symbols):
        sym = f"sh.60000{si}"
        base = 10.0 + si
        rng = np.random.default_rng(20260920 + si)
        steps = 1.0 + rng.normal(0, 0.02, size=n_dates)
        close = base * np.cumprod(steps)
        for i, d in enumerate(dates):
            pre = close[i - 1] if i else base
            rows.append(
                {
                    "date": d,
                    "symbol": sym,
                    "open": close[i] * 0.995,
                    "high": close[i] * 1.02,
                    "low": close[i] * 0.98,
                    "close": close[i],
                    "preclose": pre,
                    "volume": 1_000_000.0,
                    "amount": 1.0e7,
                    "turn": 1.5,
                    "pct_chg": (close[i] / pre - 1.0) * 100.0,
                    "trade_status": 1,
                    "is_st": 0,
                }
            )
    return pd.DataFrame(rows).sort_values(["symbol", "date"], kind="stable").reset_index(drop=True)


def smoke_test(defn: FactorDef) -> str | None:
    """跑一遍最小集合，校验"能 import → 能签名 → 能调用 → 返回等长 Series"。

    返回 None 表示通过，否则是中文原因。
    这是防"写了但调用就崩"的最低成本手段（本项目第一大失效模式）。

    ★ 数据替换钩子（2026-09-22 新增）：
      依赖**本地落盘数据**的因子（目前只有基本面：数据在
      `data/raw/fundamental/`，由回补工具事先下载）没法在一份合成行情上跑通 ——
      体检结论会退化成"看盘上恰好有什么数据"，换台机器可能忽然全红或全绿。
      约定：若实现模块提供 `synthetic_data()` 上下文管理器，自检时就用它替换
      数据读取。**刻意不按分类硬编码**（不是 `if category == "fundamental"`）——
      将来任何"依赖外部落盘数据"的因子只要照这个约定实现即可，loader 不用改。
    """
    try:
        fn = resolve_impl(defn)
    except ConfigError as exc:
        return exc.message

    err = signature_error(defn, fn)
    if err:
        return err

    q = smoke_frame()
    try:
        module = importlib.import_module(defn.impl_module)
        maker = getattr(module, "synthetic_data", None)
        with (maker() if callable(maker) else nullcontext()):
            out = fn(q, **defn.params)
    except Exception as exc:
        return f"调用失败（{type(exc).__name__}）：{exc}"

    if not isinstance(out, pd.Series):
        return f"返回值应为 pd.Series，实际是 {type(out).__name__}"
    if len(out) != len(q):
        return f"返回长度 {len(out)} 与输入 {len(q)} 不一致（必须逐行对齐）"
    if not out.index.equals(q.index):
        return "返回的 index 与输入不一致（必须逐行对齐，否则数值会错配到别的股票）"
    if out.notna().sum() == 0:
        return "全部为 NaN —— 窗口长度可能大于数据长度，或实现有误"
    return None


def check(*, with_smoke: bool = True) -> list[dict[str, Any]]:
    """逐因子自检。**单项失败不影响其它项**，供 CLI 与启动自检使用。"""
    rows: list[dict[str, Any]] = []
    for defn in list_factors(include_disabled=True):
        error = smoke_test(defn) if with_smoke else None
        cat = category_of(defn.category)
        rows.append(
            {
                "name": defn.name,
                "cname": defn.cname,
                "category": defn.category,
                "category_status": cat.status if cat else "unknown",
                "enabled": defn.enabled,
                "impl": defn.impl,
                "status": "ok" if error is None else "broken",
                "message": error or "",
            }
        )
    return rows


__all__ = [
    "CATEGORY_ORDER",
    "CATEGORY_STATUSES",
    "DIRECTIONS",
    "NAME_RE",
    "CategoryDef",
    "FactorDef",
    "available_categories",
    "categories",
    "category_of",
    "check",
    "get_factor",
    "invalidate_cache",
    "list_factors",
    "load_registry",
    "resolve_impl",
    "signature_error",
    "smoke_frame",
    "smoke_test",
]
