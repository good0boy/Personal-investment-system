"""策略 v2.0/v2.1/v2.2「中频质量反转」（`quality_reversal`）。

**规则唯一依据：`docs/策略规格说明书-v2.0-中频质量反转.md`（文档内版本 v2.2）。**
本文件只实现它 —— 规则要改先改文档并升版本，再改这里。

v2.2 变更（2026-10-06）
----------------------
**阈值基准 `p` 的口径改了**（止损/止盈的倍数档位不变）：
- v2.1：`p = |近 5 日累计涨跌幅| ÷ 5`（因日间正负对消而**系统性偏小**，实测中位 ≈0.93%）
- **v2.2：`p = mean(|当日涨跌幅|)`，取最近 5 个交易日**（实测中位 ≈**2.25%**，约 v2.1 的 2.43 倍）
- 并要求窗口内 **5 天全部可交易**（`trade_status == 1`）且 `pct_chg` 有限，否则 `p` 不可用 → 不触发。
- 效果：`1×p` 止损线由 −0.4%（"贴脸"）变成 **−2.25%（正常量级）**。

v2.1 变更（2026-10-06）
----------------------
**新增止损 / 止盈两条退出**（规格 §2.3 条件 5/6）：
- 阈值基准 **`p` = |个股近 5 日累计涨跌幅| ÷ 5**（近 5 日**日均**涨跌幅的绝对值，**不是振幅**）
  —— p 是个股价、每只不同，阈值随个股近期波动自适应。
- `stop_loss_p_mult` / `take_profit_p_mult`：毛损益 ≥ mult × p 即触发；`take_profit_abs` 是绝对止盈档。
- **0 = 关闭**（参数默认全 0 → 行为与 v2.0/M1 基线一致）；`p` 不可用 → 不触发（不误杀）。
- 与既有的 `over_realized` / `time_stop` / `quality_broken` / `st` / `limit_down` **并存**。

定位（与现役 v1.6 `momentum` 的本质差别）
-----------------------------------------
| 维度 | v1.6 momentum | **v2.0 quality_reversal** |
|---|---|---|
| 信号方向 | 买**最强**（动量/涨幅/振幅） | 买**温和弱势**（反转）+ 基本面质量 |
| 持有期 | ≤10 个交易日 | **20~40 个交易日** |
| 信号频率 | 每日选股 | **每 `rebalance_every` 个交易日**才建仓一次 |
| 持仓数 | 5 | **8~10**（逆向选股靠广度） |
| 个股止损 | 1.2×近5日均振幅（紧） | **不设**（v1.5 已实测"越松越好"） |
| 固定止盈 | 6% | **可选**（v2.1：×p 或绝对 6%；默认关闭） |

为什么这么改（本项目实测依据，见 `docs/回测与策略研究.md`）
----------------------------------------------------------
1. **根因：alpha 在隔夜漏光** —— 买"当日/近期强势股"→ 次日平均低开 1.8%（A 股短期反转）。
   所以**方向反着做**：不追强，买温和弱势。
2. **反转信号形状干净**：`ret_50` 分层单调性 **−1.00**（全组一致反向）；`volatility_20` −0.70、
   `turnover_20` −0.60（低波动/低换手更好）。
3. **基本面无反面证据**：`revenue_yoy`/`profit_yoy` 是全部因子里**唯二 IC 为正且单调 +1.00** 的
   —— 但量级弱（ICIR 0.02~0.05），故**只当质量过滤门，不当收益引擎**。
4. **频率本身就是 alpha 杠杆**：持有成本 10 日 ≈4.9%/年 → 20 日 2.4% → 40 日 1.2%；
   日频合成成本 30%/年（不可交易）→ **低频才让信号变得可交易**。

实现纪律（别踩）
----------------
- `on_bar()` **只下单不成交**：买单由引擎在 T+1 开盘撮合；卖单在 T 日尾盘按收盘价撮合
  （跌停/停牌顺延 T+1 开盘）—— 与 v1.6 同一套引擎口径。
- **可交易性过滤一律走引擎股票池**（板块/ST/停牌/新股 90/禁买 60/一字板已在
  `ctx.universe.symbols` 里做过），这里**不重复实现**。
- 因子读取走 `_load_factor_matrix`（模块级缓存，键含 mtime/size/时间锁/当天）；
  **注意 category**：日度因子在 `daily`，基本面因子在 `fundamental` —— 目录不同。
- 「算了不呈现 = 白算」：各门的通过/剔除数在 `teardown` 汇总输出。
"""
from __future__ import annotations

