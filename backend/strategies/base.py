"""策略基类与**输出契约** —— 阶段 2 · 模块 3。

为什么契约要单独一个文件、并且写死到这里
--------------------------------------
`data/backtest/{strategy}/{run_id}/` 是**策略与前端唯一的耦合点**
（见 `技术架构.md` 判断一）。前端不做回测、不做任务队列、不碰策略代码，
它只读这个目录里的文件。

所以：**文件名和列名一旦定了就不能随便改** —— 改一处，前端、指标计算、
参数寻优对比表全都要跟着动。把它们集中在 `EQUITY_COLUMNS` /
`TRADE_COLUMNS` / 等常量里，并且由 `write_run()` 统一落盘，
就不会出现"引擎写了 `total_asset`、前端读的是 `totalAsset`"这种事。

目录结构
--------
    data/backtest/{strategy_name}/{run_id}/
        config.json        # 本次运行的完整配置快照（可复现的唯一凭证）
        equity.parquet     # 每日净值曲线
        trades.parquet     # 已平仓的交易记录
        positions.parquet  # 每日持仓快照
        orders.parquet     # 委托流水（含未成交/顺延 —— 排查"跌停卖不掉"用）
        metrics.json       # 指标

`run_id` 规则：`{YYYYMMDD-HHMMSS}-{短哈希}`，短哈希取自配置快照 ——
**同一份配置跑两次会得到不同的 run_id，但 config 内容一致**，
所以"结果不一致"时可以直接 diff config 定位。
"""
from __future__ import annotations

import hashlib
import json
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from app.core.jsonutils import json_safe
from app.core.logging import logger
from app.core.paths import BACKTEST_DIR

# ======================================================================
# 输出契约（改这里 = 改契约，务必同步前端与指标计算）
# ======================================================================
#: 净值曲线列
#:
#: ⚠️ 这份元组是 `ensure_contract()` 的**列白名单** —— 它会把不在表里的列
#:    **直接丢掉**（`out[list(columns)]`）。所以 `equity_curve()` 算出来的
#:    `benchmark_nav` 曾经静默消失在落盘那一刻：文件里根本没有这一列，
#:    前端的"策略 vs 基准"双线图画不出来，而且**不报任何错**。
#:    加列时务必同步这里（契约改动的唯一入口）。
EQUITY_COLUMNS: tuple[str, ...] = (
    "date",            # 交易日
    "cash",            # 现金
    "market_value",    # 持仓市值（按当日收盘价）
    "total_asset",     # 总资产 = cash + market_value（**净值分母**）
    "position_count",  # 持仓只数
    "nav",             # 净值（total_asset / 初始资金）—— 由 metrics 层补齐
    "drawdown",        # 当时回撤（负数）
    "benchmark_nav",   # 基准净值（同起点归一化）；无基准时为 NA
)

#: 交易记录列（**只记已平仓的交易**；未成交的委托走 orders.parquet）
TRADE_COLUMNS: tuple[str, ...] = (
    "symbol",
    "entry_date",      # 买入成交日（= 信号日 T 的下一个交易日）
    "entry_price",     # 买入成交价（后复权开盘价）
    "shares",          # 成交股数（整手）
    "entry_amount",    # 成交金额 = entry_price × shares（不含费用）
    "entry_cost",      # 买入费用（元）
    "exit_date",       # 卖出成交日（v1.6：正常清仓 = 信号日 T 当日；顺延/退市除外）
    "exit_price",      # 卖出成交价（v1.6：正常清仓 = 信号日后复权**收盘价**；顺延卖出 = 开盘价）
    "exit_amount",     # 卖出成交金额
    "exit_cost",       # 卖出费用（元，含印花税）
    "cost",            # 总费用 = entry_cost + exit_cost
    "gross_pnl",       # 毛盈亏 = exit_amount - entry_amount（未扣费用）
    "net_pnl",         # 净盈亏 = gross_pnl - cost
    "net_return",      # 净收益率 = net_pnl / (entry_amount + entry_cost)
    "hold_days",       # 持有交易日数（含首尾：买入日 = 1）
    "entry_reason",    # 买入原因（策略标签）
    "exit_reason",     # 卖出原因，多个用 `|` 连接（time_stop|take_profit|…）
)

