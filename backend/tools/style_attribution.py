"""风格/暴露归因 —— 策略收益里有多少是 beta、多少是选股 alpha。

动机（研究 §7.12 的收尾）
--------------------------
`quality_reversal` 在样本内/牛市/非牛市三个窗口里呈"涨市跑输、跌市抗跌"的形态，
**疑似低 beta / 防御性暴露而非选股 alpha**（§7.12.5）。本工具把它量化：

    ① **持仓暴露**：策略持仓在每个风格维度上的平均截面分位（vs 全池中位 50%）；
    ② **风格回归**：策略日收益对一组风格因子收益做 OLS →
       看 α 是否显著、各风格 β 多大、R² 解释了多少波动。

结论用途：若 α ≈ 0（t 不显著）而风格 β 显著 → **收益来自风格暴露，不是 alpha**。

口径（复用 `factors.analyze`，禁止另写加载器）
----------------------------------------------
行情矩阵 = `factors.analyze.load_market`；可交易池 = `build_investable_masks`；
因子矩阵 = `load_factor_matrix`（散射对齐，与因子分析同口径）。

风格因子（日频 long-short，截面分位分组、**次日等权**收益）
----------------------------------------------------------
    MKT    池内等权次日收益（= 对齐口径基准）
    SMB    小市值 − 大市值      （`amount_mean_20` 分位，值小 = 小市值）
    VOL    低波动 − 高波动      （`volatility_20`）
    TURN   低换手 − 高换手      （`turnover_20`）
    MOM    低动量 − 高动量      （`ret_50`，低 = 近 50 日弱）
    GROW   高成长 − 低成长      （`profit_yoy` + `revenue_yoy` 平均秩）

⚠️ long 侧一律取**策略偏好的那一侧**（低波动/低换手/低动量/小市值/高成长），
   所以**正的 β = 策略在该风格上有暴露**。

用法
----
    cd backend
    python -m tools.style_attribution                                  # 默认质量反转的样本内基线
    python -m tools.style_attribution --tag v2.2-baseline
    python -m tools.style_attribution --tag oos-baseline --start 2025-01-01 --end 2026-09-16
    python -m tools.style_attribution --dir data/backtest/quality_reversal/<runid>

产物：`data/_style_attribution_report.txt`（报告，含回归表与暴露表）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.errors import AppError
from app.core.logging import logger, setup_logging
from app.core.paths import DATA_DIR, relative_to_project
from factors.analyze import (
    build_investable_masks,
    load_factor_matrix,
    load_market,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REPORT_PATH = DATA_DIR / "_style_attribution_report.txt"
DEFAULT_BACKTEST_DIR = DATA_DIR / "backtest" / "quality_reversal"

#: 策略默认回测（样本内基线）
DEFAULT_TAG = "v2.2-baseline"

#: (键, 中文名, 因子名, 类别, 方向)  方向 +1 = long 侧取"因子值小"的一侧
STYLES: tuple[tuple[str, str, str, str, int], ...] = (
    ("SMB", "小市值", "amount_mean_20", "daily", +1),
    ("VOL", "低波动", "volatility_20", "daily", +1),
    ("TURN", "低换手", "turnover_20", "daily", +1),
    ("MOM", "低动量", "ret_50", "daily", +1),
    ("GROW", "高成长", "profit_yoy", "fundamental", -1),
)

STYLE_KEYS: tuple[str, ...] = tuple(s[0] for s in STYLES)
_DIRECTION: dict[str, int] = {s[0]: s[4] for s in STYLES}


# ======================================================================
# 工具函数
# ======================================================================
def _pct_rank(x: np.ndarray) -> np.ndarray:
    """截面百分位秩（平均秩口径，与策略打分一致）。NaN → NaN。"""
    return pd.Series(x).rank(pct=True).to_numpy()


def _ls_return(rank: np.ndarray, ret: np.ndarray, q: float, direction: int) -> float:
    """按分位分组的 long-short 次日等权收益。rank/ret 已对齐且有限。"""
    lo = rank <= q
    hi = rank >= 1.0 - q
    long_mask, short_mask = (lo, hi) if direction > 0 else (hi, lo)
    if long_mask.sum() < 5 or short_mask.sum() < 5:
        return float("nan")
    return float(ret[long_mask].mean() - ret[short_mask].mean())


def _ols(y: np.ndarray, x: np.ndarray, names: list[str]) -> dict:
    """带常数项的 OLS，返回 β / t 值 / R²。"""
    n = len(y)
    design = np.column_stack([np.ones(n), x])
    beta, *_ = np.linalg.lstsq(design, y, rcond=None)
    resid = y - design @ beta
    dof = n - design.shape[1]
    if dof <= 0:
        raise AppError(f"样本太少（{n} 天）无法做 {design.shape[1]} 元回归")
    sigma2 = float(resid @ resid) / dof
    try:
        cov = sigma2 * np.linalg.inv(design.T @ design)
    except np.linalg.LinAlgError:
        cov = sigma2 * np.linalg.pinv(design.T @ design)
    se = np.sqrt(np.maximum(np.diag(cov), 0.0))
    with np.errstate(divide="ignore", invalid="ignore"):
        tvals = np.where(se > 0, beta / se, np.nan)
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1.0 - float(resid @ resid) / ss_tot if ss_tot > 0 else float("nan")
    adj = 1 - (1 - r2) * (n - 1) / dof if np.isfinite(r2) else float("nan")
    return {"n": n, "names": ["const", *names], "beta": beta, "t": tvals,
            "r2": r2, "adj_r2": adj}


def _resolve_run(tag: str | None, run_dir: str | None) -> Path:
    """按 tag 或目录定位回测产物目录。"""
    if run_dir:
        p = Path(run_dir)
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        if not (p / "equity.parquet").exists():
            raise AppError(f"目录里没有 equity.parquet：{p}")
        return p
    if not DEFAULT_BACKTEST_DIR.exists():
        raise AppError(f"回测目录不存在：{DEFAULT_BACKTEST_DIR}")
    want = tag or DEFAULT_TAG
    hits: list[tuple[str, Path]] = []
    for d in DEFAULT_BACKTEST_DIR.iterdir():
        cfg, eq = d / "config.json", d / "equity.parquet"
        if not (cfg.exists() and eq.exists()):
            continue
        try:
            c = json.loads(cfg.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if ((c.get("_run") or {}).get("tag") or "") == want:
            hits.append((d.name, d))
    if not hits:
        raise AppError(f"没找到 tag={want!r} 的回测产物（可加 --dir 指定）")
    hits.sort(key=lambda x: x[0], reverse=True)   # run_id 含时间戳 → 取最新
    if len(hits) > 1:
        logger.warning(f"tag={want!r} 有 {len(hits)} 个产物，取最新的 {hits[0][0]}")
    return hits[0][1]


# ======================================================================
# 主流程
# ======================================================================
def run(*, tag: str | None, run_dir: str | None, start: str | None,
        end: str | None, q: float, min_symbols: int) -> str:
    rdir = _resolve_run(tag, run_dir)
    equity = pd.read_parquet(rdir / "equity.parquet")
    positions = pd.read_parquet(rdir / "positions.parquet")
    eq_dates = pd.to_datetime(equity["date"]).dt.strftime("%Y-%m-%d").to_numpy()

    bt_start = start or str(eq_dates[0])
    bt_end = end or str(eq_dates[-1])
    logger.info(f"回测产物：{relative_to_project(rdir)}｜区间 {bt_start} ~ {bt_end}")

    market = load_market(bt_start, bt_end, None)
    masks, _info = build_investable_masks(market, bt_start, bt_end)
    dates = np.asarray(market.dates)
    n_days = len(dates)
    n_sym = len(market.symbols)
    logger.info(f"行情矩阵 {n_days} 日 × {n_sym} 只")

    # ---- 因子矩阵（float32 省内存）----
    mats: dict[str, np.ndarray] = {
        key: load_factor_matrix(fname, cat, market).astype("float32")
        for key, _cn, fname, cat, _d in STYLES
    }
    rev = load_factor_matrix("revenue_yoy", "fundamental", market).astype("float32")

    close = market.close
    ts = market.trade_status

    pos = positions.copy()
    pos["d"] = pd.to_datetime(pos["date"]).dt.strftime("%Y-%m-%d")
    sym_idx = {s: i for i, s in enumerate(market.symbols)}
    # ⚠️ 不能用 dict(pos.groupby("d"))：DataFrameGroupBy 带 keys 属性，
    #    dict() 会把它当 mapping 去调用 keys → TypeError: 'str' object is not callable
    by_day: dict[str, pd.DataFrame] = {}
    for _day_key, _day_grp in pos.groupby("d"):
        by_day[str(_day_key)] = _day_grp

    rows: list[dict] = []
    exposure_sum = dict.fromkeys(STYLE_KEYS, 0.0)
    exposure_w = 0.0

    for di in range(n_days - 1):
        with np.errstate(invalid="ignore", divide="ignore"):
            ret = close[di + 1] / close[di] - 1.0
        base = (
            masks[di] & masks[di + 1] & (ts[di + 1] == 1)
            & np.isfinite(close[di]) & np.isfinite(close[di + 1])
            & (close[di] > 0) & (close[di + 1] > 0) & np.isfinite(ret)
        )
        if int(base.sum()) < min_symbols:
            continue

        profit = mats["GROW"][di]   # mats 以风格键为索引；GROW 载入的即 profit_yoy
        both = np.isfinite(profit) & np.isfinite(rev[di])
        grow = np.full(n_sym, np.nan)
        if int(both.sum()) >= min_symbols:
            grow = (_pct_rank(np.where(both, profit, np.nan))
                    + _pct_rank(np.where(both, rev[di], np.nan))) / 2.0

        values: dict[str, np.ndarray] = {
            key: (grow if key == "GROW" else mats[key][di]) for key in STYLE_KEYS
        }

        # 因子值取 di（决策时点），收益是**次日** di+1 实现的 → 记录日必须是 di+1，
        # 否则风格序列与策略日收益错位一天，回归 β 会塌成 ~0（踩过）。
        rec: dict[str, float] = {"date": str(dates[di + 1]), "mkt": float(ret[base].mean())}
        rank_full: dict[str, np.ndarray | None] = {}
        for key in STYLE_KEYS:
            v = values[key]
            good = base & np.isfinite(v)
            if int(good.sum()) < min_symbols:
                rec[key] = float("nan")
                rank_full[key] = None
                continue
            r_sub = _pct_rank(v[good])
            rec[key] = _ls_return(r_sub, ret[good], q, _DIRECTION[key])
            full = np.full(n_sym, np.nan)
            full[good] = r_sub
            rank_full[key] = full
        rows.append(rec)

        # ---- 持仓暴露（市值加权平均分位）----
        g = by_day.get(str(dates[di]))
        if g is None or g.empty:
            continue
        for sym, mv in zip(g["symbol"].to_numpy(), g["market_value"].to_numpy(dtype="float64"), strict=True):
            si = sym_idx.get(sym)
            if si is None:
                continue
            used = False
            for key in STYLE_KEYS:
                rf = rank_full[key]
                if rf is not None and np.isfinite(rf[si]):
                    exposure_sum[key] += float(rf[si]) * mv
                    used = True
            if used:
                exposure_w += mv

    style_df = pd.DataFrame(rows).set_index("date")

    # ---- 策略日收益（对齐日期）----
    nav = pd.Series(equity["nav"].to_numpy(), index=eq_dates)
    strat = nav.pct_change().dropna()
    common = style_df.index.intersection(strat.index)
    if len(common) < 60:
        raise AppError(f"策略与风格序列重叠交易日仅 {len(common)} 天，无法回归")

    y_all = strat.loc[common].to_numpy(dtype="float64")
    x_all = style_df.loc[common, ["mkt", *STYLE_KEYS]].to_numpy(dtype="float64")
    finite = np.isfinite(y_all) & np.isfinite(x_all).all(axis=1)
    y, x = y_all[finite], x_all[finite]

    reg = _ols(y, x, ["MKT", *STYLE_KEYS])

    # ---- 报告 ----
    out: list[str] = []
    out.append("=" * 92)
    out.append("  风格 / 暴露归因报告")
    out.append("=" * 92)
    out.append(f"  回测产物：{relative_to_project(rdir)}")
    out.append(f"  行情区间：{dates[0]} ~ {dates[-1]}｜参与回归的重叠交易日 {reg['n']} 天")
    out.append(f"  分组：风格因子截面分位 q = {q:.0%} 取 long/short，**次日等权**收益")
    out.append("")
    out.append("  【一】策略日收益对风格因子收益的回归（含常数项）")
    out.append("        r_strategy = α + Σ β_i · F_i      （α 为日度，年化 = ×244）")
    out.append("")
    out.append(f"    {'因子':<10}{'β':>11}{'t值':>9}")
    out.append("    " + "-" * 32)
    for nm, b, tv in zip(reg["names"], reg["beta"], reg["t"], strict=True):
        if nm == "const":
            out.append(f"    {'α(日度)':<10}{b:>11.5f}{tv:>9.2f}   ← 选股 alpha")
            out.append(f"    {'α 年化':<10}{b * 244:>10.3%}")
            continue
        star = (" ***" if abs(tv) >= 2.58 else " **" if abs(tv) >= 1.96
                else " *" if abs(tv) >= 1.65 else "")
        out.append(f"    {nm:<10}{b:>11.3f}{tv:>9.2f}{star}")
    out.append("")
    out.append(f"    R² = {reg['r2']:.3f}｜调整 R² = {reg['adj_r2']:.3f}｜样本 {reg['n']} 天")
    out.append("    显著性：*** |t|≥2.58 ／ ** |t|≥1.96 ／ * |t|≥1.65")
    out.append("")
    out.append("  【二】策略持仓的风格暴露（市值加权平均截面分位；全池中位 = 50%）")
    out.append("")
    out.append(f"    {'风格':<8}{'中文':<10}{'持仓平均分位':>14}{'相对全池':>12}")
    out.append("    " + "-" * 44)
    if exposure_w > 0:
        for key, cname, _f, _cat, _d in STYLES:
            avg = exposure_sum[key] / exposure_w
            out.append(f"    {key:<8}{cname:<10}{avg:>13.1%}{avg - 0.5:>+12.1%}")
    else:
        out.append("    （持仓暴露为空 —— 检查回测产物是否含 positions.parquet）")
    out.append("")
    out.append("  读法：分位 < 50% = 持仓更靠近「因子值小」那一端（如 VOL 30% = 更低波动）。")
    out.append("")

    txt = "\n".join(out)
    REPORT_PATH.write_text(txt, encoding="utf-8")
    print(txt)
    logger.info(f"报告已写出：{relative_to_project(REPORT_PATH)}")
    return txt


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m tools.style_attribution",
        description="风格/暴露归因：策略收益里有多少是 beta、多少是 alpha",
    )
    p.add_argument("--tag", default=DEFAULT_TAG, help=f"回测 tag（默认 {DEFAULT_TAG}）")
    p.add_argument("--dir", default=None, help="直接指定回测产物目录（优先于 --tag）")
    p.add_argument("--start", default=None, help="覆盖起始日（默认用回测产物首日）")
    p.add_argument("--end", default=None, help="覆盖结束日（默认用回测产物末日）")
    p.add_argument("--q", type=float, default=0.3, help="分组分位（默认 0.30 → 两端各 30%%）")
    p.add_argument("--min-symbols", type=int, default=300, help="当日有效股票数下限（默认 300）")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging()
    try:
        run(tag=args.tag, run_dir=args.dir, start=args.start, end=args.end,
            q=args.q, min_symbols=args.min_symbols)
    except AppError as exc:
        logger.error(f"风格归因失败：{exc.message}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
