"""`app/core/paths.py` 的路径与展示辅助测试。

★ 为什么单独建这个文件：`relative_to_project()` 的入参类型收窄过一次，
   导致"用产物清单打印结果目录"在**算完、落盘完之后**才崩
   （`AttributeError: 'str' object has no attribute 'resolve'`），
   退出码 1 但结果其实完好 —— 属于最容易被误判成"回测失败"的那类假失败。
   本文件把"同时接受 Path 与 str"钉死。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.core.paths import (
    BACKTEST_DIR,
    PROJECT_ROOT,
    relative_to_project,
)
from strategies.base import run_layout


# ======================================================================
# relative_to_project：入参类型
# ======================================================================
def test_relative_to_project_accepts_str():
    """★ str 入参必须支持。

    `summary["root"]` / `RunLayout.as_dict()["root"]` 都是 `str`
    （要能 JSON 序列化、跨进程传），而内部计算拿到的是 `Path` ——
    两种都会流到本函数。收窄成 `Path` 就会在重构后静默埋雷。
    """
    p = BACKTEST_DIR / "momentum" / "some-run"
    assert relative_to_project(str(p)) == "data/backtest/momentum/some-run"


def test_relative_to_project_accepts_path():
    p = BACKTEST_DIR / "momentum" / "some-run"
    assert relative_to_project(p) == "data/backtest/momentum/some-run"


def test_relative_to_project_str_and_path_agree():
    """两种入参必须给出一致结果 —— 否则调用点就得记住"该传哪种"。"""
    p = BACKTEST_DIR / "momentum" / "some-run"
    assert relative_to_project(p) == relative_to_project(str(p))


def test_relative_to_project_uses_forward_slashes():
    """展示路径统一正斜杠（前端与日志都按这个约定解析）。"""
    out = relative_to_project(BACKTEST_DIR / "momentum" / "x")
    assert "\\" not in out


def test_relative_to_project_outside_project_is_absolute():
    """项目外的路径原样返回（相对不出来的不硬凑）。"""
    outside = Path("C:/Windows/Temp/somewhere")
    out = relative_to_project(outside)
    assert out == str(outside)


def test_relative_to_project_project_root_itself():
    assert relative_to_project(PROJECT_ROOT) == "."


def test_relative_to_project_is_cached_but_type_agnostic():
    """缓存不得因入参类型不同而返回错值。

    `lru_cache` 的键包含类型（`Path`/`str` 是两个键），所以这里要确认
    两种键各自都给出正确结果 —— 曾经 `maxsize=1` 时交替调用会互相顶掉。
    """
    p = BACKTEST_DIR / "momentum" / "cached"
    for _ in range(3):
        assert relative_to_project(str(p)) == "data/backtest/momentum/cached"
        assert relative_to_project(p) == "data/backtest/momentum/cached"


# ======================================================================
# 与产物契约的联动：root 是 str，必须能直接喂进 relative_to_project
# ======================================================================
def test_run_layout_as_dict_root_is_str_and_printable():
    """`run_layout().as_dict()['root']` 是 str，且能直接喂 relative_to_project。

    这条正是崩溃现场的复现路径：`summary['root']` → `relative_to_project()`。
    """
    layout = run_layout("momentum", "20260918-000000-deadbeef")
    d = layout.as_dict()
    assert isinstance(d["root"], str)
    out = relative_to_project(d["root"])          # 不得抛 AttributeError
    assert out == "data/backtest/momentum/20260918-000000-deadbeef"


def test_run_layout_root_is_path():
    """反向确认：layout.root 本身仍是 Path（内部计算要用）。"""
    layout = run_layout("momentum", "r1")
    assert isinstance(layout.root, Path)
    assert layout.metrics_path == layout.root / "metrics.json"


@pytest.mark.parametrize("run_id", ["r1", "20260918-170125-5273a1dc", "mock-debug"])
def test_relative_to_project_survives_any_run_id_shape(run_id):
    out = relative_to_project(run_layout("momentum", run_id).as_dict()["root"])
    assert out == f"data/backtest/momentum/{run_id}"
