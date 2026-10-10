"""本地数据同步编排 —— 每日跑批与启动补跑共用的「把本地数据补到最新」。

背景（2026-10-08 审计，为什么会有这个模块）
--------------------------------------------
此前只有 `paper_daily`（交易日 23:00）会在「就绪探针失败」时才回补行情，
而探针只有 **2 个标的**（流动性最好的个股 + 上证指数）。2026-09-18 一次
中断的回补留下了 ~555 只的部分数据，探针恰好在其中 → 此后每晚都判「就绪」
→ 不回补；job 记成功 → 启动补跑（catch_up）也没有"失败任务"可重试
→ **09-18~09-30 的缺口挂了两周没人发现**。

第二层问题：自动链路只管**行情** —— 股票池快照与因子都不在链路里，
所以即便行情齐了，因子也停在 09-17。而策略建仓打分读的正是
`factor/*.parquet`：**行情新了、因子旧了 = 用旧信号跑新行情**。

维度与顺序（后一步依赖前一步的产物）
------------------------------------
    1. 行情（个股+指数）  `tools.backfill_quotes --incremental`      30~50 分钟（有缺口才慢）
    2. 股票池快照         `tools.backfill_universe --start D --end D` 秒级（已存在的日期自动跳过）
    3. 因子重算           `python -m factors.calc`                    约 10~20 分钟
    4. 基本面原始数据     ⚠️ **不自动**：`tools.backfill_fundamental` 全量约 9 小时
                          （东财分页 20~25 页/次），放进每日跑批会把 23:00 的
                          job 拖到天亮；季报频率本来就低 → 只做陈旧度告警，主人手动跑。

就绪判据（v2：覆盖率闸门，2026-10-08 起）
------------------------------------------
    ① 探针：上证指数 + 流动性最好的个股在 `run_date` 必须有 K 线
       （指数是择时闸门的信号源，缺了它闸门会**静默失效**，比报错更危险）；
    ② 覆盖率：当日有个股 K 线的数量 ≥ `data_sync.min_daily_symbols`（默认 3000，
       约为全市场 ~4570 只的 65%）—— 「2 个标的有数据」≠「全市场有数据」。

所有子步骤都走**子进程**：baostock 登录是进程级单例，长任务与其它
baostock 调用（catalog 自检等）会互踢；子进程同时隔离失败 —— 单步失败
不中断编排，由最终的就绪校验做唯一闸门。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.errors import DataNotReadyError
from app.core.logging import logger
from app.core.paths import BACKFILL_STATE_FILE
from app.core.subprocess_runner import run_with_watchdog
from app.services import data_store

#: 行情就绪探针：流动性最好的个股 + 择时闸门的指数信号源。
#: （baostock 日线一般 17:30~19:00 就绪。）
READY_PROBE_SYMBOLS: tuple[str, ...] = ("sh.600519", "sh.000001")

_INDEX_SYMBOL = "sh.000001"

#: 子步骤超时：行情回补 4792 只 × 单只一请求，实测 30~50 分钟；其余步骤放宽到 60 分钟。
_QUOTES_TIMEOUT = 90 * 60
_STEP_TIMEOUT = 60 * 60

#: backend 目录（子进程的 cwd —— `python -m tools.xxx` 需要在 backend 下才能解析模块）
_BACKEND_DIR = Path(__file__).resolve().parents[2]


# ======================================================================
# 就绪校验（探针 + 覆盖率）
# ======================================================================
def daily_symbol_count(run_date: str) -> int:
    """`run_date` 当日有个股 K 线的数量（不含指数）。"""
    try:
        return len(data_store.read_quotes(None, run_date, run_date, columns=["symbol"]))
    except Exception as exc:
        logger.warning(f"[data_sync] {run_date} 覆盖率读取失败：{exc!r}")
        return 0


def quotes_readiness(run_date: str) -> dict[str, Any]:
    """就绪度报告（不抛异常，供日志与测试用）。"""
    probe_missing: list[str] = []
    for sym in READY_PROBE_SYMBOLS:
        is_index = sym == _INDEX_SYMBOL
        try:
            df = data_store.read_quotes(sym, run_date, run_date, is_index=is_index, columns=["date"])
        except Exception as exc:
            logger.warning(f"[data_sync] 探针 {sym} 读取失败：{exc!r}")
            probe_missing.append(sym)
            continue
        if df.empty:
            probe_missing.append(sym)

    min_required = int(settings.data_sync.min_daily_symbols)
    symbols = daily_symbol_count(run_date)
    return {
        "run_date": run_date,
        "probe_missing": probe_missing,
        "symbol_count": symbols,
        "min_required": min_required,
        "coverage_ok": symbols >= min_required,
        "ok": not probe_missing and symbols >= min_required,
    }


def assert_quotes_ready(run_date: str) -> None:
    """就绪校验（唯一闸门）：探针 + **全市场覆盖率** 双判据。

    只查 2 个标的的旧版探针被 2026-09-18~09-30 的缺口击穿过：
    部分数据恰好包含探针 → 判"就绪" → 不回补。覆盖率闸门防的就是这个。
    """
    rep = quotes_readiness(run_date)
    if rep["ok"]:
        return
    reasons: list[str] = []
    if rep["probe_missing"]:
        reasons.append(f"探针 {'、'.join(rep['probe_missing'])} 在 {run_date} 没有日线数据")
    if not rep["coverage_ok"]:
        reasons.append(
            f"当日全市场覆盖 {rep['symbol_count']} 只 < 阈值 {rep['min_required']}"
            f"（部分回补/数据源未更新完的典型症状）"
        )
    raise DataNotReadyError(
        f"行情未就绪：{'；'.join(reasons)}。"
        f"数据源（baostock）通常在 17:30~19:00 更新完毕；"
        f"本次跑批记录为失败，次日启动补跑会自动补上（保底方案）。"
    )


# ======================================================================
# 子步骤（全部子进程，失败不抛 —— 由最终就绪校验做唯一闸门）
# ======================================================================
def _run_step(
    name: str,
    cmd: list[str],
    timeout: int,
    *,
    progress_files: tuple[Path, ...] = (),
    stall_limit: float | None = None,
) -> dict[str, Any]:
    """跑一个补数子步骤（带整体超时 + 进度看门狗）。

    停滞判定（2026-10-10 兜底新增）：baostock/akshare 挂死时进程活着但不写
    任何文件 —— 状态文件 mtime 就是天然心跳，停滞超过 stall_limit 判挂死杀掉。
    返回与旧版 step dict 同构（+ timeout/stalled 标记），不抛异常。
    """
    out = run_with_watchdog(
        cmd,
        cwd=str(_BACKEND_DIR),
        timeout=timeout,
        name=name,
        progress_files=progress_files,
        stall_limit=stall_limit,
        tail_lines=5,
    )
    # step dict 键名兼容（旧字段 "step"，watchdog 返回 "name"）
    out["step"] = out.pop("name", name)
    return out


def sync_quotes_incremental() -> dict[str, Any]:
    """增量回补行情（个股 + 指数，`tools.backfill_quotes --incremental`）。

    进度心跳 = `data/meta/backfill_state.json`（工具每 10 只落盘一次，
    单只一请求通常秒级）→ 停滞 15 分钟判定挂死杀掉重报，不再干等 90 分钟超时。
    """
    return _run_step(
        "行情增量回补",
        [sys.executable, "-m", "tools.backfill_quotes", "--incremental"],
        _QUOTES_TIMEOUT,
        progress_files=(BACKFILL_STATE_FILE,),
        stall_limit=15 * 60,
    )


def sync_universe(run_date: str) -> dict[str, Any]:
    """落当日股票池快照（幂等：已存在的日期自动跳过；秒级）。"""
    return _run_step(
        f"股票池快照 {run_date}",
        [sys.executable, "-m", "tools.backfill_universe", "--start", run_date, "--end", run_date],
        _STEP_TIMEOUT,
    )


def sync_factors() -> dict[str, Any]:
    """重算全部已注册因子（行情更新之后、净值重算之前）。"""
    return _run_step("因子重算", [sys.executable, "-m", "factors.calc"], _STEP_TIMEOUT)


def fundamental_staleness() -> dict[str, Any]:
    """基本面原始数据的陈旧度（按 profit_yoy 因子的最新日期近似）。"""
    path = data_store.factor_path("profit_yoy", "fundamental")
    if not path.exists():
        return {"available": False, "reason": f"缺少 {path.name}"}
    try:
        df = data_store.query_duck(f"SELECT max(date) AS d FROM read_parquet('{path.as_posix()}')")
        last = str(df["d"].iloc[0])[:10] if not df.empty and df["d"].iloc[0] is not None else None
    except Exception as exc:
        return {"available": False, "reason": repr(exc)}
    return {"available": True, "last_date": last}


# ======================================================================
# 编排入口
# ======================================================================
def _safe(name: str, step_fn, *args) -> dict[str, Any]:
    """编排层的兜底：单步抛异常也只记录，不让整个 sync_all 崩（契约见 docstring）。"""
    try:
        out = step_fn(*args)
    except Exception as exc:
        logger.warning(f"[data_sync] 步骤 {name} 异常：{exc!r}")
        return {"step": name, "ok": False, "error": repr(exc)}
    out.setdefault("step", name)
    return out


def sync_all(run_date: str) -> dict[str, Any]:
    """把本地数据补到 `run_date`：行情 → 快照 → 因子 → 基本面陈旧度报告。

    幂等：每一步都由"数据在不在"驱动（行情由覆盖率闸门驱动、快照自动跳过
    已有日期、因子全量重算本身幂等）。**不抛异常** —— 单步失败记进结果，
    由调用方在编排结束后跑一次 `assert_quotes_ready` 做最终闸门。
    """
    steps: list[dict[str, Any]] = []

    # 1) 行情：先判就绪，缺了才回补（有缺口的夜晚这一步 30~50 分钟，其余秒级）
    rep = quotes_readiness(run_date)
    if rep["ok"]:
        logger.info(f"[data_sync] {run_date} 行情已就绪（覆盖 {rep['symbol_count']} 只），跳过回补")
        steps.append({"step": "行情增量回补", "ok": True, "skipped": True,
                      "reason": f"已就绪（{rep['symbol_count']} 只）"})
    else:
        logger.info(
            f"[data_sync] {run_date} 行情未就绪（探针缺 {rep['probe_missing']}"
            f"｜覆盖 {rep['symbol_count']} < {rep['min_required']}），开始增量回补"
        )
        steps.append(_safe("行情增量回补", sync_quotes_incremental))

    # 2)/3) 依赖行情 —— 行情步失败则跳过（短路口径，2026-10-10）：
    #   残缺行情写出的快照/因子没有任何自愈路径（增量"已覆盖"跳过），
    #   白算 10~20 分钟还会落脏数据。调用方（run_paper_day）会因失败步骤整体 raise。
    quotes_failed = not steps[-1].get("ok", False)
    if quotes_failed:
        logger.warning("[data_sync] 行情回补失败 → 跳过快照与因子重算（依赖行情）")
        steps.append({"step": "股票池快照", "ok": False, "skipped": True,
                      "reason": "行情回补失败，跳过"})
        steps.append({"step": "因子重算", "ok": False, "skipped": True,
                      "reason": "行情回补失败，跳过"})
    else:
        # 2) 股票池快照（秒级；幂等）
        if settings.data_sync.sync_universe:
            steps.append(_safe(f"股票池快照 {run_date}", sync_universe, run_date))

        # 3) 因子重算（必须在净值重算之前 —— 策略打分读 factor/*.parquet）
        if settings.data_sync.sync_factors:
            steps.append(_safe("因子重算", sync_factors))

    # 4) 基本面：只报告陈旧度（全量回补约 9h，不自动）
    steps.append(_safe("基本面陈旧度", lambda: {"ok": True, **fundamental_staleness()}))

    return {"run_date": run_date, "steps": steps, "readiness_before": rep}
