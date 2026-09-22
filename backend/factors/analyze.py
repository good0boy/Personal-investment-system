"""因子有效性分析 —— 分层回测 + RankIC（**离线算，落盘，前端只读**）。

为什么必须离线算
--------------
分层回测要对每个交易日做一次"全市场截面分组 + 构建可交易股票池"，
1100+ 个交易日 × 4800 只，跑一个因子约 10~20s。放进 HTTP 请求链路会让服务卡死。
与模块 3 同一条纪律：**计算在 CLI，Web 只读结果**。

产物：`data/factor/analysis/{name}.json`（含分组净值、IC 序列、结论与口径说明）

================================ 口径（必须写清，否则数字没法解读）================================
1. **成交时点**：T 日收盘的因子值 → **T+1 开盘买入** → 持有 `horizon` 个交易日后
   在 T+1+horizon 开盘卖出。与回测引擎 `fill_rule=next_open` 完全一致。
   （不用 T 日收盘价成交 —— 那是未来函数，也是本策略回测里 alpha 漏光的地方。）
2. **股票池**：逐日走 `tools/universe.build_universe`（**与回测引擎同一实现**）——
   沪深主板+创业板，剔除 ST / 停牌 / 上市<90 交易日 / 一字板 / 科创板 / 北交所 / B股。
   ⚠️ 不自己写一套过滤 —— 同一口径两处定义必然漂移。
3. **入场可行性**：要求标的在 **T 日与 T+1 日都在股票池内**
   （T+1 不在池子里 = 开盘涨停/停牌买不到）。这一条是模块 3 的血泪：
   旧引擎因为涨跌停漏判，让"买不到的却买入了"，虚增了 53 个百分点。
4. **出场可行性：刻意未强制**。若出场日跌停/停牌，真实交易里要顺延到下一日，
   本分析按理论价结算 → 存在**轻微乐观偏差**。原因是不做顺延会让"分组净值"
   这件事变得不可比（每只标的持仓期不同）。结论里已如实标注。
5. **毛收益 vs 净收益**：分组的累计收益是**毛的**（不含交易成本），
   同时给出日均换手率与**年化成本估算**（走 `services/cost_model.py`，印花税按日期分段）。
   这不是可选项 —— 本项目的核心教训之一就是"高换手策略的毛收益够不够覆盖成本"。
6. **IC**：RankIC（Spearman 秩相关），逐日截面计算，与前瞻收益相关。
   ICIR = IC 均值 / IC 标准差。⚠️ 报告的是**单期** ICIR，不是年化的。
==================================================================================================
"""
from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from app.core.config import settings
from app.core.errors import AppError
from app.core.logging import logger, setup_logging
from app.core.paths import FACTOR_ANALYSIS_DIR, relative_to_project
from app.services import cost_model, data_store
from factors import loader, manifest

#: 每期至少要有的标的数（低于 `groups × MIN_SYMBOLS_PER_GROUP` 的交易日跳过）
MIN_SYMBOLS_PER_GROUP = 5

#: 分析口径说明（写进产物，前端直接展示 —— 数字脱离口径就是垃圾）
METHOD_NOTES: tuple[str, ...] = (
    "成交时点：T 日因子 → T+1 开盘买入 → 持有 horizon 个交易日 → T+1+horizon 开盘卖出"
    "（与回测引擎 fill_rule=next_open 一致）",
    "股票池：逐日走 tools/universe.build_universe（与回测引擎同一实现），"
    "剔除 ST/停牌/上市<90交易日/一字板/科创/北交/B股",
    "入场可行性：要求 T 日与 T+1 日都在股票池内 —— "
    "T+1 不在池子里说明开盘涨停或停牌，根本买不到",
    "出场可行性：刻意未强制。出场日跌停/停牌时真实交易需顺延，"
    "本分析按理论价结算 → 存在轻微乐观偏差",
    "分组收益为毛收益（不含成本）；另给日均换手率与年化成本估算"
    "（印花税按 2023-08-28 分段）",
    "IC 为逐日截面 RankIC（Spearman）；ICIR = IC 均值 / IC 标准差，是单期口径不是年化口径",
    "换手率口径：某组「今日成员 △ 昨日成员」÷ 两边成员数之和"
    "（等规模时即被替换掉的比例，单边口径）；多空组合的换手取首尾两组换手的平均",
    "annual_return 是**算术年化**（日均收益 × 252），cum_return 是**累计复利**。"
    "波动大时两者差异显著 —— 判断排序质量看 cum_return 与单调性，估算年化水平才看 annual_return",
)


