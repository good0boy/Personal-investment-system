"""【历史版本冻结快照】本策略实现：短期动量截面选股 + 上证指数择时（v1.3）。

> ⚠️ **这是 v1.3 规则的冻结快照，不是现役策略。**
> - 来源：`git show HEAD:backend/strategies/impl/momentum.py`
>   （blob `5814f1aa`，最后一笔提交 `ac7f983`，2026-09-22 —— 恰是 v1.3 那一版的代码）。
> - **用途**：只在对照回测 / 参数寻优里跑历史版本（`--strategy momentum_v13`），
>   **不再随主策略演进、不接受新规则改动**。现役策略是 `strategies/impl/momentum.py`。
> - 与原文件**唯一的实质改动**：把 `settings.strategy` 的参数来源换成本模块的
>   `V13_DEFAULTS` 冻结常量（`take_profit_net` / `entry.today_pct_max` / `entry.ret10_min`
>   等字段在 v1.4 之后已被改名或移除，读现役 config 会直接崩）。
>   规则逻辑**逐字未动**：闸门 1 累计口径、闸门 3 无板块化/无上界、**无闸门 4**、
>   闸门 3.5 启用（`exclude_top_pct = 0.2`）、止盈**净口径** 10%、**无个股止损**。
> - ⚠️ 产物落在 `data/backtest/momentum_v13/`（目录按策略名分）。
> - ⚠️ v1.3 当年跑的是**旧引擎（T+1 开盘卖出）**；现在跑在同一套 v1.6 引擎上，
>   清仓成交时点已变为「T 日尾盘按收盘价」，故与之**不可直接比数字**。

**规则唯一依据：`策略规格说明书.md` v1.3**（现役文档已升到 v1.6，v1.3 的规则描述见 §7.3）。

v1.3 变更（2026-09-20）：开仓新增「闸门 3.5 极端动量剔除」—— 按信号日截面
momentum_20 剔除最高 20%（`exclude_top_pct`），只作用于开仓候选。变更依据见
规格书 §7.3（G5 崩坏 + 持有期扫描，`docs/回测与策略研究.md` §3.1/§3.2）。

一句话：在上证指数不处于急跌状态时，买入沪深主板/创业板里"近期持续上涨且
波动大"的强势股，最多持 5 只、每只 20% 仓位，持有满 10 个交易日**或**
扣成本后净盈利 10% 即卖出。

性格（评估结果时必须知道）
------------------------
- **低胜率 + 高盈亏比**：每笔只有 10 个交易日去博 10.25% 的毛涨幅，
  博不到就时间止损出局。多数交易会小赚/小亏，少数止盈贡献主要收益。
  → 看**盈亏比与期望值**，不要只看胜率。
- **交易成本是主要敌人**：持有期短，实测年成本 6%~9%
  （见回测报告里的「年化交易成本」）。
- **不设个股止损**：买入后一路阴跌也要扛满 10 个交易日，
  极端单笔可能亏 15%+。→ 回测报告必须看「最大单笔亏损」。

⚠️ 两个容易混淆的参数（规格 §7.1 特别标注）
    新股入池门槛 **90** 交易日（`universe.listed_days_min`，由引擎的股票池负责）
    卖出后禁买窗口 **60** 交易日（`universe.no_rebuy_days`，同样由股票池负责）
    两者是**独立参数**，不要混用。
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from app.core.config import settings
from app.core.errors import DataNotReadyError
from app.core.logging import logger
from app.services import data_store
from strategies.base import BaseStrategy, StrategyContext
from tools.universe import at_limit_down, price_limit_pct

#: v1.3 的冻结参数默认值。
#: 取值来自 v1.3 那次回测落盘的 `config.json`
#: （`data/backtest/momentum/20260920-234513-044dca7f/config.json` 的 strategy.params），
#: 原文件里这些值本应从 `settings.strategy` 读，但相关字段在 v1.4 之后已被改名/移除。
V13_DEFAULTS: dict[str, Any] = {
    # --- 仓位与持有期 ---
    "max_positions": 5,
    "max_weight_per_stock": 0.20,
    "hold_days_max": 10,
    # --- 清仓：止盈是**扣成本净口径**（v1.3 特有；v1.4 才改成毛口径）---
    "take_profit_net": 0.10,
    "index_panic_pct": -0.015,
    # --- 择时（v1.3 是**累计**口径，不是 v1.4 的逐日口径）---
    "index_code": "sh.000001",
    "timing_ret3_min": -0.01,
    "timing_today_min": -0.005,
    # --- 开仓闸门 3（v1.3 无板块化、无 10 日累计上界、无闸门 4）---
    "entry_today_pct_min": 0.01,
    "entry_today_pct_max": 0.05,
    "entry_ret3_min": 0.05,
    "entry_ret10_min": 0.05,
    "entry_ret20_min": 0.05,
    "amplitude_window": 5,
    # --- 闸门 3.5（v1.3 **启用**，剔除极端动量前 20%）---
    "exclude_top_pct": 0.2,
}


class MomentumV13Strategy(BaseStrategy):
    """动量选股 + 指数择时（**v1.3 冻结快照**，仅供对照回测）。"""

    name = "momentum_v13"
    version = "1.3"      # 跟随策略规格说明书版本

    #: 闸门 3.5 固定使用的剔除因子（规格 v1.3 写死 momentum_20，不做参数）
    _EXCLUDE_FACTOR = "momentum_20"

    # ------------------------------------------------------------------
    # 参数（默认取自本模块的 V13_DEFAULTS 冻结常量，可被调用方覆盖）
    # ------------------------------------------------------------------
    def __init__(self, params: dict[str, Any] | None = None) -> None:
        super().__init__(params)
        p = dict(V13_DEFAULTS)
        p.update(self.params)          # 调用方传入的覆盖默认值（参数寻优靠它）
        self.params = p

        # 闸门 3.5 的运行期状态：
        #   _m20_loaded  只加载一次的闸（含加载失败的情况，失败不会反复重试）
        #   _m20_matrix  {date × symbol} 矩阵；None = 数据不可用（已告警）
        #   _m20_warned  各类降级告警只打一次，刷屏没意义
        #   _excl_stats  剔除统计（teardown 汇总输出，规则效果必须可见）
        self._m20_loaded = False
        self._m20_matrix: pd.DataFrame | None = None
        self._m20_warned = False
        self._excl_stats = {"days": 0, "excluded": 0, "missing_days": 0}

    # ------------------------------------------------------------------
    def on_bar(self, ctx: StrategyContext) -> None:
        di = ctx.market.date_index[ctx.day]
        self._sell_side(ctx, di)
        self._buy_side(ctx, di)

    def teardown(self, ctx: StrategyContext) -> None:
        """剔除规则的效果必须可见（「算了不呈现 = 白算」）—— 结束时汇总一次。"""
        if float(self.params["exclude_top_pct"]) <= 0:
            return
        s = self._excl_stats
        logger.info(
            f"闸门 3.5 极端动量剔除统计：应用 {s['days']} 个信号日｜"
            f"累计剔除候选 {s['excluded']} 只｜因子缺失跳过 {s['missing_days']} 日"
        )

    # ==================================================================
    # 清仓（规格 2.3：5 个条件，任一触发即卖出；T 日收盘判定 → T+1 开盘卖）
    # ==================================================================
    def _sell_side(self, ctx: StrategyContext, di: int) -> None:
        market_panic = self._is_market_panic(ctx, di)

        for symbol in list(ctx.account.positions.keys()):
            reasons: list[str] = []

            # 条件 1 · 时间止损：持有满 hold_days_max 个交易日
            #   ⚠️ hold_days() 是**含首尾**计数（买入成交日算第 1 天），
            #      与规格的 `trading_day_offset(entry, T) >= 10` 口径一致。
            if ctx.account.hold_days(symbol) >= int(self.params["hold_days_max"]):
                reasons.append("time_stop")

            # 条件 2 · 止盈：**扣成本后**的净浮盈达标
            net = ctx.account.net_return(symbol, di)
            if np.isfinite(net) and net > float(self.params["take_profit_net"]):
                reasons.append("take_profit")

            # 条件 3 · 大盘急跌：上证**当日收盘**跌幅超阈值
            if market_panic:
                reasons.append("market_panic")

            # 条件 4 · 跌停（收盘价 = 跌停价）
            if self._is_limit_down(ctx, symbol, di):
                reasons.append("limit_down")

            # 条件 5 · 风险警示：当日 isST=1
            if self._is_st(ctx, symbol, di):
                reasons.append("st")

            if reasons:
                # 多个条件同时成立时**全部记录**（用 | 连接），便于事后归因
                ctx.account.order_sell(symbol, reason="|".join(reasons))

    def _is_market_panic(self, ctx: StrategyContext, di: int) -> bool:
        pct = ctx.market.index_pct(di)
        if not np.isfinite(pct):
            return False
        return pct < float(self.params["index_panic_pct"])

    def _is_limit_down(self, ctx: StrategyContext, symbol: str, di: int) -> bool:
        """收盘价是否触及跌停价（比例容差，复权安全 —— 走 universe 单一实现）。"""
        close = ctx.market.close_at(symbol, di)
        pre = ctx.market.preclose_at(symbol, di)
        if not np.isfinite(close) or not np.isfinite(pre) or pre <= 0:
            return False
        si = ctx.market.symbol_index[symbol]
        limit = price_limit_pct(
            symbol, ctx.market.dates[di], int(ctx.market.is_st[di, si])
        )
        return at_limit_down(close, pre, limit)

    def _is_st(self, ctx: StrategyContext, symbol: str, di: int) -> bool:
        si = ctx.market.symbol_index.get(symbol)
        if si is None or not ctx.market.has_data[di, si]:
            return False
        return int(ctx.market.is_st[di, si]) == 1

    # ==================================================================
    # 开仓（规格 2.2：三道闸门，须全部通过）
    # ==================================================================
    def _buy_side(self, ctx: StrategyContext, di: int) -> None:
        n_hold = ctx.account.position_count
        max_pos = int(self.params["max_positions"])

        # 闸门 2 · 持仓数已满 → 本轮不开仓
        if n_hold >= max_pos:
            return

        # 闸门 1 · 大盘择时（近 3 日累计 > 阈值 且 当日 > 阈值）
        ret3 = ctx.market.index_ret(di, 3)
        today = ctx.market.index_pct(di)
        if not (
            np.isfinite(ret3)
            and np.isfinite(today)
            and ret3 > float(self.params["timing_ret3_min"])
            and today > float(self.params["timing_today_min"])
        ):
            return

        # 闸门 3 · 个股筛选
        #   候选范围来自引擎构建的**当日股票池** —— 板块/ST/停牌/新股/
        #   一字涨跌停/禁买窗口 这些红线过滤已在那里做过，这里不重复实现。
        universe = ctx.universe
        pool: list[str] = list(getattr(universe, "symbols", []) or [])
        # 规格 v1.3 · 闸门 3.5：极端动量剔除 —— 先剔，再做个股筛选与排序
        pool = self._apply_extreme_momentum_exclusion(ctx, pool)
        held = set(ctx.account.positions.keys())
        need = max_pos - n_hold

        cands: list[tuple[str, float]] = []
        for symbol in pool:
            if symbol in held:
                continue
            if not self._entry_signal(ctx, symbol, di):
                continue
            amp = ctx.market.avg_amplitude(symbol, di, int(self.params["amplitude_window"]))
            if not np.isfinite(amp):
                continue
            cands.append((symbol, float(amp)))

        if not cands:
            return

        # 排序：近 N 日平均振幅**降序**；代码作 tiebreaker，保证可复现
        cands.sort(key=lambda x: (-x[1], x[0]))

        target_amount = float(self.params["max_weight_per_stock"]) * ctx.total_asset
        for symbol, _amp in cands[:need]:
            ctx.account.order_buy(symbol, target_amount, reason="momentum")

    def _entry_signal(self, ctx: StrategyContext, symbol: str, di: int) -> bool:
        """规格 2.2 闸门 3 的 5 个条件（前 4 个在这里；第 5 个由股票池的禁买窗口负责）。"""
        r1 = ctx.market.pct_chg[di, ctx.market.symbol_index[symbol]] / 100.0
        if not np.isfinite(r1):
            return False

        # ① 今日涨跌幅 ∈ (下限, 上限) —— 上涨但未追高
        if not (
            float(self.params["entry_today_pct_min"])
            < r1
            < float(self.params["entry_today_pct_max"])
        ):
            return False

        # ②③④ 近 3 / 10 / 20 个交易日累计涨幅均 > 阈值
        for n, key in ((3, "entry_ret3_min"), (10, "entry_ret10_min"), (20, "entry_ret20_min")):
            r = ctx.market.cum_return(symbol, di, n)
            if not np.isfinite(r) or r <= float(self.params[key]):
                return False

        # ⑤ 最近 60 个交易日未买过该股票 → 由股票池的禁买窗口过滤（引擎传入
        #    last_sell_date + calendar_days_fn），此处不再重复。
        return True

    # ------------------------------------------------------------------
    # 闸门 3.5（规格 v1.3）：极端动量剔除
    # ------------------------------------------------------------------
    def _momentum_matrix(self) -> pd.DataFrame | None:
        """读剔除因子的全量长表并转成 {date × symbol} 矩阵（实例内只加载一次）。

        为什么整表读进内存而不是逐日查询：回测 1100+ 个交易日，逐日 DuckDB
        查询的启动开销远大于一次整表读 + 一次 pivot（全量约 700 万行，
        pivot 后约 60 MB，可接受）。时间对齐：引擎侧 `ctx.day` 与矩阵行键
        统一为 `YYYY-MM-DD` 字符串。

        返回 None = 因子数据不可用（文件缺失/空表）。**只告警一次**，之后
        所有信号日直接跳过剔除 —— 数据缺失时降级为 v1.2 行为，但必须留痕。
        """
        if self._m20_loaded:
            return self._m20_matrix
        self._m20_loaded = True
        try:
            df = data_store.read_factor(self._EXCLUDE_FACTOR)
        except DataNotReadyError as exc:
            self._m20_warned = True
            logger.warning(
                f"闸门 3.5：因子 {self._EXCLUDE_FACTOR} 不可用（{exc}），"
                f"本次运行极端动量剔除失效（按 v1.2 行为执行）"
            )
            return None
        if df.empty:
            self._m20_warned = True
            logger.warning(
                f"闸门 3.5：因子 {self._EXCLUDE_FACTOR} 数据为空，本次运行剔除失效"
            )
            return None
        d = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
        # pivot_table：万一历史长表里有 (date, symbol) 重复，按均值聚合而不是报错
        self._m20_matrix = df.assign(date=d).pivot_table(
            index="date", columns="symbol", values="value", aggfunc="mean"
        )
        return self._m20_matrix

    def _apply_extreme_momentum_exclusion(
        self, ctx: StrategyContext, pool: list[str]
    ) -> list[str]:
        """规格 v1.3 闸门 3.5：剔除 T 日截面 momentum_20 最高 exclude_top_pct 分位的股票。

        - 分位基准 = 当日**股票池**（引擎注入的可交易池，与因子研究的可交易池
          同口径），不是候选子集 —— 候选只是全池的子样本，拿它算分位会漂移。
        - 个股缺因子值（NaN / 不在截面里）→ 无法判定，**不剔除**。
        - 因子整体不可用或当日截面缺失 → 该日跳过剔除并告警/计数（不允许静默）。
        """
        pct = float(self.params["exclude_top_pct"])
        if pct <= 0:
            return pool
        mat = self._momentum_matrix()
        if mat is None or ctx.day not in mat.index:
            self._excl_stats["missing_days"] += 1
            if mat is not None and not self._m20_warned:
                # 数据整体在、唯独这一天没有 —— 首次出现时告警，之后交给 teardown 统计
                self._m20_warned = True
                logger.warning(
                    f"闸门 3.5：{ctx.day} 不在 {self._EXCLUDE_FACTOR} 截面里，该日跳过剔除"
                )
            return pool
        row = pd.to_numeric(mat.loc[ctx.day].reindex(pool), errors="coerce")
        finite = row.dropna()
        if finite.empty:
            self._excl_stats["missing_days"] += 1
            return pool
        threshold = float(np.quantile(finite.to_numpy(dtype=float), 1.0 - pct))
        excluded = row >= threshold          # NaN >= thr → False → 缺值保留
        self._excl_stats["days"] += 1
        self._excl_stats["excluded"] += int(excluded.sum())
        return [s for s, ex in zip(pool, excluded.to_numpy(), strict=True) if not ex]

    # ==================================================================
    def config_snapshot(self) -> dict[str, Any]:
        snap = super().config_snapshot()
        snap["rules_version"] = "策略规格说明书.md v1.3"
        snap["description"] = "短期动量截面选股 + 上证指数择时（v1.3 起开仓剔除极端动量前 20%）"
        snap["exclude_factor"] = self._EXCLUDE_FACTOR
        # 股票池口径写进快照：换规则时能一眼看出当时用的是哪一套
        snap["boards"] = ["sh_main", "sz_main", "gem"]
        snap["no_rebuy_days"] = settings.universe.no_rebuy_days
        snap["listed_days_min"] = settings.universe.listed_days_min
        return snap
