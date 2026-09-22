"""基准计算 —— **口径与回测内股票池严格对齐**。

为什么需要这个模块（研究文档 §1「产物缺口」第 2 条）
----------------------------------------------------
本项目头号分析纪律是「**没有基准不动结论**」。但此前基准只能用
`data/raw/quote_daily` 全量凑，而回测内的股票池是**剔除了** ST / 停牌 /
新股(<90 交易日) / 一字涨跌停 / 非目标板块的 —— 两者口径不同，
于是"策略跑输基准 X 个百分点"这句话的**分母是错的**。

`tools/backfill_universe.py` 落盘逐日快照之后，基准可以直接读快照，
两边口径就统一了。本模块就是那个读取端。

三个口径要说清楚（**别混用**）
-----------------------------
1. **等权日再平衡**（`universe_equal_weight_daily`）：每天在池内等权持有，
   收益率 = 池内当日收益的**算术平均**，逐日复利。这是与"日频策略"对比的
   标准口径，也是本模块的主口径。
2. **等权买入持有**（`equal_weight_buy_hold`）：区间内每只股票 c1/c0 的等权平均。
   与项目早期研究数字（+3.52% / +18.26%）同口径，用于**对照**历史结论，
   不要拿它跟日频策略的年化直接比。
3. 指数的 `benchmark_nav` 是另一回事（那是上证指数，不是全市场等权），
   由 `strategies/runner.py` 负责，本模块不碰。

⚠️ 为什么不把等权基准塞进回测产物（`equity.parquet`）：那要改
`strategies/base.py::EQUITY_COLUMNS`，属**引擎输出契约变更**（引擎、指标、mock 生成、
前端类型、测试全要跟着动）。本模块用**完全相同的口径**在引擎之外重算，
既不动契约、又能随时复核；将来若确实需要随回测落盘，再按契约变更流程走。
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from app.core.errors import AppError
from app.core.logging import logger
from app.services import data_store

#: 一个自然日的纳秒数（用于把日期压成"天序号"做复合键）
_NS_PER_DAY = 86_400_000_000_000

#: 复合键里给 symbol 预留的位宽（标的数远小于这个上界）
_SYM_STRIDE = 10_000_000


def _sym_codes(*symbol_columns: pd.Series) -> pd.Series:
    """给一组标的分配整数编码（复合键用）。

    ★ 必须传**所有**要参与编码的标的列（行情 ∪ 快照）。
      只按快照建编码的话，行情里"不在快照内"的标的 reindex 后会拿到 NaN，
      转 int64 变成垃圾值 —— 那可能**恰好撞上**另一个合法的复合键，
      于是本该被剔除的行被当成池内标的留下。这类错不会报错，只会让基准偏高。
    """
    values = np.concatenate([pd.Series(c).astype(str).to_numpy() for c in symbol_columns])
    uniq = pd.Index(pd.unique(values))
    return pd.Series(np.arange(len(uniq)), index=uniq)


def _pair_keys(dates: pd.Series, symbols: pd.Series, code: pd.Series) -> np.ndarray:
    """把 (日期, 标的) 压成一个 int64 复合键 —— 比 MultiIndex 轻一两个数量级。

    用 `astype("int64")` 而不是 `view`：后者在 pandas 2.x 已弃用，
    且遇到 NaT 会给出 `invalid value encountered in cast` 警告。
    `astype("int64")` 把 NaT 稳定映射成 int64 最小值 —— 那些行自然匹配不上任何快照。
    """
    day = pd.to_datetime(dates).astype("datetime64[ns]").astype("int64") // _NS_PER_DAY
    sym = code.reindex(pd.Series(symbols).astype(str).to_numpy()).to_numpy(dtype="float64")
    # 编码缺失（理论上不该发生，见 _sym_codes）→ 置成一个不可能撞上的值
    sym = np.where(np.isnan(sym), -1.0, sym)
    return day.to_numpy(dtype="int64") * _SYM_STRIDE + sym.astype("int64")


def _daily_returns(start: str, end: str) -> pd.DataFrame:
    """区间内全市场逐日收益（后复权口径：close/preclose − 1）。"""
    q = data_store.read_quotes(
        symbols=None,
        start_date=start,
        end_date=end,
        columns=["date", "symbol", "close", "preclose"],
    )
    if q.empty:
        raise AppError(f"行情为空：{start} ~ {end}。请先执行 python -m tools.backfill_quotes")
    q = q.copy()
    q["date"] = pd.to_datetime(q["date"])
    q["symbol"] = q["symbol"].astype(str)
    close = pd.to_numeric(q["close"], errors="coerce")
    preclose = pd.to_numeric(q["preclose"], errors="coerce")
    # 停牌/无效价行由 read_quotes 保留原始值，这里按"有前收且为正"过滤
    q["ret"] = np.where(preclose > 0, close / preclose - 1.0, np.nan)
    return q[["date", "symbol", "ret"]]


def universe_equal_weight_daily(
    start: str,
    end: str,
    *,
    use_universe: bool = True,
) -> pd.DataFrame:
    """逐日等权组合收益与净值（**主口径：等权日再平衡**）。

    Args:
        start / end: 闭区间，YYYY-MM-DD
        use_universe: True → 只保留当日股票池快照里的标的（与回测内口径一致）；
                      False → 全市场不过滤（**仅用于口径对照**，是此前的老做法）

    Returns:
        列 `[date, n_symbols, ew_return, ew_nav]`；`ew_nav` 以区间首日为 1.0 起算。
        `n_symbols` 是当日参与计算的只数 —— 它同时是"快照覆盖度"的自检指标。

    Raises:
        AppError: 快照缺失（并给出回补命令）
    """
    rets = _daily_returns(start, end)

    if use_universe:
        snap = data_store.read_universe_range(start, end)
        if snap.empty:
            raise AppError(
                f"股票池快照为空（{start} ~ {end}）。基准要与回测内口径对齐就必须有快照，"
                f"请先执行：python -m tools.backfill_universe"
            )
        code = _sym_codes(rets["symbol"], snap["symbol"])
        key_q = _pair_keys(rets["date"], rets["symbol"], code)
        key_s = np.unique(_pair_keys(snap["date"], snap["symbol"], code))
        keep = np.isin(key_q, key_s)
        dropped = int((~keep).sum())
        covered = rets.loc[keep, ["date", "symbol", "ret"]]
        logger.info(
            f"基准口径：股票池快照（区间 {snap['date'].min().date()} ~ {snap['date'].max().date()}，"
            f"{len(snap):,} 条）｜按快照剔除 {dropped:,} 行行情"
        )
    else:
        covered = rets
        logger.warning("基准口径：**未过滤全市场**（仅供口径对照，不是与回测对齐的口径）")

    g = covered.dropna(subset=["ret"]).groupby("date", sort=True)["ret"]
    daily = g.mean().rename("ew_return").to_frame()
    daily["n_symbols"] = g.size()
    daily = daily.reset_index()
    daily["ew_nav"] = (1.0 + daily["ew_return"]).cumprod()
    return daily[["date", "n_symbols", "ew_return", "ew_nav"]]


def equal_weight_buy_hold(
    start: str,
    end: str,
    *,
    use_universe: bool = True,
) -> dict[str, Any]:
    """等权买入持有（**对照口径**，与项目早期研究数字同源）。

    每只股票取区间内首末可用收盘价的收益，再等权平均。
    ⚠️ 不要拿它跟日频策略的年化直接比 —— 口径不同（一个是持有、一个是逐日再平衡）。
    """
    q = data_store.read_quotes(
        symbols=None, start_date=start, end_date=end, columns=["date", "symbol", "close"]
    )
    if q.empty:
        raise AppError(f"行情为空：{start} ~ {end}")
    q = q.copy()
    q["date"] = pd.to_datetime(q["date"])
    q["symbol"] = q["symbol"].astype(str)
    q["close"] = pd.to_numeric(q["close"], errors="coerce")
    q = q.dropna(subset=["close"])

    if use_universe:
        snap = data_store.read_universe_range(start, end)
        if snap.empty:
            raise AppError(f"股票池快照为空（{start} ~ {end}），请先 python -m tools.backfill_universe")
        code = _sym_codes(q["symbol"], snap["symbol"])
        keep = np.isin(
            _pair_keys(q["date"], q["symbol"], code),
            np.unique(_pair_keys(snap["date"], snap["symbol"], code)),
        )
        q = q[keep]

    first = q.sort_values("date").groupby("symbol")["close"].first()
    last = q.sort_values("date").groupby("symbol")["close"].last()
    r = (last / first - 1.0).replace([np.inf, -np.inf], np.nan).dropna()
    return {
        "n": int(r.size),
        "mean": float(r.mean()) if r.size else float("nan"),
        "median": float(r.median()) if r.size else float("nan"),
        "win_rate": float((r > 0).mean()) if r.size else float("nan"),
    }


def summarize(daily: pd.DataFrame) -> dict[str, Any]:
    """把逐日序列压成"结论表"要用的几个数。"""
    if daily.empty:
        return {"days": 0, "cum_return": float("nan"), "annual_return": float("nan")}
    n = len(daily)
    nav = float(daily["ew_nav"].iloc[-1])
    # 年化用 242 交易日/年（与技术架构 §5.0 的口径一致）
    years = n / 242.0
    annual = nav ** (1.0 / years) - 1.0 if years > 0 and nav > 0 else float("nan")
    return {
        "days": n,
        "start": str(daily["date"].iloc[0].date()),
        "end": str(daily["date"].iloc[-1].date()),
        "n_symbols_avg": float(daily["n_symbols"].mean()),
        "cum_return": nav - 1.0,
        "annual_return": annual,
        "daily_vol": float(daily["ew_return"].std(ddof=1)),
        "max_drawdown": float(
            (daily["ew_nav"] / daily["ew_nav"].cummax() - 1.0).min()
        ),
    }


__all__ = [
    "equal_weight_buy_hold",
    "summarize",
    "universe_equal_weight_daily",
]
