"""JSON 净化：把 NaN / Infinity 递归替换成 None。

为什么需要这个
------------
Python 的 `json.dumps` 默认 `allow_nan=True`，会把 `float('nan')` 写成
字面量 `NaN`、把 `float('inf')` 写成 `Infinity`。**这两者都不是合法 JSON**
（RFC 8259 没有它们）。Python 的 `json.loads` 出于兼容会读回来，所以本地
自测发现不了；但浏览器端的 `JSON.parse` 会直接抛
`SyntaxError: Unexpected token 'N'` —— 整个响应解析失败、页面白屏。

本项目为什么必然踩到
------------------
指标层是"尽力而为"的：`annual_return = nav[-1] ** (1/n_years) - 1` 在净值
非正时是 NaN；`_safe_ratio` 之外的裸除法（夏普、Calmar、盈亏比）在
分母为 0 时也可能产出 NaN。只要有一处没兜住，**整个 `metrics.json` 变成
非法 JSON**，详情页就整个打不开 —— 一个 NaN 毁掉整页，代价极不对称。

所以：**写产物和返回响应两条路径都要过一遍净化**。

numpy 标量为什么也在这里处理
--------------------------
最初的设计写着「numpy 交给调用方在外层转」（怕本模块反向依赖 numpy）。
**这个判断被现实证伪了**：两个调用方各自踩了一遍 ——
`strategies/base.py` 只好自己加 `_json_default`，`strategies/sweep.py` 的 `_j()`
则在**跑完 12 组网格之后**生成报告时炸
`TypeError: Object of type int64 is not JSON serializable`，白等一场。

根因很隐蔽：`np.float64` **是** Python `float` 的子类，所以浮点参数一直正常；
`np.int64` **不是** `int` 的子类，于是只有整数参数（如 `hold_days_max`）出事 ——
这正是"部分能过"型的坑，最容易漏测。

现在统一在这里收口：numpy 只在**真的遇到**时才惰性导入（`numpy` 对它保持软依赖）。
"""
from __future__ import annotations

import math
from typing import Any

#: 惰性缓存的 numpy 标量/数组类型；`None` = 还没探测过，`()` = 没有 numpy
_NUMPY_TYPES: tuple[type, ...] | None = None


def _numpy_types() -> tuple[type, ...]:
    """拿到需要特殊处理的 numpy 类型（numpy 不可用时返回空，保持软依赖）。"""
    global _NUMPY_TYPES
    if _NUMPY_TYPES is None:
        try:
            # 惰性导入是有意的：让本模块对 numpy 保持**软依赖**
            import numpy as np  # noqa: PLC0415
        except ImportError:  # pragma: no cover - 环境里必然有 numpy
            _NUMPY_TYPES = ()
        else:
            _NUMPY_TYPES = (np.integer, np.floating, np.bool_, np.ndarray)
    return _NUMPY_TYPES


def is_bad_float(value: Any) -> bool:
    """是不是 NaN / ±Infinity（只认 float，整数与 None 不在此列）。"""
    return isinstance(value, float) and not math.isfinite(value)


def _from_numpy(value: Any, *, depth: int) -> Any:
    """numpy 值 → JSON 可序列化的原生类型。

    - 整数 / 布尔 → `int` / `bool`（**不能**退化成 `str`，否则报告里出现
      `{"hold_days_max": "20"}` 这种引号包着的数字，前端排序就坏了）
    - 浮点 → `float`，NaN/Inf 归成 `None`
    - 数组 → 走 `tolist()` 后递归，顺带把里面的 NaN 也清掉
    """
    import numpy as np  # noqa: PLC0415

    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        v = float(value)
        return v if math.isfinite(v) else None
    if isinstance(value, np.ndarray):
        return json_safe(value.tolist(), depth=depth + 1)
    return str(value)  # pragma: no cover - 兜底，正常不该走到


def json_safe(value: Any, *, depth: int = 0) -> Any:
    """递归把 NaN / Infinity 换成 None、numpy 标量换成原生类型，其余原样返回。

    - dict / list / tuple 递归处理（tuple 转 list，JSON 没有 tuple）
    - numpy 标量/数组按上面 `_from_numpy` 的规则转换
    - 深度上限 32 层：异常深的嵌套直接返回 None，防止构造出的递归对象把栈打爆
    """
    if depth > 32:
        return None
    if is_bad_float(value):
        return None
    np_types = _numpy_types()
    if np_types and isinstance(value, np_types):
        return _from_numpy(value, depth=depth)
    if isinstance(value, dict):
        return {k: json_safe(v, depth=depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v, depth=depth + 1) for v in value]
    return value