from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from app.core.config import settings
from app.core.errors import DataNotReadyError
from app.core.logging import logger
from app.services import data_store
from strategies.base import BaseStrategy, StrategyContext
from tools.universe import at_limit_down, price_limit_pct

# ----------------------------------------------------------------------
# 因子清单（名字 → 分类）。★ category 必须写对：日度因子在 daily/，
# 基本面因子在 fundamental/ —— `read_factor` 按目录取文件，写错就直接
# 找不到文件（抛 DataNotReadyError），是"配了名但读不到"的典型坑。
# ----------------------------------------------------------------------
FACTOR_SPECS: dict[str, str] = {
    "ret_50": "daily",           # 反转主信号（形状最干净：单调性 −1.00）
    "max_ret_20": "daily",       # 排除脉冲股（与 G5 崩坏同源）
    "volatility_20": "daily",    # 低波动
    "turnover_20": "daily",      # 低换手
    "amount_mean_20": "daily",   # 流动性下限（成交额）
    "profit_yoy": "fundamental",  # 质量门
    "revenue_yoy": "fundamental",  # 质量门
}

#: 参与打分的维度（名字 → 方向）。+1 = 越大越好；−1 = 越小越好。
#: 打分前每个维度先做**逐日截面百分位秩**（量纲不同，不标准化就等于让量纲大的独占权重）。
SCORE_DIMS: dict[str, int] = {
    "ret_50": -1,          # 越弱越好（反转）
    "profit_yoy": +1,      # 成长越强越好
    "revenue_yoy": +1,
    "volatility_20": -1,   # 低波动
    "turnover_20": -1,     # 低换手
}

# ----------------------------------------------------------------------
# 因子矩阵的**模块级缓存**（按 路径 + mtime/size + 时间锁 + 当天 失效）
#
# 与 `momentum.py::_load_factor_matrix` 同构，唯一区别是**多一个 category 形参**
# —— 那边写死了 daily，而本策略要读 fundamental。刻意复制而不是改它：
# v1.6 是现役策略，不在本次改动范围内。
# ----------------------------------------------------------------------
_FACTOR_MATRIX_CACHE: dict[tuple[str, int, int, str, str, str], pd.DataFrame | None] = {}


def _load_factor_matrix(name: str, category: str) -> pd.DataFrame | None:
    """读因子长表 → `{date × symbol}` 矩阵（float32）；不可用返回 None。

    时间对齐：矩阵行键统一为 `YYYY-MM-DD` 字符串（与 `ctx.day` 一致）。

    ⚠️ 缓存键必须带**时间锁**与**当天日期**（同 `momentum.py` 的理由）：
    `read_factor` 会把读取上界截到 `min(全局时间锁, 今天)`，只按 mtime 缓存
    会让长驻进程一直复用"第一次加载那天"的矩阵（静默过期）。

    内存：本策略要同时持有 7 张矩阵（1541 日 × ~4800 只），float64 约 59MB/张
    → 用 **float32** 折半；打分是秩口径、门限比较在 1e7 量级，float32 的
    相对误差 ~1e-7 不影响任何判定。
    """
    key: tuple[str, int, int, str, str, str] | None = None
    try:
        path = data_store.factor_path(name, category)
        st = path.stat()
        key = (
            str(path),
            st.st_mtime_ns,
            st.st_size,
            data_store.get_read_until() or "",
            date.today().isoformat(),
            category,
        )
    except OSError:
        key = None
    if key is not None and key in _FACTOR_MATRIX_CACHE:
        return _FACTOR_MATRIX_CACHE[key]

    try:
        df = data_store.read_factor(name, category=category)
    except DataNotReadyError:
        matrix: pd.DataFrame | None = None
    else:
        if df is None or df.empty:
            matrix = None
        else:
            d = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
            matrix = (
                df.assign(date=d)
                .pivot_table(index="date", columns="symbol", values="value", aggfunc="mean")
                .astype("float32")
            )
    if key is not None:
        _FACTOR_MATRIX_CACHE[key] = matrix
    return matrix