#: 持仓快照列
POSITION_COLUMNS: tuple[str, ...] = (
    "date",
    "symbol",
    "shares",
    "entry_date",
    "entry_price",
    "close",
    "market_value",
    "unrealized_pnl",
    "hold_days",
)

#: 委托流水列（含被拒/顺延的，是排查撮合问题的唯一凭证）
ORDER_COLUMNS: tuple[str, ...] = (
    "signal_date",     # 信号生成日（T）
    "exec_date",       # 实际成交日（买单 = T+1；卖单 = 信号日当日或顺延后的某日）
    "symbol",
    "side",            # buy | sell
    "shares",
    "price",           # 成交价（未成交为空）
    "amount",
    "status",          # filled | rejected | pending
    "reason",
    "note",            # 未成交原因（跌停/停牌/一字板…）
)

#: 卖出原因枚举（策略实现里只用这些字面量，便于前端着色与统计）
EXIT_REASONS: tuple[str, ...] = (
    "time_stop",       # 持有满 N 个交易日
    "take_profit",     # 毛收益率达到板块止盈阈值（v1.4 起为毛口径）
    "stop_loss",       # 毛亏损达到板块止损倍数 × 近5日平均振幅（v1.4 新增）
    "market_panic",    # 上证指数当日收盘大跌
    "limit_down",      # 跌停
    "st",              # isST == 1
    "delist",          # 退市/数据终止强平（引擎发起，非策略信号）
    # --- 策略 v2.0「中频质量反转」新增（与 v1.6 并存，各自只用自己的子集）---
    "quality_broken",  # 基本面成长转负（profit_yoy / revenue_yoy < 0）
    "over_realized",   # 近 20 日累计涨幅 ≥ 阈值：反转已过度兑现
)


# ======================================================================
# 运行元信息
# ======================================================================
def make_run_id(config: dict[str, Any]) -> str:
    """`{YYYYMMDD-HHMMSS}-{config 短哈希}`。

    短哈希来自配置快照，所以"同一份配置跑两次"的 run_id 前缀不同、
    但哈希相同 —— 结果不一致时可以直接比对 config 定位差异。
    """
    payload = json.dumps(config, sort_keys=True, ensure_ascii=False, default=str)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8]
    return f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{digest}"


@dataclass
class RunLayout:
    """一次回测的落盘位置。"""

    strategy: str
    run_id: str
    root: Path

    @property
    def config_path(self) -> Path:
        return self.root / "config.json"

    @property
    def equity_path(self) -> Path:
        return self.root / "equity.parquet"

    @property
    def trades_path(self) -> Path:
        return self.root / "trades.parquet"

    @property
    def positions_path(self) -> Path:
        return self.root / "positions.parquet"

    @property
    def orders_path(self) -> Path:
        return self.root / "orders.parquet"

    @property
    def metrics_path(self) -> Path:
        return self.root / "metrics.json"

    def as_dict(self) -> dict[str, str]:
        return {
            "strategy": self.strategy,
            "run_id": self.run_id,
            "root": str(self.root),
        }


def run_layout(strategy: str, run_id: str) -> RunLayout:
    return RunLayout(strategy=strategy, run_id=run_id, root=BACKTEST_DIR / strategy / run_id)


# ======================================================================
# 落盘
# ======================================================================
def ensure_contract(df: pd.DataFrame, columns: tuple[str, ...]) -> pd.DataFrame:
    """给结果表补齐契约列，并**按契约固定列顺序**。

    ★ 空表也必须带上列名：`pd.DataFrame()` 是 0 行 **0 列**，
      消费者只要写 `df["side"]` 就会 KeyError —— "0 笔交易"是正常结果，
      不该让下游崩（前端、指标计算、参数寻优对比表都会读它）。
      单列实现，落盘与内存两条路径共用。
    """
    out = df.copy() if df is not None else pd.DataFrame()
    for c in columns:
        if c not in out.columns:
            out[c] = pd.NA
    return out[list(columns)]