# ======================================================================
# 数据准备
# ======================================================================
def load_market(start: str, end: str, symbols: list[str] | None = None) -> Any:
    """加载行情矩阵。

    ★ 复用 `strategies.engine.MarketData` 而不是另写一个加载器：
      它是"日期 × 标的"矩阵 + `day_frame()`（正是 `build_universe` 需要的输入）
      的**唯一实现**，重复实现必然在停牌/缺失值处理上分叉。

    ⚠️ 延迟导入：`strategies` 是 CLI 侧包，避免服务层导入时连带拉起它。
    """
    from strategies.engine import MarketData  # noqa: PLC0415

    return MarketData.load(start=start, end=end, symbols=symbols)


def first_trade_dates() -> dict[str, str]:
    """每只标的的**真实**首个交易日。

    与 `BacktestEngine._derive_first_trade_dates` 的差别：引擎是从矩阵推断
    （矩阵起点之前的标的会被记成矩阵首日，是个"保守下界"，靠预热窗口 ≥ 90 交易日兜住）；
    这里直接对**全量行情**做一次聚合，拿到真值 —— 因为分析窗口不像回测那样
    有预热窗口，用推断值会**误把 2020 年 1~5 月的老股票当新股剔除**。

    成本：一次全量聚合 SQL（约 1~2s），换掉一整类静默错误。
    """
    df = data_store.query_parquet(
        data_store.quote_glob(),
        "SELECT symbol, min(date) AS first_date FROM t GROUP BY symbol",
    )
    if df.empty:
        return {}
    return {
        str(r.symbol): pd.Timestamp(r.first_date).strftime("%Y-%m-%d")
        for r in df.itertuples()
    }


def _calendar_fn_from_store() -> Any:
    """用**完整交易日历**做 `calendar_days_fn`（而不是"矩阵内日期"）。

    ★ 为什么必须用完整日历（这是一个会静默算错的地方）：
      `_mask_listed_days` 内部会调 `calendar_days_fn(最早上市日, 当日)`，
      再数 `[最早上市日, 当日]` 区间里有多少个交易日来比 90 日门槛。
      回测引擎能安全地用"矩阵内日期"当日历，是因为它的矩阵从 `PREHEAT_START`
      起、且 `first_trade_date` 也是从矩阵推断的 —— 两者的起点**天然对齐**。
      本模块用的是**真实上市日**（可能是 1991 年），若日历也只到矩阵起点，
      `counts` 会从 2020 年才开始数 → **所有老股票都会被当成"上市不足 90 日"剔除**，
      表现为"股票池只有几十只"或"分层结果全空"，而且不报错。

    Returns:
        callable(start, end) -> list[pd.Timestamp]；日历不可用时返回 None
        （此时 `build_universe` 退化为自然日近似，会记录 warning）。
    """
    try:
        cal = data_store.read_trade_calendar()
    except AppError as exc:
        logger.warning(f"交易日历不可用，股票池的上市天数门槛将退化为自然日近似：{exc.message}")
        return None
    if cal is None or cal.empty:
        return None
    if "is_trading_day" in cal.columns:
        cal = cal[pd.to_numeric(cal["is_trading_day"], errors="coerce") == 1]
    days = pd.DatetimeIndex(pd.to_datetime(cal["date"])).sort_values()

    def fn(a: str, b: str) -> list[Any]:
        i = int(days.searchsorted(pd.Timestamp(a), side="left"))
        j = int(days.searchsorted(pd.Timestamp(b), side="right"))
        return days[i:j].tolist()

    return fn


