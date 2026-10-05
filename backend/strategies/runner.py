"""回测执行流水线 —— CLI（`run.py`）与参数寻优（`sweep.py`）共用的那一段。

为什么单独成一个模块
------------------
一次回测要做的事有 10 步（建配置 → 登记 → 跑引擎 → 补基准曲线 → 算指标 →
落盘 → 回写状态）。`run.py` 与 `sweep.py` 都要做，**只是输入参数不同**。

如果让 sweep 自己再拼一遍，两处就会慢慢走偏（历史上本项目已经踩过：
`refresh()` 与 `stats()` 各拼一份统计结构，字段名与含义都不一致，
CLI 打印出 `None` 却全程不报错）。所以：**流水线只有一份**。

与 `run.py` 的分工
-----------------
- `runner.py`：纯执行，不解析命令行、不决定区间、不打印报告。
- `run.py`    ：单次回测 CLI（解析参数 → 调 runner → 打印报告）。
- `sweep.py`  ：参数寻优 CLI（展开网格 → 循环调 runner → 汇总）。
"""
from __future__ import annotations

import time
from datetime import datetime
from itertools import product
from typing import Any

import pandas as pd

from app.core.config import settings
from app.core.errors import AppError
from app.core.logging import logger
from app.repos import meta_repo
from app.services import data_store
from app.services import metrics as metrics_mod
from strategies import registry
from strategies.base import make_run_id, run_layout, write_run
from strategies.engine import BacktestEngine, MarketData

#: 预热起点：回测起点前需要 ≥20 个交易日预热，取 5 个月（实测 97 个交易日）
PREHEAT_START = "2020-01-01"

#: 网格组数上限 —— 防止手滑把 4×4 写成 40×40（一次几小时）。
#: v1.5 由 256 提到 2048：主网格（止盈4 × 止损4 × 窗口4 × 持有期3 × 仓位3 × 持仓数2）
#: 已是 1152 组。仍保留硬上限 + `WARN_GRID_COMBOS` 的耗时提醒，护栏不至于形同虚设。
MAX_GRID_COMBOS = 2048

#: 超过这个组数就提醒预计耗时（按实测 ≈17.6s/组 估算）
WARN_GRID_COMBOS = 512


