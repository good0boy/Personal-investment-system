"""股票池（Universe）过滤规则 —— 纯函数，无副作用。

★ 这是"数据正确性红线"之一。回测引擎与模拟盘**必须**调用本模块构建每日股票池，
否则回测收益会被"理论上买不到"的股票污染（连续涨停、停牌、ST、新股等）。

⚠️ 两个容易混淆的参数（见 策略规格说明书.md 2.1）：

    | 参数             | 值          | 作用对象      | 含义                       |
    |------------------|-------------|---------------|----------------------------|
    | 新股入池门槛     | 90 交易日   | 股票**入池**  | 上市满 90 日后才有资格入选 |
    | 卖出后禁买窗口   | 60 交易日   | 个股**卖出后**| 卖出后 60 日内不得再买     |

过滤维度（全部必须生效）：
    1. 板块：仅沪主板 / 深主板 / 创业板
    2. ST：`is_st == 0`（baostock 的 isST 字段，逐日历史状态）
    3. 停牌：`trade_status == 1`
    4. 新股：上市满 90 个交易日
    5. 一字涨跌停：无法成交
    6. 数据完整性：当日必须有有效收盘价
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from app.core.config import settings
from app.core.logging import logger

# ★ 板块判定必须用**看交易所**的那一套（`*_symbol`）。
#   原因：`sh.000001`（上证指数）与 `sz.000001`（平安银行）6 位码相同，
#   不看交易所就会把上证指数当成深主板股票 —— 实测全市场清单 5014 只里
#   混进了 223 个 `sh.000xxx` 指数（2026-09-18 发现）。
from app.services.data_source.symbols import board_of_symbol, is_target_board_symbol

# ----------------------------------------------------------------------
# 常量（来自 config.yaml，此处给出代码级默认值便于单测）
# ----------------------------------------------------------------------
LISTED_DAYS_MIN: int = 90        # 新股入池门槛（交易日）
NO_REBUY_DAYS: int = 60          # 卖出后禁买窗口（交易日）—— 与上者不同，勿混淆
MIN_AMOUNT: float = 0.0          # 可选：最小成交额门槛（0 = 不启用）


def _cfg_listed_days_min() -> int:
    try:
        return int(settings.universe.listed_days_min)
    except Exception:
        return LISTED_DAYS_MIN


def _cfg_no_rebuy_days() -> int:
    try:
        return int(settings.universe.no_rebuy_days)
    except Exception:
        return NO_REBUY_DAYS


# ----------------------------------------------------------------------
# 过滤结果
# ----------------------------------------------------------------------
@dataclass
class UniverseResult:
    """股票池构建结果，含每一步的剔除数量（便于诊断）。"""

    day: str
    symbols: list[str] = field(default_factory=list)
    dropped: dict[str, int] = field(default_factory=dict)
    total_candidates: int = 0

    def add_drop(self, reason: str, n: int) -> None:
        if n:
            self.dropped[reason] = self.dropped.get(reason, 0) + n

    def summary(self) -> str:
        parts = [f"{self.day} 股票池 {len(self.symbols)} 只（候选 {self.total_candidates}）"]
        if self.dropped:
            detail = "，".join(f"剔除{k}={v}" for k, v in self.dropped.items())
            parts.append(f"[{detail}]")
        return " ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "day": self.day,
            "symbol_count": len(self.symbols),
            "total_candidates": self.total_candidates,
            "dropped": self.dropped,
            "summary": self.summary(),
        }


# ----------------------------------------------------------------------
# 单条规则（纯函数，便于单测）
# ----------------------------------------------------------------------
# `is_target_board_symbol` 由 `app.services.data_source.symbols` 提供并在此
# 重新导出（历史上这里有一份本地实现，用裸 6 位码判定 → 会把 `sh.000001`
# 上证指数判成深主板股票）。改判定规则请改 symbols.py，不要在这里另写一份。


def not_st(row: pd.Series) -> bool:
    """非 ST。⚠️ 必须用逐日 is_st，不能用当前 ST 列表。"""
    v = row.get("is_st")
    if pd.isna(v):
        # is_st 缺失时保守处理：视为不可交易（宁可漏掉，不可误买）
        return False
    return int(v) == 0


def is_trading(row: pd.Series) -> bool:
    """当日正常交易（非停牌）。"""
    v = row.get("trade_status")
    if pd.isna(v):
        return False
    return int(v) == 1


def has_valid_price(row: pd.Series) -> bool:
    """有有效收盘价。"""
    for col in ("close", "preclose"):
        v = row.get(col)
        if pd.isna(v) or float(v) <= 0:
            return False
    return True


#: 涨跌幅限制（小数）
LIMIT_PCT_ST: float = 0.05          # ST 股 ±5%
LIMIT_PCT_DEFAULT: float = 0.10     # 主板 ±10%
LIMIT_PCT_GEM_20: float = 0.20      # 创业板注册制后 ±20%
#: 创业板 20% 生效日（2020-08-24 首批注册制股票上市）
GEM_20PCT_FROM: str = "2020-08-24"


def _round2(x: float) -> float:
    """四舍五入到分（交易所的限价取整方式）。

    不用内置 `round()`：它是**银行家舍入**（.5 往偶数靠），
    在 11.055 这类边界上会与交易所的报价不一致。
    """
    return math.floor(float(x) * 100 + 0.5 + 1e-9) / 100


def price_limit_pct(
    symbol: str,
    day: Any,
    is_st: int | bool | None = 0,
) -> float:
    """当日该股票的涨跌幅限制（小数，如 0.10）。

    ★ **单一实现**：`is_limit_locked()`（判断一字板）与回测引擎
      （判断"开盘即跌停卖不掉 / 开盘即涨停买不到"）都走这里。
      两处各写一份的话，创业板 20% 的生效日（2020-08-24）迟早不一致，
      而这类差错只会让撮合变宽松 → 悄悄高估收益。
    """
    if is_st is not None and not pd.isna(is_st) and int(is_st) == 1:
        return LIMIT_PCT_ST
    try:
        is_gem = board_of_symbol(symbol) == "gem"
    except Exception:
        is_gem = False
    if (
        is_gem
        and day is not None
        and not pd.isna(day)
        and pd.Timestamp(day) >= pd.Timestamp(GEM_20PCT_FROM)
    ):
        return LIMIT_PCT_GEM_20
    return LIMIT_PCT_DEFAULT


def limit_up_price(preclose: float, limit: float) -> float:
    """涨停价（四舍五入到分）。"""
    return _round2(float(preclose) * (1.0 + float(limit)))


def limit_down_price(preclose: float, limit: float) -> float:
    """跌停价（四舍五入到分）。"""
    return _round2(float(preclose) * (1.0 - float(limit)))


# ----------------------------------------------------------------------
# 涨跌停**触及判定**（唯一实现 —— 引擎撮合与策略信号都走这里）
# ----------------------------------------------------------------------
#: 触及判定的**比例容差**：`|price/preclose − 1 ∓ limit|` 的允许偏差。
#:
#: 为什么必须用比例而不是绝对元数（2026-09-18 审计实测）：
#:   真实涨跌停价的取整发生在**不复权价**上（四舍五入到分，偏差 ≤ 0.005 元）。
#:   本项目行情统一存**后复权**价，复权因子 k 会把这点取整错位放大 k 倍，
#:   绝对容差（如 0.005 元）在复权尺度上失效 —— 实测 410 只 × 39 万交易日，
#:   开盘涨停漏判 58%、开盘跌停漏判 55%、收盘跌停漏判 58%。
#:   换成比例口径后与复权因子无关（open/preclose 的比值不受复权影响）。
#:
#: 0.005 的定界：取整偏差换算成比例 ≤ 0.005/pre_raw；A 股最低价约 1 元
#:   （面值退市线）→ 比例上限 0.5%。副作用：pre 较大时"离涨停价还差半分钱
#:   以上但涨幅落在 [limit−0.5%, limit]"的开盘会被保守判为触及涨停 ——
#:   错杀（少买/顺延）比错放（按不可能的价格成交）安全，方向可接受。
LIMIT_RATIO_TOL: float = 0.005


def at_limit_up(price: float, preclose: float, limit: float) -> bool:
    """价格是否触及涨停（比例容差，复权安全）。

    引擎 `_buyable`（T+1 开盘涨停 → 买不到）与股票池一字板判定共用。
    """
    pre = float(preclose)
    if not np.isfinite(price) or not np.isfinite(pre) or pre <= 0:
        return False
    return abs(float(price) / pre - 1.0 - float(limit)) <= LIMIT_RATIO_TOL


def at_limit_down(price: float, preclose: float, limit: float) -> bool:
    """价格是否触及跌停（比例容差，复权安全）。

    引擎 `_sellable`（开盘跌停 → 无买盘卖不掉）与策略「跌停清仓」共用。
    """
    pre = float(preclose)
    if not np.isfinite(price) or not np.isfinite(pre) or pre <= 0:
        return False
    return abs(float(price) / pre - 1.0 + float(limit)) <= LIMIT_RATIO_TOL


def is_limit_locked(row: pd.Series, *, tol: float | None = None) -> bool:
    """是否一字涨跌停（当日无成交空间，无法买卖）。

    判断依据：**开=高=低=收，且价格触及按前收盘算出的涨/跌停价**。

    ★ 触及判定走 `at_limit_up` / `at_limit_down`（比例容差，复权安全）。
      旧实现用绝对元数容差（0.005）在 hfq 价格上比对限价 —— 复权因子放大
      取整错位后系统性漏判（实测 58%），已废弃。`tol` 参数保留仅为兼容旧
      调用签名，不再使用。
      按限价精确比对而非"涨幅 ≥ 9.8%"的原因见下（低价股经验容差不可靠）。

    涨跌幅限制按板块与日期区分（ST ±5%；创业板 2020-08-24 起 ±20%；其余 ±10%）。
    """
    del tol  # 兼容旧签名；触及判定已统一为 LIMIT_RATIO_TOL
    o, h, low, c = (row.get("open"), row.get("high"), row.get("low"), row.get("close"))
    if any(pd.isna(x) for x in (o, h, low, c)):
        return False
    o, h, low, c = float(o), float(h), float(low), float(c)
    if not (abs(o - h) < 1e-9 and abs(h - low) < 1e-9 and abs(low - c) < 1e-9):
        return False

    preclose = row.get("preclose")
    if preclose is None or pd.isna(preclose) or float(preclose) <= 0:
        return False

    limit = price_limit_pct(
        str(row.get("symbol", "")), row.get("date"), row.get("is_st")
    )
    return at_limit_up(c, float(preclose), limit) or at_limit_down(
        c, float(preclose), limit
    )


def listed_days_enough(
    symbol: str,
    day: str,
    first_trade_date: dict[str, str] | pd.Series,
    *,
    min_days: int | None = None,
    calendar_days_fn: Any = None,
) -> bool:
    """上市是否满 N 个交易日。

    Args:
        first_trade_date: symbol -> 首个交易日（YYYY-MM-DD）
        min_days: 默认取 config.universe.listed_days_min（90）
        calendar_days_fn: callable(start, end) -> list[str]，用交易日计数
                         不传则用自然日近似（仅用于快速校验，不推荐）
    """
    need = min_days if min_days is not None else _cfg_listed_days_min()

    if isinstance(first_trade_date, pd.Series):
        first = first_trade_date.get(symbol)
    else:
        first = first_trade_date.get(symbol)

    if first is None or (isinstance(first, float) and pd.isna(first)):
        return False

    first_s = pd.Timestamp(first).strftime("%Y-%m-%d")
    day_s = pd.Timestamp(day).strftime("%Y-%m-%d")
    if first_s > day_s:
        return False

    if calendar_days_fn is not None:
        return len(calendar_days_fn(first_s, day_s)) >= need

    # 无日历时的保守近似：自然日 / 1.45 ≈ 交易日（一年 365/242）
    approx = (pd.Timestamp(day_s) - pd.Timestamp(first_s)).days / 1.45
    return approx >= need


def rebuy_allowed(
    symbol: str,
    day: str,
    last_sell_date: dict[str, str],
    *,
    window: int | None = None,
    calendar_days_fn: Any = None,
) -> bool:
    """是否允许再买入（卖出后 60 个交易日禁买）。

    ⚠️ 这是"卖出后禁买窗口"，与"新股入池门槛 90 日"是两个不同参数。
    """
    need = window if window is not None else _cfg_no_rebuy_days()

    last = last_sell_date.get(symbol)
    if not last or pd.isna(last):
        return True

    last_s = pd.Timestamp(last).strftime("%Y-%m-%d")
    day_s = pd.Timestamp(day).strftime("%Y-%m-%d")
    if last_s > day_s:
        return True

    if calendar_days_fn is not None:
        return len(calendar_days_fn(last_s, day_s)) > need

    approx = (pd.Timestamp(day_s) - pd.Timestamp(last_s)).days / 1.45
    return approx > need


# ----------------------------------------------------------------------
# 向量化掩码（build_universe 用）
#
# ★ 为什么必须向量化，而不是逐行 `df.apply(fn, axis=1)`：
#   1. **性能**：逐行 apply 实测 0.77ms/只 × 4500 只 ≈ 3.5s/天，
#      回测 1115 天累积到 ~65 分钟。
#   2. **正确性**：`DataFrame.apply(axis=1)` 在**空 DataFrame** 上返回的是
#      DataFrame 而不是 Series，后续 `df[mask]` 会得到无列的空表，
#      再取 `df["symbol"]` 直接 KeyError。
#      即"某天所有股票都被过滤掉"时整个回测会崩，而不是返回空池。
#
# 单行纯函数（not_st / is_limit_locked / ...）保留不变 ——
# 它们服务于"查单只股票"的场景，且有独立单测。
# ----------------------------------------------------------------------
def _code6_series(df: pd.DataFrame) -> pd.Series:
    """从 symbol 列抽出 6 位代码（sh.600519 -> 600519）。"""
    return df["symbol"].astype(str).str.slice(3, 9)


def _mask_target_board(df: pd.DataFrame) -> pd.Series:
    """目标板块掩码（沪主板 / 深主板 / 创业板）。

    ★ 必须按**完整 symbol**（`sh.600519`）判定，不能只取 6 位码：
      `sh.000001`（上证指数）与 `sz.000001`（平安银行）6 位码相同。
    """
    syms = df["symbol"].astype(str)
    uniq = pd.unique(syms)
    board_ok = {s: is_target_board_symbol(str(s)) for s in uniq}
    return syms.map(board_ok).fillna(False).astype(bool)


def _mask_not_st(df: pd.DataFrame) -> pd.Series:
    """非 ST 掩码。⚠️ 缺失视为不可交易（宁可漏掉，不可误买）。"""
    v = pd.to_numeric(df["is_st"], errors="coerce")
    return (v == 0).fillna(False)


def _mask_trading(df: pd.DataFrame) -> pd.Series:
    """正常交易（非停牌）掩码。"""
    v = pd.to_numeric(df["trade_status"], errors="coerce")
    return (v == 1).fillna(False)


def _mask_valid_price(df: pd.DataFrame) -> pd.Series:
    """有有效收盘价与前收盘价。"""
    close = pd.to_numeric(df["close"], errors="coerce")
    pre = pd.to_numeric(df["preclose"], errors="coerce")
    return close.notna() & (close > 0) & pre.notna() & (pre > 0)


def _mask_not_limit_locked(df: pd.DataFrame) -> pd.Series:
    """非一字涨跌停掩码（一字板无法成交）。

    判定：开=高=低=收 且 |涨跌幅| 达到该板块当日的涨跌幅限制。
    限制按板块与日期区分：ST ±5%；创业板 2020-08-24 起 ±20%，之前 ±10%；其余 ±10%。
    """
    o = pd.to_numeric(df["open"], errors="coerce")
    h = pd.to_numeric(df["high"], errors="coerce")
    low = pd.to_numeric(df["low"], errors="coerce")
    c = pd.to_numeric(df["close"], errors="coerce")

    is_flat = o.notna() & (o == h) & (h == low) & (low == c)

    abs_pct = pd.to_numeric(df["pct_chg"], errors="coerce").abs()
    st_flag = pd.to_numeric(df["is_st"], errors="coerce").fillna(0).astype("int64") == 1

    codes = _code6_series(df)
    is_gem = codes.str.slice(0, 3).isin(("300", "301"))

    dates = pd.to_datetime(df["date"], errors="coerce")
    # 与单行实现保持一致：日期缺失时按"已实施 20%"处理
    post_20200824 = dates.isna() | (dates >= pd.Timestamp("2020-08-24"))
    gem_20pct = is_gem & post_20200824

    limit = np.where(st_flag, 4.8, np.where(gem_20pct, 19.5, 9.8))
    locked = is_flat & abs_pct.notna() & (abs_pct.to_numpy() >= limit)
    return ~pd.Series(locked, index=df.index)


def _mask_listed_days(
    df: pd.DataFrame,
    day_s: str,
    first_trade_date: dict[str, str] | pd.Series,
    *,
    min_days: int | None = None,
    calendar_days_fn: Any = None,
) -> pd.Series:
    """上市满 N 个交易日的掩码。

    ★ 性能要点：不要对每只股票调一次 `calendar_days_fn`（那是一次全量日历扫描）。
      改为**一次性**取出 [最早上市日, day] 的交易日序列，
      再用 `searchsorted` 对全部标的做 O(log n) 定位。
    """
    need = min_days if min_days is not None else _cfg_listed_days_min()
    symbols = df["symbol"].astype(str)

    firsts = pd.to_datetime(symbols.map(first_trade_date), errors="coerce")
    day_ts = pd.Timestamp(day_s)

    base_ok = firsts.notna() & (firsts <= day_ts)
    if not base_ok.any():
        return pd.Series(False, index=df.index)

    if calendar_days_fn is not None:
        subset = firsts[base_ok]
        earliest = subset.min().strftime("%Y-%m-%d")
        all_days = pd.DatetimeIndex(
            pd.to_datetime(sorted(calendar_days_fn(earliest, day_s)))
        )
        # [first, day] 内的交易日数 = 序列中 >= first 的元素个数
        left = all_days.searchsorted(subset.to_numpy(), side="left")
        counts = pd.Series(len(all_days) - left, index=subset.index)
        ok = counts >= need
    else:
        # 无日历时的保守近似：自然日 / 1.45 ≈ 交易日（一年 365/242）
        approx = (day_ts - firsts).dt.days / 1.45
        ok = approx >= need

    out = pd.Series(False, index=df.index)
    out.loc[base_ok] = ok.reindex(base_ok[base_ok].index).fillna(False)
    return out


def _mask_rebuy_allowed(
    df: pd.DataFrame,
    day_s: str,
    last_sell_date: dict[str, str] | pd.Series,
    *,
    window: int | None = None,
    calendar_days_fn: Any = None,
) -> pd.Series:
    """是否允许再买入（卖出后 60 个交易日禁买）。

    ⚠️ 与"新股入池门槛 90 日"是两个独立参数。
    ⚠️ 单行实现用的是**严格大于**（`> need`），这里保持一致。
    """
    need = window if window is not None else _cfg_no_rebuy_days()
    symbols = df["symbol"].astype(str)

    lasts = pd.to_datetime(symbols.map(last_sell_date), errors="coerce")
    day_ts = pd.Timestamp(day_s)

    # 从未卖出 或 卖出日在未来 → 允许
    free = lasts.isna() | (lasts > day_ts)
    blocked_candidates = ~free

    out = pd.Series(True, index=df.index)
    if not blocked_candidates.any():
        return out

    subset = lasts[blocked_candidates]
    if calendar_days_fn is not None:
        earliest = subset.min().strftime("%Y-%m-%d")
        all_days = pd.DatetimeIndex(
            pd.to_datetime(sorted(calendar_days_fn(earliest, day_s)))
        )
        left = all_days.searchsorted(subset.to_numpy(), side="left")
        counts = pd.Series(len(all_days) - left, index=subset.index)
        allowed = counts > need
    else:
        approx = (day_ts - subset).dt.days / 1.45
        allowed = approx > need

    out.loc[blocked_candidates] = allowed.reindex(blocked_candidates[blocked_candidates].index).fillna(False)
    return out


# ----------------------------------------------------------------------
# 主入口
# ----------------------------------------------------------------------
def build_universe(
    day: str,
    quotes_today: pd.DataFrame,
    *,
    first_trade_date: dict[str, str] | None = None,
    last_sell_date: dict[str, str] | None = None,
    calendar_days_fn: Any = None,
    min_listed_days: int | None = None,
    exclude_limit_locked: bool = True,
    verbose: bool = False,
) -> UniverseResult:
    """构建某一交易日的股票池。

    Args:
        day: 交易日 YYYY-MM-DD
        quotes_today: 当日全市场行情快照，须含列
                      [symbol, close, preclose, open, high, low, pct_chg, trade_status, is_st]
        first_trade_date: symbol -> 首个交易日，用于新股门槛
        last_sell_date: symbol -> 最近卖出日，用于禁买窗口
        calendar_days_fn: callable(start, end) -> list[str]（交易日计数）
        min_listed_days: 覆盖默认的 90 交易日门槛

    Returns:
        UniverseResult
    """
    day_s = pd.Timestamp(day).strftime("%Y-%m-%d")
    result = UniverseResult(day=day_s)

    if quotes_today is None or quotes_today.empty:
        logger.warning(f"{day_s} 行情快照为空，股票池为空")
        return result

    df = quotes_today.copy()
    result.total_candidates = len(df)

    # 1) 板块
    mask_board = _mask_target_board(df)
    result.add_drop("非目标板块", int((~mask_board).sum()))
    df = df[mask_board]
    if df.empty:
        if verbose:
            logger.info(result.summary())
        return result

    # 2) ST（逐日 is_st 字段）
    if "is_st" in df.columns:
        mask_st = _mask_not_st(df)
        result.add_drop("ST", int((~mask_st).sum()))
        df = df[mask_st]
        if df.empty:
            if verbose:
                logger.info(result.summary())
            return result
    else:
        logger.warning("行情缺少 is_st 列，无法剔除 ST —— 请确认使用了 baostock 数据")

    # 3) 停牌
    if "trade_status" in df.columns:
        mask_trade = _mask_trading(df)
        result.add_drop("停牌", int((~mask_trade).sum()))
        df = df[mask_trade]
        if df.empty:
            if verbose:
                logger.info(result.summary())
            return result

    # 4) 有效价格
    if {"close", "preclose"}.issubset(df.columns):
        mask_price = _mask_valid_price(df)
        result.add_drop("价格无效", int((~mask_price).sum()))
        df = df[mask_price]
        if df.empty:
            if verbose:
                logger.info(result.summary())
            return result

    # 5) 新股门槛（90 交易日）
    if first_trade_date:
        mask_listed = _mask_listed_days(
            df, day_s, first_trade_date,
            min_days=min_listed_days,
            calendar_days_fn=calendar_days_fn,
        )
        result.add_drop(
            f"上市不足{min_listed_days or _cfg_listed_days_min()}交易日",
            int((~mask_listed).sum()),
        )
        df = df[mask_listed]
        if df.empty:
            if verbose:
                logger.info(result.summary())
            return result

    # 6) 禁买窗口（60 交易日）
    if last_sell_date:
        mask_rebuy = _mask_rebuy_allowed(
            df, day_s, last_sell_date, calendar_days_fn=calendar_days_fn
        )
        result.add_drop(f"卖出后{_cfg_no_rebuy_days()}交易日内", int((~mask_rebuy).sum()))
        df = df[mask_rebuy]
        if df.empty:
            if verbose:
                logger.info(result.summary())
            return result

    # 7) 一字涨跌停
    if exclude_limit_locked and {"open", "high", "low", "close"}.issubset(df.columns):
        mask_limit = _mask_not_limit_locked(df)
        result.add_drop("一字涨跌停", int((~mask_limit).sum()))
        df = df[mask_limit]

    result.symbols = [] if df.empty else df["symbol"].astype(str).tolist()
    if verbose:
        logger.info(result.summary())
    return result


def build_universe_from_store(
    day: str,
    *,
    first_trade_date: dict[str, str] | None = None,
    last_sell_date: dict[str, str] | None = None,
    verbose: bool = False,
) -> UniverseResult:
    """从本地数据仓库读取当日快照并构建股票池（生产路径）。"""
    from app.services import data_store  # 局部导入避免循环  # noqa: PLC0415
    from app.services.calendar_service import calendar  # noqa: PLC0415

    day_s = pd.Timestamp(day).strftime("%Y-%m-%d")

    quotes = data_store.read_quotes(
        symbols=None,
        start_date=day_s,
        end_date=day_s,
        columns=[
            "date", "symbol", "open", "high", "low", "close",
            "preclose", "amount", "pct_chg", "trade_status", "is_st",
        ],
    )

    return build_universe(
        day_s,
        quotes,
        first_trade_date=first_trade_date,
        last_sell_date=last_sell_date,
        calendar_days_fn=calendar.trading_days,
        verbose=verbose,
    )


def derive_first_trade_date(quotes: pd.DataFrame) -> dict[str, str]:
    """从行情长表推导每个 symbol 的首个交易日（用于新股门槛）。"""
    if quotes is None or quotes.empty or "symbol" not in quotes.columns:
        return {}
    g = quotes.groupby("symbol")["date"].min()
    return {str(k): pd.Timestamp(v).strftime("%Y-%m-%d") for k, v in g.items()}
