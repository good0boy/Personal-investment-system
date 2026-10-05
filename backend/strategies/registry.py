"""策略注册表：名字 → 实现类。

为什么要一张注册表
----------------
回测 CLI、参数寻优（`sweep.py`）、前端详情页都要按**名字**拿到策略，
而"名字"又是目录名 `data/backtest/{name}/` 的一部分。散落的
`if name == "momentum"` 会让新增策略时要改三个地方。
"""
from __future__ import annotations

from app.core.errors import NotFoundError
from strategies.base import BaseStrategy
from strategies.impl.momentum import MomentumStrategy
from strategies.impl.momentum_v13 import MomentumV13Strategy

#: 策略名 → 类。新增策略在这里加一行即可。
#: ⚠️ `momentum_v13` 是 **v1.3 规则的冻结快照**（见该模块 docstring），
#:    只为历史对照回测/寻优存在，不再维护。
REGISTRY: dict[str, type[BaseStrategy]] = {
    MomentumStrategy.name: MomentumStrategy,
    MomentumV13Strategy.name: MomentumV13Strategy,
}


def strategy_names() -> list[str]:
    return sorted(REGISTRY)


def get_strategy_cls(name: str) -> type[BaseStrategy]:
    cls = REGISTRY.get(str(name).strip().lower())
    if cls is None:
        raise NotFoundError(
            f"未知策略：{name}（可选：{strategy_names()}）",
            detail={"available": strategy_names()},
        )
    return cls


def get_strategy(name: str, params: dict | None = None) -> BaseStrategy:
    return get_strategy_cls(name)(params)


def describe(name: str) -> dict:
    """策略元信息（用于登记到 `strategy_registry` 与前端展示）。"""
    cls = get_strategy_cls(name)
    inst = cls()
    return {
        "id": cls.name,
        "name": cls.name,
        "version": cls.version,
        "impl_path": f"strategies/impl/{cls.name}.py",
        "description": (cls.__doc__ or "").strip().splitlines()[0] if cls.__doc__ else "",
        "params": inst.params,
    }


__all__ = [
    "REGISTRY",
    "describe",
    "get_strategy",
    "get_strategy_cls",
    "strategy_names",
]
