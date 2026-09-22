"""JSON 净化测试。

★ 本文件的存在理由：`jsonutils.py` **踩过两次同样的坑都没被发现**，
  因为它此前一个测试都没有。

第一次：`metrics.json` 里混进 NaN → `json.dumps` 写出字面量 `NaN` →
        浏览器 `JSON.parse` 直接 `SyntaxError`，整个详情页白屏。
第二次：`sweep.py` 网格结果来自 DataFrame → 整数参数是 `numpy.int64` →
        裸 `json.dumps` 抛 `TypeError`，而且是在**跑完 12 组之后**生成报告时才炸。

第二次尤其阴：`np.float64` **是** Python `float` 的子类所以浮点一路正常，
`np.int64` **不是** `int` 的子类 —— 于是"浮点参数能过、整数参数炸"，
局部自测（只试浮点）根本发现不了。这里就是把这个洞钉死的地方。
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from app.core.jsonutils import is_bad_float, json_safe


# ----------------------------------------------------------------------
# NaN / Infinity
# ----------------------------------------------------------------------
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_bad_float_goes_to_none(bad: float) -> None:
    assert json_safe(bad) is None


def test_is_bad_float_only_flags_non_finite_floats() -> None:
    assert is_bad_float(float("nan"))
    assert is_bad_float(float("inf"))
    # 整数、None、有限浮点都不算"坏"
    assert not is_bad_float(0)
    assert not is_bad_float(None)
    assert not is_bad_float(1.5)


def test_nested_structure_fully_sanitised() -> None:
    """嵌套里的 NaN 也要清掉 —— 只清顶层的实现等于没清。"""
    payload = {
        "nav": [1.0, float("nan"), 1.2],
        "meta": {"sharpe": float("inf"), "note": "ok"},
        "deep": [{"x": [float("-inf")]}],
    }
    out = json_safe(payload)
    assert out == {"nav": [1.0, None, 1.2], "meta": {"sharpe": None, "note": "ok"},
                   "deep": [{"x": [None]}]}
    # 核心断言：净化后必须是**合法 JSON**
    json.dumps(out, allow_nan=False)


def test_tuple_becomes_list() -> None:
    """JSON 没有 tuple，转成 list；顺带要合法。"""
    out = json_safe((1, float("nan"), 3))
    assert out == [1, None, 3]


def test_depth_limit_truncates_deep_branch() -> None:
    """超深分支被截断成 None，防止构造出的递归对象把栈打爆。

    ⚠️ 断言怎么写才叫严：
       `json_safe` 从 depth 0 起算，截断点在 **32 层以内**（`depth > 32` 才返回
       None），所以前几层仍是正常 dict —— 写 `assert json_safe(node) is None`
       或 `assert out["next"] is None` 都是错的（我前后错了两次，验证脚本报 FAIL
       但每次都是断言错，不是代码错）。
       正确做法：**沿链向下走，验证它在有限步内一定终止于 None**。
    """
    node: dict = {"keep": "ok"}
    cur = node
    for _ in range(40):
        cur["next"] = {}
        cur = cur["next"]

    out = json_safe(node)
    assert out["keep"] == "ok"      # 浅层数据照常保留
    assert out["next"] is not None  # 紧邻的一层还在

    # 沿链下钻：必须在 40 步内遇到 None，且遇 None 的深度不超过 33
    node_, depth = out, 0
    while node_ is not None:
        assert isinstance(node_, dict), f"第 {depth} 层不是 dict：{type(node_)}"
        node_ = node_.get("next")
        depth += 1
        assert depth <= 34, "深度截断没生效，链一直没终止"
    assert depth <= 34


# ----------------------------------------------------------------------
# numpy 标量 —— 第二次踩坑的正面战场
# ----------------------------------------------------------------------
@pytest.mark.parametrize("dtype,expected", [
    (np.int64, 20),
    (np.int32, 20),
    (np.int16, 20),
    (np.uint8, 20),
    (np.intp, 20),
])
def test_numpy_integer_becomes_int_not_str(dtype, expected: int) -> None:
    """`numpy.int64` → `int`。

    ★ 关键：**不能**退化成字符串。`default=str` 会给出 `"20"`，
      报告里就出现 `{"hold_days_max": "20"}` —— 前端一排序就错。
    """
    out = json_safe(dtype(20))
    assert out == expected
    assert isinstance(out, int) and not isinstance(out, bool)


def test_numpy_float_becomes_float() -> None:
    out = json_safe(np.float64(0.5))
    assert out == 0.5
    assert isinstance(out, float)


def test_numpy_nan_inf_goes_to_none() -> None:
    assert json_safe(np.float64("nan")) is None
    assert json_safe(np.float32("inf")) is None


def test_numpy_bool_becomes_bool() -> None:
    out = json_safe(np.bool_(True))
    assert out is True or out == True  # noqa: E712 - 兼容 np.bool_ 比较
    assert isinstance(out, bool)


def test_numpy_array_recurses() -> None:
    """数组要 `tolist()` 后继续递归 —— 里面的 NaN 也得清。"""
    out = json_safe(np.array([1.0, np.nan, 3.0]))
    assert out == [1.0, None, 3.0]


# ----------------------------------------------------------------------
# 端到端：DataFrame 行（sweep 崩掉的那条路径）
# ----------------------------------------------------------------------
def test_dataframe_row_is_serialisable() -> None:
    """Excel 式的复现：`df.iterrows()` 取一行 → `json.dumps`。

    这就是 `sweep.py` 生成 summary 时崩掉的最小复现。
    parquet 读回来的整数列是 `np.int64`，裸 dumps 必炸。
    """
    df = pd.DataFrame([{"hold_days_max": 20, "take_profit_net": 0.1},
                       {"hold_days_max": 60, "take_profit_net": 0.15}])
    row = df.iloc[0]
    out = json_safe({k: row[k] for k in df.columns})
    text = json.dumps(out, ensure_ascii=False, allow_nan=False)
    assert json.loads(text) == {"hold_days_max": 20, "take_profit_net": 0.1}


def test_parquet_roundtrip_row_is_serialisable(tmp_path) -> None:
    """真·落盘往返：写 parquet → 读回 → 取行 → 序列化。

    parquet 往返会改变 dtype（这正是 `int64` 出现的真实来源），
    用纯内存 DataFrame 造不出来。
    """
    path = tmp_path / "results.parquet"
    pd.DataFrame([{"hold_days_max": 20, "take_profit_net": 0.1,
                   "annual_return": 0.12, "is_spike": False}]).to_parquet(
        path, index=False
    )
    df = pd.read_parquet(path)
    row = df.iloc[0]
    assert isinstance(row["hold_days_max"], np.integer)  # 前置事实：确实是 np.int64

    out = json_safe({k: row[k] for k in df.columns})
    json.dumps(out, ensure_ascii=False, allow_nan=False)
    assert out["hold_days_max"] == 20 and out["is_spike"] is False


def test_numpy_missing_degrades_gracefully(monkeypatch) -> None:
    """numpy 不可用时降级为纯原生处理 —— 保持软依赖，不能把调用方带崩。

    这里直接重置模块缓存来模拟"没有 numpy"的环境。
    """
    from app.core import jsonutils

    monkeypatch.setattr(jsonutils, "_NUMPY_TYPES", ())
    # numpy 标量不再被识别，但原生类型与 NaN 处理照旧
    assert jsonutils.json_safe(float("nan")) is None
    assert jsonutils.json_safe({"a": [1, 2]}) == {"a": [1, 2]}
    assert jsonutils.json_safe(1.5) == 1.5