def build_investable_masks(market: Any, start: str, end: str) -> tuple[np.ndarray, dict[str, Any]]:
    """逐日构建"可交易"掩码，形状 (n_dates, n_symbols)。

    ★ 必须逐日算，不能一次性向量化：ST 与停牌是**逐日**字段，
      上市天数与一字板也依赖当日状态（见 技术架构 §6.2 红线）。

    ⚠️ 不能用自然日近似交易日区间 —— 见开发进度问题集第 19 条。

    Returns:
        (掩码矩阵, 口径信息) —— 口径信息会原样写进产物，前端展示。
    """
    first_dates = first_trade_dates()
    calendar_fn = _calendar_fn_from_store()
    info: dict[str, Any] = {
        "first_trade_date_source": "全量行情 min(date) 聚合（真实上市日，非从矩阵推断）",
        "calendar": "trade_calendar（完整交易日历）" if calendar_fn else "自然日近似（日历不可用）",
    }
    if calendar_fn is None:
        info["warning"] = "交易日历不可用 → 上市天数门槛用自然日近似，新股判定可能不准"

    from tools.universe import build_universe  # noqa: PLC0415

    n_d, n_s = len(market.dates), len(market.symbols)
    masks = np.zeros((n_d, n_s), dtype=bool)
    for di in range(n_d):
        res = build_universe(
            market.dates[di],
            market.day_frame(di),
            first_trade_date=first_dates,
            last_sell_date=None,
            calendar_days_fn=calendar_fn,
        )
        if not res.symbols:
            continue
        idx = [market.symbol_index[s] for s in res.symbols if s in market.symbol_index]
        if idx:
            masks[di, np.asarray(idx, dtype=np.int64)] = True
    return masks, info


def load_factor_matrix(name: str, category: str, market: Any) -> np.ndarray:
    """把因子长表摆成与行情矩阵同形的 (n_dates, n_symbols) 数组。

    用数组散射而不是 `pivot` —— pivot 会先把 470 万行重排成宽表（几十 MB 的临时对象），
    而这套"索引映射 + 散射"就是 `MarketData.load` 里用的同一套做法。
    """
    df = data_store.read_factor(name, None, None, category=category)
    mat = np.full((len(market.dates), len(market.symbols)), np.nan, dtype="float64")
    if df.empty:
        return mat

    d = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    di = d.map(market.date_index)
    si = df["symbol"].astype(str).map(market.symbol_index)
    ok = di.notna() & si.notna()
    if not ok.any():
        logger.warning(f"因子 {name} 的日期/标的与行情矩阵完全不重叠，请检查区间")
        return mat
    mat[di[ok].astype(int).to_numpy(), si[ok].astype(int).to_numpy()] = (
        pd.to_numeric(df.loc[ok, "value"], errors="coerce").to_numpy(dtype="float64")
    )
    return mat


def forward_returns(market: Any, horizon: int) -> np.ndarray:
    """前瞻收益矩阵：T 日 → (T+1 开盘买入) → (T+1+horizon 开盘卖出)。

    行 i 的收益 = open[i+1+horizon] / open[i+1] − 1；末 horizon+1 行为 NaN。
    """
    if horizon < 1:
        raise AppError(f"horizon 必须 ≥ 1（T+1 开盘成交要求至少跨 1 个交易日），收到 {horizon}")
    n_d = len(market.dates)
    fwd = np.full_like(market.open, np.nan, dtype="float64")
    span = n_d - horizon - 1
    if span <= 0:
        return fwd
    entry = market.open[1 : 1 + span]
    exit_ = market.open[1 + horizon : 1 + horizon + span]
    with np.errstate(divide="ignore", invalid="ignore"):
        fwd[:span] = np.where(entry > 0, exit_ / entry - 1.0, np.nan)
    return fwd