def build_config(
    strategy_name: str,
    *,
    params: dict[str, Any],
    interval: dict[str, Any],
    symbols: list[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """组装写进 `config.json` 的配置快照。

    ★ 这个 dict **参与 run_id 哈希**（`make_run_id`），所以：
      - 参数不同 → 哈希不同 → run_id 不同 → 可区分，这是需要的
      - `extra` 里的东西若含时间戳/随机值，会让"同配置两次跑"哈希不同，
        破坏"哈希相同 = 配置相同"的语义。**别往里塞时间**。
    """
    strategy = registry.get_strategy(strategy_name, params)
    sp = strategy.params
    config: dict[str, Any] = {
        "strategy": {
            **strategy.config_snapshot(),
            # 基准指数写进快照：详情页要显示"跟谁比"，
            # 而它来自 settings 而非策略本身，不写就永远是空。
            "benchmark": settings.strategy.benchmark,
        },
        "interval": interval,
        "capital": {
            "initial_cash": settings.strategy.init_cash,
            # ★ 取**策略实际参数**而不是 settings：参数寻优会覆盖 max_positions /
            #   max_weight_per_stock，若这里写 settings 的固定值，config.json 就会
            #   记成"跑了 20%/5 只"而实际跑的是别的 —— 又是"写了没被读"那类坑。
            "max_positions": sp.get("max_positions", settings.strategy.max_positions),
            "max_weight_per_stock": sp.get(
                "max_weight_per_stock", settings.strategy.max_weight_per_stock
            ),
        },
        "universe": settings.universe.model_dump(),
        "cost": settings.cost.model_dump(),
        "data": {"adjust": settings.data.adjust, "source": settings.datasource.primary},
        # ★ 调试运行必须**自己声明**跑了几只标的。
        #   否则一个只跑 3 只的 run 与全市场 run 的产物结构一模一样，
        #   列表页上两者看不出区别 —— 拿它评估策略就是自欺（详情接口会把
        #   它变成一条醒目的红色提示）。
        "debug_symbols": symbols,
    }
    if extra:
        config.update(extra)
    return config


def load_market(
    *,
    start: str,
    end: str,
    symbols: list[str] | None = None,
    benchmark: str | None = None,
) -> MarketData:
    """加载一次行情矩阵（**参数寻优复用同一份**，见 `run_backtest(market=...)`）。"""
    return MarketData.load(
        start=start,
        end=end,
        symbols=symbols,
        index_symbol=benchmark or settings.strategy.benchmark,
    )


def _benchmark_series(start: str, end: str) -> pd.Series | None:
    """基准指数收盘价序列（读不到就返回 None，净值曲线只是少一条线）。"""
    try:
        bdf = data_store.read_quotes(
            settings.strategy.benchmark, start, end, is_index=True, columns=["date", "close"]
        )
    except AppError as exc:
        logger.warning(f"基准指数读取失败，净值曲线将不含基准：{exc.message}")
        return None
    if bdf.empty:
        return None
    return pd.Series(
        pd.to_numeric(bdf["close"]).to_numpy(),
        index=pd.to_datetime(bdf["date"]),
    ).sort_index()


def run_backtest(
    strategy_name: str,
    *,
    start: str,
    end: str,
    interval_name: str,
    is_out_sample: bool,
    params: dict[str, Any] | None = None,
    tag: str = "",
    symbols: list[str] | None = None,
    extra_config: dict[str, Any] | None = None,
    market: MarketData | None = None,
    write_trades: bool = True,
    extra_run: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """跑一次回测并落盘。返回摘要（含 `run_id` / `metrics` / `root`）。

    Args:
        market: 已加载的行情矩阵。参数寻优整轮只加载一次，16 组共用 ——
            `MarketData` 只读且与策略参数无关，复用不改任何回测语义。
        write_trades: 是否写 trades/positions/orders。参数寻优只关心指标，
            不写这三张表能省下大量磁盘（16 组 × 几百笔）。**默认写**，
            单次回测要保留完整证据链。
            ⚠️ 不写时产物里会记 `_run.detail_written=False` ——
               下游靠它区分"真的 0 笔交易"和"没落盘"，否则详情页会
               显示"交易数 392"却一笔都列不出来。
        extra_run: 写进 `config.json` 的 `_run` 段（不进 run_id 哈希），
            用于标记来源（参数寻优写 sweep_id / combo_index）。
    """
    params = dict(params or {})
    strategy = registry.get_strategy(strategy_name, params)

    config = build_config(
        strategy_name,
        params=params,
        interval={
            "name": interval_name,
            "start": start,
            "end": end,
            "preheat_start": PREHEAT_START,
            "is_out_sample": is_out_sample,
        },
        symbols=symbols,
        extra=extra_config,
    )
    run_id = make_run_id(config)
    layout = run_layout(strategy.name, run_id)

    meta_repo.register_strategy(
        strategy.name,
        strategy.name,
        impl_path=f"strategies/impl/{strategy.name}.py",
        version=strategy.version,
        description=registry.describe(strategy.name)["description"],
    )
    meta_repo.register_backtest_run(
        run_id, strategy.name, start, end,
        tag=tag, is_out_sample=is_out_sample, param=strategy.params, config=config,
    )

    # ★ 时间锁：回测期间任何读取都不得越过回测终点。
    #   `_apply_read_until` 还会额外卡"今天"（见 data_store._upper_bound）。
    data_store.set_read_until(end)

    t0 = time.perf_counter()
    try:
        engine = BacktestEngine(
            strategy,
            start=start,
            end=end,
            preheat_start=PREHEAT_START,
            initial_cash=settings.strategy.init_cash,
            max_positions=int(strategy.params.get("max_positions", settings.strategy.max_positions)),
            max_weight_per_stock=float(
                strategy.params.get("max_weight_per_stock", settings.strategy.max_weight_per_stock)
            ),
            symbols=symbols,
            benchmark=settings.strategy.benchmark,
            listed_days_min=settings.universe.listed_days_min,
            no_rebuy_days=settings.universe.no_rebuy_days,
            delist_confirm_days=settings.universe.delist_confirm_days,
            market=market,
        )
        result = engine.run()
    except Exception as exc:
        logger.exception(f"回测失败：{exc}")
        meta_repo.finish_backtest_run(
            run_id, "failed", message=f"{type(exc).__name__}: {str(exc)[:500]}"
        )
        raise
    finally:
        data_store.set_read_until(None)

    cost_seconds = time.perf_counter() - t0

    # 补齐净值曲线（nav / drawdown / 基准），前端可直接画
    equity = metrics_mod.equity_curve(
        result.equity,
        initial_cash=settings.strategy.init_cash,
        benchmark=_benchmark_series(start, end),
    )
    metrics = result.metrics

    write_run(
        layout,
        config=config,
        equity=equity,
        trades=result.trades if write_trades else None,
        positions=result.positions if write_trades else None,
        orders=result.orders if write_trades else None,
        metrics=metrics,
        tag=tag,
        detail_written=write_trades,
        extra_run=extra_run,
        started_at=datetime.fromtimestamp(time.time() - cost_seconds).isoformat(
            timespec="seconds"
        ),
    )
    meta_repo.finish_backtest_run(
        run_id, "success",
        metrics=metrics,
        cost_seconds=round(cost_seconds, 2),
        trade_count=int(metrics.get("trade_count") or 0),
    )

    return {
        "strategy": strategy.name,
        "strategy_version": strategy.version,
        "run_id": run_id,
        "params": dict(strategy.params),
        "interval_name": interval_name,
        "start": start,
        "end": end,
        "is_out_sample": is_out_sample,
        "tag": tag,
        "debug_symbols": symbols,
        "cost_seconds": round(cost_seconds, 2),
        "root": str(layout.root),
        "metrics": metrics,
    }


def _normalize_grid(
    grid: dict[str, list[Any]],
) -> tuple[list[str], list[list[dict[str, Any]]]]:
    """把网格每个 key 的候选值统一成"参数 dict 列表"。

    - 标量候选 `v` → `{key: v}`
    - **联合维度**（v1.4 新增）：候选值为 dict → 直接展开成多个参数键，
      用于表达"两个参数成对联动"（如止盈分主板/创业板两套、按档位配对）。

    校验（宁可早报错，也别静默少跑/互相覆盖）：
      - 同一 key 的候选**不得混用**标量与 dict；
      - 联合 dict 里的键**不得与其它维度重复**（否则 `dict.update` 会静默覆盖）。
    """
    keys = sorted(grid)  # 排序让组序稳定、可复现
    per_key: list[list[dict[str, Any]]] = []
    seen: set[str] = set()
    for k in keys:
        vals = list(grid[k])
        if not vals:
            raise AppError(f"参数 {k} 的候选值为空")
        has_dict = any(isinstance(v, dict) for v in vals)
        has_scalar = any(not isinstance(v, dict) for v in vals)
        if has_dict and has_scalar:
            raise AppError(f"参数 {k} 的候选值不得混用标量与联合(dict)：{vals!r}")
        if has_dict:
            key_sets: list[frozenset[str]] = []
            for d in vals:
                dd = dict(d)
                if not dd:
                    raise AppError(f"参数 {k} 的联合候选不能是空 dict")
                key_sets.append(frozenset(dd))
            # 同一联合维度的各候选必须"键集一致"，否则是残缺的交叉，容易误解
            if len(set(key_sets)) != 1:
                raise AppError(
                    f"参数 {k} 的联合候选键集不一致：{sorted(vals, key=str)}"
                )
            dim_keys = set(key_sets[0])
            # 只与**其它维度**比重复（同一维度内相邻候选本来就用同一批键）
            dup = sorted(dim_keys & seen)
            if dup:
                raise AppError(f"联合维度 {k} 的参数键与其它维度重复：{dup}")
            seen.update(dim_keys)
            per_key.append([dict(d) for d in vals])
        else:
            if k in seen:
                raise AppError(f"参数键重复：{k}")
            seen.add(k)
            per_key.append([{k: v} for v in vals])
    return keys, per_key


def grid_param_names(grid: dict[str, list[Any]]) -> list[str]:
    """网格展开后的**真实参数名**（联合维度展开成多个键）。

    注意：与 `sorted(grid)` 不同 —— 网格里可能只有一个“联合维度” key
    （如 `take_profit`），展开后却是 `take_profit_gross_main/_gem` 两个参数。
    """
    _, per_key = _normalize_grid(grid)
    names: set[str] = set()
    for choices in per_key:
        for d in choices:
            names.update(d)
    return sorted(names)


def expand_grid(grid: dict[str, list[Any]]) -> list[dict[str, Any]]:
    """参数网格 → 组合列表（笛卡尔积，支持联合维度）。

    用 `itertools.product` 而不是手写嵌套循环：手写版本漏一层参数不会报错，
    只会静默少跑好几组（而"少跑"在结果里完全看不出来）。
    """
    if not grid:
        raise AppError("参数网格为空")
    _, per_key = _normalize_grid(grid)
    combos: list[dict[str, Any]] = []
    for picks in product(*per_key):
        merged: dict[str, Any] = {}
        for d in picks:
            merged.update(d)
        combos.append(merged)
    if len(combos) > MAX_GRID_COMBOS:
        raise AppError(
            f"网格共 {len(combos)} 组，超过上限 {MAX_GRID_COMBOS}。"
            f"参数寻优是计算密集任务（每组 ≈ 一次完整回测），请缩小网格。"
        )
    if len(combos) > WARN_GRID_COMBOS:
        # 上限放宽到 2048 后，这里补一条耗时提醒 —— 否则护栏形同虚设
        logger.warning(
            f"网格共 {len(combos)} 组：按实测 ≈17.6s/组（全市场）估算，"
            f"预计耗时约 {len(combos) * 17.6 / 3600:.1f} 小时。建议后台跑。"
        )
    return combos
