"""轻量向量化回测引擎（截面选股版）—— 阶段 2 · 模块 3。

设计取舍
--------
不用 backtrader / vnpy / rqalpha：需求是"跑一个日频截面选股策略、把结果画出来"，
那三个都太重、A股字段映射要改、版本迭代风险高。自己写约 500 行更可控
（原 `技术架构.md` 2.2"被否决的方案"，v1.6 已并入 git 历史与本文档 docstring）。

无未来函数的三道保证
------------------
1. **策略只能下单，不能成交**：`on_bar()` 拿到的是"只读快照 + 下单接口"，
   成交价在下一个交易日开盘才产生（方案 C）。
2. **行情矩阵按日期索引只读**：策略取不到 `T+1` 之后的数据；
   所有派生指标（N 日累计涨幅、振幅）都只用 `<= di` 的行。
3. **涨跌停无法成交**：开盘即跌停 → 卖不掉（顺延重试）；开盘即涨停 → 买不到
   （顺延重试）。**绝不按理想价撮合**。

持仓与撮合规则（来自 `策略规格说明书.md` v1.2）
---------------------------------------------
- 单票仓位 = `max_weight_per_stock × 当前总资产`（复利路径随净值变化）
- 不足最大持仓数时**留现金**，不强制满仓
- 买入股数向下取整到整手 100 股，且**扣得起费用**
- 卖出后 `no_rebuy_days` 个交易日内不得再买同一只
- 个股上市满 `listed_days_min` 个交易日才有资格入池

成本一律走 `services.cost_model.py`（禁止在本文件硬编码任何费率）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from app.core.errors import AppError
from app.core.logging import logger
from app.services import cost_model, data_store
from app.services import metrics as metrics_mod
from app.services.calendar_service import get_calendar
from strategies.base import (
    EQUITY_COLUMNS,
    ORDER_COLUMNS,
    POSITION_COLUMNS,
    TRADE_COLUMNS,
    BaseStrategy,
    StrategyContext,
    StrategyResult,
    ensure_contract,
)
from tools.universe import (
    at_limit_down,
    at_limit_up,
    build_universe,
    price_limit_pct,
)

#: 引擎需要从本地仓库取的列（少取一列就少一份内存）
_QUOTE_COLUMNS: tuple[str, ...] = (
    "date",
    "symbol",
    "open",
    "high",
    "low",
    "close",
    "preclose",
    "pct_chg",
    "trade_status",
    "is_st",
)


# ======================================================================
# 行情矩阵
# ======================================================================
@dataclass
class MarketData:
    """预加载的行情矩阵（日期 × 标的）。

    为什么要矩阵而不是每天 `read_quotes`：
    1115 个交易日 × 4500 只，若每天去 DuckDB 捞一次，光 IO 就要几分钟；
    一次性读进来建成 numpy 矩阵后，取"某日某列"是 O(1) 的行切片。
    """

    dates: list[str]
    date_index: dict[str, int]
    symbols: list[str]
    symbol_index: dict[str, int]

    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    preclose: np.ndarray
    pct_chg: np.ndarray
    trade_status: np.ndarray
    is_st: np.ndarray
    has_data: np.ndarray

    #: 基准指数（择时闸门 + 大盘急跌清仓信号源）
    index_symbol: str = ""
    index_close: np.ndarray = field(default_factory=lambda: np.array([]))
    index_pct_chg: np.ndarray = field(default_factory=lambda: np.array([]))

    @classmethod
    def load(
        cls,
        *,
        start: str,
        end: str,
        symbols: list[str] | None = None,
        index_symbol: str = "sh.000001",
    ) -> MarketData:
        """一次性把区间行情读进内存并建成矩阵。"""
        raw = data_store.read_quotes(
            None, start, end, columns=list(_QUOTE_COLUMNS)
        )
        if raw.empty:
            raise AppError(
                f"行情为空：{start} ~ {end}。请先执行 python -m tools.backfill_quotes"
            )
        raw["date"] = pd.to_datetime(raw["date"])
        raw = raw.sort_values(["date", "symbol"], kind="stable")

        all_syms = sorted(raw["symbol"].astype(str).unique().tolist())
        if symbols is not None:
            allow = set(symbols)
            all_syms = [s for s in all_syms if s in allow]
            raw = raw[raw["symbol"].astype(str).isin(allow)]
            if raw.empty:
                raise AppError("按给定股票池过滤后行情为空，请检查 symbols 参数")

        dates = sorted(raw["date"].dt.strftime("%Y-%m-%d").unique().tolist())
        date_index = {d: i for i, d in enumerate(dates)}
        symbol_index = {s: i for i, s in enumerate(all_syms)}

        n_d, n_s = len(dates), len(all_syms)
        di = raw["date"].dt.strftime("%Y-%m-%d").map(date_index).to_numpy()
        si = raw["symbol"].astype(str).map(symbol_index).to_numpy()

        def _mat(col: str, dtype: Any, fill: Any) -> np.ndarray:
            arr = np.full((n_d, n_s), fill, dtype=dtype)
            if col in raw.columns:
                arr[di, si] = pd.to_numeric(raw[col], errors="coerce").to_numpy()
            return arr

        has_data = np.zeros((n_d, n_s), dtype=bool)
        has_data[di, si] = True

        idx_close = np.full(n_d, np.nan, dtype=np.float64)
        idx_pct = np.full(n_d, np.nan, dtype=np.float64)
        idx_sym = ""
        if index_symbol:
            idf = data_store.read_quotes(
                index_symbol, start, end, is_index=True, columns=["date", "close", "pct_chg"]
            )
            if not idf.empty:
                idx_sym = index_symbol
                idf = idf.assign(date=pd.to_datetime(idf["date"]))
                m = idf["date"].dt.strftime("%Y-%m-%d").map(date_index)
                ok = m.notna()
                pos = m[ok].astype(int).to_numpy()
                idx_close[pos] = pd.to_numeric(idf.loc[ok, "close"]).to_numpy()
                idx_pct[pos] = pd.to_numeric(idf.loc[ok, "pct_chg"]).to_numpy()
                if np.isnan(idx_close).all():
                    idx_sym = ""
            else:
                logger.warning(f"基准指数 {index_symbol} 无本地数据，择时闸门将不可用")

        logger.info(
            f"行情矩阵就绪：{n_d} 个交易日 × {n_s} 只标的"
            f"（{dates[0]} ~ {dates[-1]}），基准 {idx_sym or '无'}"
        )
        return cls(
            dates=dates,
            date_index=date_index,
            symbols=all_syms,
            symbol_index=symbol_index,
            open=_mat("open", np.float64, np.nan),
            high=_mat("high", np.float64, np.nan),
            low=_mat("low", np.float64, np.nan),
            close=_mat("close", np.float64, np.nan),
            preclose=_mat("preclose", np.float64, np.nan),
            pct_chg=_mat("pct_chg", np.float32, np.nan),
            trade_status=_mat("trade_status", np.int8, -1),
            is_st=_mat("is_st", np.int8, -1),
            has_data=has_data,
            index_symbol=idx_sym,
            index_close=idx_close,
            index_pct_chg=idx_pct,
        )

    # ------------------------------------------------------------------
    # 只读取数（全部按行索引 di，天然不含未来）
    # ------------------------------------------------------------------
    def day_frame(self, di: int) -> pd.DataFrame:
        """把第 di 行还原成 `build_universe` 需要的日截面 DataFrame。"""
        df = pd.DataFrame(
            {
                "symbol": self.symbols,
                "date": pd.Timestamp(self.dates[di]),
                "open": self.open[di],
                "high": self.high[di],
                "low": self.low[di],
                "close": self.close[di],
                "preclose": self.preclose[di],
                "pct_chg": self.pct_chg[di],
                "trade_status": self.trade_status[di],
                "is_st": self.is_st[di],
            }
        )
        return df[self.has_data[di]].reset_index(drop=True)

    def close_at(self, symbol: str, di: int) -> float:
        si = self.symbol_index.get(symbol)
        if si is None:
            return float("nan")
        return float(self.close[di, si])

    def open_at(self, symbol: str, di: int) -> float:
        si = self.symbol_index.get(symbol)
        if si is None:
            return float("nan")
        return float(self.open[di, si])

    def preclose_at(self, symbol: str, di: int) -> float:
        si = self.symbol_index.get(symbol)
        if si is None:
            return float("nan")
        return float(self.preclose[di, si])

    def is_tradable(self, symbol: str, di: int) -> bool:
        """当日可正常交易（有数据 + 非停牌）。"""
        si = self.symbol_index.get(symbol)
        if si is None or not self.has_data[di, si]:
            return False
        return int(self.trade_status[di, si]) == 1

    def cum_return(self, symbol: str, di: int, n: int) -> float:
        """近 n 个交易日的累计涨跌幅（含首尾：di 与 di-n 两个点）。

        用后复权收盘价直接相除 —— 复权口径已在数据层统一，这里不需要再处理。
        """
        si = self.symbol_index.get(symbol)
        if si is None or di - n < 0:
            return float("nan")
        c1, c0 = self.close[di, si], self.close[di - n, si]
        if not np.isfinite(c1) or not np.isfinite(c0) or c0 <= 0:
            return float("nan")
        return float(c1 / c0 - 1.0)

    def avg_amplitude(self, symbol: str, di: int, n: int) -> float:
        """近 n 个交易日的平均振幅（振幅 = (最高-最低)/前收盘）。"""
        si = self.symbol_index.get(symbol)
        if si is None or di - n + 1 < 0:
            return float("nan")
        lo = di - n + 1
        h = self.high[lo : di + 1, si]
        low = self.low[lo : di + 1, si]
        pre = self.preclose[lo : di + 1, si]
        with np.errstate(invalid="ignore", divide="ignore"):
            amp = (h - low) / pre
        valid = amp[np.isfinite(amp)]
        if valid.size == 0:
            return float("nan")
        return float(valid.mean())

    def index_ret(self, di: int, n: int) -> float:
        """基准指数近 n 个交易日的累计涨跌幅（用于择时闸门）。"""
        if self.index_close.size == 0 or di - n < 0:
            return float("nan")
        c1, c0 = self.index_close[di], self.index_close[di - n]
        if not np.isfinite(c1) or not np.isfinite(c0) or c0 <= 0:
            return float("nan")
        return float(c1 / c0 - 1.0)

    def index_pct(self, di: int) -> float:
        """基准指数当日涨跌幅（小数）。"""
        if self.index_pct_chg.size == 0:
            return float("nan")
        v = self.index_pct_chg[di]
        return float(v) / 100.0 if np.isfinite(v) else float("nan")


# ======================================================================
# 持仓与订单
# ======================================================================
@dataclass
class Position:
    symbol: str
    shares: int
    entry_date: str
    entry_price: float          # 买入成交价（未含费用）
    entry_cost: float           # 买入费用（元）
    entry_idx: int              # 买入成交日的行索引
    entry_reason: str = ""

    @property
    def entry_amount(self) -> float:
        return self.entry_price * self.shares


@dataclass
class Order:
    signal_date: str
    signal_idx: int
    symbol: str
    side: str                   # buy | sell
    reason: str
    #: 买单：目标投入金额；卖单：None（全平）
    target_amount: float | None = None
    shares: int | None = None
    #: 已顺延重试的交易日数
    retries: int = 0


# ======================================================================
# 账户（策略只能通过它下单，不能直接改状态）
# ======================================================================
class Account:
    """账户只读视图 + 下单接口。"""

    def __init__(self, engine: BacktestEngine) -> None:
        self._e = engine

    # ---- 只读 ----
    @property
    def cash(self) -> float:
        return self._e.cash

    @property
    def total_asset(self) -> float:
        return self._e.total_asset

    @property
    def positions(self) -> dict[str, Position]:
        return dict(self._e.positions)

    @property
    def position_count(self) -> int:
        return len(self._e.positions)

    def hold_days(self, symbol: str) -> int:
        """含首尾：买入成交日算第 1 天（与 `trading_day_offset` 口径一致）。"""
        pos = self._e.positions.get(symbol)
        if pos is None:
            return 0
        return self._e.di - pos.entry_idx + 1

    def net_return(self, symbol: str, di: int) -> float:
        """按 di 日收盘价算的**扣成本净收益率**（止盈判定的唯一口径）。

        ⚠️ `di` 是**行情矩阵的行索引**（含预热期），不是回测日序号 ——
           取日期必须走 `market.dates`，用 `engine.days` 会错位。
        """
        pos = self._e.positions.get(symbol)
        if pos is None:
            return float("nan")
        price = self._e.market.close_at(symbol, di)
        if not np.isfinite(price) or price <= 0:
            return float("nan")
        return cost_model.net_return(
            pos.entry_price, price, self._e.market.dates[di], pos.shares
        )

    def last_sell_date(self, symbol: str) -> str | None:
        return self._e.last_sell_date.get(symbol)

    # ---- 下单 ----
    def order_buy(self, symbol: str, amount: float, *, reason: str = "") -> None:
        if symbol in self._e.positions:
            return
        if any(o.side == "buy" and o.symbol == symbol for o in self._e.pending):
            return
        self._e.pending.append(
            Order(
                signal_date=self._e.day,
                signal_idx=self._e.di,
                symbol=symbol,
                side="buy",
                reason=reason,
                target_amount=float(amount),
            )
        )

    def order_sell(self, symbol: str, *, reason: str) -> None:
        pos = self._e.positions.get(symbol)
        if pos is None:
            return
        if any(o.side == "sell" and o.symbol == symbol for o in self._e.pending):
            return
        self._e.pending.append(
            Order(
                signal_date=self._e.day,
                signal_idx=self._e.di,
                symbol=symbol,
                side="sell",
                reason=reason,
                shares=pos.shares,
            )
        )


# ======================================================================
# 引擎
# ======================================================================
class BacktestEngine:
    """日频截面选股回测引擎。"""

    def __init__(
        self,
        strategy: BaseStrategy,
        *,
        start: str,
        end: str,
        preheat_start: str,
        initial_cash: float,
        max_positions: int = 5,
        max_weight_per_stock: float = 0.20,
        symbols: list[str] | None = None,
        benchmark: str = "sh.000001",
        listed_days_min: int | None = None,
        no_rebuy_days: int | None = None,
        delist_confirm_days: int = 3,
        market: MarketData | None = None,
    ) -> None:
        self.strategy = strategy
        self.start, self.end, self.preheat_start = start, end, preheat_start
        self.initial_cash = float(initial_cash)
        self.max_positions = int(max_positions)
        self.max_weight = float(max_weight_per_stock)
        self.listed_days_min = listed_days_min
        self.no_rebuy_days = no_rebuy_days
        #: 数据终止（退市）强平的确认窗口：标的连续 N 个交易日后仍无任何行情
        #: 且矩阵中已无后续数据 → 判定退市，按最后可用收盘价强平。
        #: 缓冲的目的：最后交易日恰在回测终点附近的正常标的（只差 1~2 天数据）
        #: 不会被误判成退市。
        self.delist_confirm_days = max(0, int(delist_confirm_days))

        # ★ 行情矩阵可以由调用方传入并**在多次回测间复用**（参数寻优用）。
        #   理由：`MarketData.load()` 只依赖区间与标的范围，**与策略参数无关**。
        #   16 组网格若各自重建一次矩阵，就是白读 16 遍盘（全市场约 1~2 分钟/次）。
        #
        #   ⚠️ 安全性前提：`MarketData` 建成后是**只读**的 —— 所有写操作都在
        #      `BacktestEngine` 自己的状态上（`_universe_cache` / positions / cash）。
        #      将来若往 MarketData 里加可变缓存，这个复用就会串味，务必同步检查。
        if market is not None:
            self.market = market
            self._assert_market_covers(market, preheat_start=preheat_start, end=end)
        else:
            self.market = MarketData.load(
                start=preheat_start, end=end, symbols=symbols, index_symbol=benchmark
            )
        # 回测区间（含端点），必须在矩阵日期里
        self.days = [d for d in self.market.dates if start <= d <= end]
        if not self.days:
            raise AppError(f"回测区间 {start} ~ {end} 内没有任何交易日")
        self.first_di = self.market.date_index[self.days[0]]
        self.last_di = self.market.date_index[self.days[-1]]

        # 上市首日（新股 90 交易日门槛用）
        self.first_trade_date = self._derive_first_trade_dates()
        # 每只标的在矩阵内的**最后一个**有数据日（退市强平判定用）
        self.last_data_di = self._derive_last_data_di()

        self.cash = self.initial_cash
        self.positions: dict[str, Position] = {}
        self.pending: list[Order] = []
        #: symbol -> 最近一次**卖出**日期（禁买窗口 60 交易日按它算）
        self.last_sell_date: dict[str, str] = {}
        self._universe_cache: dict[int, Any] = {}

        self.di = self.first_di
        self.day = self.days[0]
        self.total_asset = self.initial_cash

        self.account = Account(self)
        self._equity_rows: list[dict[str, Any]] = []
        self._trade_rows: list[dict[str, Any]] = []
        self._position_rows: list[dict[str, Any]] = []
        self._order_rows: list[dict[str, Any]] = []

        logger.info(
            f"引擎就绪：{self.days[0]} ~ {self.days[-1]}（{len(self.days)} 个交易日）"
            f"｜初始资金 ¥{self.initial_cash:,.0f}｜最多 {self.max_positions} 只"
            f"｜单票 {self.max_weight:.0%}"
        )

    # ------------------------------------------------------------------
    # 行情矩阵复用的前置校验
    # ------------------------------------------------------------------
    def _assert_market_covers(
        self, market: MarketData, *, preheat_start: str, end: str
    ) -> None:
        """复用外部矩阵时兜住范围。

        **为什么必须校验**：矩阵短了不会报错，`self.days` 会静默变少 ——
        少算的日子不产生任何异常，只会让净值曲线悄悄缺一段、指标悄悄变好看。

        ⚠️ 校验口径踩过一次坑：早先写的是
           `market.dates[0] <= preheat_start and market.dates[-1] >= end`，
           **必然失败** —— `preheat_start` 是自然日（2020-01-01，元旦），
           而矩阵里只有交易日，首日是 2020-01-02，`2020-01-02 <= 2020-01-01`
           为假，参数寻优 12 组全挂在这一行。同理 `end` 若落在周末/节假日，
           第二个条件也会误报。

        ✅ 正确口径：**问交易日历，而不是拿自然日边界硬比**。
           矩阵必须包含 `[preheat_start, end]` 内的**每一个交易日**。
           多出来的日子（矩阵比要求更宽）是允许的 —— 复用同一份大矩阵很正常。
        """
        if not market.dates:
            raise AppError("传入的行情矩阵为空")
        # ⚠️ 局部变量别叫 `calendar` —— 会遮蔽标准库同名模块（本文件其他位置若
        #    用到 `calendar` 就会静默变成这个对象）。用 `cal`。
        cal = get_calendar()
        try:
            want = cal.trading_days(preheat_start, end)
        except Exception as exc:  # pragma: no cover - 日历未初始化
            logger.warning(f"交易日历不可用（{exc}），跳过矩阵覆盖度校验")
            return
        if not want:
            raise AppError(f"{preheat_start} ~ {end} 区间内没有交易日")

        # ① 首尾边界：必须严格盖住区间的第一个/最后一个**交易日**。
        #    用日历给出的交易日当基准，而不是拿自然日比 —— 后者在
        #    preheat_start 落到元旦、end 落到周末时必然误报。
        if market.dates[0] > want[0] or market.dates[-1] < want[-1]:
            raise AppError(
                f"传入的行情矩阵覆盖 {market.dates[0]} ~ {market.dates[-1]}，"
                f"不足以覆盖 {preheat_start} ~ {end} 的交易日区间 "
                f"（应为 {want[0]} ~ {want[-1]}）。"
                f"复用矩阵时它的区间必须覆盖整个预热窗口与回测区间。"
            )

        # ② 内部缺口只告警不报错：子集调试（几十只标的）碰上某天全体停牌
        #    本来就会出现空日；那是数据完整性问题，不是"复用"引入的。
        #    报错会把这些合法的调试运行一并打死。
        have = set(market.dates)
        missing = [d for d in want if d not in have]
        if missing:
            logger.warning(
                f"行情矩阵在 {preheat_start} ~ {end} 内有 {len(missing)} 个交易日无任何标的行情"
                f"（如 {', '.join(missing[:5])}）—— 通常是因为标的范围太窄或数据未回补完。"
            )

    def _derive_first_trade_dates(self) -> dict[str, str]:
        """每只标的在矩阵里的首个**有数据**日期（用于新股 90 交易日门槛）。

        ⚠️ 口径说明：矩阵起点是 `preheat_start`，所以**在预热开始之前就已上市**
           的标的，其"首日"会被记成预热首日 —— 相当于一个**保守下界**
           （把它们的上市时间算晚了）。

           为什么可以接受：预热窗口 2020-01-01 ~ 2020-05-31 实测 **97 个交易日
           > 90 日门槛**，所以到回测起点（2020-06-01）时，这些标的已累计
           97 个交易日，门槛判定结果与真实一致。

           ⚠️ 但这条依赖"预热窗口 ≥ 新股门槛"这个不等式。若将来把预热缩短到
           90 个交易日以下，本函数就会**误把老股票当新股剔除**。
           真要严谨，应当从数据源另取一次上市日期（baostock `query_stock_basic`
           有 `ipoDate` 字段）而不是从矩阵推断。
        """
        out: dict[str, str] = {}
        for si, sym in enumerate(self.market.symbols):
            rows = np.nonzero(self.market.has_data[:, si])[0]
            if rows.size:
                out[sym] = self.market.dates[int(rows[0])]
        return out

    def _derive_last_data_di(self) -> dict[str, int]:
        """每只标的在矩阵里的最后一个有数据日的行索引（退市强平判定用）。

        ⚠️ 用的是**全矩阵**信息（含回测区间之后）：某标的的行情在矩阵里
           已经到头（`di > last_data_di`），意味着从此再无任何数据 ——
           这只能是退市（或长期终止交易的证券）；正常停牌标的的后续必然
           还有数据，`last_data_di` 会大于当前 di，不触发强平。

           因此这不是"用未来数据做交易决策"，而是**会计上的清算判定**：
           数据物理终止 = 证券已不存在，持仓只能按最后可交易价格变现。
        """
        out: dict[str, int] = {}
        for si, sym in enumerate(self.market.symbols):
            rows = np.nonzero(self.market.has_data[:, si])[0]
            if rows.size:
                out[sym] = int(rows[-1])
        return out

    # ------------------------------------------------------------------
    # 撮合
    # ------------------------------------------------------------------
    def _mark_to_market(self, di: int, shares: dict[str, int]) -> float:
        """按 di 日收盘价算持仓市值。

        ⚠️ 停牌日没有收盘价 → 用**最近的**可用收盘价（停牌不等于归零）。
        """
        total = 0.0
        for sym, sh in shares.items():
            px = self._price_or_last(sym, di)
            if np.isfinite(px):
                total += px * sh
        return total

    def _price_or_last(self, symbol: str, di: int) -> float:
        si = self.market.symbol_index.get(symbol)
        if si is None:
            return float("nan")
        col = self.market.close[: di + 1, si]
        idx = np.nonzero(np.isfinite(col))[0]
        return float(col[idx[-1]]) if idx.size else float("nan")

    def _sellable(self, symbol: str, di: int) -> tuple[bool, str]:
        """能否按 di 日开盘价卖出。返回 (可卖, 原因)。"""
        if not self.market.has_data[di, self.market.symbol_index[symbol]]:
            return False, "该日无行情数据"
        if not self.market.is_tradable(symbol, di):
            return False, "停牌（trade_status=0）"
        px = self.market.open_at(symbol, di)
        pre = self.market.preclose_at(symbol, di)
        if not np.isfinite(px) or not np.isfinite(pre) or pre <= 0:
            return False, "开盘价/前收盘缺失"
        si = self.market.symbol_index[symbol]
        limit = price_limit_pct(symbol, self.market.dates[di], int(self.market.is_st[di, si]))
        # ★ 比例容差判定（universe.at_limit_down，复权安全）：
        #   真实跌停价取整发生在不复权价上，hfq 价格上比绝对元数会系统性漏判。
        if at_limit_down(px, pre, limit):
            return False, f"开盘即跌停（≈{pre * (1 - limit):.2f}），无买盘"
        return True, ""

    def _buyable(self, symbol: str, di: int) -> tuple[bool, str]:
        """能否按 di 日开盘价买入。"""
        if not self.market.has_data[di, self.market.symbol_index[symbol]]:
            return False, "该日无行情数据"
        if not self.market.is_tradable(symbol, di):
            return False, "停牌（trade_status=0）"
        px = self.market.open_at(symbol, di)
        pre = self.market.preclose_at(symbol, di)
        if not np.isfinite(px) or not np.isfinite(pre) or pre <= 0:
            return False, "开盘价/前收盘缺失"
        si = self.market.symbol_index[symbol]
        limit = price_limit_pct(symbol, self.market.dates[di], int(self.market.is_st[di, si]))
        if at_limit_up(px, pre, limit):
            return False, f"开盘即涨停（≈{pre * (1 + limit):.2f}），无卖盘"
        return True, ""

    def _execute_pending(self, di: int, day: str) -> None:
        """在 di 日开盘执行挂单。无法成交的**顺延**，绝不按理想价撮合。"""
        if not self.pending:
            return
        still_pending: list[Order] = []
        for order in self.pending:
            sym = order.symbol
            if order.side == "sell":
                if sym not in self.positions:      # 已因别的原因平掉
                    continue
                ok, why = self._sellable(sym, di)
                if not ok:
                    order.retries += 1
                    still_pending.append(order)
                    self._record_order(order, day, None, None, "pending", why)
                    continue
                self._fill_sell(order, di, day)
            else:
                if sym in self.positions:          # 已持有 → 不再加仓
                    continue
                ok, why = self._buyable(sym, di)
                if not ok:
                    order.retries += 1
                    still_pending.append(order)
                    self._record_order(order, day, None, None, "pending", why)
                    continue
                self._fill_buy(order, di, day)
        self.pending = still_pending

    def _fill_sell(self, order: Order, di: int, day: str) -> None:
        pos = self.positions[order.symbol]
        price = self.market.open_at(order.symbol, di)
        self._settle_sell(pos, price, di, day, order.reason, order=order)

    def _settle_sell(
        self,
        pos: Position,
        price: float,
        di: int,
        day: str,
        reason: str,
        *,
        order: Order | None = None,
    ) -> None:
        """卖出结算（正常卖出与退市强平共用的唯一记账出口）。

        Args:
            price: 成交价。正常卖出 = 当日开盘价；退市强平 = 最后可用收盘价。
            order: 正常卖出传入（orders 流水走 `_record_order`）；
                   退市强平没有挂单过程，传 None（流水在调用方补记）。
        """
        proceeds = cost_model.sell_net(price, pos.shares, day)
        exit_cost = pos.shares * price - proceeds
        self.cash += proceeds

        entry_amount = pos.entry_amount
        gross = price * pos.shares - entry_amount
        total_cost = pos.entry_cost + exit_cost
        net = gross - total_cost
        paid = entry_amount + pos.entry_cost

        self._trade_rows.append(
            {
                "symbol": pos.symbol,
                "entry_date": pos.entry_date,
                "entry_price": pos.entry_price,
                "shares": pos.shares,
                "entry_amount": entry_amount,
                "entry_cost": pos.entry_cost,
                "exit_date": day,
                "exit_price": price,
                "exit_amount": price * pos.shares,
                "exit_cost": exit_cost,
                "cost": total_cost,
                "gross_pnl": gross,
                "net_pnl": net,
                "net_return": net / paid if paid else float("nan"),
                "hold_days": di - pos.entry_idx + 1,   # 含首尾
                "entry_reason": pos.entry_reason,
                "exit_reason": reason,
            }
        )
        if order is not None:
            self._record_order(order, day, price, price * pos.shares, "filled", reason)
        else:
            self._order_rows.append(
                {
                    "signal_date": day,
                    "exec_date": day,
                    "symbol": pos.symbol,
                    "side": "sell",
                    "shares": pos.shares,
                    "price": price,
                    "amount": price * pos.shares,
                    "status": "filled",
                    "reason": reason,
                    "note": "退市/数据终止强平（按最后可用收盘价变现）",
                }
            )
        # 禁买窗口按**卖出日**算（规格 2.1：卖出后 60 个交易日内不得再买同一只）
        self.last_sell_date[pos.symbol] = day
        del self.positions[pos.symbol]

    def _force_liquidate_delisted(self, di: int, day: str) -> None:
        """持仓标的数据永久终止（退市）→ 按最后可用收盘价强平。

        ⚠️ 为什么在"执行挂单"之前做：数据已经终止的标的永远不可能满足
           `_sellable`（该日无行情数据），它的卖单只会无限顺延到回测结束，
           市值则一直按最后可用价挂在账上 —— 期末资产里混着"永远无法兑现"
           的残值（技术架构 §6.3 引擎要点 #10 要求的退市强平）。

        判定口径：`di > last_data_di + delist_confirm_days`。
        - `last_data_di` 是该标的在**全矩阵**的最后一个有数据日 —— 若后续
          还有数据（正常停牌），`last_data_di > di` 恒成立，不触发；
        - `delist_confirm_days` 缓冲是为了让"回测终点前 1~2 天恰好没数据"
          的正常标的不被误判（数据源偶发缺口）。
        """
        confirm = self.delist_confirm_days
        for sym in list(self.positions.keys()):
            last_di = self.last_data_di.get(sym)
            if last_di is None or di <= last_di + confirm:
                continue
            price = self._price_or_last(sym, di)
            if not np.isfinite(price) or price <= 0:
                continue
            pos = self.positions[sym]
            logger.warning(
                f"[退市强平] {sym} 自 {self.market.dates[last_di]} 起无任何行情，"
                f"按最后可用收盘价 {price:.4f} 强制平仓（{pos.shares} 股）"
            )
            self._settle_sell(pos, price, di, day, "delist")

    def _fill_buy(self, order: Order, di: int, day: str) -> None:
        price = self.market.open_at(order.symbol, di)
        if not np.isfinite(price) or price <= 0:
            return
        want = float(order.target_amount or 0.0)
        # 目标金额与可用现金取小：不能透支
        budget = min(want, self.cash)
        shares = cost_model.max_affordable_shares(budget, price)
        if shares <= 0:
            self._record_order(order, day, None, None, "rejected", "现金不足 1 手")
            return
        paid = cost_model.buy_paid(price, shares)
        if paid > self.cash + 1e-6:      # 兜底：绝不透支
            self._record_order(order, day, None, None, "rejected", "现金不足（费用后）")
            return
        self.cash -= paid
        self.positions[order.symbol] = Position(
            symbol=order.symbol,
            shares=shares,
            entry_date=day,
            entry_price=price,
            entry_cost=paid - price * shares,
            entry_idx=di,
            entry_reason=order.reason,
        )
        self._record_order(order, day, price, price * shares, "filled", order.reason)

    def _record_order(
        self,
        order: Order,
        exec_date: str,
        price: float | None,
        amount: float | None,
        status: str,
        note: str,
    ) -> None:
        self._order_rows.append(
            {
                "signal_date": order.signal_date,
                "exec_date": exec_date,
                "symbol": order.symbol,
                "side": order.side,
                "shares": order.shares,
                "price": price,
                "amount": amount,
                "status": status,
                "reason": order.reason,
                "note": note,
            }
        )

    # ------------------------------------------------------------------
    # 股票池
    # ------------------------------------------------------------------
    def _calendar_days(self, start: str, end: str) -> list[str]:
        """闭区间内的交易日（只用矩阵内的日期，不再查日历服务）。

        ⚠️ 矩阵起点是 `preheat_start`，所以回测开头几十天的"禁买窗口回看"
           会被截断。预热窗口 97 个交易日 > 禁买窗口 60 日，因此不影响判定。

        ⚠️ **不能**用"自然日近似"：周末与节假日会让 60 个交易日 ≈ 88 个自然日，
           用自然日算会把窗口缩短或拉长，直接改变可买标的集合。
        """
        i = self.market.date_index.get(start)
        j = self.market.date_index.get(end)
        if i is None or j is None or i > j:
            return []
        return self.market.dates[i : j + 1]

    def universe(self, di: int) -> Any:
        """当日股票池（按日缓存 —— 一天只会构建一次）。"""
        cached = self._universe_cache.get(di)
        if cached is not None:
            return cached
        res = build_universe(
            self.market.dates[di],
            self.market.day_frame(di),
            first_trade_date=self.first_trade_date,
            last_sell_date=self.last_sell_date,
            calendar_days_fn=self._calendar_days,
            min_listed_days=self.listed_days_min,
        )
        self._universe_cache[di] = res
        return res

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------
    def run(self) -> StrategyResult:
        for day in self.days:
            di = self.market.date_index[day]
            self.di, self.day = di, day

            # 0) 退市/数据终止的持仓强平（技术架构 §6.3 要点 #10）
            self._force_liquidate_delisted(di, day)

            # 1) 执行上一交易日挂出的订单（今日开盘）
            self._execute_pending(di, day)

            # 2) 记录持仓快照与净值（今日收盘 mark-to-market）
            self._mark_and_snapshot(di, day)

            # 3) 策略决策 → 挂单到下一交易日
            ctx = StrategyContext(
                day=day,
                total_asset=self.total_asset,
                cash=self.cash,
                position_count=len(self.positions),
                account=self.account,
                market=self.market,
                calendar=None,
                universe=self.universe(di),
            )
            if di == self.first_di:
                self.strategy.setup(ctx)
            self.strategy.on_bar(ctx)

            # 4) 最后一天挂出的单子没有下一个交易日可执行 → 丢弃并记录
            if day == self.days[-1] and self.pending:
                for o in self.pending:
                    self._record_order(o, day, None, None, "rejected", "回测结束，未及成交")
                self.pending = []

        self.strategy.teardown(
            StrategyContext(
                day=self.days[-1],
                total_asset=self.total_asset,
                cash=self.cash,
                position_count=len(self.positions),
                account=self.account,
                market=self.market,
                calendar=None,
                universe=self.universe(self.last_di),
            )
        )

        equity = ensure_contract(pd.DataFrame(self._equity_rows), EQUITY_COLUMNS)
        trades = ensure_contract(pd.DataFrame(self._trade_rows), TRADE_COLUMNS)
        result = StrategyResult(
            equity=equity,
            trades=trades,
            positions=ensure_contract(pd.DataFrame(self._position_rows), POSITION_COLUMNS),
            orders=ensure_contract(pd.DataFrame(self._order_rows), ORDER_COLUMNS),
        )
        result.metrics = metrics_mod.compute_metrics(
            equity, trades, initial_cash=self.initial_cash
        )
        logger.info(
            f"回测完成：{len(self.days)} 个交易日｜交易 {len(trades)} 笔｜"
            f"期末总资产 ¥{self.total_asset:,.0f}"
        )
        return result

    # ------------------------------------------------------------------
    def _mark_and_snapshot(self, di: int, day: str) -> None:
        shares = {s: p.shares for s, p in self.positions.items()}
        mv = self._mark_to_market(di, shares)
        self.total_asset = self.cash + mv

        self._equity_rows.append(
            {
                "date": day,
                "cash": self.cash,
                "market_value": mv,
                "total_asset": self.total_asset,
                "position_count": len(self.positions),
            }
        )
        for sym, pos in self.positions.items():
            px = self._price_or_last(sym, di)
            self._position_rows.append(
                {
                    "date": day,
                    "symbol": sym,
                    "shares": pos.shares,
                    "entry_date": pos.entry_date,
                    "entry_price": pos.entry_price,
                    "close": px if np.isfinite(px) else None,
                    "market_value": px * pos.shares if np.isfinite(px) else None,
                    "unrealized_pnl": (
                        (px - pos.entry_price) * pos.shares if np.isfinite(px) else None
                    ),
                    "hold_days": di - pos.entry_idx + 1,
                }
            )


__all__ = [
    "ORDER_COLUMNS",
    "POSITION_COLUMNS",
    "TRADE_COLUMNS",
    "Account",
    "BacktestEngine",
    "MarketData",
    "Order",
    "Position",
]