def _write_parquet(df: pd.DataFrame, path: Path, columns: tuple[str, ...]) -> int:
    """写 parquet（列顺序按契约固定；空表也写，前端要能读到"0 笔交易"）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    out = ensure_contract(df, columns)
    out.to_parquet(path, index=False, compression="zstd")
    return len(out)


def _json_default(obj: Any) -> Any:
    """`json.dumps` 的兜底转换。

    ★ 为什么不能简单 `default=str`：numpy 标量（`np.float64(0.5)`）默认会变成
      **字符串 `"0.5"`** —— 前端拿到字符串做数值运算/格式化会静默出错
      （`"0.5" * 100` 是重复 50 次，不是 50）。必须先识别成数值类型。
    """
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        v = float(obj)
        return v if math.isfinite(v) else None
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, pd.Timestamp):
        return obj.strftime("%Y-%m-%d")
    return str(obj)


def _dump_json(payload: dict[str, Any]) -> str:
    """统一的 JSON 落盘（净化 NaN/Inf + 处理 numpy 标量）。"""
    return json.dumps(
        json_safe(payload), ensure_ascii=False, indent=2, default=_json_default
    )


def write_run(
    layout: RunLayout,
    *,
    config: dict[str, Any],
    equity: pd.DataFrame,
    trades: pd.DataFrame,
    positions: pd.DataFrame | None = None,
    orders: pd.DataFrame | None = None,
    metrics: dict[str, Any] | None = None,
    tag: str = "",
    started_at: str | None = None,
    finished_at: str | None = None,
    detail_written: bool = True,
    extra_run: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把一次回测的产物按契约落盘。返回一份落盘清单（供日志与登记用）。

    ★ `tag` 写进 `config.json` 的 `_run` 段（**不参与 run_id 哈希**）：
      前端列表页需要一个人类可读的标签来区分 run（baseline / smoke / v2…）。
      此前它只存在 meta.db，导致「扫目录」这条读取路径拿不到它 ——
      结果就是服务层被迫依赖 DB 状态。放进产物里，目录自洽。

    ★ `detail_written=False` 时**不写** trades/positions/orders 三张明细表
      （参数寻优每组只关心指标，12 组 × 几百笔纯属浪费）。
      此时必须在产物里**明确标出**，否则「392 笔交易的 metrics」+「0 行的
      trades.parquet」会让详情页显示"交易数 392"却列不出一笔 ——
      这正是本项目反复踩的"看着成功、其实残缺"那一类坑。
      `_run.detail_written` 就是给下游用来区分"真的没有交易"和"没落盘"的。

      为什么放 `_run` 而不是顶层：`_run` 不参与 `make_run_id` 哈希。
      放顶层会让"同一配置跑两次"哈希不同，破坏"哈希相同 = 配置相同"的语义。
    """
    layout.root.mkdir(parents=True, exist_ok=True)

    n_equity = _write_parquet(equity, layout.equity_path, EQUITY_COLUMNS)
    if detail_written:
        n_trades = _write_parquet(trades, layout.trades_path, TRADE_COLUMNS)
        n_pos = _write_parquet(
            positions if positions is not None else pd.DataFrame(),
            layout.positions_path,
            POSITION_COLUMNS,
        )
        n_orders = _write_parquet(
            orders if orders is not None else pd.DataFrame(),
            layout.orders_path,
            ORDER_COLUMNS,
        )
    else:
        # 不留残缺文件：宁可文件不存在（下游 `_read_run_parquet` 会返回空表并
        # 配合 `detail_written=False` 给出"未落盘"提示），也不要留一个
        # "0 行但看着正常"的假明细表。
        n_trades = n_pos = n_orders = None
        for p in (layout.trades_path, layout.positions_path, layout.orders_path):
            p.unlink(missing_ok=True)

    payload = dict(config)
    payload["_run"] = {
        "strategy": layout.strategy,
        "run_id": layout.run_id,
        "tag": tag,
        "started_at": started_at,
        "finished_at": finished_at or datetime.now().isoformat(timespec="seconds"),
        # ★ 明细表是否落盘。下游靠它区分"真的 0 笔"与"没写"。
        "detail_written": bool(detail_written),
        "rows": {
            "equity": n_equity,
            "trades": n_trades,
            "positions": n_pos,
            "orders": n_orders,
        },
        # 额外的来源标记（参数寻优写 sweep_id / combo_index 等），同样不进哈希
        **(extra_run or {}),
    }
    layout.config_path.write_text(_dump_json(payload), encoding="utf-8")
    layout.metrics_path.write_text(_dump_json(metrics or {}), encoding="utf-8")

    logger.info(
        f"回测结果已落盘：{layout.root}｜净值 {n_equity} 行 / 交易 {n_trades} 笔 / "
        f"持仓快照 {n_pos} 行 / 委托 {n_orders} 条"
    )
    return {
        **layout.as_dict(),
        "rows": {"equity": n_equity, "trades": n_trades, "positions": n_pos, "orders": n_orders},
    }


