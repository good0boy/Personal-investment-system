"""交易成本模型 —— ★ 全项目**唯一**允许计算交易成本的地方。

为什么它必须是独立模块
--------------------
成本模型有**三个消费者**，它们必须共用同一份实现，否则报告会自相矛盾：

1. **止盈判定**（策略规格 2.3 条件 2）—— 净浮盈是否 > 10%
2. **回测引擎的成交成本** —— 买入实付 / 卖出净得 / 「年化交易成本」指标
3. **参数寻优的横向对比** —— 不同持有期的成本差异

如果策略里手写一份、引擎里手写一份，两边的滑点或印花税口径早迟会分叉，
而分叉的表现是"回测赚钱、实盘亏钱"——最难查的那类 bug。

本模块的硬约束（来自 `策略规格说明书.md` v1.2 与 `技术 config.yaml`）
------------------------------------------------------------------
- 三档费率全部**从配置读**（`settings.cost`），不硬编码。
- **印花税按日期分段**（卖出单边）：2020-06-01 ~ 2023-08-25 = 0.1%；
  2023-08-28 起 = 0.05%。回测横跨了这次减半，不分段就是错的。
- **佣金最低 5 元**：`max(rate × 金额, 5)`。单笔金额低于 2 万元时生效。
- **整手 100 股**：买入股数向下取整。
- 滑点 5bp 作为**成本率的一部分**（与规格 §2.4 的表述一致），不是调整成交价。

⚠️ 一个容易搞错的地方：规格里写的"单边买入成本率 0.076%""往返 0.202%"
   都是**纯比例**口径，前提是佣金最低值不生效。本模块的 `buy_cost()` /
   `sell_cost()` 是**精确到元**的实现（含最低佣金），比例口径由
   `buy_rate()` / `sell_rate()` / `round_trip_rate()` 提供，
   两者在大额下单时数值一致，有测试钉住这个一致性。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import pandas as pd

from app.core.config import settings
from app.core.errors import AppError

__all__ = [
    "CostBreakdown",
    "buy_cost",
    "buy_paid",
    "buy_rate",
    "gross_return_for_net",
    "lots_for_amount",
    "max_affordable_shares",
    "net_return",
    "round_trip_rate",
    "sell_cost",
    "sell_net",
    "sell_rate",
    "stamp_tax_rate",
]


# ======================================================================
# 成本明细
# ======================================================================
@dataclass(frozen=True)
class CostBreakdown:
    """一笔交易的成本明细（单位：元）。四项之和 = `total`。"""

    notional: float          # 成交金额（不含费用）
    commission: float        # 佣金（已应用最低值）
    transfer_fee: float      # 过户费
    stamp_tax: float         # 印花税（买入恒为 0）
    slippage: float          # 滑点
    total: float

    def as_dict(self) -> dict[str, float]:
        return {
            "notional": round(self.notional, 4),
            "commission": round(self.commission, 4),
            "transfer_fee": round(self.transfer_fee, 4),
            "stamp_tax": round(self.stamp_tax, 4),
            "slippage": round(self.slippage, 4),
            "total": round(self.total, 4),
        }


def _cfg() -> Any:
    """取成本配置（每次现取，支持 `settings.reload()` 后生效）。"""
    cfg = getattr(settings, "cost", None)
    if cfg is None:  # pragma: no cover —— 配置缺失属于部署错误
        raise AppError("settings.cost 缺失：交易成本配置未加载")
    return cfg


def _as_date(day: str | date | datetime | pd.Timestamp) -> str:
    """统一成 YYYY-MM-DD 字符串，便于与配置里的日期字符串比较。"""
    if isinstance(day, str):
        return pd.Timestamp(day).strftime("%Y-%m-%d")
    if isinstance(day, pd.Timestamp):
        return day.strftime("%Y-%m-%d")
    if isinstance(day, datetime | date):
        return day.strftime("%Y-%m-%d")
    raise AppError(f"无法识别的日期类型：{type(day)!r}")


# ======================================================================
# 费率（纯比例口径）
# ======================================================================
def stamp_tax_rate(day: str | date | datetime | pd.Timestamp) -> float:
    """按**成交日**取印花税率（卖出单边）。

    ★ 必须分段：2023-08-28 起从 0.1% 减半到 0.05%，而本项目回测区间
      （2020-06-01 ~ 2024-12-31）正好横跨这次变更。

    Raises:
        AppError: 日期不在任何配置档位内 —— 属于配置错误，必须大声报错，
            绝不静默取 0（那会让跨区间的回测少算一半印花税）。
    """
    d = _as_date(day)
    for tier in _cfg().stamp_tax:
        if str(tier.from_date) <= d <= str(tier.to_date):
            return float(tier.rate)
    raise AppError(
        f"日期 {d} 不在任何印花税档位内，请检查 config.yaml 的 cost.stamp_tax",
        detail={"available": [
            {"from": t.from_date, "to": t.to_date, "rate": t.rate}
            for t in _cfg().stamp_tax
        ]},
    )


def buy_rate() -> float:
    """单边买入成本率（纯比例，不含佣金最低值）。规格值 0.076%。"""
    cfg = _cfg()
    return (
        float(cfg.commission_rate)
        + float(cfg.transfer_fee_rate)
        + float(cfg.slippage_bp) / 10_000.0
    )


def sell_rate(day: str | date | datetime | pd.Timestamp) -> float:
    """单边卖出成本率（纯比例，含当日印花税）。规格值 0.126% / 0.176%。"""
    cfg = _cfg()
    return (
        float(cfg.commission_rate)
        + stamp_tax_rate(day)
        + float(cfg.transfer_fee_rate)
        + float(cfg.slippage_bp) / 10_000.0
    )


def round_trip_rate(day: str | date | datetime | pd.Timestamp) -> float:
    """单次往返成本率（纯比例）。规格值 0.202%（2023-08 后）/ 0.252%（之前）。"""
    return buy_rate() + sell_rate(day)


# ======================================================================
# 金额口径（精确到元）
# ======================================================================
def _commission(notional: float) -> float:
    """佣金 = max(rate × 金额, 最低值)。"""
    cfg = _cfg()
    return max(float(cfg.commission_rate) * notional, float(cfg.commission_min))


def buy_cost(notional: float) -> CostBreakdown:
    """买入费用（印花税恒为 0）。"""
    cfg = _cfg()
    n = float(notional)
    commission = _commission(n)
    transfer_fee = float(cfg.transfer_fee_rate) * n
    slippage = float(cfg.slippage_bp) / 10_000.0 * n
    total = commission + transfer_fee + slippage
    return CostBreakdown(n, commission, transfer_fee, 0.0, slippage, total)


def sell_cost(notional: float, day: str | date | datetime | pd.Timestamp) -> CostBreakdown:
    """卖出费用（含当日印花税）。"""
    cfg = _cfg()
    n = float(notional)
    commission = _commission(n)
    transfer_fee = float(cfg.transfer_fee_rate) * n
    stamp = stamp_tax_rate(day) * n
    slippage = float(cfg.slippage_bp) / 10_000.0 * n
    total = commission + transfer_fee + stamp + slippage
    return CostBreakdown(n, commission, transfer_fee, stamp, slippage, total)


def buy_paid(price: float, shares: int) -> float:
    """买入实付现金 = 成交金额 + 买入费用。"""
    n = float(price) * int(shares)
    return n + buy_cost(n).total


def sell_net(price: float, shares: int, day: str | date | datetime | pd.Timestamp) -> float:
    """卖出净得现金 = 成交金额 - 卖出费用。"""
    n = float(price) * int(shares)
    return n - sell_cost(n, day).total


# ======================================================================
# 整手与可买股数
# ======================================================================
def _lot_size() -> int:
    return int(_cfg().lot_size)


def lots_for_amount(amount: float, price: float) -> int:
    """按目标金额算可买股数（向下取整到整手）。

    ⚠️ 这里的 amount 是"打算投入的**成交金额**"，不是现金。
       要按现金算可买股数请用 `max_affordable_shares()`（它扣了费用）。
    """
    price = float(price)
    if price <= 0 or amount <= 0:
        return 0
    lot = _lot_size()
    return math.floor(float(amount) / price / lot) * lot


def max_affordable_shares(cash: float, price: float) -> int:
    """现金能买得起多少股（整手，且**扣得起费用**）。

    先用比例费率估一次，再回退校正 —— 因为佣金有 5 元最低值，
    只按比例估会在小额下单时超支（现金不足导致下单失败）。
    """
    price = float(price)
    if price <= 0 or cash <= 0:
        return 0
    lot = _lot_size()

    shares = math.floor(float(cash) / (price * (1.0 + buy_rate())) / lot) * lot
    # 回退校正：确保 金额 + 费用 <= 现金
    while shares > 0 and buy_paid(price, shares) > cash:
        shares -= lot
    return max(shares, 0)


# ======================================================================
# 收益率口径（止盈判定的唯一实现）
# ======================================================================
def net_return(
    entry_price: float,
    exit_price: float,
    day: str | date | datetime | pd.Timestamp,
    shares: int,
) -> float:
    """已扣交易成本的**净收益率**。

    ★ 这是策略规格 2.3 条件 2「止盈」的唯一判定依据，也是回测引擎算
      单笔盈亏的同一把尺子。

    ```
    净收益率 = 卖出净得 / 买入实付 - 1
    ```
    与规格给的公式 `close_hfq×(1-卖出成本率)/(entry×(1+买入成本率)) - 1`
    在"佣金最低值不生效"时**完全等价**；本实现额外覆盖了最低佣金生效的
    小额场景（净值跌到 10 万以下时 20% 仓位不足 2 万元）。

    Args:
        entry_price: 买入成交价（后复权）
        exit_price: 卖出判定价（后复权收盘价）
        day: **成交日**（决定印花税档位）。注意止盈判定用 T 日（收盘判定），
             实际成交在 T+1 —— 除非跨过 2023-08-28，两者税率相同。
        shares: 持股数（整手）
    """
    shares = int(shares)
    if shares <= 0:
        raise AppError(f"net_return 需要正整数股数，收到 {shares}")
    paid = buy_paid(entry_price, shares)
    if paid <= 0:
        raise AppError(f"买入实付金额非正：entry_price={entry_price}, shares={shares}")
    return sell_net(exit_price, shares, day) / paid - 1.0


def gross_return_for_net(
    target_net: float,
    day: str | date | datetime | pd.Timestamp,
) -> float:
    """把"目标净收益率"换算成所需的**毛涨幅**。

    用途：把止盈阈值 10% 翻译成人能理解的"毛涨约 10.22% / 10.28%"，
    便于核对规格文档里的换算表。

    推导（佣金最低值不生效时）：
        (1+g)(1-sell_rate) / (1+buy_rate) - 1 = target_net
      ⇒ g = (1 + target_net) × (1 + buy_rate) / (1 - sell_rate) - 1
    """
    if not 0 <= target_net < 1:
        raise AppError(f"目标净收益率应在 [0, 1) 内，收到 {target_net}")
    return (1.0 + float(target_net)) * (1.0 + buy_rate()) / (1.0 - sell_rate(day)) - 1.0
