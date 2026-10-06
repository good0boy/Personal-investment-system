"""日度行情因子实现。

统一契约
--------
    def fn(q: pd.DataFrame, **params) -> pd.Series

`q` 由 `factors/calc.py` 准备好：长表、已按 `[symbol, date]` 稳定排序、
**已剔除停牌行（trade_status != 1）与价格无效行**、index 为干净的 RangeIndex。
返回的 Series 必须与 `q.index` 等长同序（允许 NaN）。

为什么"剔除停牌行"放在 calc 层而不是每个因子自己处理
--------------------------------------------------
停牌日的 baostock 行是**有价无市**（`trade_status=0`，close 等于前收）——
把它当有效观测，会让"20 日波动率"在停牌期被人为压低、"20 日动量"被拉长。
这是一个**口径**问题，不是某个因子的问题；散在各个实现里必然出现
"A 因子剔了、B 因子忘了"。所以统一在入口收口（本项目纪律：同一口径集中定义）。

对外的口径说明以 `factors/registry.yaml` 的 `desc` 字段为准（前端展示的就是它）；
本文件的 docstring 给的是**实现公式**，两者是同一件事的两种表述。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = [
    "amount_mean",
    "amplitude_mean",
    "ma_bias",
    "max_daily_ret",
    "ret_n",
    "rsi",
    "today_pct",
    "turnover_mean",
    "volatility",
    "volume_ratio_n",
]

#: 因子计算要求的最少有效观测 = 窗口长度（不足即 NaN）
_MIN_PERIODS_POLICY = "full"


def _rolling(q: pd.DataFrame, values: np.ndarray, window: int, how: str) -> pd.Series:
    """按 symbol 分组做滚动聚合，返回与 `q` 对齐的 Series。

    为什么这么写（踩过的坑）
    ----------------------
    最直觉的写法是 `q.groupby("symbol")["v"].rolling(w)`。但 `GroupBy.rolling`
    返回的是 **MultiIndex(symbol, 原始 index)** 的 Series，而 `groupby.pct_change`
    这类返回的是**原索引对齐**的 Series —— 两者混用会让"有的因子能直接返回、
    有的因子索引错位"，后者**不报错**，只是数值悄悄对不上股票（最坏的那种 bug）。

    所以这里统一走同一条路：用 numpy 数组重建一个干净的 RangeIndex 帧
    （与 `q.index` 无关，杜绝重复索引的隐患）→ rolling → `sort_index()`
    还原成行序 → 最后把 index 换成 `q.index`。

    Args:
        values: 与 `q` 等长的数组（不是 Series，避免索引参与对齐）
        how: "mean" | "std" | "max"
    """
    if window <= 0:
        raise ValueError(f"window 必须为正整数，收到 {window}")

    frame = pd.DataFrame(
        {"symbol": q["symbol"].to_numpy(), "v": values}, copy=False
    )
    grp = frame.groupby("symbol", sort=False)["v"]
    roll = grp.rolling(window, min_periods=window)

    if how == "mean":
        out = roll.mean()
    elif how == "std":
        # ddof=1：样本标准差。与 pandas Series.std() 默认一致。
        out = roll.std(ddof=1)
    elif how == "max":
        out = roll.max()
    else:  # pragma: no cover - 编程错误
        raise ValueError(f"不支持的滚动聚合：{how!r}")

    out = out.reset_index(level=0, drop=True).sort_index()
    out.index = q.index
    return out.astype("float64")


def _daily_return(q: pd.DataFrame) -> np.ndarray:
    """日收益率（小数）。`pct_chg` 在 baostock 里是**百分数**（1.15 表示 +1.15%）。"""
    return pd.to_numeric(q["pct_chg"], errors="coerce").to_numpy(dtype="float64") / 100.0


def _group_shift(q: pd.DataFrame, values: np.ndarray, periods: int) -> np.ndarray:
    """按 symbol 分组位移，返回与 `q.index` 等长的数组。

    与 `ret_n` 里 `groupby(...).shift(...)` 同源，刻意**显式**写 groupby：
      · 杜绝"把上一只股票的末行当成下一只的首行"的**跨标的拼接** ——
        这种错**不报错**，只是数值悄悄对不上标的（本项目最忌讳的那类 bug）；
      · 用 numpy 数组进出，不让索引参与对齐，避开 `GroupBy.rolling` 的
        MultiIndex 坑（见 `_rolling` 的说明）。
    """
    return (
        pd.Series(np.asarray(values, dtype="float64"), index=q.index, dtype="float64")
        .groupby(q["symbol"].to_numpy(), sort=False)
        .shift(periods)
        .to_numpy(dtype="float64")
    )


# ======================================================================
# 动量族
# ======================================================================
def ret_n(q: pd.DataFrame, window: int = 20) -> pd.Series:
    """区间累计收益：`close / close[t-window] - 1`。

    为什么显式写 `groupby(...).shift(window)` 而不是 `groupby(...).pct_change(window)`：
      · **跨标的拼接**风险 —— 忘了 groupby 的 `shift` 会把上一只股票的末行
        当成下一只的首行，算出一个不存在的"跨股票收益"，且**不报错**。
        这里 groupby 是显式的，shift 也显式。
      · `pct_change` 的 `fill_method` 形参在 pandas 2.2 被弃用、3.0 被移除
        （本项目约束 `pandas>=2.2,<3.0`，但没理由给自己埋一颗升级雷）。
        `shift` 没有这类历史包袱。

    窗口不足 → NaN（`shift` 天然如此）。
    """
    if window <= 0:
        raise ValueError(f"window 必须为正整数，收到 {window}")
    close = pd.to_numeric(q["close"], errors="coerce").to_numpy(dtype="float64")
    base = (
        pd.Series(close, index=q.index, dtype="float64")
        .groupby(q["symbol"].to_numpy(), sort=False)
        .shift(window)
    )
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.where(base.to_numpy() > 0, close / base.to_numpy() - 1.0, np.nan)
    return pd.Series(out, index=q.index, dtype="float64")


def max_daily_ret(q: pd.DataFrame, window: int = 20) -> pd.Series:
    """近 `window` 个交易日**单日涨幅的最大值**（小数，可为负）。"""
    return _rolling(q, _daily_return(q), window, "max")


def today_pct(q: pd.DataFrame) -> pd.Series:
    """当日涨跌幅（小数）。"""
    return pd.Series(_daily_return(q), index=q.index, dtype="float64")


def ma_bias(q: pd.DataFrame, window: int = 20) -> pd.Series:
    """乖离率：`close / MA(window) - 1`。

    均线用**算术平均**（不是指数加权）。窗口内有效观测不足时返回 NaN
    —— 与 `_rolling` 的 `min_periods=window` 一致。
    """
    close = pd.to_numeric(q["close"], errors="coerce").to_numpy(dtype="float64")
    ma = _rolling(q, close, window, "mean")
    return (pd.Series(close, index=q.index, dtype="float64") / ma - 1.0).astype("float64")


# ======================================================================
# 波动 / 振幅族
# ======================================================================
def volatility(q: pd.DataFrame, window: int = 20) -> pd.Series:
    """波动率：日收益率的样本标准差（ddof=1），单位小数。"""
    return _rolling(q, _daily_return(q), window, "std")


def amplitude_mean(q: pd.DataFrame, window: int = 5) -> pd.Series:
    """平均振幅：`(high - low) / preclose` 的窗口均值，小数。

    ⚠️ 分母用 **preclose**（前收）而不是 close —— 与交易所的振幅定义一致，
    也与策略规格说明书里"近 5 日平均振幅"的口径一致。用 close 会得到
    另一个数（差距不大但**不是同一个因子**），而两者都能"跑出结果"，
    属于典型的口径漂移隐患。
    """
    high = pd.to_numeric(q["high"], errors="coerce").to_numpy(dtype="float64")
    low = pd.to_numeric(q["low"], errors="coerce").to_numpy(dtype="float64")
    preclose = pd.to_numeric(q["preclose"], errors="coerce").to_numpy(dtype="float64")
    with np.errstate(divide="ignore", invalid="ignore"):
        amp = np.where(preclose > 0, (high - low) / preclose, np.nan)
    return _rolling(q, amp, window, "mean")


# ======================================================================
# 流动性 / 超买超卖
# ======================================================================
def turnover_mean(q: pd.DataFrame, window: int = 20) -> pd.Series:
    """平均换手率（**单位 %**，沿用 baostock `turn` 字段的原始单位）。

    ⚠️ 单位是百分数不是小数：全市场均值约 1~3（即 1%~3%）。
    前端展示时不要再乘 100。
    """
    turn = pd.to_numeric(q["turn"], errors="coerce").to_numpy(dtype="float64")
    return _rolling(q, turn, window, "mean")


def amount_mean(q: pd.DataFrame, window: int = 20) -> pd.Series:
    """近 `window` 个交易日的平均成交额（**amount 原始单位，通常为元**）。

    用途：中频策略的「流动性下限」过滤（成交额过小的标的进出不便）。
    ⚠️ 与 `turnover_mean` 是**两个不同口径**，别混用：
      · `turnover_mean` 取 `turn` 字段，是**换手率（%）**，刻画"换手活跃度"，与股本归一；
      · `amount_mean` 取 `amount` 字段，是**成交额（元）**，刻画"能承接多少钱"，
        与股本**无关** —— 小盘股换手率可以很高但成交额很小。
    窗口不足 → NaN（`_rolling` 的 `min_periods=window`）。
    """
    amount = pd.to_numeric(q["amount"], errors="coerce").to_numpy(dtype="float64")
    return _rolling(q, amount, window, "mean")


def volume_ratio_n(q: pd.DataFrame, window: int = 5) -> pd.Series:
    """量比：`volume[t] / mean(volume[t-window .. t-1])` —— 分母**不含今日**。

    口径（对外说明以 `registry.yaml` 的 `desc` 为准）：
      · 分子 = 今日成交量；
      · 分母 = **前 window 个交易日**的平均成交量。

    ⚠️ 分母不含今日是刻意的。写 `_rolling(q, volume, window, "mean")`（含今日）
    只差一个 shift，但数值会**系统性偏小**（今日自己的量被平均进分母），
    属于典型的口径漂移 —— 两种写法都能"跑出结果"，必须写死一种并声明。

    `window=1` 时退化为"今日量 ÷ 昨日量"（最朴素的单日量比）。

    边界：窗口不足 → NaN（`_rolling` 的 `min_periods=window`）；
          分母 ≤ 0（前一日零成交）→ NaN，与 `ret_n` 的 `base > 0` 保护同一惯例。
    """
    if window <= 0:
        raise ValueError(f"window 必须为正整数，收到 {window}")
    volume = pd.to_numeric(q["volume"], errors="coerce").to_numpy(dtype="float64")
    # 先把量整体后移一日，再求前 window 日均值 —— 即"前 window 个交易日"的均量
    base = _rolling(q, _group_shift(q, volume, 1), window, "mean")
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.where(base.to_numpy() > 0, volume / base.to_numpy(), np.nan)
    return pd.Series(out, index=q.index, dtype="float64")


def rsi(q: pd.DataFrame, window: int = 14) -> pd.Series:
    """RSI(`window`)，**SMA 口径**（不是 Wilder 平滑），取值 0~100。

    公式：
        delta    = close.diff()
        avg_gain = MA(max(delta, 0), window)
        avg_loss = MA(max(-delta, 0), window)
        RSI      = 100 × avg_gain / (avg_gain + avg_loss)

    边界：`avg_loss == 0`（窗口内只涨不跌）时 RSI = 100；
          `avg_gain == avg_loss == 0`（窗口内完全不动）时 RSI = 50（中性），
          不是 100 —— 否则一只连续横盘的票会被读成"极度超买"。

    ⚠️ 选 SMA 口径的理由：与通达信/同花顺默认显示一致，肉眼可核对。
    Wilder 平滑（`ewm(alpha=1/window)`）会得到略不同的数，属于另一个因子。
    """
    close = pd.to_numeric(q["close"], errors="coerce").to_numpy(dtype="float64")
    s = pd.Series(close, index=q.index, dtype="float64")
    delta = s.groupby(q["symbol"].to_numpy(), sort=False).diff()

    gain = _rolling(q, delta.clip(lower=0).to_numpy(dtype="float64"), window, "mean")
    loss = _rolling(q, (-delta).clip(lower=0).to_numpy(dtype="float64"), window, "mean")

    total = gain + loss
    out = (100.0 * gain / total).astype("float64")
    # 窗口内完全不动（gain=loss=0 → 0/0=NaN）→ 中性 50
    out = out.mask((total.fillna(0) == 0) & gain.notna(), 50.0)
    return out
