"""模拟盘单日增量执行器（`PaperEngine`）—— 2026-10-10 方案批准后实施。

与 `BacktestEngine` 的关系
--------------------------
**继承**而非另写：窗口矩阵实例化（start=end=run_date、预热窗口往前推 N 个
交易日），`run()` 的逐日 6 步原样复用 —— 循环长度天然只有 1 天。
策略（`on_bar/_sell_side/_buy_side`）、成本模型（纯函数）、撮合规则
（`_execute_pending*` 等）与全量模式**零分叉**。

跨日状态
--------
全量模式的状态（cash/positions/last_sell_date/pending）只活在引擎内存；
增量模式每晚"接着昨天跑"，这些状态必须落盘（`state.json`，由
paper_service 读写）：

- `entry_idx` / `signal_idx` 是**矩阵行索引** —— 窗口起点每天在变，
  状态里只存**日期**，载入时用当日窗口矩阵的 `date_index` 重算行号；
- `last_sell_date` 的值本来就是日期字符串，原样存取；
- `pending`（顺延单）用 `run(final_pending="keep")` 保留 ——
  T 日尾盘挂的卖单/涨停没买进的买单，明天开盘还要执行；
  全量模式的"末日 rejected"语义在增量下**绝不能发生**。

退市强平
--------
主人拍板（2026-10-10）：接受时点差。窗口矩阵下 `_derive_last_data_di`
判定天然工作（窗口截断未来数据），与全量的确认延迟差 1-3 个交易日，
对拍时按白名单规则单独核对。

自愈
----
状态载入的任何异常（KeyError/窗口缺数据/损坏）由调用方捕获后回退
全量重算 —— PaperEngine 自身不吞异常，坏了就大声抛。
"""
from __future__ import annotations

from typing import Any

from app.core.logging import logger
from app.services.calendar_service import calendar
from strategies.base import BaseStrategy
from strategies.engine import BacktestEngine, Order, Position

#: 窗口预热天数（交易日）。实测全量预热 97 交易日（ret_50 窗口 + 禁买 60 日
#: + 新股 90 日门槛 + 门1 指数 20 日）—— 取 120 留余量。
DEFAULT_PREHEAT_DAYS = 120


class PaperEngine(BacktestEngine):
    """单日增量执行器：载入 T-1 状态 → 只执行 run_date 一天 → 导出新状态。"""

    def __init__(
        self,
        strategy: BaseStrategy,
        *,
        run_date: str,
        state: dict[str, Any],
        initial_cash: float,
        max_positions: int = 5,
        max_weight_per_stock: float = 0.20,
        benchmark: str = "sh.000001",
        listed_days_min: int | None = None,
        no_rebuy_days: int | None = None,
        delist_confirm_days: int = 3,
        preheat_days: int = DEFAULT_PREHEAT_DAYS,
        preheat_start: str | None = None,
        rebalance_anchor: str | None = None,
        trading_days: list[str] | None = None,
    ) -> None:
        self._state = dict(state)
        self._run_date = run_date

        # 预热起点：显式传入（对拍测试的合成日历与真实交易日历不同轴），
        # 否则按交易日历回推 preheat_days 个交易日（生产路径）。
        if preheat_start is None:
            preheat_start = str(calendar.prev_trading_day(run_date, n=preheat_days))

        super().__init__(
            strategy,
            start=run_date,
            end=run_date,
            preheat_start=str(preheat_start),
            initial_cash=initial_cash,  # 占位 —— 紧接着被 T-1 状态覆盖
            max_positions=max_positions,
            max_weight_per_stock=max_weight_per_stock,
            symbols=None,  # 全市场（与全量口径一致，截面统计必须全池）
            benchmark=benchmark,
            listed_days_min=listed_days_min,
            no_rebuy_days=no_rebuy_days,
            delist_confirm_days=delist_confirm_days,
            rebalance_anchor=rebalance_anchor,
            trading_days=trading_days,
        )
        self._inject_state()

    # ------------------------------------------------------------------
    # 状态注入（T-1 → 今天）
    # ------------------------------------------------------------------
    def _inject_state(self) -> None:
        """把 `state.json` 的跨日状态灌进引擎（行索引按当日窗口矩阵重算）。

        任何重映射失败（如持仓买入日不在窗口内 —— 理论上不可能：
        hold_days_max=30 << 120）直接抛异常，由调用方回退全量重算。
        """
        st = self._state
        self.cash = float(st["cash"])

        positions: dict[str, Position] = {}
        for p in st.get("positions") or []:
            entry_date = str(p["entry_date"])[:10]
            if entry_date not in self.market.date_index:
                raise KeyError(
                    f"持仓 {p['symbol']} 的买入日 {entry_date} 不在当日窗口矩阵内"
                    f"（{self.market.dates[0]}~{self.market.dates[-1]}）—— 状态异常"
                )
            positions[p["symbol"]] = Position(
                symbol=p["symbol"],
                shares=int(p["shares"]),
                entry_date=entry_date,
                entry_price=float(p["entry_price"]),
                entry_cost=float(p.get("entry_cost") or 0.0),
                entry_idx=self.market.date_index[entry_date],
                entry_reason=str(p.get("entry_reason") or ""),
            )
        self.positions = positions

        self.last_sell_date = {
            sym: str(d)[:10] for sym, d in (st.get("last_sell_date") or {}).items()
        }

        pending: list[Order] = []
        for o in st.get("pending") or []:
            sig_date = str(o["signal_date"])[:10]
            if sig_date not in self.market.date_index:
                raise KeyError(
                    f"顺延单 {o['symbol']} 的信号日 {sig_date} 不在当日窗口矩阵内 —— 状态异常"
                )
            pending.append(
                Order(
                    signal_date=sig_date,
                    signal_idx=self.market.date_index[sig_date],
                    symbol=o["symbol"],
                    side=o["side"],
                    reason=str(o.get("reason") or ""),
                    target_amount=(float(o["target_amount"]) if o.get("target_amount") is not None else None),
                    shares=(int(o["shares"]) if o.get("shares") is not None else None),
                    retries=int(o.get("retries") or 0),
                )
            )
        self.pending = pending

        # run() 第 2 步会重算；这里先标一次，保证强平判定等不依赖旧值
        self._set_total_asset(self.first_di)
        logger.info(
            f"[PaperEngine] 状态注入完成：cash ¥{self.cash:,.0f}｜"
            f"持仓 {len(self.positions)} 只｜顺延单 {len(self.pending)} 笔｜"
            f"窗口 {self.market.dates[0]}~{self.market.dates[-1]}"
        )

    # ------------------------------------------------------------------
    # 执行与导出
    # ------------------------------------------------------------------
    def run_one_day(self) -> Any:
        """执行 run_date 一天（开盘撮昨日单 → 决策 → 尾盘撮清仓单 → 快照）。

        与全量 `run()` 的唯一差别：末日挂单**保留**（明天开盘执行）。
        """
        return self.run(final_pending="keep")

    # export_state 继承自 BacktestEngine（基类统一实现，两个引擎同构）
