"""P1-1：启动补跑必须**不阻塞端口**。

★ 这条防线为什么值得单独测：
  这是"看起来一切正常、实际完全不可用"的典型 —— 服务进程活着、日志在滚、
  却因为没有 bind 端口而让所有请求被拒。而它此前正是"换机器后服务起不来"
  这个现象的主要成因之一。测试要钉死的是**接线**，不是函数存在：
  本项目最大的失效模式就是"函数写了、没人调"（开发进度 §4 第 8 条）。
"""
from __future__ import annotations

import inspect
import threading
import time

from app.scheduler import scheduler as sched_mod


def _reset_state() -> None:
    with sched_mod._catch_up_lock:
        sched_mod._catch_up_state.update(
            running=False, started_at=None, finished_at=None, result=None, error=None
        )


def test_start_catch_up_background_returns_without_waiting(monkeypatch):
    """调用必须立刻返回，不能被补跑时长拖住（这就是"端口能不能按时就绪"）。"""
    released = threading.Event()
    started = threading.Event()

    def _slow_catch_up():
        started.set()
        released.wait(timeout=10)
        return {"jobs": {"paper_daily": {"status": "success"}}}

    monkeypatch.setattr(sched_mod, "run_startup_catch_up", _slow_catch_up)
    _reset_state()

    t0 = time.monotonic()
    res = sched_mod.start_catch_up_background()
    elapsed = time.monotonic() - t0

    assert res["started"] is True
    assert elapsed < 1.0, f"调用阻塞了 {elapsed:.2f}s —— 后台化没生效"
    assert started.wait(timeout=5), "后台线程应当已经开跑"
    assert sched_mod.catch_up_running() is True

    released.set()
    for _ in range(100):
        if not sched_mod.catch_up_running():
            break
        time.sleep(0.05)
    assert sched_mod.catch_up_running() is False
    assert sched_mod.catch_up_state()["result"]["jobs"]["paper_daily"]["status"] == "success"


def test_start_catch_up_background_is_idempotent(monkeypatch):
    """重复调用不得起第二个线程（两个补跑同时动模拟盘是灾难）。"""
    released = threading.Event()
    calls: list[int] = []

    def _slow_catch_up():
        calls.append(1)
        released.wait(timeout=10)
        return {"jobs": {}}

    monkeypatch.setattr(sched_mod, "run_startup_catch_up", _slow_catch_up)
    _reset_state()

    first = sched_mod.start_catch_up_background()
    second = sched_mod.start_catch_up_background()
    assert first["started"] is True
    assert second == {"started": False, "reason": "already_running"}

    released.set()
    for _ in range(100):
        if not sched_mod.catch_up_running():
            break
        time.sleep(0.05)
    assert len(calls) == 1


def test_catch_up_disabled_by_config(monkeypatch):
    monkeypatch.setattr(sched_mod.settings.scheduler, "catch_up_on_startup", False)
    _reset_state()
    res = sched_mod.start_catch_up_background()
    assert res == {"started": False, "reason": "disabled_by_config"}
    assert sched_mod.catch_up_running() is False


def test_cron_runner_skips_while_catch_up_running(monkeypatch):
    """补跑进行中时，定时任务必须**干净跳过**，不能跑去撞 _RUN_LOCK 记 failed。"""
    invoked: list[str] = []
    monkeypatch.setattr(sched_mod, "run_job", lambda *a, **kw: invoked.append(a[0]))
    _reset_state()

    with sched_mod._catch_up_lock:
        sched_mod._catch_up_state["running"] = True
    try:
        sched_mod._make_runner("paper_daily")()
        assert invoked == [], "补跑进行中仍然触发了定时任务 —— 会误报任务失败"
    finally:
        _reset_state()

    # 补跑结束后要恢复正常触发
    sched_mod._make_runner("paper_daily")()
    assert invoked == ["paper_daily"]


def test_catch_up_state_is_a_copy():
    """状态快照必须是副本 —— 外部改动不得污染内部状态（状态接口会返回它）。"""
    _reset_state()
    snap = sched_mod.catch_up_state()
    snap["running"] = True
    assert sched_mod.catch_up_running() is False


def test_scheduler_status_exposes_catch_up():
    _reset_state()
    st = sched_mod.scheduler_status()
    assert "catch_up" in st, "状态接口必须暴露补跑进度，否则前端只能显示空页面"
    assert st["catch_up"]["running"] is False


def test_lifespan_uses_background_catch_up_not_blocking_one():
    """接线检查：lifespan 必须调后台版本，**不能**直接调同步版本。

    ★ 为什么用源码断言：这个 bug 不会让任何测试变红 ——
      同步调用在功能上完全正确，只是让服务几十秒到几十分钟不可访问。
      本项目已有的先例（`catch_up_missing_days` 的 stub 活了整整一个阶段）
      说明这类"接线级"错误只能靠显式断言兜住。
    """
    import app.main as main_mod

    src = inspect.getsource(main_mod.lifespan)
    assert "start_catch_up_background" in src
    assert "run_startup_catch_up()" not in src, (
        "lifespan 里出现了同步补跑调用 —— 端口会被补跑挡到结束才监听"
    )