# ======================================================================
# 核心指标
# ======================================================================
def _group_index(values: np.ndarray, groups: int) -> np.ndarray:
    """按**秩**分组（等价于等频分箱），返回 0..groups-1。

    用秩而不是 `pd.qcut` 的原因：qcut 在因子值有大量重复时（如 today_pct 的 0）
    会因 `duplicates` 抛错或静默减组；秩分组在任何取值分布下都稳定。
    组号越大 = 因子值越大（direction=1 时越大越好）。
    """
    n = values.size
    order = np.argsort(values, kind="stable")
    ranks = np.empty(n, dtype=np.int64)
    ranks[order] = np.arange(n)
    return np.minimum((ranks * groups) // n, groups - 1)


def analyze_one(
    defn: loader.FactorDef,
    market: Any,
    fmat: np.ndarray,
    fwd: np.ndarray,
    masks: np.ndarray,
    *,
    groups: int,
    horizon: int,
    universe_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """一个因子的分层回测 + IC 序列。"""
    n_d = len(market.dates)
    n_s = len(market.symbols)

    # 入场可行性：T 日与 T+1 日都在股票池内。
    # 为什么把 T+1 也纳入判据 —— T+1 不在池子里 = 当日开盘涨停/停牌，
    # 物理上买不到。模块 3 曾因漏判这一点虚增了 53 个百分点。
    entry_ok = masks.copy()
    if n_d > 1:
        entry_ok[:-1] &= masks[1:]
        entry_ok[-1] = False  # 最后一行没有 T+1，必然不可入场

    dates_used: list[str] = []
    ic_rows: list[dict[str, Any]] = []
    nav = np.ones(groups, dtype="float64")
    nav_ls = np.ones(1, dtype="float64")
    nav_hist: list[list[float]] = []
    dates_hist: list[str] = []
    group_daily: list[list[float]] = [[] for _ in range(groups)]
    ls_daily: list[float] = []
    ls_turnovers: list[float] = []
    group_turnovers: list[list[float]] = [[] for _ in range(groups)]
    sizes: list[list[int]] = [[] for _ in range(groups)]
    skipped_short = 0
    universe_sizes: list[int] = []

    prev_group = np.full(n_s, -1, dtype=np.int64)

    for di in range(n_d):
        if not np.isfinite(fwd[di]).any():
            continue
        valid = entry_ok[di] & np.isfinite(fmat[di]) & np.isfinite(fwd[di])
        idx = np.nonzero(valid)[0]
        if idx.size < groups * MIN_SYMBOLS_PER_GROUP:
            skipped_short += 1
            continue
        universe_sizes.append(int(entry_ok[di].sum()))

        vals = fmat[di][idx]
        rets = fwd[di][idx]
        gidx = _group_index(vals, groups)

        # ---- IC（Spearman = 秩的 Pearson）----
        ic = _spearman(vals, rets)
        dates_used.append(market.dates[di])
        ic_rows.append({"date": market.dates[di], "ic": None if ic is None else round(float(ic), 6)})

        # ---- 分组收益 ----
        cur = np.full(n_s, -1, dtype=np.int64)
        cur[idx] = gidx
        day_ret: list[float] = []
        for g in range(groups):
            sel = rets[gidx == g]
            r = float(sel.mean()) if sel.size else 0.0
            day_ret.append(r)
            nav[g] *= 1.0 + r
            group_daily[g].append(r)
            sizes[g].append(int(sel.size))
            # 换手：昨天不在这组、今天在（或反之）—— 按平均持仓数归一（双边/2）
            cur_in = cur == g
            prev_in = prev_group == g
            changed = int(np.count_nonzero(cur_in ^ prev_in))
            base = max(int(cur_in.sum()) + int(prev_in.sum()), 1)
            group_turnovers[g].append(changed / base)

        ls = day_ret[groups - 1] - day_ret[0]
        ls_daily.append(ls)
        nav_ls[0] *= 1.0 + ls

        # ★ 多空组合的换手 = 两条腿换手的平均。
        #   ⚠️ 这里**不能**用"可交易池（eligible）的换手"代替：
        #      池子只随 ST/停牌/新股小幅变动（实测 0.18%/日），而多空组合
        #      每天要按因子值重排（实测 17%~19%/日）—— 差了 100 倍。
        #      用错口径会让"年化成本"从 10% 掉到 0.1%，
        #      结论从"成本吃掉三分之一的多空收益"变成"成本可以忽略"，
        #      方向完全反了。这是本模块最危险的一处口径错误。
        ls_turnovers.append(
            (group_turnovers[groups - 1][-1] + group_turnovers[0][-1]) / 2.0
        )
        prev_group = cur

        dates_hist.append(market.dates[di])
        nav_hist.append(
            [round(float(x), 6) for x in nav] + [round(float(nav_ls[0]), 6)]
        )

    if not dates_used:
        return {
            "name": defn.name,
            "warnings": ["没有任何交易日满足「截面标的数 ≥ groups × 5 且因子值/前瞻收益均有效」，无法分析"],
            "dates_used": 0,
        }

    # ---------------- IC 汇总 ----------------
    ic_vals = np.array([r["ic"] for r in ic_rows if r["ic"] is not None], dtype="float64")
    ic_summary: dict[str, Any] = {"n_dates": int(ic_vals.size)}
    if ic_vals.size:
        mean = float(ic_vals.mean())
        std = float(ic_vals.std(ddof=1)) if ic_vals.size > 1 else 0.0
        ir = mean / std if std > 0 else None
        ic_summary.update(
            {
                "mean": round(mean, 6),
                "std": round(std, 6),
                "ir": None if ir is None else round(ir, 4),
                "t_stat": None if ir is None else round(ir * float(np.sqrt(ic_vals.size)), 3),
                "win_rate": round(float((ic_vals > 0).mean()), 4),
                "abs_mean": round(float(np.abs(ic_vals).mean()), 6),
            }
        )

    # ---------------- 分层汇总 ----------------
    cost_rates = [cost_model.round_trip_rate(d) for d in dates_used]
    mean_cost_rate = float(np.mean(cost_rates)) if cost_rates else 0.0

    summary: list[dict[str, Any]] = []
    for g in range(groups):
        avg_daily = float(np.mean(group_daily[g])) if group_daily[g] else 0.0
        turn = float(np.mean(group_turnovers[g])) if group_turnovers[g] else 0.0
        summary.append(
            {
                "group": g + 1,
                "avg_daily": round(avg_daily, 6),
                "annual_return": round(avg_daily * 252, 4),
                "cum_return": round(float(nav[g] - 1.0), 6),
                "avg_size": round(float(np.mean(sizes[g])), 1) if sizes[g] else 0.0,
                "turnover_daily": round(turn, 4),
                "cost_annual": round(turn * mean_cost_rate * 252, 4),
                "annual_return_net": round((avg_daily - turn * mean_cost_rate) * 252, 4),
            }
        )

    cum = np.array([s["cum_return"] for s in summary], dtype="float64")
    mono = _spearman(np.arange(groups, dtype="float64"), cum)

    ls_avg = float(np.mean(ls_daily)) if ls_daily else 0.0
    ls_turn = float(np.mean(ls_turnovers)) if ls_turnovers else 0.0
    long_short = {
        "avg_daily": round(ls_avg, 6),
        "annual_gross": round(ls_avg * 252, 4),
        "turnover_daily": round(ls_turn, 4),
        "cost_annual": round(ls_turn * mean_cost_rate * 252, 4),
        "annual_net": round((ls_avg - ls_turn * mean_cost_rate) * 252, 4),
        "cum_return": round(float(nav_ls[0] - 1.0), 6),
    }

    warnings: list[str] = []
    if ic_summary.get("mean") is not None and abs(ic_summary["mean"]) < 0.01:
        warnings.append(
            f"IC 均值 {ic_summary['mean']:.4f} 接近 0 —— 该因子在此区间内几乎没有选股能力"
        )
    if mono is not None and mono < 0.5:
        warnings.append(
            f"分组收益单调性差（秩相关 {mono:.2f}）—— 按因子值排序并不能带来收益排序，"
            f"分层曲线的形状比极值更重要"
        )
    if long_short["annual_gross"] > 0 and long_short["annual_net"] < long_short["annual_gross"] * 0.5:
        warnings.append(
            f"换手成本吃掉了一半以上的多空毛收益"
            f"（毛 {long_short['annual_gross']:.1%} → 净 {long_short['annual_net']:.1%}）"
        )
    elif long_short["cost_annual"] > abs(long_short["annual_gross"]):
        # ⚠️ 这里**不能**用 `净 < 毛 × 0.5` 判断：毛收益为负时 毛×0.5 仍是负数，
        #    条件恒真，会对每一个亏损因子都喊"成本吃掉一半"—— 噪声警告比没有警告更糟
        #    （它教人忽略警告）。改为看**量级**：成本是否已经和毛收益同量级。
        warnings.append(
            f"年化换手成本 {long_short['cost_annual']:.1%} 已达多空毛收益"
            f"（{long_short['annual_gross']:.1%}）的量级 —— 该持有期下换手过高"
        )

    return {
        "name": defn.name,
        "cname": defn.cname,
        "category": defn.category,
        "direction": defn.direction,
        "desc": defn.desc,
        "tags": list(defn.tags),
        "params": defn.params,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "interval": {
            "start": dates_used[0],
            "end": dates_used[-1],
            "dates_used": len(dates_used),
            "dates_skipped_too_few": skipped_short,
        },
        "settings": {
            "groups": groups,
            "horizon": horizon,
            "min_symbols_per_group": MIN_SYMBOLS_PER_GROUP,
            "mean_round_trip_cost": round(mean_cost_rate, 6),
            "avg_universe_size": round(float(np.mean(universe_sizes)), 1) if universe_sizes else 0.0,
            "universe": universe_info or {},
        },
        "ic": {**ic_summary, "series": ic_rows},
        "layered": {
            "group_summary": summary,
            "monotonicity": None if mono is None else round(float(mono), 4),
            "long_short": long_short,
            "dates": dates_hist,
            "nav": nav_hist,
            "nav_columns": [f"G{i + 1}" for i in range(groups)] + ["LS"],
        },
        "warnings": warnings,
        "notes": list(METHOD_NOTES),
    }


def _spearman(a: np.ndarray, b: np.ndarray) -> float | None:
    """秩相关系数（无 scipy 依赖的纯 numpy 实现，避免为一个函数引一个包）。

    并列值用**平均秩**（与 scipy.stats.spearmanr 的默认口径一致）。
    """
    n = a.size
    if n < 3:
        return None
    ra = _average_rank(a)
    rb = _average_rank(b)
    ra = ra - ra.mean()
    rb = rb - rb.mean()
    denom = float(np.sqrt((ra * ra).sum() * (rb * rb).sum()))
    if denom <= 0:
        return None
    return float((ra * rb).sum() / denom)


def _average_rank(x: np.ndarray) -> np.ndarray:
    """平均秩：并列的取值分享同一平均名次。"""
    order = np.argsort(x, kind="stable")
    ranks = np.empty(x.size, dtype="float64")
    ranks[order] = np.arange(1, x.size + 1, dtype="float64")
    # 对并列值取平均名次
    xs = x[order]
    i = 0
    while i < xs.size:
        j = i + 1
        while j < xs.size and xs[j] == xs[i]:
            j += 1
        if j - i > 1:
            ranks[order[i:j]] = ranks[order[i:j]].mean()
        i = j
    return ranks


# ======================================================================
# 落盘
# ======================================================================
def analysis_path(name: str, horizon: int = 1) -> Path:
    """产物路径。horizon=1 落 `{name}.json`（历史兼容）；h≠1 落 `{name}_h{n}.json`。"""
    from app.services.data_store import _safe_name  # noqa: PLC0415

    suffix = "" if horizon == 1 else f"_h{int(horizon)}"
    return FACTOR_ANALYSIS_DIR / f"{_safe_name(name)}{suffix}.json"


def run(
    *,
    names: list[str],
    start: str,
    end: str,
    groups: int = 5,
    horizon: int = 1,
    debug_symbols: list[str] | None = None,
) -> list[dict[str, Any]]:
    """对给定因子逐个跑分层 + IC 并落盘。返回产物路径列表。"""
    if groups < 2:
        raise AppError(f"groups 至少为 2，收到 {groups}")
    defns = [loader.get_factor(n) for n in names]

    t0 = time.monotonic()
    market = load_market(start, end, debug_symbols)
    logger.info(
        f"分析用行情矩阵：{len(market.dates)} 交易日 × {len(market.symbols)} 只"
        f"（{market.dates[0]} ~ {market.dates[-1]}）"
    )
    masks, universe_info = build_investable_masks(market, start, end)
    logger.info(
        f"逐日股票池就绪（与回测引擎同一实现）｜平均 "
        f"{masks.sum(axis=1).mean():.0f} 只/日｜日历口径：{universe_info.get('calendar')}"
        f"｜耗时 {time.monotonic() - t0:.1f}s"
    )
    fwd = forward_returns(market, horizon)

    out: list[dict[str, Any]] = []
    for defn in defns:
        ts = time.monotonic()
        fmat = load_factor_matrix(defn.name, defn.category, market)
        res = analyze_one(
            defn,
            market,
            fmat,
            fwd,
            masks,
            groups=groups,
            horizon=horizon,
            universe_info=universe_info,
        )
        res["seconds"] = round(time.monotonic() - ts, 2)
        res["analysis_interval"] = {"start": start, "end": end}
        if debug_symbols:
            res["debug_symbols"] = list(debug_symbols)
            res["warnings"] = [
                *(res.get("warnings") or []),
                "⚠️ 调试运行：股票池被限制在少量标的，截面/IC 结论不可用",
            ]

        path = analysis_path(defn.name, horizon)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)

        ic = res.get("ic") or {}
        ls = (res.get("layered") or {}).get("long_short") or {}
        flag = "⚠️ " if res.get("warnings") else ""
        print(
            f"  ✅ {defn.name:<16s} IC均值 {_fmt(ic.get('mean')):>8s}｜ICIR {_fmt(ic.get('ir'), 3):>7s}"
            f"｜胜率 {_fmt(ic.get('win_rate'), 3):>6s}｜单调性 "
            f"{_fmt((res.get('layered') or {}).get('monotonicity'), 2):>5s}"
            f"｜多空年化(毛/净) {_pct(ls.get('annual_gross'))}/{_pct(ls.get('annual_net'))}"
            f"｜成本 {_pct(ls.get('cost_annual'))}｜{res['seconds']:>5.1f}s {flag}"
        )
        for w in res.get("warnings") or []:
            print(f"      └─ {w}")
        out.append({"name": defn.name, "path": str(path), "ic": ic, "long_short": ls})

    print("-" * 96)
    print(
        f"  分析完成｜{len(out)} 个因子｜总耗时 {time.monotonic() - t0:.1f}s"
        f"｜产物目录 {relative_to_project(FACTOR_ANALYSIS_DIR)}"
    )
    return out


def _fmt(v: Any, nd: int = 4) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):.{nd}f}"
    except (TypeError, ValueError):
        return "—"