# ======================================================================
# 策略基类
# ======================================================================
@dataclass
class StrategyContext:
    """策略能看到的全部信息（**只读**）。

    刻意做成"只读快照 + 显式下单接口"：策略不得直接改账户状态，
    否则交易成本、整手取整、涨跌停撮合这些约束会被绕过。
    """

    #: 当前交易日（信号生成日 T）
    day: str
    #: 当前总资产（= 现金 + 持仓市值），单票仓位按它算
    total_asset: float
    #: 可用现金
    cash: float
    #: 当前持仓数
    position_count: int
    #: 账户只读视图（引擎注入）
    account: Any
    #: 行情只读视图（引擎注入）
    market: Any
    #: 交易日历服务
    calendar: Any
    #: 当日股票池（`tools.universe.UniverseResult`）—— 引擎注入
    #: ★ 板块/ST/停牌/新股/一字涨跌停 这些**红线过滤只在这一处**实现，
    #:   策略只负责在自己的信号里挑，不要把过滤条件抄一遍。
    universe: Any = None


class BaseStrategy(ABC):
    """策略契约。

    ★ 一条铁律：**`on_bar()` 只允许"生成订单"，不允许直接成交**。
      成交一律由引擎在 `on_bar` 之后撮合 —— 策略拿不到成交价，自然也没法偷看。
      v1.6 起口径分两侧：**买单**在下一个交易日开盘成交；**卖单（清仓）**在
      信号日**当日尾盘按收盘价**成交（收盘跌停/停牌则顺延到下一交易日开盘）。
    """

    #: 策略名（用于目录名 `data/backtest/{name}/`）
    name: str = "base"
    #: 策略版本（规则变更必须升版本，便于结果对比）
    version: str = "0"

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        self.params: dict[str, Any] = dict(params or {})

    # ------------------------------------------------------------------
    # 生命周期
    #
    # `setup` / `teardown` 是**可选钩子**，刻意不加 @abstractmethod ——
    # 强制子类实现两个空方法只会长出 `pass`，没有任何约束价值。
    # ruff 的 B027 就是冲着"忘了加装饰器"来的，这里是有意为之，故显式豁免。
    # ------------------------------------------------------------------
    def setup(self, ctx: StrategyContext) -> None:  # noqa: B027
        """回测开始前调用一次（预热期第一天）。默认什么都不做。"""

    def teardown(self, ctx: StrategyContext) -> None:  # noqa: B027
        """回测结束后调用一次。默认什么都不做。"""

    # ------------------------------------------------------------------
    # 每根 K 线（日线 → 每个交易日收盘后）
    # ------------------------------------------------------------------
    @abstractmethod
    def on_bar(self, ctx: StrategyContext) -> None:
        """T 日收盘后执行：判清仓、判开仓，并**下单到 T+1**。

        实现里只能调用 `ctx.account.order_buy(...)` / `order_sell(...)`。
        """

    # ------------------------------------------------------------------
    # 元信息
    # ------------------------------------------------------------------
    def config_snapshot(self) -> dict[str, Any]:
        """写进 config.json 的策略信息（参数寻优时前端靠它区分不同组）。"""
        return {"name": self.name, "version": self.version, "params": self.params}

    def __repr__(self) -> str:  # pragma: no cover - 调试便利
        return f"<{type(self).__name__} {self.name} v{self.version} {self.params}>"


@dataclass
class StrategyResult:
    """引擎跑完之后交出来的东西（落盘前的内存态）。"""

    equity: pd.DataFrame = field(default_factory=pd.DataFrame)
    trades: pd.DataFrame = field(default_factory=pd.DataFrame)
    positions: pd.DataFrame = field(default_factory=pd.DataFrame)
    orders: pd.DataFrame = field(default_factory=pd.DataFrame)
    metrics: dict[str, Any] = field(default_factory=dict)
