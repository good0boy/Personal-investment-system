"""看门狗子进程运行器（`app.core.subprocess_runner`）的单测。

全部用假命令（python -c）验证，不碰真实补数工具；
停滞阈值/检查间隔参数化，测试秒级完成。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from app.core.subprocess_runner import run_with_watchdog

PY = sys.executable


def _touch_old(path: Path, age_seconds: float = 3600.0) -> None:
    """创建一个 mtime 在过去的进度文件（模拟"很久没有心跳"）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")
    old = time.time() - age_seconds
    os.utime(path, (old, old))


def test_normal_completion_ok(tmp_path: Path) -> None:
    """正常完成的短命令 → ok=True，tail 捕获到输出。"""
    res = run_with_watchdog(
        [PY, "-c", "print('hello-watchdog')"],
        cwd=str(tmp_path),
        timeout=60,
        name="t-normal",
        interval=0.2,
    )
    assert res["ok"] is True
    assert res["returncode"] == 0
    assert "hello-watchdog" in "\n".join(res["tail"])
    assert "stalled" not in res and "timeout" not in res


def test_stall_kills_silent_process(tmp_path: Path) -> None:
    """★ 停滞看门狗：进程活着但进度文件 mtime 很旧 → 判挂死杀掉，stalled=True。"""
    stale = tmp_path / "meta" / "backfill_state.json"
    _touch_old(stale, age_seconds=3600.0)

    res = run_with_watchdog(
        [PY, "-c", "import time; time.sleep(120)"],   # 挂死模拟：活着但不推进
        cwd=str(tmp_path),
        timeout=300,
        name="t-stall",
        progress_files=(stale,),
        stall_limit=2.0,
        interval=0.2,
    )
    assert res["ok"] is False
    assert res.get("stalled") is True
    assert "停滞" in (res.get("reason") or "")


def test_progress_advances_not_killed(tmp_path: Path) -> None:
    """★ 反向验证：进度文件持续更新（心跳）→ 不会被停滞看门狗误杀。"""
    heart = tmp_path / "meta" / "state.json"
    heart.parent.mkdir(parents=True, exist_ok=True)
    heart.write_text("{}", encoding="utf-8")

    # 子进程每 0.3s 摸一次心跳文件，共 2.5s；停滞阈值 1.5s + 检查间隔 0.2s
    res = run_with_watchdog(
        [
            PY, "-c",
            "import time,pathlib\n"
            f"p = pathlib.Path(r'{heart}')\n"
            "for _ in range(8):\n"
            "    time.sleep(0.3)\n"
            "    p.write_text(str(time.time()), encoding='utf-8')\n",
        ],
        cwd=str(tmp_path),
        timeout=300,
        name="t-heartbeat",
        progress_files=(heart,),
        stall_limit=1.5,
        interval=0.2,
    )
    assert res["ok"] is True, f"心跳进程被误杀：{res}"


def test_timeout_kills_long_process(tmp_path: Path) -> None:
    """整体超时兜底：进程存活超 timeout → 杀掉，timeout=True。"""
    res = run_with_watchdog(
        [PY, "-c", "import time; time.sleep(120)"],
        cwd=str(tmp_path),
        timeout=2,
        name="t-timeout",
        interval=0.2,
    )
    assert res["ok"] is False
    assert res.get("timeout") is True


def test_no_progress_files_stall_check_skipped(tmp_path: Path) -> None:
    """不传 progress_files（如因子重算）→ 停滞检测不启用，只有整体超时。"""
    res = run_with_watchdog(
        [PY, "-c", "import time; time.sleep(3)"],
        cwd=str(tmp_path),
        timeout=10,
        name="t-nostall",
        interval=0.2,
    )
    # 3s < 10s timeout：正常结束（停滞检测没参与）
    assert res["ok"] is True
    assert "stalled" not in res


@pytest.mark.parametrize("rc", [1, 2])
def test_nonzero_exit_not_ok(tmp_path: Path, rc: int) -> None:
    """子进程正常退出但 rc≠0（如"有 failed 项"）→ ok=False，不误报 timeout/stalled。"""
    res = run_with_watchdog(
        [PY, "-c", f"import sys; sys.exit({rc})"],
        cwd=str(tmp_path),
        timeout=60,
        name=f"t-rc{rc}",
        interval=0.2,
    )
    assert res["ok"] is False
    assert res["returncode"] == rc
    assert "stalled" not in res and "timeout" not in res