def _pct(v: Any) -> str:
    if v is None:
        return "  —   "
    try:
        return f"{float(v):.1%}"
    except (TypeError, ValueError):
        return "  —   "


# ======================================================================
# CLI
# ======================================================================
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m factors.analyze",
        description="模块 2 · 因子有效性分析（分层回测 + RankIC，离线落盘）",
    )
    p.add_argument("--name", action="append", default=None, help="因子名，可重复")
    p.add_argument("--category", default=None, help="按分类分析全部因子")
    p.add_argument("--all", action="store_true", help="分析清单里所有已算过的因子")
    p.add_argument("--start", default=None, help="起始日（默认 config.data.start_date）")
    p.add_argument("--end", default=None, help="结束日（默认今天）")
    p.add_argument("--groups", type=int, default=5, help="分组数（默认 5）")
    p.add_argument("--horizon", type=int, default=1, help="前瞻持有交易日数（默认 1）")
    p.add_argument("--symbols", default=None, help="逗号分隔标的，⚠️ 调试模式")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging()

    names: list[str] = [str(n).strip() for n in (args.name or []) if str(n).strip()]
    if not names and args.category:
        names = [f.name for f in loader.list_factors(args.category)]
    if not names and args.all:
        entries = manifest.read_manifest()["factors"]
        names = sorted(entries)
    if not names:
        print("  请指定 --name / --category / --all 之一（--all 读清单里已算过的因子）")
        return 2

    start = args.start or settings.data.start_date
    end = args.end or None
    if end:
        data_store.set_read_until(end)

    debug_symbols = [s.strip() for s in (args.symbols or "").split(",") if s.strip()] or None
    print()
    print("=" * 96)
    print(
        f"  因子有效性分析｜{len(names)} 个因子｜区间 {start} ~ {end or '今天'}"
        f"｜{args.groups} 组｜持有 {args.horizon} 日"
    )
    if debug_symbols:
        print(f"  ⚠️ 调试模式：股票池限制为 {debug_symbols}，结论不可用")
    print("=" * 96)

    try:
        run(
            names=names,
            start=start,
            end=end or _today(),
            groups=args.groups,
            horizon=args.horizon,
            debug_symbols=debug_symbols,
        )
    except AppError as exc:
        logger.error(f"分析失败：{exc.message}")
        return 2
    print("=" * 96)
    print()
    return 0


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


if __name__ == "__main__":
    raise SystemExit(main())
