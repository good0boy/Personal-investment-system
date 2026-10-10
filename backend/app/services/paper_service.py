"""模块 4 · 模拟仿真服务（阶段 4）。

核心设计（主人 2026-09-20 拍板）
================================
**每日"全量重算"，不做增量撮合。**

模拟盘每天 = 复用回测引擎从模拟起始日（`PAPER_START_DATE = 2025-01-01`）
跑到数据最新交易日。理由：

1. 引擎是确定性的（同一 config 跑两次逐行一致）→ 重算天然幂等，
   补跑 = 重跑当天那次，不需要任何状态迁移；
2. 增量撮合要把"卖出后 60 交易日禁买 / 停牌顺延 / 退市强平"等撮合状态
   在引擎之外再维护一遍 —— 等于重写一遍引擎的撮合逻辑，出错风险高；
3. 全市场重算实测 45s，夜间跑批毫无压力。

与回测的关系：**直接调 `BacktestEngine`（照抄 `strategies/runner.py` 的
调用序列），但落盘到 `data/paper/{account_id}/`，不写 `backtest_run` 表、
不进回测列表** —— 全量重算每天 end 都在变，若走 `run_backtest` 会每天
生成一个新 run_id，把回测列表刷爆。

真相源分工（架构纪律 6「不搞双份真相」）：
- **数据（nav/positions/trades/config）→ Parquet/JSON**（磁盘产物为真相源，
  与回测/因子/寻优一致）；
- **账户状态（last_run_date / cash / status）→ `paper_account` 表**。
  `paper_nav` / `paper_position` 两张表**不使用**（schema 里保留仅为兼容，
  增量模式的遗留设计；全量重算下再用它们就是两份真相）。

每日跑批流程（23:00，见 `scheduler/jobs.py`）：
    1. `data_sync_service.sync_all`：行情增量回补（覆盖率闸门驱动，尽力而为）
       → 股票池快照 → **因子重算**（策略打分读 factor/*.parquet，必须先于净值重算）；
    2. **就绪校验**（唯一闸门，探针 + 全市场覆盖率）：不就绪 → `DataNotReadyError`，
       job 记 failed，等次日启动补跑兜底（主人拍板的保底方案）；
    3. 引擎全量重算 起始日 → run_date；
    4. 覆盖落盘 + 更新 `paper_account.last_run_date`。

幂等：`nav.parquet` 最后日期 >= run_date → 跳过（防止"补跑旧日期把
新净值覆盖回去"，这是全量重算模式的补跑陷阱）。
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from app.core.config import settings
from app.core.errors import DataNotReadyError, NotFoundError, PaperError
from app.core.jsonutils import json_safe
from app.core.logging import logger
from app.core.paths import PAPER_DIR
from app.repos import meta_repo
from app.services import data_store
from app.services import metrics as metrics_mod
from app.services.calendar_service import calendar
from app.services.data_sync_service import (  # noqa: F401 - 再导出保持旧 API 兼容
    READY_PROBE_SYMBOLS,
    assert_quotes_ready,
    sync_all,
    sync_quotes_incremental,
)
from strategies.base import (
    EQUITY_COLUMNS,
    ORDER_COLUMNS,
    POSITION_COLUMNS,
    TRADE_COLUMNS,
    ensure_contract,
)
from strategies.engine import BacktestEngine
from strategies.paper_engine import PaperEngine
from strategies.registry import get_strategy
from strategies.runner import PREHEAT_START

#: 模拟盘起始日（主人 2026-09-20 拍板：样本外起点，自带约 1 年 8 个月历史净值）
PAPER_START_DATE = "2025-01-01"

#: 默认策略与参数来源：`paper_account.param_json`（空 = 策略默认 = 规格 v1.2）
DEFAULT_STRATEGY = "momentum"

#: 「最近交易」展示上限（已平仓 ∪ 持仓中，按买入日降序）
RECENT_TRADES_LIMIT = 100

# 就绪探针 / 行情回补 / 就绪校验已上收到 `data_sync_service`（探针 + 全市场覆盖率双判据），
# 本模块经顶部 import 再导出 READY_PROBE_SYMBOLS / sync_quotes_incremental / assert_quotes_ready。

#: 进程内互斥锁：API 手动触发与 APScheduler cron 可能并发（两者是不同的
#: job id，APScheduler 的 max_instances 管不到跨 id），锁是最终防线。
_RUN_LOCK = threading.Lock()


# ======================================================================
# 账户
# ======================================================================
def account_dir(account_id: str) -> Path:
    return PAPER_DIR / account_id


def ensure_default_account() -> dict[str, Any]:
    """幂等初始化默认模拟账户并返回（DB 行）。"""
    cfg = settings.paper
    meta_repo.ensure_paper_account(
        cfg.default_account_id,
        cfg.default_account_name,
        cfg.init_cash,
        strategy=DEFAULT_STRATEGY,
        start_date=PAPER_START_DATE,
    )
    row = meta_repo.get_paper_account(cfg.default_account_id)
    if row is None:  # pragma: no cover - ensure 后必存在
        raise PaperError(f"模拟账户初始化失败：{cfg.default_account_id}")

    # 策略切换（2026-10-09）：config.yaml 的 paper.strategy 与账户行不一致时落库。
    # 策略变化会触发全量重算（净值历史整体重写）—— 设计使然，主人已知情。
    cfg_strategy = (cfg.strategy or "").strip()
    if cfg_strategy and str(row.get("strategy") or "") != cfg_strategy:
        params_json = (
            json.dumps(cfg.params, ensure_ascii=False) if cfg.params else None
        )
        meta_repo.update_paper_account_strategy(cfg.default_account_id, cfg_strategy, params_json)
        # ★ 旧策略产物必须同步作废：幂等闸（last_nav_date >= run_date → 跳过）
        #   只看日期不看策略 —— 不删的话，旧 momentum 净值会一直展示在
        #   前端净值曲线上，且今晚 23:00 的跑批也会被幂等闸拦住不重算。
        removed = _invalidate_paper_artifacts(cfg.default_account_id)
        logger.info(
            f"[paper] 模拟盘策略切换：{row.get('strategy')!r} -> {cfg_strategy!r}（原策略下线，"
            f"净值自 {PAPER_START_DATE} 起全量重算）；已作废旧产物 {removed} 份"
        )
        row = meta_repo.get_paper_account(cfg.default_account_id)
    return row


def _invalidate_paper_artifacts(account_id: str) -> int:
    """删除模拟盘全部数据产物（nav/positions/trades/orders/config）。

    只在**策略切换**时调用：策略变了，旧净值曲线就是另一条策略的历史，
    留着只会误导（前端"历史净值曲线"展示的就是这些文件）。删除后
    下一次 run_paper_day 会从 PAPER_START_DATE 全量重算新策略。
    """
    d = account_dir(account_id)
    removed = 0
    if not d.exists():
        return 0
    for name in (
        "nav.parquet", "positions.parquet", "trades.parquet",
        "orders.parquet", "config.json", STATE_FILE,
    ):
        f = d / name
        if f.exists():
            f.unlink()
            removed += 1
    return removed


# ======================================================================
# 跨日状态（state.json）—— 增量撮合的续跑依据
# ======================================================================
STATE_FILE = "state.json"


def _load_state(account_id: str) -> dict[str, Any] | None:
    """读跨日状态；不存在/损坏返回 None（调用方回退全量重建）。"""
    path = account_dir(account_id) / STATE_FILE
    if not path.exists():
        return None
    try:
        st = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(st, dict) or not st.get("as_of"):
            raise ValueError("state 缺少 as_of")
        return st
    except Exception as exc:  # 损坏 = 回退全量
        logger.warning(f"[paper] state.json 读取失败（将回退全量重建）：{exc!r}")
        return None


def _save_state(account_id: str, state: dict[str, Any]) -> None:
    """原子写跨日状态（tmp + os.replace，与 parquet 同纪律）。"""
    path = account_dir(account_id) / STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(json_safe(state), ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def require_account(account_id: str) -> dict[str, Any]:
    """取账户行，不存在抛 404（API 路径校验用）。"""
    row = meta_repo.get_paper_account(account_id)
    if row is None:
        raise NotFoundError(f"模拟账户不存在：{account_id}")
    return row


_require_account = require_account  # 模块内别名（历史书写习惯，语义相同）


# ======================================================================
# 跑批（核心）
# ======================================================================
def last_nav_date(account_id: str) -> str | None:
    """nav.parquet 的最后一个净值日期；无产物时返回 None。"""
    path = account_dir(account_id) / "nav.parquet"
    if not path.exists():
        return None
    try:
        df = data_store.query_duck(
            f"SELECT max(date) AS d FROM read_parquet('{path.as_posix()}')"
        )
    except Exception:
        return None
    if df.empty or df["d"].iloc[0] is None:
        return None
    return str(df["d"].iloc[0])[:10]


def run_paper_day(
    run_date: str,
    *,
    trigger: str = "manual",
    notify: bool | None = None,
) -> dict[str, Any]:
    """把模拟盘净值更新到 `run_date`（幂等；默认单日增量撮合）。

    数据流：`sync_all`（行情→快照→因子，覆盖率闸门驱动，缺口夜约 30~80 分钟）
    → 就绪校验（唯一闸门）→ 引擎执行 → 落盘 → 派发交易提醒。

    执行模式（`paper.engine_mode`，2026-10-10 主人拍板）：
    - ``incremental``（默认）：PaperEngine 只撮合 `run_date` 一天，跨日状态
      存 `state.json`（内存峰值 <500MB、秒级）；state 缺失/损坏/不连续 →
      **自动回退全量重算**（同一次调用内完成，不会失败）；
    - ``full``：每晚从 PAPER_START_DATE 全量重算（旧语义，峰值 3.3GB，
      保留为冷启动/自愈/对拍基线路径）。

    Args:
        run_date: 逻辑运行日（净值终点），来自日历的最新已发生交易日。
        trigger: manual | cron | catch_up（仅记录用）。
        notify: 是否派发企微交易提醒。默认 `None` = **自动判断**
                （`run_date` 是最近一个交易日才推，补跑历史日期不推）。

                ★ 为什么必须自动判断而不是默认 True：补跑 200 个历史交易日
                  就是 200×N 条消息轰炸，而它们全是"已经过去的事"，
                  推了只会让人屏蔽群。补跑静默是设计，不是遗漏。
    """
    if not calendar.is_trading_day(run_date):
        return {"skipped": True, "reason": f"{run_date} 非交易日"}

    if not _RUN_LOCK.acquire(blocking=False):
        raise PaperError("模拟盘跑批已在进行中，请勿重复触发")
    try:
        account = ensure_default_account()
        account_id = account["id"]

        # 幂等：已有不早于 run_date 的净值 → 跳过。
        # 场景：23:00 跑 T 成功后，次日启动补跑扫描出漏掉的 T-1，
        # 若不拦住就会把净值从 T 覆盖回 T-1（全量重算的补跑陷阱）。
        done = last_nav_date(account_id)
        if done is not None and done >= run_date:
            return {"skipped": True, "reason": f"净值已更新至 {done}（不早于 {run_date}）"}

        # 数据同步（行情 → 股票池快照 → 因子）→ 最终就绪校验（唯一闸门）。
        # sync_all 不抛异常（单步失败记进结果）；此处仍不就绪 → DataNotReadyError
        # 抛给 job 记 failed，次日启动补跑会自动重试（catch_up 兜底）。
        sync_result = sync_all(run_date)
        failed = [s for s in sync_result.get("steps", []) if not s.get("ok")]
        if failed:
            # ★ 任一步失败都不算数（2026-10-10 兜底）：旧行为是 warning 继续跑 ——
            #   "行情新因子旧"正是数据同步 docstring 点名的头号坑，闸门却只查行情。
            #   改为整体 failed：job 记 failed + 企微告警 + 次日启动 catch_up 重跑。
            names = [str(f.get("step") or "?") for f in failed]
            raise DataNotReadyError(
                f"数据同步有失败步骤：{'、'.join(names)}。"
                f"为避免用旧数据算新净值，本次跑批记为失败，次日启动补跑会自动重试。"
            )
        assert_quotes_ready(run_date)

        strategy_name = account.get("strategy") or DEFAULT_STRATEGY
        try:
            params = json.loads(account.get("param_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            params = {}
        strategy = get_strategy(strategy_name, params)
        init_cash = float(account["init_cash"])

        mode = (settings.paper.engine_mode or "full").strip().lower()
        if mode == "incremental":
            try:
                return _run_incremental_day(
                    account=account,
                    account_id=account_id,
                    strategy=strategy,
                    strategy_name=strategy_name,
                    init_cash=init_cash,
                    run_date=run_date,
                    trigger=trigger,
                    notify=notify,
                )
            except Exception as exc:  # 自愈：增量失败回退全量
                logger.warning(
                    f"[paper] {run_date} 增量撮合失败，自动回退全量重算：{exc!r}"
                )
        return _run_full_day(
            account=account,
            account_id=account_id,
            strategy=strategy,
            strategy_name=strategy_name,
            init_cash=init_cash,
            run_date=run_date,
            trigger=trigger,
            notify=notify,
        )
    finally:
        _RUN_LOCK.release()


def _run_full_day(
    *,
    account: dict[str, Any],
    account_id: str,
    strategy: Any,
    strategy_name: str,
    init_cash: float,
    run_date: str,
    trigger: str,
    notify: bool | None,
) -> dict[str, Any]:
    """全量重算路径（冷启动 / state 异常自愈 / 对拍基线）—— 旧语义原样保留。"""
    t0 = time.perf_counter()
    with data_store.read_until(run_date):
        engine = BacktestEngine(
            strategy,
            start=PAPER_START_DATE,
            end=run_date,
            preheat_start=PREHEAT_START,
            initial_cash=init_cash,
            max_positions=settings.strategy.max_positions,
            max_weight_per_stock=settings.strategy.max_weight_per_stock,
            symbols=None,  # 全市场（与回测同口径）
            benchmark=settings.strategy.benchmark,
            listed_days_min=settings.universe.listed_days_min,
            no_rebuy_days=settings.universe.no_rebuy_days,
            delist_confirm_days=settings.universe.delist_confirm_days,
        )
        result = engine.run()
    cost_seconds = round(time.perf_counter() - t0, 2)

    bser = _benchmark_series(PAPER_START_DATE, run_date)
    equity = metrics_mod.equity_curve(result.equity, initial_cash=init_cash, benchmark=bser)
    if equity.empty:
        raise PaperError(
            f"模拟盘重算结果为空（{PAPER_START_DATE} ~ {run_date}）："
            f"请确认该区间内有行情数据"
        )

    summary = _write_artifacts(
        account_id,
        equity=equity,
        result=result,
        run_date=run_date,
        trigger=trigger,
        cost_seconds=cost_seconds,
        params=dict(strategy.params),
        strategy_name=strategy_name,
    )
    final = equity.iloc[-1]
    meta_repo.update_paper_run_state(
        account_id,
        last_run_date=run_date,
        cash=float(final.get("cash") or 0.0),
        status="idle",
    )
    # 跨日状态落盘（增量模式的续跑依据）；策略切换作废产物时一并删除
    benchmark_base = None
    if bser is not None and not bser.empty:
        benchmark_base = float(bser.iloc[0])
    _save_state(
        account_id,
        engine.export_state() | {"benchmark_base": benchmark_base, "start_date": PAPER_START_DATE},
    )
    logger.info(
        f"[paper] {account_id} 净值已更新至 {run_date}（全量重算）："
        f"nav={final.get('nav')}，耗时 {cost_seconds}s（{trigger}）"
    )
    return _finish_with_alerts(
        account=account,
        account_id=account_id,
        strategy_name=strategy_name,
        result=result,
        equity=equity,
        run_date=run_date,
        trigger=trigger,
        notify=notify,
        summary=summary,
        entry_candidates=getattr(engine, "last_entry_candidates", None),
        max_positions=int(strategy.params.get("max_positions", 10)),
    )


def _run_incremental_day(
    *,
    account: dict[str, Any],
    account_id: str,
    strategy: Any,
    strategy_name: str,
    init_cash: float,
    run_date: str,
    trigger: str,
    notify: bool | None,
) -> dict[str, Any]:
    """单日增量撮合路径：载入 T-1 状态 → 只执行 run_date → 追加产物 + 落状态。

    任何状态异常（缺失/损坏/与产物不连续）直接抛 —— 由 run_paper_day 捕获
    后回退全量重算（同一次调用内完成自愈，跑批不会失败）。
    """
    state = _load_state(account_id)
    if state is None:
        raise PaperError("缺少 state.json（增量模式冷启动）")
    done = last_nav_date(account_id)
    if str(state.get("as_of") or "") != (done or ""):
        raise PaperError(
            f"状态不连续：state.as_of={state.get('as_of')} ≠ nav 最后日 {done} —— 需全量重建"
        )

    t0 = time.perf_counter()
    with data_store.read_until(run_date):
        engine = PaperEngine(
            strategy,
            run_date=run_date,
            state=state,
            initial_cash=init_cash,
            max_positions=settings.strategy.max_positions,
            max_weight_per_stock=settings.strategy.max_weight_per_stock,
            benchmark=settings.strategy.benchmark,
            listed_days_min=settings.universe.listed_days_min,
            no_rebuy_days=settings.universe.no_rebuy_days,
            delist_confirm_days=settings.universe.delist_confirm_days,
        )
        result = engine.run_one_day()
    cost_seconds = round(time.perf_counter() - t0, 2)

    # ---- 当日 equity 行（nav/drawdown/benchmark_nav 增量计算）----
    if result.equity.empty:
        raise PaperError(f"增量撮合结果为空（{run_date}）")
    day_row = dict(result.equity.iloc[-1])
    day_row["date"] = pd.to_datetime(day_row["date"])
    new_nav = float(day_row["total_asset"]) / init_cash

    old_nav = pd.read_parquet(account_dir(account_id) / "nav.parquet")
    cummax_prev = float(old_nav["nav"].max()) if not old_nav.empty else init_cash and 1.0
    base = state.get("benchmark_base")
    bnav = None
    if base:
        try:
            bdf = data_store.read_quotes(
                settings.strategy.benchmark, run_date, run_date,
                is_index=True, columns=["close"],
            )
            if not bdf.empty:
                bnav = float(bdf["close"].iloc[0]) / float(base)
        except Exception as exc:  # 基准缺失不阻塞（与 equity_curve NA 行为一致）
            logger.warning(f"[paper] 基准指数读取失败（{run_date}）：{exc!r}")
    day_row.update(
        {
            "nav": new_nav,
            "drawdown": (new_nav / cummax_prev - 1.0) if cummax_prev > 0 else 0.0,
            "benchmark_nav": bnav,
        }
    )
    equity = pd.concat([old_nav, pd.DataFrame([day_row])], ignore_index=True)
    equity = equity[list(EQUITY_COLUMNS)]

    # ---- 追加其余产物（原子覆写；行数 = 全历史 + 1，读写开销可忽略）----
    pdir = account_dir(account_id)
    old_trades = pd.read_parquet(pdir / "trades.parquet")
    old_positions = pd.read_parquet(pdir / "positions.parquet")
    old_orders = pd.read_parquet(pdir / "orders.parquet")
    trades = ensure_contract(
        pd.concat([old_trades, result.trades], ignore_index=True), TRADE_COLUMNS
    )
    positions = ensure_contract(
        pd.concat([old_positions, result.positions], ignore_index=True), POSITION_COLUMNS
    )
    orders = ensure_contract(
        pd.concat([old_orders, result.orders], ignore_index=True), ORDER_COLUMNS
    )
    data_store.write_parquet(equity, pdir / "nav.parquet", allow_empty=True)
    data_store.write_parquet(trades, pdir / "trades.parquet", allow_empty=True)
    data_store.write_parquet(positions, pdir / "positions.parquet", allow_empty=True)
    data_store.write_parquet(orders, pdir / "orders.parquet", allow_empty=True)

    # ---- config.json：全期指标重算（nav/trades 已是完整历史）----
    metrics = metrics_mod.compute_metrics(equity, trades, initial_cash=init_cash)
    config = {
        "account_id": account_id,
        "strategy": strategy_name,
        "params": dict(strategy.params),
        "start_date": state.get("start_date") or PAPER_START_DATE,
        "data_as_of": run_date,
        "init_cash": init_cash,
        "cost_settings": settings.cost.model_dump(),
        "universe_settings": {
            "listed_days_min": settings.universe.listed_days_min,
            "no_rebuy_days": settings.universe.no_rebuy_days,
            "delist_confirm_days": settings.universe.delist_confirm_days,
        },
        "trigger": trigger,
        "cost_seconds": cost_seconds,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "metrics": metrics,
        "engine_mode": "incremental",
        "note": (
            "模拟盘 = quality_reversal（oosma10-base 口径：MA10 上行确认 + 全门开启）"
            "单日增量撮合。⚠️ 该配置无干净样本外佐证（参数在看过牛市OOS 结果后选定，"
            "规格书 §7.15），且本质是「低波+低换+低动+高成长」beta 组合（§7.13）；"
            "本产物用于仿真跟踪，不代表盈利能力。"
            if strategy_name == "quality_reversal"
            else None
        ),
    }
    (pdir / "config.json").write_text(
        json.dumps(json_safe(config), ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    # ---- 跨日状态落盘 ----
    _save_state(account_id, engine.export_state() | {"benchmark_base": base})
    meta_repo.update_paper_run_state(
        account_id,
        last_run_date=run_date,
        cash=float(day_row["cash"] or 0.0),
        status="idle",
    )
    logger.info(
        f"[paper] {account_id} 净值已更新至 {run_date}（增量撮合）："
        f"nav={new_nav:.4f}，耗时 {cost_seconds}s（{trigger}）"
    )
    return _finish_with_alerts(
        account=account,
        account_id=account_id,
        strategy_name=strategy_name,
        result=result,
        equity=equity,
        run_date=run_date,
        trigger=trigger,
        notify=notify,
        summary={
            "account_id": account_id,
            "data_as_of": run_date,
            "nav": new_nav,
            "total_asset": float(day_row["total_asset"]),
            "position_count": int(day_row.get("position_count") or 0),
            "trade_count": len(result.trades),
            "engine_mode": "incremental",
            "cost_seconds": cost_seconds,
            "trigger": trigger,
        },
        entry_candidates=getattr(engine, "last_entry_candidates", None),
        max_positions=int(strategy.params.get("max_positions", 10)),
    )


def _finish_with_alerts(
    *,
    account: dict[str, Any],
    account_id: str,
    strategy_name: str,
    result: Any,
    equity: pd.DataFrame,
    run_date: str,
    trigger: str,
    notify: bool | None,
    summary: dict[str, Any],
    entry_candidates: dict[str, Any] | None,
    max_positions: int,
) -> dict[str, Any]:
    """交易提醒派发（跑批收尾，与执行模式无关）。

    ★ 放在最后、且**不参与 try**：提醒失败绝不回滚已完成的跑批。
    """
    notify_stats = _dispatch_alerts(
        account=account,
        strategy_name=strategy_name,
        result=result,
        equity=equity,
        account_id=account_id,
        run_date=run_date,
        notify=notify,
        entry_candidates=entry_candidates,
        max_positions=max_positions,
    )
    summary["alerts"] = notify_stats
    return summary


def _dispatch_alerts(
    *,
    account: dict[str, Any],
    strategy_name: str,
    result: Any,
    equity: pd.DataFrame,
    account_id: str,
    run_date: str,
    notify: bool | None,
    entry_candidates: dict[str, Any] | None = None,
    max_positions: int = 10,
) -> dict[str, Any]:
    """派发交易提醒（模块 5）。**任何异常都不许冒泡**。

    自动判断闸门：`notify=None` 时，只有 `run_date` 是"最近一个已发生交易日"
    才推。判据用 `calendar.latest_trading_day()`（而不是"今天"）——
    因为 23:00 跑批时"今天"就是 run_date，但补跑时不是。
    """
    if not settings.alert.enabled:
        return {"skipped": True, "reason": "提醒功能未开启"}

    if notify is None:
        latest = _latest_trading_day()
        notify = bool(latest) and run_date >= latest
        if not notify:
            logger.debug(
                f"[paper] {run_date} 非最新交易日（最新 {latest}），跳过交易提醒"
            )

    if not notify:
        return {"skipped": True, "reason": f"{run_date} 非最新交易日，补跑不推送"}

    out: dict[str, Any] = {}
    try:
        from app.services import alert_service  # noqa: PLC0415 —— 避免模块级循环导入

        out["trades"] = alert_service.notify_trades(
            orders=result.orders,
            trades=result.trades,
            nav=equity,
            run_date=run_date,
            account_id=account_id,
            account_name=str(account.get("name") or account_id),
            strategy_name=strategy_name,
            max_positions=int(max_positions),
            async_send=True,      # 别把跑批线程卡在退避睡眠里
        )
    except Exception as exc:
        logger.exception(f"[paper] 交易提醒派发异常（不影响跑批结果）：{exc!r}")
        out["trades"] = {"skipped": True, "reason": f"提醒派发异常：{exc}"}

    # 建仓计划（2026-10-09）：T 日信号 -> T+1 开盘成交，当晚推送、次日开盘前可看。
    # ★ 持仓已满也推送候选（主人指定）—— 引擎槽位满不下单，但候选照报。
    try:
        cands = entry_candidates or {}
        items = cands.get("items") or []
        if items:
            from app.services import alert_service  # noqa: PLC0415

            out["entry_plan"] = alert_service.notify_entry_plan(
                run_date=run_date,
                signal_date=str(cands.get("date") or run_date),
                candidates=items,
                position_count=int(cands.get("position_count") or 0),
                max_positions=int(cands.get("max_positions") or 0),
                account_id=account_id,
                account_name=str(account.get("name") or account_id),
                strategy_name=strategy_name,
                async_send=True,
            )
    except Exception as exc:
        logger.exception(f"[paper] 建仓计划推送异常（不影响跑批结果）：{exc!r}")
        out["entry_plan"] = {"skipped": True, "reason": f"推送异常：{exc}"}
    return out


def _latest_trading_day() -> str | None:
    """日历里最后一个**已发生**的交易日（业务可用上界）。"""
    try:
        stats = calendar.stats()
        return stats.get("last_date") or stats.get("latest_trading_day")
    except Exception as exc:
        logger.warning(f"[paper] 读取最新交易日失败，按不推送处理：{exc!r}")
        return None


def _benchmark_series(start: str, end: str) -> pd.Series | None:
    """基准指数收盘价序列（与 runner._benchmark_series 同口径，读不到返回 None）。"""
    try:
        bdf = data_store.read_quotes(
            settings.strategy.benchmark, start, end, is_index=True, columns=["date", "close"]
        )
    except Exception as exc:
        logger.warning(f"[paper] 基准指数读取失败，净值曲线将不含基准：{exc.message}")
        return None
    if bdf.empty:
        return None
    return pd.Series(
        pd.to_numeric(bdf["close"]).to_numpy(),
        index=pd.to_datetime(bdf["date"]),
    ).sort_index()


def _write_artifacts(
    account_id: str,
    *,
    equity: pd.DataFrame,
    result: Any,
    run_date: str,
    trigger: str,
    cost_seconds: float,
    params: dict[str, Any],
    strategy_name: str,
) -> dict[str, Any]:
    """覆盖落盘 `data/paper/{account_id}/`（nav/positions/trades/orders/config）。

    全量重算模式：五份产物都是"起始日 → run_date"的**整体快照**，
    每次跑批整体覆写 —— `write_parquet` 内部先写临时文件再原子替换，
    中途崩溃不会留下半份产物。

    ★ `orders.parquet` 是阶段 5 加的：交易提醒（模块 5）需要"今天成交了哪几笔"，
      而全量重算下 trades.parquet 每次整体覆写、无法做新旧差；
      只有 orders 带 `exec_date` 能认出当天成交。复用既有 `ORDER_COLUMNS`
      契约，零新契约 —— 引擎本来就在内存里生成它，之前只是没落盘。
    """
    out = account_dir(account_id)
    out.mkdir(parents=True, exist_ok=True)
    # ensure_contract：空表也必须带列结构 —— "0 笔交易"是正常结果，
    # 下游读 df["side"] 不能 KeyError（引擎导出的列契约，与回测产物一致）。
    # allow_empty=True：0 行也写文件，否则"真的 0 笔"与"没落盘"不可区分。
    data_store.write_parquet(equity, out / "nav.parquet", allow_empty=True)
    data_store.write_parquet(
        ensure_contract(result.trades, TRADE_COLUMNS), out / "trades.parquet", allow_empty=True
    )
    data_store.write_parquet(
        ensure_contract(result.positions, POSITION_COLUMNS),
        out / "positions.parquet",
        allow_empty=True,
    )
    data_store.write_parquet(
        ensure_contract(result.orders, ORDER_COLUMNS), out / "orders.parquet", allow_empty=True
    )

    final = equity.iloc[-1]
    config = {
        "account_id": account_id,
        "strategy": strategy_name,
        "params": params,
        "start_date": PAPER_START_DATE,
        "data_as_of": run_date,
        "init_cash": settings.paper.init_cash,
        "cost_settings": settings.cost.model_dump(),
        "universe_settings": {
            "listed_days_min": settings.universe.listed_days_min,
            "no_rebuy_days": settings.universe.no_rebuy_days,
            "delist_confirm_days": settings.universe.delist_confirm_days,
        },
        "trigger": trigger,
        "cost_seconds": cost_seconds,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "metrics": result.metrics,
        "note": (
            "模拟盘 = quality_reversal（oosma10-base 口径：MA10 上行确认 + 全门开启）"
            "日度全量重算。⚠️ 该配置无干净样本外佐证（参数在看过牛市OOS 结果后选定，"
            "规格书 §7.15），且本质是「低波+低换+低动+高成长」beta 组合（§7.13）；"
            "本产物用于仿真跟踪，不代表盈利能力。"
            if strategy_name == "quality_reversal"
            else (
                "模拟盘 = 冻结策略 v1.2 的日度全量重算；策略当前被证实无效"
                "（样本内年化 -49.5%），本产物的价值是验证日度跟踪链路，不代表盈利能力。"
            )
        ),
    }
    # ⚠️ 必须先过 json_safe：metrics 里有 numpy 标量，np.int64 不是 int 子类。
    #    不能只靠 default=str 兜底 —— 那会把 20 变 "20"，前端排序就坏（§4 第 20 条）。
    (out / "config.json").write_text(
        json.dumps(json_safe(config), ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return {
        "account_id": account_id,
        "data_as_of": run_date,
        "nav": float(final["nav"]),
        "total_asset": float(final["total_asset"]),
        "position_count": int(final.get("position_count") or 0),
        "trade_count": int(result.metrics.get("trade_count") or 0),
        "cost_seconds": cost_seconds,
        "trigger": trigger,
    }


# ======================================================================
# 只读路径（API）
# ======================================================================
def list_account_summaries() -> list[dict[str, Any]]:
    """账户列表（含最新净值摘要）。"""
    ensure_default_account()
    out: list[dict[str, Any]] = []
    for row in meta_repo.list_paper_accounts():
        item = dict(row)
        overview = _load_artifacts(row["id"])
        nav_df = overview["nav"]
        if not nav_df.empty:
            last = nav_df.iloc[-1]
            item["data_as_of"] = str(last["date"])[:10]
            item["nav"] = float(last["nav"])
            item["drawdown"] = float(last.get("drawdown") or 0.0)
            item["position_count"] = int(last.get("position_count") or 0)
        item["start_date"] = item.get("start_date") or PAPER_START_DATE
        out.append(item)
    return out


def read_overview(account_id: str) -> dict[str, Any]:
    """详情：账户 + 净值（LTTB）+ 指标 + 最新持仓 + 最近交易 + 任务状态。"""
    account = _require_account(account_id)
    overview = _load_artifacts(account_id)
    nav_df = overview["nav"]

    if nav_df.empty:
        # 延迟 import：scheduler → jobs → 本模块，顶部导入会成环
        from app.scheduler.scheduler import list_jobs  # noqa: PLC0415

        return {
            "account": account,
            "empty": True,
            "note": "暂无净值数据：等首个交易日 23:00 自动跑批，或点「立即跑一次」",
            "job": {
                "schedule": next(
                    (j for j in list_jobs() if j.get("job_id") == "paper_daily"), None
                ),
                "last_log": None,
            },
        }

    points_df = data_store.downsample_series(
        nav_df,
        x_col="date",
        y_cols=("nav", "benchmark_nav", "drawdown"),
        max_points=2000,
    )
    cfg = overview["config"]
    last = nav_df.iloc[-1]

    # ★ 顶部「当前净值」卡与净值曲线保持一致（主人 2026-10-09）：
    # 账户行没有 nav 列（历史设计），此前前端显示为空。一律以净值曲线
    # 最后一点为准 —— 最新交易日未跑批时它就是前一日的净值，跑批后
    # 自动变为最新值。附 nav_date 让前端能标注净值所属交易日。
    account = {
        **account,
        "nav": float(last["nav"]),
        "cash": float(last["cash"]),
        "total_asset": float(last["total_asset"]),
        "position_count": int(last.get("position_count") or 0),
        "drawdown": float(last.get("drawdown") or 0.0),
        "nav_date": str(last["date"])[:10],
    }

    positions_df = overview["positions"]
    latest_pos: list[dict[str, Any]] = []
    if not positions_df.empty:
        last_pos_date = str(positions_df["date"].max())[:10]
        latest_pos = _records_clean(
            positions_df[positions_df["date"].astype(str).str[:10] == last_pos_date]
            .sort_values("market_value", ascending=False)
        )

    trades_df = overview["trades"]
    # ★ 最近交易 = 已平仓记录 ∪ 当前持仓（主人 2026-10-09）：
    #   持仓中的票卖出日/卖出原因/盈亏留空，待卖出后由平仓记录自然接替
    #   （trades 只记已平仓，同一 (symbol, entry_date) 不会两边重复）。
    #   按买入日降序，最多 100 笔。
    closed_rows = _records_clean(trades_df) if not trades_df.empty else []
    open_rows: list[dict[str, Any]] = [
        {
            "symbol": r["symbol"],
            "entry_date": r["entry_date"],
            "entry_price": r.get("entry_price"),
            "shares": r["shares"],
            "exit_date": None,
            "net_pnl": None,
            "net_return": None,
            "exit_reason": None,
            "holding": True,
        }
        for r in latest_pos
    ]
    recent_trades = sorted(
        closed_rows + open_rows,
        key=lambda r: str(r.get("entry_date") or ""),   # ISO 字符串序 = 时间序
        reverse=True,
    )[:RECENT_TRADES_LIMIT]

    # 延迟 import：scheduler → jobs → 本模块，顶部导入会成环
    from app.scheduler.scheduler import list_jobs  # noqa: PLC0415

    job_state = next((j for j in list_jobs() if j.get("job_id") == "paper_daily"), None)
    last_log_rows = meta_repo.db.query(
        "SELECT run_date, status, duration, message, created_at FROM job_run_log "
        "WHERE job_id = 'paper_daily' ORDER BY id DESC LIMIT 1"
    )
    last_log = last_log_rows[0] if last_log_rows else None

    return {
        "account": account,
        "empty": False,
        "data_as_of": str(last["date"])[:10],
        "start_date": cfg.get("start_date", PAPER_START_DATE) if cfg else PAPER_START_DATE,
        "metrics": cfg.get("metrics", {}) if cfg else {},
        "finished_at": cfg.get("finished_at") if cfg else None,
        "note": cfg.get("note") if cfg else None,
        "nav_series": _records_clean(points_df),
        "latest_positions": latest_pos,
        "recent_trades": recent_trades,
        "job": {"schedule": job_state, "last_log": last_log},
    }


def _records_clean(df: pd.DataFrame) -> list[dict[str, Any]]:
    """DataFrame → JSON 安全的 records（NaN → null，时间戳保留由 FastAPI 序列化）。

    ⚠️ NaN 直接进 JSON 会变成裸 `NaN` 字面量，前端 `JSON.parse` 直接报错。
    """
    return df.astype(object).where(df.notna(), None).to_dict("records")


def _load_artifacts(account_id: str) -> dict[str, Any]:
    """读产物（缺文件返回空结构，不抛错 —— 首跑之前目录可能还不存在）。

    ★ `orders` 是模块 5 加的（阶段 5）：它是**交易提醒的事件源** ——
      全量重算下 trades.parquet 每次整体覆写，没法做"新旧文件差"，
      只能靠 orders 的 `exec_date` 认出"今天成交了哪几笔"。
    """
    out = account_dir(account_id)
    nav_df = positions_df = trades_df = orders_df = pd.DataFrame()
    cfg: dict[str, Any] | None = None
    artifacts = {
        "nav": "nav.parquet",
        "positions": "positions.parquet",
        "trades": "trades.parquet",
        "orders": "orders.parquet",
    }
    loaded: dict[str, pd.DataFrame] = {}
    for key, fname in artifacts.items():
        path = out / fname
        if not path.exists():
            loaded[key] = pd.DataFrame()
            continue
        try:
            loaded[key] = data_store.query_duck(f"SELECT * FROM read_parquet('{path.as_posix()}')")
        except Exception as exc:
            logger.warning(f"[paper] {path.name} 读取失败，按空数据处理：{exc!r}")
            loaded[key] = pd.DataFrame()

    nav_df = loaded["nav"]
    positions_df = loaded["positions"]
    trades_df = loaded["trades"]
    orders_df = loaded["orders"]
    cfg_path = out / "config.json"
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            cfg = None
    return {
        "nav": nav_df,
        "positions": positions_df,
        "trades": trades_df,
        "orders": orders_df,
        "config": cfg,
    }


def trigger_run_now() -> dict[str, Any]:
    """手动触发一次跑批（调度器立即执行，HTTP 立即返回）。

    为什么不同步跑：一次跑批 = 增量回补（约 30 分钟）+ 重算（45s），
    HTTP 请求链路里同步等会超时。扔给 APScheduler 立即执行，
    前端轮询账户的 `last_run_date` 与任务日志看结果。
    """
    from app.scheduler.scheduler import _make_runner, get_scheduler  # noqa: PLC0415

    ensure_default_account()
    sched = get_scheduler()
    if not sched.running:
        raise PaperError("调度器未运行（scheduler.enabled=false？），无法触发跑批")
    if _RUN_LOCK.locked():
        raise PaperError("模拟盘跑批已在进行中，请勿重复触发")
    sched.add_job(
        _make_runner("paper_daily"),
        trigger="date",
        run_date=datetime.now(),
        id="paper_daily_manual_once",
        name="模拟盘手动跑批",
        replace_existing=True,
    )
    return {"triggered": True, "note": "已加入调度，跑批完成后账户 last_run_date 会更新"}
