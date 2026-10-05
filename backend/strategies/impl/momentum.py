"""本策略实现：短期动量截面选股 + 上证指数择时。

**规则唯一依据：`策略规格说明书.md` v1.6。**
本文件只实现它 —— 规则要改先改文档并升版本，再改这里。

v1.6 变更（2026-10-05）
----------------------
**清仓成交时点**：6 个清仓条件（时间止损 / 止盈 / 大盘急跌 / 跌停 / ST / 个股止损）
仍按 T 日收盘判定，但**成交改为 T 日尾盘按收盘价**（原为 T+1 开盘），**阈值一律不变**。
- **买入不变**：仍是 T 日信号 → T+1 开盘成交。
- 成交时点的实现**全在引擎**（`engine._execute_pending_at_close`），策略侧一行没改 ——
  策略只负责"下卖单"，什么时候成交由引擎决定（`on_bar` 不得直接成交是铁律）。
- 例外：T 日收盘**跌停/停牌**卖不掉 → 顺延 T+1 开盘重试（规格 §2.3 条件 4 红线）。
- A 股 T+1 守卫：当日买入的持仓当日不得卖出 → 这类信号退化为 T+1 开盘卖出。

v1.5 变更（2026-10-05）
----------------------
1. **新增闸门 5「近期无涨停/触板」**：近 N 个交易日（**含当日 T**）内不得出现涨停或触板
   （触板 = 当日**最高价**触及涨停价）。`no_limit_up_days` 默认 10，**0 = 关闭该门**。
2. **个股止损收紧到 1.2× 近5日均振幅**，且**主板/创业板不再区分**（v1.4 是 2×/3×）。
3. **止盈统一为 6%**，主板/创业板同值（v1.4 是 6%/10%）。
   ⚠️ 止损从 2×/3× 收到 1.2× 是为了修 **v1.4 的赔率倒挂**（止盈 6% 对止损 -8%~-11%，
   保本胜率要 57%+）；收到约 -4.8% 后赔率 ≈ 1.25、保本胜率降到约 44%。代价是更容易被洗盘震出。

v1.4 变更（2026-10-02）
----------------------
1. **闸门 1 改逐日口径**：上证近 3 个交易日（T、T-1、T-2）**每天**涨跌幅都 > -1%。
2. **闸门 3 板块化**：今日涨跌幅上限 主板 6% / 创业板 11%；近 10 日累计涨跌幅
   区间 主板 (3%,15%) / 创业板 (5%,20%)；近 3 日 / 近 20 日 > 5% 两条**通用条件保留**。
3. **新增闸门 4**：`volume_ratio_1 > 1.5` 且 `volume_ratio_5 > 1.5`。
4. **闸门 3.5 停用**（实现保留，默认 `exclude_top_pct = 0.0`，供对照回测）。
5. **止盈改「毛口径」**（v1.3 是扣成本净口径）。
6. **新增个股止损**（v1.3 不设止损）。

一句话：在上证近 3 日逐日未明显下跌时，买入沪深主板/创业板中"近期持续上涨、放量、
波动大**且近 10 个交易日没涨停/触板**"的强势股，毛盈利 6% 止盈、毛亏 1.2×均振幅止损
（**均在信号日 T 的尾盘按收盘价成交**），或持有满 `hold_days_max` 个交易日即卖出。

性格（评估结果时必须知道）
------------------------
- **交易成本是主要敌人**：持有期短，实测年成本 6%~9%（见回测报告的「年化交易成本」）；
  v1.6 把清仓提前 1 天，换手与成本进一步上升。
- **有个股止损**（v1.4 新增、v1.5 收紧）→ 单笔最大亏损相对可控。
- ⚠️ **闸门 5 与选股取向直接对冲**：本策略猎"近期强势股"，而强势股天然频繁触板 ——
  该门可能大幅压缩候选池，因此 `teardown` 必须输出被剔除的候选数（算了不呈现 = 白算）。

⚠️ 两个容易混淆的参数（规格 §7.1 特别标注）
    新股入池门槛 **90** 交易日（`universe.listed_days_min`，由引擎的股票池负责）
    卖出后禁买窗口 **60** 交易日（`universe.no_rebuy_days`，同样由股票池负责）
    两者是**独立参数**，不要混用。
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
from app.services.data_source.symbols import board_of_symbol
from strategies.base import BaseStrategy, StrategyContext
from tools.universe import at_limit_down, price_limit_pct

# ----------------------------------------------------------------------
# 量比因子矩阵的**模块级缓存**（按 文件路径 + mtime + size 失效）
#
# 为什么放模块级：参数寻优一轮会 new 出 72 个策略实例（每组一个），若每个实例都
# 重新读 59MB 的 parquet 再 pivot，白读 71 遍。键里带 mtime/size 是为了"文件变了
# 就失效"——本项目的头号静默失效就是"缓存没失效"。
# ----------------------------------------------------------------------
_FACTOR_MATRIX_CACHE: dict[tuple[str, int, int, str, str], pd.DataFrame | None] = {}


def _load_factor_matrix(name: str) -> pd.DataFrame | None:
    """读因子长表 → `{date × symbol}` 矩阵；不可用返回 None。

    时间对齐：矩阵行键统一为 `YYYY-MM-DD` 字符串（与 `ctx.day` 一致）。

    ⚠️ 缓存键必须带上**时间锁**与**当天日期**：`read_factor` 会把读取上界截到
    `min(全局时间锁, 今天)`。若只按文件 mtime 缓存，长驻进程（如 Web 里的模拟盘）
    会一直复用"第一次加载那天"的矩阵 —— 之后新增的交易日全部缺失（静默过期）。
    """
    key: tuple[str, int, int, str, str] | None = None
    try:
        path = data_store.factor_path(name)
        st = path.stat()
        key = (
            str(path),
            st.st_mtime_ns,
            st.st_size,
            data_store.get_read_until() or "",
            date.today().isoformat(),
        )
    except OSError:
        key = None
    if key is not None and key in _FACTOR_MATRIX_CACHE:
        return _FACTOR_MATRIX_CACHE[key]

    try:
        df = data_store.read_factor(name)
    except DataNotReadyError:
        matrix: pd.DataFrame | None = None
    else:
        if df is None or df.empty:
            matrix = None
        else:
            d = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
            # pivot_table：万一长表里有 (date, symbol) 重复，按均值聚合而不是报错
            matrix = df.assign(date=d).pivot_table(
                index="date", columns="symbol", values="value", aggfunc="mean"
            )
    if key is not None:
        _FACTOR_MATRIX_CACHE[key] = matrix
    return matrix


class MomentumStrategy(BaseStrategy):
    """动量选股 + 指数择时（v1.6）。"""

    name = "momentum"
    version = "1.6"      # 跟随策略规格说明书版本

    #: 闸门 3.5 固定使用的剔除因子（规格 v1.3；v1.4 起默认停用，保留供对照回测）
    _EXCLUDE_FACTOR = "momentum_20"

    #: 闸门 4 使用的量比因子（规格 v1.4，两个都须 > 阈值）
    _VR_FACTORS = ("volume_ratio_1", "volume_ratio_5")

    # ------------------------------------------------------------------
    # 参数（默认全部来自 config.yaml 的 strategy_defaults，可被覆盖）
    # ------------------------------------------------------------------
    def __init__(self, params: dict[str, Any] | None = None) -> None:
        super().__init__(params)
        s = settings.strategy
        e = s.entry

        def _board_field(key: str, field: str, default: float) -> float:
            """板块差异化配置取值的兜底（YAML 只给部分板块时不 KeyError）。"""
            b = e.boards.get(key)
            return float(getattr(b, field)) if b is not None else float(default)

        p = {
            "max_positions": s.max_positions,
            "max_weight_per_stock": s.max_weight_per_stock,
            "hold_days_max": s.hold_days_max,
            # --- v1.4：止盈（毛口径）；v1.5 起主板/创业板取同值 6% ---
            "take_profit_gross_main": s.take_profit_gross_main,
            "take_profit_gross_gem": s.take_profit_gross_gem,
            "index_panic_pct": s.index_panic_pct,
            # --- 闸门 1：逐日口径 ---
            "index_code": s.timing.index_code,
            "timing_daily_min": s.timing.daily_min,
            "timing_today_min": s.timing.today_min,
            # --- 闸门 3：板块化 ---
            "entry_today_pct_min": e.today_pct_min,
            "entry_today_pct_max_main": _board_field("main", "today_pct_max", 0.06),
            "entry_today_pct_max_gem": _board_field("gem", "today_pct_max", 0.11),
            "entry_ret3_min": e.ret3_min,
            "entry_ret20_min": e.ret20_min,
            "entry_ret10_min_main": _board_field("main", "ret10_min", 0.03),
            "entry_ret10_max_main": _board_field("main", "ret10_max", 0.15),
            "entry_ret10_min_gem": _board_field("gem", "ret10_min", 0.05),
            "entry_ret10_max_gem": _board_field("gem", "ret10_max", 0.20),
            "amplitude_window": e.amplitude_window,
            # --- 闸门 4：量比 ---
            "volume_ratio_min": e.volume_ratio_min,
            # --- 闸门 5（v1.5）：近 N 个交易日（含当日）不得涨停/触板；0 = 关闭 ---
            "no_limit_up_days": e.no_limit_up_days,
            # --- v1.4：个股止损；v1.5 起收紧到 1.2× 且两板块取同值 ---
            "use_stop_loss": s.use_stop_loss,
            "stop_loss_amp_window": s.stop_loss_amp_window,
            "stop_loss_amp_mult_main": s.stop_loss_amp_mult_main,
            "stop_loss_amp_mult_gem": s.stop_loss_amp_mult_gem,
            # 规格 v1.3 闸门 3.5：极端动量剔除比例。
            # ★ 代码默认 0 = 关闭（即 v1.2 行为）；v1.4 起**不再启用**，
            #   保留该开关仅供对照回测。
            "exclude_top_pct": 0.0,
        }
        p.update(self.params)          # 调用方传入的覆盖默认值（参数寻优靠它）
        self.params = p

        # 闸门 3.5 的运行期状态（v1.4 默认关闭，保留实现）
        self._m20_loaded = False
        self._m20_matrix: pd.DataFrame | None = None
        self._m20_warned = False
        self._excl_stats = {"days": 0, "excluded": 0, "missing_days": 0}

        # 闸门 4 的运行期状态
        self._vr_warned = False
        self._g4_stats = {"days": 0, "passed": 0, "missing_days": 0}

        # 闸门 5（v1.5）的运行期状态：应用了多少个信号日 / 剔除了多少候选
        self._g5_stats = {"days": 0, "blocked": 0}

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def on_bar(self, ctx: StrategyContext) -> None:
        di = ctx.market.date_index[ctx.day]
        self._sell_side(ctx, di)
        self._buy_side(ctx, di)

    def teardown(self, ctx: StrategyContext) -> None:
        """规则的效果必须可见（「算了不呈现 = 白算」）—— 结束时各汇总一次。"""
        if float(self.params["exclude_top_pct"]) > 0:
            s = self._excl_stats
            logger.info(
                f"闸门 3.5 极端动量剔除统计：应用 {s['days']} 个信号日｜"
                f"累计剔除候选 {s['excluded']} 只｜因子缺失跳过 {s['missing_days']} 日"
            )
        g = self._g4_stats
        if g["days"] or g["missing_days"]:
            logger.info(
                f"闸门 4 量比统计：应用 {g['days']} 个信号日｜"
                f"通过量比的候选累计 {g['passed']} 只｜因子缺失跳过 {g['missing_days']} 日"
            )
        lu_n = int(self.params.get("no_limit_up_days", 0))
        if lu_n > 0:
            l5 = self._g5_stats
            logger.info(
                f"闸门 5 近{lu_n}日无涨停统计：应用 {l5['days']} 个信号日｜"
                f"因近期涨停/触板被剔除的候选累计 {l5['blocked']} 只"
            )

    # ==================================================================
    # 工具：板块判定
    # ==================================================================
    @staticmethod
    def _board_suffix(symbol: str) -> str:
        """主板 → 'main'，创业板 → 'gem'（单一实现走 board_of_symbol）。"""
        return "gem" if board_of_symbol(symbol) == "gem" else "main"

    # ==================================================================
    # 清仓（规格 2.3：6 个条件，任一触发即卖出；T 日收盘判定 → **T 日尾盘收盘价成交**，v1.6）
    # ==================================================================
    def _sell_side(self, ctx: StrategyContext, di: int) -> None:
        market_panic = self._is_market_panic(ctx, di)
        use_sl = bool(self.params.get("use_stop_loss", True))
        sl_window = int(self.params["stop_loss_amp_window"])

        positions = ctx.account.positions
        for symbol in list(positions.keys()):
            pos = positions[symbol]
            reasons: list[str] = []

            # 条件 1 · 时间止损：持有满 hold_days_max 个交易日
            #   ⚠️ hold_days() 是**含首尾**计数（买入成交日算第 1 天），
            #      与规格的 `trading_day_offset(entry, T) >= hold_days_max` 口径一致。
            if ctx.account.hold_days(symbol) >= int(self.params["hold_days_max"]):
                reasons.append("time_stop")

            # 毛收益率（v1.4 起止盈/止损都用毛口径，不扣成本）
            gross = self._gross_return(ctx, pos.entry_price, symbol, di)
            if np.isfinite(gross):
                # 条件 2 · 止盈：板块化毛口径（主板 6% / 创业板 10%）
                if gross >= self._take_profit_threshold(symbol):
                    reasons.append("take_profit")
                # 条件 6 · 个股止损：毛亏 ≥ 倍数 × 近5日平均振幅（v1.4 新增）
                if use_sl:
                    amp = ctx.market.avg_amplitude(symbol, di, sl_window)
                    if np.isfinite(amp) and amp > 0 and gross <= -self._stop_loss_mult(symbol) * amp:
                        reasons.append("stop_loss")

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

    def _gross_return(
        self, ctx: StrategyContext, entry_price: float, symbol: str, di: int
    ) -> float:
        """毛收益率 = 当日后复权收盘价 / 买入成交价 − 1（不扣任何费用）。"""
        price = ctx.market.close_at(symbol, di)
        if not np.isfinite(price) or price <= 0 or entry_price <= 0:
            return float("nan")
        return float(price / entry_price - 1.0)

    def _take_profit_threshold(self, symbol: str) -> float:
        """止盈阈值（毛）。v1.5 起主板/创业板**取同值 6%**，但字段仍分开存放，
        以便将来需要时重新分板块 —— 所以这里仍按板块取键。"""
        key = f"take_profit_gross_{self._board_suffix(symbol)}"
        return float(self.params[key])

    def _stop_loss_mult(self, symbol: str) -> float:
        """止损倍数。v1.5 起主板/创业板**取同值 1.2**（v1.4 是 2×/3× 板块化），
        字段仍分开存放，理由同上。"""
        key = f"stop_loss_amp_mult_{self._board_suffix(symbol)}"
        return float(self.params[key])

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
    # 开仓（规格 2.2：四道闸门，须全部通过）
    # ==================================================================
    def _buy_side(self, ctx: StrategyContext, di: int) -> None:
        n_hold = ctx.account.position_count
        max_pos = int(self.params["max_positions"])

        # 闸门 2 · 持仓数已满 → 本轮不开仓
        if n_hold >= max_pos:
            return

        # 闸门 1 · 大盘择时（v1.4 逐日口径）：近 3 个交易日**每天**涨跌幅 > 下限，
        #   且当日 > 当日下限。di 不足 2（缺 T-1/T-2）→ 视为不通过。
        if not self._timing_gate_pass(ctx, di):
            return

        # 闸门 3 / 4 / 5 · 个股筛选
        #   候选范围来自引擎构建的**当日股票池** —— 板块/ST/停牌/新股/
        #   一字涨跌停/禁买窗口 这些红线过滤已在那里做过，这里不重复实现。
        universe = ctx.universe
        pool: list[str] = list(getattr(universe, "symbols", []) or [])
        # 规格 v1.3 · 闸门 3.5：极端动量剔除（v1.4 默认关闭，pct=0 时原样返回）
        pool = self._apply_extreme_momentum_exclusion(ctx, pool)
        # 规格 v1.4 · 闸门 4：量比掩码（None = 该日降级不启用）
        vr_mask = self._volume_ratio_mask(ctx, pool)
        # 规格 v1.5 · 闸门 5：本日是否启用（用于 teardown 统计"应用了几个信号日"）
        if int(self.params.get("no_limit_up_days", 0)) > 0:
            self._g5_stats["days"] += 1

        held = set(ctx.account.positions.keys())
        need = max_pos - n_hold

        cands: list[tuple[str, float]] = []
        for symbol in pool:
            if symbol in held:
                continue
            if vr_mask is not None and not bool(vr_mask.get(symbol, False)):
                continue                                   # 闸门 4 不通过（缺值 → False）
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

    def _timing_gate_pass(self, ctx: StrategyContext, di: int) -> bool:
        """规格 v1.4 闸门 1：近 3 个交易日逐日涨跌幅均 > daily_min，且当日 > today_min。

        ⚠️ `index_pct(di - k)` 会按下标取数组；di-2 < 0 时负下标会**回绕到末尾**
        （静默取到未来/末尾数据），必须先挡住。
        """
        if di - 2 < 0:
            return False
        daily_min = float(self.params["timing_daily_min"])
        for k in (0, 1, 2):
            p = ctx.market.index_pct(di - k)
            if not (np.isfinite(p) and p > daily_min):
                return False
        return ctx.market.index_pct(di) > float(self.params["timing_today_min"])

    def _entry_signal(self, ctx: StrategyContext, symbol: str, di: int) -> bool:
        """规格 2.2 闸门 3 的条件（①今日 / ②近10日 板块化；③④ 通用）+ 闸门 5。

        第 ⑤ 条（60 日禁买）由股票池的禁买窗口负责，此处不再重复。
        """
        # 闸门 5（v1.5）：近 N 个交易日（**含当日 T**）不得涨停/触板。
        #   放在最前面：它最便宜（O(1) 前缀和查表），且先短路掉一批候选。
        n_lu = int(self.params.get("no_limit_up_days", 0))
        if n_lu > 0 and ctx.market.recent_limit_up_touched(symbol, di, n_lu):
            self._g5_stats["blocked"] += 1
            return False

        si = ctx.market.symbol_index[symbol]
        r1 = ctx.market.pct_chg[di, si] / 100.0
        if not np.isfinite(r1):
            return False
        sfx = self._board_suffix(symbol)

        # ① 今日涨跌幅 ∈ (下限, 板块上限)
        if not (
            float(self.params["entry_today_pct_min"])
            < r1
            < float(self.params[f"entry_today_pct_max_{sfx}"])
        ):
            return False

        # ② 近 10 个交易日累计涨跌幅 ∈ (板块下限, 板块上限)
        r10 = ctx.market.cum_return(symbol, di, 10)
        if not (
            np.isfinite(r10)
            and float(self.params[f"entry_ret10_min_{sfx}"])
            < r10
            < float(self.params[f"entry_ret10_max_{sfx}"])
        ):
            return False

        # ③④ 近 3 / 近 20 个交易日累计涨幅均 > 阈值（通用，不分板块）
        for n, key in ((3, "entry_ret3_min"), (20, "entry_ret20_min")):
            r = ctx.market.cum_return(symbol, di, n)
            if not np.isfinite(r) or r <= float(self.params[key]):
                return False

        return True

    # ------------------------------------------------------------------
    # 闸门 4（规格 v1.4）：成交量比
    # ------------------------------------------------------------------
    def _volume_ratio_mask(self, ctx: StrategyContext, pool: list[str]) -> pd.Series | None:
        """返回 pool 上"量比通过"的布尔掩码；None = 该日闸门 4 降级不启用。

        - 两个量比因子都须 > `volume_ratio_min`（默认 1.5）。
        - 个股缺值（NaN / 不在截面）→ 比较结果为 False → **不通过**。
        - 因子文件整体缺失 / 当日截面缺失 → 返回 None（跳过闸门 4）并计数，
          首次出现时告警一次（数据缺失不允许静默）。
        """
        mats = [_load_factor_matrix(name) for name in self._VR_FACTORS]
        if any(m is None for m in mats):
            self._g4_stats["missing_days"] += 1
            if not self._vr_warned:
                self._vr_warned = True
                logger.warning(
                    f"闸门 4：量比因子 {self._VR_FACTORS} 不可用，本次运行量比闸门失效"
                    f"（降级为不启用该闸门）"
                )
            return None
        mat1, mat5 = mats  # type: ignore[misc]
        if ctx.day not in mat1.index or ctx.day not in mat5.index:
            self._g4_stats["missing_days"] += 1
            if not self._vr_warned:
                self._vr_warned = True
                logger.warning(f"闸门 4：{ctx.day} 不在量比因子截面里，该日跳过量比闸门")
            return None
        thr = float(self.params["volume_ratio_min"])
        r1 = pd.to_numeric(mat1.loc[ctx.day].reindex(pool), errors="coerce")
        r5 = pd.to_numeric(mat5.loc[ctx.day].reindex(pool), errors="coerce")
        # ★ 该日股票池内**没有任何**量比值 → 截面没覆盖当前池（因子缺失 / 标的范围
        #   不匹配）→ 降级不启用闸门 4。若不这样兜底，"因子文件在、只是不含这批
        #   标的"会让所有候选被静默拒绝（trades=0 却看不出原因）。
        if not (r1.notna().any() and r5.notna().any()):
            self._g4_stats["missing_days"] += 1
            if not self._vr_warned:
                self._vr_warned = True
                logger.warning(
                    f"闸门 4：{ctx.day} 股票池内无量比因子值（因子未覆盖当前标的范围？），"
                    f"该日跳过量比闸门"
                )
            return None
        mask = (r1 > thr) & (r5 > thr)      # NaN 比较 → False → 个股缺值不通过
        self._g4_stats["days"] += 1
        self._g4_stats["passed"] += int(mask.sum())
        return mask

    # ------------------------------------------------------------------
    # 闸门 3.5（规格 v1.3；v1.4 默认停用，保留实现供对照回测）
    # ------------------------------------------------------------------
    def _momentum_matrix(self) -> pd.DataFrame | None:
        """读剔除因子的全量长表并转成 {date × symbol} 矩阵（实例内只加载一次）。

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
        self._m20_matrix = df.assign(date=d).pivot_table(
            index="date", columns="symbol", values="value", aggfunc="mean"
        )
        return self._m20_matrix

    def _apply_extreme_momentum_exclusion(
        self, ctx: StrategyContext, pool: list[str]
    ) -> list[str]:
        """规格 v1.3 闸门 3.5：剔除 T 日截面 momentum_20 最高 exclude_top_pct 分位的股票。

        v1.4 起默认关闭（`exclude_top_pct = 0`）→ 原样返回 pool。
        """
        pct = float(self.params["exclude_top_pct"])
        if pct <= 0:
            return pool
        mat = self._momentum_matrix()
        if mat is None or ctx.day not in mat.index:
            self._excl_stats["missing_days"] += 1
            if mat is not None and not self._m20_warned:
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
        snap["rules_version"] = "策略规格说明书.md v1.6"
        snap["description"] = (
            "短期动量截面选股 + 上证指数择时（v1.6：清仓改为 T 日尾盘按收盘价成交；"
            "闸门1 逐日 + 闸门3 板块化 + 闸门4 量比 + 闸门5 近期无涨停 + 毛止盈 6% 同值 + 止损 1.2× 同值）"
        )
        # v1.6 成交口径（写进 config.json，让新旧口径的产物可区分；不是开关，只有唯一合法值）
        snap["buy_fill_rule"] = "next_open"      # T 日信号 → T+1 开盘价
        snap["sell_fill_rule"] = "same_close"    # T 日信号 → T 日尾盘收盘价（跌停/停牌顺延 T+1 开盘）
        snap["boards"] = ["sh_main", "sz_main", "gem"]
        # 闸门 3.5 的因子（v1.4 已停用，仍记录，便于识别对照回测）
        snap["exclude_factor"] = self._EXCLUDE_FACTOR
        # 股票池口径写进快照：换规则时能一眼看出当时用的是哪一套
        snap["no_rebuy_days"] = settings.universe.no_rebuy_days
        snap["listed_days_min"] = settings.universe.listed_days_min
        # v1.4/v1.5 关键口径摘要（便于事后从 config.json 判断这轮跑的到底是哪版规则）
        snap["take_profit_gross"] = {
            "main": self.params["take_profit_gross_main"],
            "gem": self.params["take_profit_gross_gem"],
        }
        snap["stop_loss"] = {
            "enabled": bool(self.params.get("use_stop_loss", True)),
            "amp_window": self.params["stop_loss_amp_window"],
            "mult_main": self.params["stop_loss_amp_mult_main"],
            "mult_gem": self.params["stop_loss_amp_mult_gem"],
        }
        # 闸门 5（v1.5）：近 N 日无涨停/触板；0 = 关闭
        snap["no_limit_up_days"] = self.params["no_limit_up_days"]
        snap["volume_ratio_min"] = self.params["volume_ratio_min"]
        snap["exclude_top_pct"] = self.params["exclude_top_pct"]
        return snap