class QualityReversalStrategy(BaseStrategy):
    """中频「质量反转」选股（v2.0）。"""

    name = "quality_reversal"
    version = "2.2"

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        super().__init__(params)
        v2 = settings.strategy_v2
        # ★ 默认值来自**独立配置节** `strategy_defaults_v2`（不是 v1.6 的 strategy_defaults）。
        #   `max_positions` / `max_weight_per_stock` 必须在这里 → `runner.build_config`
        #   用 `strategy.params` 记这两项，缺了就会退回 settings 的 v1.6 值（"写了没被读"）。
        p: dict[str, Any] = {
            "max_positions": v2.max_positions,
            "max_weight_per_stock": v2.max_weight_per_stock,
            "hold_days_max": v2.hold_days_max,
            "rebalance_every": v2.rebalance_every,
            "index_ret20_min": v2.index_ret20_min,
            "reversal_pct": v2.reversal_pct,
            "max_ret_20_max": v2.max_ret_20_max,
            "use_quality_gate": v2.use_quality_gate,
            "use_lowvol_gate": v2.use_lowvol_gate,
            "use_lowturn_gate": v2.use_lowturn_gate,
            "use_liquidity_gate": v2.use_liquidity_gate,
            "min_amount_mean_20": v2.min_amount_mean_20,
            "realized_ret20_max": v2.realized_ret20_max,
            # --- v2.1 新增：止损 / 止盈（阈值基准 p，0 = 关闭）---
            "stop_loss_p_mult": v2.stop_loss_p_mult,
            "take_profit_p_mult": v2.take_profit_p_mult,
            "take_profit_abs": v2.take_profit_abs,
        }
        p.update(self.params)          # 调用方/寻优覆盖
        self.params = p

        # ---- 运行期统计（teardown 汇总；"算了不呈现 = 白算"）----
        #: 建仓评估日数（= 通过 rebalance_every 的日子）
        self._rebalance_days = 0
        #: 门1（大盘）放行日数
        self._index_pass_days = 0
        #: 逐门裁剪后的**累计**候选数（键是门名；值是"这道门之后还剩多少只·日"）
        self._gate_stats: dict[str, int] = {}
        #: 参与过门评估的候选总数（只·日）
        self._pool_total = 0
        #: 因因子不可用而降级的门（去重）
        self._degraded: set[str] = set()
        self._warned: set[str] = set()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def on_bar(self, ctx: StrategyContext) -> None:
        di = ctx.market.date_index[ctx.day]
        self._sell_side(ctx, di)
        self._buy_side(ctx, di)

    def teardown(self, ctx: StrategyContext) -> None:
        s = self._gate_stats
        logger.info(
            f"quality_reversal 统计：建仓评估 {self._rebalance_days} 日｜"
            f"门1(大盘)放行 {self._index_pass_days} 日｜"
            f"评估候选合计 {self._pool_total} 只·日；逐门裁剪后剩余（累计）："
            f"反转门 {s.get('reversal', 0)} → 脉冲门 {s.get('pulse', 0)} → "
            f"质量门 {s.get('quality', 0)} → 低波动 {s.get('lowvol', 0)} → "
            f"低换手 {s.get('lowturn', 0)} → 流动性 {s.get('liquidity', 0)}"
        )
        if self._degraded:
            logger.warning(
                f"quality_reversal：以下门因因子不可用而降级（未生效）：{sorted(self._degraded)}"
            )

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------
    def _warn_once(self, key: str, message: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            logger.warning(message)

    def _factor_row(
        self, name: str, day: str, pool: list[str]
    ) -> pd.Series | None:
        """取 `day` 日、`pool` 上的因子截面；不可用（文件缺失/无该日/全空）返回 None。"""
        mat = _load_factor_matrix(name, FACTOR_SPECS[name])
        if mat is None or day not in mat.index:
            return None
        row = pd.to_numeric(mat.loc[day].reindex(pool), errors="coerce")
        if not row.notna().any():
            return None
        return row

    def _is_st(self, ctx: StrategyContext, symbol: str, di: int) -> bool:
        si = ctx.market.symbol_index.get(symbol)
        if si is None or not ctx.market.has_data[di, si]:
            return False
        return int(ctx.market.is_st[di, si]) == 1

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

    # ==================================================================
    # 清仓（规格 §2.3：6 个条件，任一触发即卖出；T 日尾盘按收盘价成交）
    # ==================================================================
    def _sell_side(self, ctx: StrategyContext, di: int) -> None:
        hold_max = int(self.params["hold_days_max"])
        realized_max = float(self.params["realized_ret20_max"])
        # v2.1：止损/止盈（阈值 = mult × p；0 = 关闭）
        sl_mult = float(self.params.get("stop_loss_p_mult", 0.0))
        tp_mult = float(self.params.get("take_profit_p_mult", 0.0))
        tp_abs = float(self.params.get("take_profit_abs", 0.0))
        day = ctx.day
        profit = self._factor_row("profit_yoy", day, list(ctx.account.positions.keys()))
        revenue = self._factor_row("revenue_yoy", day, list(ctx.account.positions.keys()))

        for symbol in list(ctx.account.positions.keys()):
            pos = ctx.account.positions[symbol]
            reasons: list[str] = []

            # 条件 1 · 到期换仓：持有满 hold_days_max 个交易日（含首尾计数）
            if ctx.account.hold_days(symbol) >= hold_max:
                reasons.append("time_stop")

            # 条件 2 · 质量恶化：新公告后成长转负（缺值 = 无法判定 → 不触发）
            if self._quality_broken(profit, revenue, symbol):
                reasons.append("quality_broken")

            # 条件 3 · 风险事件：ST / 跌停
            if self._is_st(ctx, symbol, di):
                reasons.append("st")
            if self._is_limit_down(ctx, symbol, di):
                reasons.append("limit_down")

            # 条件 4 · 过度兑现：近 20 日累计涨幅已达阈值（反转已兑现甚至过热）
            r20 = ctx.market.cum_return(symbol, di, 20)
            if np.isfinite(r20) and r20 >= realized_max:
                reasons.append("over_realized")

            # 条件 5/6 · 止损 / 止盈（v2.1；p 口径见 v2.2）：阈值随个股近期波动自适应
            #   p = 近 5 个交易日「每日涨跌幅绝对值」的均值（小数，v2.2）
            #   ⚠️ 毛口径；p 不可用（NaN / ≤0）→ 不触发（不误杀）；mult/abs = 0 → 关闭
            if sl_mult > 0 or tp_mult > 0 or tp_abs > 0:
                gross = self._gross_return(ctx, pos, symbol, di)
                if np.isfinite(gross):
                    p = self._p_absmean(ctx, symbol, di, 5)
                    if sl_mult > 0 and np.isfinite(p) and p > 0 and gross <= -sl_mult * p:
                        reasons.append("stop_loss")
                    hit_tp = tp_mult > 0 and np.isfinite(p) and p > 0 and gross >= tp_mult * p
                    hit_tp = hit_tp or (tp_abs > 0 and gross >= tp_abs)
                    if hit_tp:
                        reasons.append("take_profit")

            if reasons:
                # 多条件同时成立时全部记录（用 | 连接），便于事后归因
                ctx.account.order_sell(symbol, reason="|".join(reasons))

    @staticmethod
    def _p_absmean(ctx: StrategyContext, symbol: str, di: int, n: int = 5) -> float:
        """阈值基准 `p` = 近 `n` 个交易日「每日涨跌幅绝对值」的均值（小数）。规格 v2.2。

        口径（写死，别改）：
          · `pct_chg` 矩阵是**百分数**（1.15 = +1.15%）→ 结果 ÷100，与 `cum_return`（小数）对齐；
          · **要求窗口内 n 天全部可交易（`trade_status == 1`）且 `pct_chg` 有限**，
            否则返回 NaN（→ 该规则当日**不触发**）。
            为什么必须掩码：baostock 的停牌日**有行**（`has_data=True`），
            但 `trade_status=0` 且 `pct_chg` 为 **0 或 NULL**（close 恒等于前收）——
            不掩码会让停牌日把均值**人为压低**。
          · 与 v2.1 的 `|cum_return(5)|/5` **不是同一个口径**：后者因日间正负对消而系统性偏小
            （实测中位 0.93%），本口径中位约 **2.25%**。
        """
        si = ctx.market.symbol_index.get(symbol)
        lo = di - n + 1
        if si is None or lo < 0:
            return float("nan")
        ts = ctx.market.trade_status[lo : di + 1, si]
        seg = ctx.market.pct_chg[lo : di + 1, si]
        if ts.shape[0] != n or not bool(np.all(ts == 1)) or not bool(np.all(np.isfinite(seg))):
            return float("nan")
        return float(np.abs(seg).mean()) / 100.0

    @staticmethod
    def _gross_return(ctx: StrategyContext, pos: Any, symbol: str, di: int) -> float:
        """毛收益率 = 当日后复权收盘价 / 买入成交价 − 1（不扣任何费用）。"""
        price = ctx.market.close_at(symbol, di)
        entry = float(getattr(pos, "entry_price", float("nan")))
        if not np.isfinite(price) or price <= 0 or not np.isfinite(entry) or entry <= 0:
            return float("nan")
        return float(price / entry - 1.0)

    @staticmethod
    def _quality_broken(
        profit: pd.Series | None, revenue: pd.Series | None, symbol: str
    ) -> bool:
        """成长转负即触发；任一因子缺值 → 无法判定 → 不触发（不误杀）。"""
        for row in (profit, revenue):
            if row is None or symbol not in row.index:
                continue
            v = row.get(symbol)
            if v is not None and np.isfinite(v) and float(v) < 0:
                return True
        return False

    # ==================================================================
    # 开仓（规格 §2.2：门0~门4；每 rebalance_every 个交易日评估一次）
    # ==================================================================
    def _buy_side(self, ctx: StrategyContext, di: int) -> None:
        every = int(self.params["rebalance_every"])
        if every > 0 and di % every != 0:
            return                                   # 未到建仓日
        self._rebalance_days += 1

        max_pos = int(self.params["max_positions"])
        if ctx.account.position_count >= max_pos:
            return                                   # 槽位已满：只补空缺，不强制换手

        # ---- 门1 · 大盘非急跌（粗择时：近 20 日累计涨跌幅 > 下限）----
        r20_idx = ctx.market.index_ret(di, 20)
        if not (np.isfinite(r20_idx) and r20_idx > float(self.params["index_ret20_min"])):
            return
        self._index_pass_days += 1

        pool = list(getattr(ctx.universe, "symbols", []) or [])
        if not pool:
            return
        self._pool_total += len(pool)
        day = ctx.day
        rows = {name: self._factor_row(name, day, pool) for name in FACTOR_SPECS}

        # ---- 逐门裁剪（因子不可用 → 该门降级，绝不静默）----
        keep = self._apply_gates(ctx, day, pool, rows)
        n_after_gates = int(keep.sum())
        if n_after_gates == 0:
            return

        # ---- 打分与排序 ----
        idx = pd.Index(pool)
        dims: list[pd.Series] = []
        for name, sign in SCORE_DIMS.items():
            row = rows.get(name)
            if row is None:
                continue
            rk = row.rank(pct=True)                  # 逐日截面百分位秩（0~1，NaN 保留）
            dims.append(rk if sign > 0 else (1.0 - rk))
        if not dims:
            return
        score = pd.concat(dims, axis=1).mean(axis=1, skipna=False).reindex(idx)
        score = score.where(keep)                    # 未过门的直接抹掉

        cands = score.dropna()
        if cands.empty:
            return

        # 稳定排序 + 代码 tiebreaker（同一份配置跑两次结果逐行一致）
        ordered = sorted(cands.items(), key=lambda kv: (-float(kv[1]), kv[0]))

        held = set(ctx.account.positions.keys())
        need = max_pos - ctx.account.position_count
        target_amount = float(self.params["max_weight_per_stock"]) * ctx.total_asset
        bought = 0
        for symbol, _sc in ordered:
            if bought >= need:
                break
            if symbol in held:
                continue
            ctx.account.order_buy(symbol, target_amount, reason="quality_reversal")
            bought += 1

    def _apply_gates(
        self, ctx: StrategyContext, day: str, pool: list[str], rows: dict[str, pd.Series | None]
    ) -> pd.Series:
        """返回 pool 上的布尔掩码（True = 通过全部门）。

        每道门单独计数；因子不可用（rows[name] is None）→ **该门降级不裁剪** + 告警一次。
        缺个股值（NaN）→ 该股**不通过**（不猜）。
        """
        idx = pd.Index(pool)
        keep = pd.Series(True, index=idx)

        def _gate(key: str, row: pd.Series | None, cond) -> None:
            """裁剪一道门；`row is None`（因子不可用）→ 该门降级不裁剪 + 告警一次。

            ★ 统计口径：记录这道门**之后**剩余的候选数（累计），所以
              teardown 打印的是一串单调不增的数 —— 直接回答"每道门砍掉多少"。
            """
            nonlocal keep
            if row is None:
                self._degraded.add(key)
                self._warn_once(
                    key,
                    f"quality_reversal：门「{key}」所需因子不可用（缺失或不在该日截面），"
                    f"本次运行该门**降级不生效**",
                )
            else:
                passed = cond(row).fillna(False).astype(bool)
                keep = keep & passed
            self._gate_stats[key] = self._gate_stats.get(key, 0) + int(keep.sum())

        # 门2 · 反转门（不追高）：ret_50 ≤ 截面 reversal_pct 分位
        rev_pct = float(self.params["reversal_pct"])
        _gate("reversal", rows["ret_50"], lambda r: r.rank(pct=True) <= rev_pct)
        # 门2b · 排除脉冲股：近 20 日最大单日涨幅 ≤ 上限
        _gate("pulse", rows["max_ret_20"], lambda r: r <= float(self.params["max_ret_20_max"]))
        # 门3 · 质量门：成长为正
        if bool(self.params["use_quality_gate"]):
            prof, rev = rows["profit_yoy"], rows["revenue_yoy"]
            if prof is None or rev is None:
                self._degraded.add("quality")
                self._warn_once(
                    "quality",
                    "quality_reversal：门「quality」所需基本面因子不可用，本次运行该门**降级不生效**",
                )
            else:
                keep = keep & ((prof > 0) & (rev > 0)).fillna(False).astype(bool)
            self._gate_stats["quality"] = self._gate_stats.get("quality", 0) + int(keep.sum())
        # 门4 · 低波动 / 低换手（≤ 截面中位）
        if bool(self.params["use_lowvol_gate"]):
            _gate("lowvol", rows["volatility_20"], lambda r: r <= r.median())
        if bool(self.params["use_lowturn_gate"]):
            _gate("lowturn", rows["turnover_20"], lambda r: r <= r.median())
        # 门0 · 流动性下限
        if bool(self.params["use_liquidity_gate"]):
            _gate(
                "liquidity",
                rows["amount_mean_20"],
                lambda r: r >= float(self.params["min_amount_mean_20"]),
            )
        return keep

    # ==================================================================
    # 元信息
    # ==================================================================
    def config_snapshot(self) -> dict[str, Any]:
        snap = super().config_snapshot()
        snap["rules_version"] = "策略规格说明书-v2.0-中频质量反转.md v2.2"
        snap["description"] = (
            "中频「质量反转」：买温和弱势(反转)+基本面成长+低波动低换手，"
            "每 rebalance_every 个交易日建仓、持有 hold_days_max 个交易日"
        )
        snap["buy_fill_rule"] = "next_open"      # T 日信号 → T+1 开盘价（与 v1.6 同口径）
        snap["sell_fill_rule"] = "same_close"    # T 日信号 → T 日尾盘收盘价（跌停/停牌顺延）
        snap["gates"] = {
            "index_ret20_min": self.params["index_ret20_min"],
            "reversal_pct": self.params["reversal_pct"],
            "max_ret_20_max": self.params["max_ret_20_max"],
            "quality": bool(self.params["use_quality_gate"]),
            "lowvol": bool(self.params["use_lowvol_gate"]),
            "lowturn": bool(self.params["use_lowturn_gate"]),
            "liquidity": bool(self.params["use_liquidity_gate"]),
            "min_amount_mean_20": self.params["min_amount_mean_20"],
        }
        snap["score_dims"] = dict(SCORE_DIMS)
        snap["realized_ret20_max"] = self.params["realized_ret20_max"]
        # v2.1/v2.2：止损/止盈（阈值 = mult × p；p = 近5日「每日涨跌幅绝对值」的均值；0 = 关闭）
        snap["exit_p_rules"] = {
            "p_def": "mean(abs(pct_chg)) over last 5 tradable days / 100",
            "p_window": 5,
            "p_requires_full_tradable_window": True,
            "stop_loss_p_mult": self.params.get("stop_loss_p_mult", 0.0),
            "take_profit_p_mult": self.params.get("take_profit_p_mult", 0.0),
            "take_profit_abs": self.params.get("take_profit_abs", 0.0),
        }
        return snap
