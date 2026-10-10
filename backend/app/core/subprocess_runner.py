"""带进度看门狗的子进程运行器 —— 补数任务的「挂死自愈」底座（2026-10-10）。

为什么必须有它
--------------
baostock/akshare 的长任务会「挂死」：进程活着、不退出、也不写任何文件
（socket 无响应、连接悬挂）。此前唯一兜底是整体超时 —— 行情回补盲区
90 分钟、基本面周频盲区 **12 小时**，期间零感知、零告警（2026-10-09
本地实测出现过一次：4h37m 后卡死，日志/状态文件/产物 mtime 全停，进程还在）。

本模块给子进程加第二重兜底 —— **进度看门狗**：
    每隔 `interval` 秒检查 `progress_files` 的最新 mtime，
    停滞超过 `stall_limit` 秒 → 判定挂死 → 杀进程 → 返回 stalled。
补数工具都按固定节奏落盘进度（backfill_quotes 每 10 只、
backfill_fundamental 每通道 25 只），状态文件 mtime 就是天然心跳。

与 `subprocess.run(timeout=)` 的关系
------------------------------------
    整体超时仍是最后防线（保留）；看门狗在其内层提前拦截「无进度」。
    两者都杀进程、都以返回值（不抛异常）报告结果 —— 与现有
    `_run_step` 的 step dict 契约同构（ok / returncode / cost_seconds / tail
    + timeout / stalled 标记）。

输出捕获：stdout/stderr 写**临时文件**而非管道 —— 管道方式（capture_output）
父进程在运行期读不到任何输出；临时文件方式结束后读尾部即可，
顺带避免长任务输出全部缓冲在内存里。
"""
from __future__ import annotations

import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from app.core.logging import logger

#: 监控线程的检查间隔（秒）。测试可传小值加速。
DEFAULT_INTERVAL = 30.0


def run_with_watchdog(
    cmd: list[str],
    *,
    cwd: str,
    timeout: int,
    name: str,
    progress_files: tuple[Path | str, ...] = (),
    stall_limit: float | None = None,
    tail_lines: int = 20,
    interval: float = DEFAULT_INTERVAL,
) -> dict[str, Any]:
    """运行子进程，带「整体超时 + 进度停滞」双重看门狗。不抛异常。

    Args:
        cmd: 命令行（不含 shell）。
        cwd: 子进程工作目录。
        timeout: 整体超时上限（秒），超时杀进程，返回 ``timeout=True``。
        name: 步骤名（日志与返回值用）。
        progress_files: 进度心跳文件（补数工具的状态文件）。
            任一文件的 mtime 前进即视为有进展；全部停滞才触发停滞判定。
        stall_limit: 停滞判定阈值（秒）。None = 不启用停滞检测（仅整体超时）。
            ⚠️ 必须大于补数工具的正常"静默上限" —— 例如 backfill_fundamental
            熔断等待最长 600s，阈值应 ≥ 1800s。
        tail_lines: 结束后从输出文件尾部取多少行。
        interval: 看门狗检查间隔（秒），仅测试用小值。

    Returns:
        与 data_sync_service 的 step dict 同构：
        ``{"name", "ok", "returncode", "cost_seconds", "tail", "timeout"?, "stalled"?}``
        杀掉（停滞/超时）时 ``returncode`` 为 None（进程被 TerminateProcess）。
    """
    t0 = time.perf_counter()
    # SIM115 豁免：Popen 需要句柄存活到子进程结束，属"必须跨语句持有"场景
    out_file = tempfile.NamedTemporaryFile(  # noqa: SIM115
        prefix=f"watchdog-{name}-", suffix=".log", delete=False
    )
    out_path = Path(out_file.name)
    proc: subprocess.Popen[bytes] | None = None
    outcome: dict[str, Any] = {}

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            stdout=out_file,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
        )

        killed = _watch(
            proc,
            timeout=timeout,
            progress_files=tuple(Path(p) for p in progress_files),
            stall_limit=stall_limit,
            interval=interval,
        )

        rc = proc.poll()
        if rc is None:
            # 监控线程已判 kill；给收尸留一点时间，仍未退出就再等
            try:
                rc = proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                rc = proc.wait(timeout=15)

        if killed.get("stalled"):
            outcome["stalled"] = True
            outcome["reason"] = f"进度停滞超过 {stall_limit:.0f}s（挂死特征）"
        elif killed.get("timeout"):
            outcome["timeout"] = True
            outcome["reason"] = f"整体超时（>{timeout}s）"

        ok = (rc == 0) and not killed
        outcome.update(
            {
                "name": name,
                "ok": ok,
                "returncode": rc,
                "cost_seconds": round(time.perf_counter() - t0, 1),
                "tail": _read_tail(out_path, tail_lines),
            }
        )
        if not ok:
            outcome.setdefault("reason", f"退出码 {rc}")
            msg = (
                f"[watchdog] {name} 失败：{outcome.get('reason')}"
                f"（{outcome['cost_seconds']}s）tail={outcome['tail'][-2:]}"
            )
            if killed:
                logger.warning(msg)
            else:
                logger.error(msg)
        else:
            logger.info(f"[watchdog] {name} ok（{outcome['cost_seconds']}s）")
        return outcome
    except Exception as exc:  # Popen 本身失败等
        logger.warning(f"[watchdog] {name} 异常：{exc!r}")
        return {
            "name": name,
            "ok": False,
            "error": repr(exc),
            "cost_seconds": round(time.perf_counter() - t0, 1),
        }
    finally:
        out_file.close()
        # 留日志文件便于排查（交给系统临时目录自动清理），不再主动删。


def _watch(
    proc: subprocess.Popen[bytes],
    *,
    timeout: int,
    progress_files: tuple[Path, ...],
    stall_limit: float | None,
    interval: float,
) -> dict[str, Any]:
    """监控循环（阻塞直到进程退出/超时/停滞）。返回触发原因。"""
    start = time.monotonic()
    deadline = start + timeout

    def _progress_mtime() -> float:
        latest = 0.0
        for p in progress_files:
            try:
                latest = max(latest, p.stat().st_mtime)
            except OSError:
                continue  # 文件还没被创建 —— 用当前时间兜底
        return latest

    # 停滞判定统一用 epoch（mtime 是 epoch 时间戳，与 monotonic 不同轴，不可混比）
    last_progress = max(_progress_mtime(), time.time())
    while True:
        if proc.poll() is not None:
            return {}
        now = time.monotonic()
        if now >= deadline:
            proc.kill()
            return {"timeout": True}
        if stall_limit is not None:
            mtime = _progress_mtime()
            if mtime > last_progress:
                last_progress = mtime
            elif time.time() - last_progress > stall_limit:
                proc.kill()
                return {"stalled": True}
        time.sleep(min(interval, max(0.0, deadline - time.monotonic())))


def _read_tail(path: Path, n: int) -> list[str]:
    """读输出文件最后 n 行（大文件安全：流式读）。"""
    try:
        with path.open("rb") as f:
            lines = list(f)[-n:]
        return [ln.decode("utf-8", errors="replace").rstrip() for ln in lines]
    except OSError:
        return []
