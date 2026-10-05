"""参数寻优 CLI —— `python -m strategies.sweep`。

**为什么走 CLI 不走 Web 触发**
参数寻优是计算密集任务（12~16 组 × 1115 天 × 5000 只，跑一次几十分钟）。
放进 HTTP 请求链路会让服务卡死。符合"只读展示优先"的架构纪律：
**计算在 CLI，Web 只读结果。**

用法
----
    cd backend
    python -m strategies.sweep --config sweeps/hold_tp.yaml --tag v1
    python -m strategies.sweep --config sweeps/hold_tp.yaml --symbols sh.600519,sz.000001  # 通线
    python -m strategies.sweep --list

产物（`data/sweep/{sweep_id}/`）
-------------------------------
    grid.json        参数网格定义 + 区间 + 目标指标
    results.parquet  一行一组：参数 + 指标 + run_id + 敏感性标记
    runs.json        组序号 → run_id（供前端跳到回测详情页）
    summary.md       自动生成的结论（含「高原 vs 尖峰」判断）

================================ 重要 ================================
防过拟合三条纪律（规格 §5.1）**硬编码在本文件里**，不靠注释提醒：

1. **只允许样本内区间**。传样本外的起点/终点 → `_load_grid_config` 直接抛
   `AppError`。样本外数据在规则冻结前绝不允许被看到。
2. **强制报告参数敏感性**。每组都算"邻居落差"，并在 summary.md 里点出
   「尖峰」组合 —— 相邻参数收益差异巨大说明是噪声拟合。
3. **推荐「高原」而非「尖峰」**。`pick_plateau()` 选的是**邻域平均值最高**的
   组合，不是单点最优值。单点最优几乎必然是过拟合。

这三条如果只写在文档里，迟早会有人为了"跑出好看的数"绕过它们。
=====================================================================
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from statistics import fmean
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd
import yaml

from app.core.config import settings
from app.core.errors import AppError
from app.core.jsonutils import json_safe
from app.core.logging import logger, setup_logging
from app.core.paths import SWEEP_DIR, SWEEPS_DIR, ensure_dirs, relative_to_project
from app.repos import db
from app.services import sweep_service
from app.services.data_source import symbols
from strategies import registry
from strategies.runner import (
    PREHEAT_START,
    expand_grid,
    grid_param_names,
    load_market,
    run_backtest,
)

#: 可选的目标指标（用于挑最优与算敏感性）
OBJECTIVES = ("annual_return", "total_return", "sharpe", "calmar")

#: 敏感性落差阈值：邻居平均收益 / 本组收益 低于它 → 判为"尖峰"（噪声拟合）
#:   1.6 的含义：旁边两组平均只有本组 62.5% 的水平，就说明这个点站不住。
#:   为什么用比值而不是绝对差：比值与收益量纲无关，牛市熊市都适用。
SPIKE_RATIO = 1.6


# ======================================================================
# 纪律 1 · 只允许样本内
# ======================================================================
def _list_grid_configs() -> str:
    """列出可用的网格配置文件，让"找不到配置"的报错能自解。"""
    if not SWEEPS_DIR.exists():
        return "（sweeps/ 目录不存在）"
    names = sorted(f.name for f in SWEEPS_DIR.glob("*.y*ml"))
    return "、".join(names) if names else "（sweeps/ 下没有 yaml）"


def assert_in_sample(start: str, end: str) -> None:
    """样本外区间一律拒绝 —— 这是硬约束，不是提示。"""
    bt = settings.backtest
    if start < bt.in_sample_start or end > bt.in_sample_end:
        raise AppError(
            f"参数寻优只允许样本内区间 {bt.in_sample_start} ~ {bt.in_sample_end}，"
            f"收到 {start} ~ {end}。\n"
            f"样本外数据在规则冻结前**绝不允许被看到**（策略规格说明书 §5.1 纪律 1）——"
            f"反复查看样本外会让它变成事后挑参数的依据。\n"
            f"规则冻结后再用 `python -m strategies.run --out-sample` 跑一次。"
        )


def _resolve_grid_path(path: str) -> Path:
    """把 `--config` 的值解析成真实文件。

    ⚠️ 这里踩过一次：文档与 YAML 注释里写的用法是
       `--config sweeps/hold_tp.yaml`（带 `sweeps/` 前缀），
       而代码无条件拼成 `SWEEPS_DIR / p` → `backend/sweeps/sweeps/hold_tp.yaml`，
       直接报"网格配置不存在"。**带前缀与不带前缀两种写法都必须能用** ——
       否则文档写什么就会有一半人照着报错。

    解析顺序：
      绝对路径 → 相对 backend/ → 相对 backend/sweeps/ → 相对 cwd/ → 兜底 backend/sweeps/
    """
    p = Path(path)
    if p.is_absolute():
        return p
    for cand in (SWEEPS_DIR.parent / p, SWEEPS_DIR / p, Path.cwd() / p):
        if cand.exists():
            return cand
    return SWEEPS_DIR / p


def _load_grid_config(path: str) -> dict[str, Any]:
    p = _resolve_grid_path(path)
    if not p.exists():
        raise AppError(
            f"网格配置不存在：{p}\n"
            f"可用配置：{_list_grid_configs()}（放在 {relative_to_project(SWEEPS_DIR)}/ 下）"
        )
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise AppError(f"网格配置格式错误（应为 mapping）：{p}")

    strategy = str(raw.get("strategy") or "momentum").strip()
    registry.get_strategy_cls(strategy)  # 名字不存在时直接抛错，别跑到一半才发现
    grid = raw.get("params") or {}
    if not isinstance(grid, dict) or not grid:
        raise AppError(f"网格配置缺少 params：{p}")

    objective = str(raw.get("objective") or "annual_return")
    if objective not in OBJECTIVES:
        raise AppError(f"objective 只支持 {OBJECTIVES}，收到 {objective!r}")

    return {
        "path": p,
        "strategy": strategy,
        "description": str(raw.get("description") or "").strip(),
        "grid": {k: list(v) for k, v in grid.items()},
        "objective": objective,
        "sensitivity": raw.get("sensitivity") or {},
    }


def expand_combos(grid: dict[str, list[Any]]) -> list[dict[str, Any]]:
    """参数网格 → 组合列表。

    实现**委托给** `strategies.runner.expand_grid`（单一实现）—— 后者支持
    v1.4 的「联合维度」（某 key 的候选值是 dict，用于主板/创业板成对联动）。
    """
    return expand_grid(grid)


def make_sweep_id(strategy: str, stamp: str | None = None) -> str:
    return f"{stamp or datetime.now().strftime('%Y%m%d-%H%M%S')}-{strategy}"


# ======================================================================
# 纪律 2 & 3 · 敏感性分析 / 高原选取
# ======================================================================
def _axis_key(row: pd.Series, x: str, series: str | None) -> tuple[Any, ...]:
    return (row[x],) if series is None else (row[x], row[series])


def analyse_sensitivity(
    df: pd.DataFrame,
    *,
    x: str,
    series: str | None,
    objective: str,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """给每组算「邻域平均收益」，据此判高原/尖峰。

    做法：在 (x, series) 网格上，取该点**上下左右四个正交邻居**的 objective 均值
    （不取对角 —— 对角同时动两个参数，噪声更大）。

    ⚠️ 邻居缺失（边缘点 / 某组失败）时用的是**实际存在的邻居**，不是补零。
       补零会把边缘点误判成尖峰（它的邻居"看起来"很差）。
    """
    out = df.copy()
    if x not in out.columns:
        out["neighbor_mean"] = None
        out["is_spike"] = False
        return out, {"x": x, "series": series, "available": False}

    # ★ 每个 (x, series) 格子里通常不止一行 —— 其余维度也在网格里
    #   （如 v1.5 的 止损倍数 × 涨停窗口 × 持有期）。所以必须**按格子取均值**。
    #   v1.5 之前这里是 `lookup[key] = value`（后写覆盖）→ 邻域均值只反映每格里
    #   随机一行的结果，高原/尖峰判定直接失真。这是本项目最忌的
    #   "看着算了、其实算错了"那类坑（数在、结论却是错的）。
    cells: dict[tuple[Any, ...], list[float]] = {}
    for _, r in out.iterrows():
        v = r.get(objective)
        if v is not None and pd.notna(v):
            cells.setdefault(_axis_key(r, x, series), []).append(float(v))
    lookup: dict[tuple[Any, ...], float] = {k: fmean(v) for k, v in cells.items()}
    n_multi = sum(1 for v in cells.values() if len(v) > 1)
    if n_multi:
        logger.info(
            f"敏感性分析：{n_multi}/{len(cells)} 个格子含多行（其余维度也在网格中）"
            f"→ 邻域均值按**格子均值**计算"
        )

    xs = sorted({k[0] for k in lookup})
    srs = sorted({k[1] for k in lookup}) if series else [None]
    x_pos = {v: i for i, v in enumerate(xs)}
    s_pos = {v: i for i, v in enumerate(srs)}

    neighbor_mean: list[float | None] = []
    is_spike: list[bool] = []
    for _, r in out.iterrows():
        cur = _axis_key(r, x, series)
        vals: list[float] = []
        # ⚠️ 用 `x_pos.get` 而不是 `xs.index`：若某个 x 取值的**所有组都失败**，
        #    它不会出现在 lookup 里，`.index()` 会直接抛 ValueError 把
        #    整个报告生成搞挂 —— 而那正是最需要看到报告的时候。
        xi = x_pos.get(cur[0])
        if xi is not None:
            for nb_x in (xi - 1, xi + 1):
                if 0 <= nb_x < len(xs):
                    k = (xs[nb_x],) if series is None else (xs[nb_x], cur[1])
                    if k in lookup:
                        vals.append(lookup[k])
        if series:
            si = s_pos.get(cur[1])
            if si is not None:
                for nb_s in (si - 1, si + 1):
                    if 0 <= nb_s < len(srs):
                        k = (cur[0], srs[nb_s])
                        if k in lookup:
                            vals.append(lookup[k])
        nm = fmean(vals) if vals else None
        neighbor_mean.append(nm)

        own = r.get(objective)
        if nm is None or own is None or pd.isna(own):
            is_spike.append(False)
        else:
            own_f, nm_f = float(own), float(nm)
            # 尖峰 = 自己不错、但邻居明显跟不上。
            # 两种形态：① 邻居在亏损而自己为正；② 自己 / 邻居 ≥ 阈值。
            is_spike.append(
                (own_f > 0 and nm_f <= 0) or (nm_f > 0 and own_f / nm_f >= SPIKE_RATIO)
            )

    out["neighbor_mean"] = neighbor_mean
    out["is_spike"] = is_spike
    return out, {"x": x, "series": series, "available": True, "spike_ratio": SPIKE_RATIO}


def pick_plateau(df: pd.DataFrame, objective: str) -> dict[str, Any] | None:
    """★ 选「高原」不选「尖峰」。

    排名依据 = (邻域均值, 本组值) 的字典序 —— **邻域均值优先**。
    这样选出来的点即使不是单点最优，也是"周围都不错"的点，
    在样本外更可能站得住。单点最优几乎必然是过拟合。

    ⚠️ 为什么邻域均值优先而不是加权混合：加权比例是拍脑袋定的，
       而且一旦混进去，就无法解释"为什么选它而不是最高分那个"。字典序可解释。
    """
    if df.empty or objective not in df.columns:
        return None
    ok = df[df[objective].notna()].copy()
    if ok.empty:
        return None
    ok["_nm"] = ok["neighbor_mean"].apply(lambda v: -1e9 if v is None or pd.isna(v) else float(v))
    ok = ok.sort_values(["_nm", objective], ascending=False)
    best = ok.iloc[0]
    row = {k: (None if pd.isna(v) else v) for k, v in best.items() if k != "_nm"}
    return row


def best_single(df: pd.DataFrame, objective: str) -> dict[str, Any] | None:
    """单点最优（仅供参考，**不是推荐值**）。"""
    if df.empty or objective not in df.columns:
        return None
    ok = df[df[objective].notna()]
    if ok.empty:
        return None
    row = ok.loc[ok[objective].idxmax()]
    return {k: (None if pd.isna(v) else v) for k, v in row.items()}


# ======================================================================
# 汇总报告
# ======================================================================
def _j(v: Any) -> str:
    """安全 JSON 序列化（给报告里的参数组合用）。

    ⚠️ 踩过一次：`pick_plateau` / `best_single` 的行来自 **DataFrame**，
       整数取出来是 `numpy.int64` —— 裸 `json.dumps` 直接抛
       `TypeError: Object of type int64 is not JSON serializable`，
       而且是在**跑完 12 组之后**生成报告时才炸，白等一场。

       `default=str` 不够：它会把 numpy 数值变成**字符串**（`"20"`），
       报告里就出现 `{"hold_days_max": "20"}` 这种引号包着的数字。
       必须走项目统一的 `json_safe`。
    """
    return json.dumps(json_safe(v), ensure_ascii=False)


def _fmt_pct(v: Any, digits: int = 2) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "—"
    if pd.isna(f):
        return "—"
    return f"{f * 100:.{digits}f}%"


def _fmt_num(v: Any, digits: int = 2) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "—"
    if pd.isna(f):
        return "—"
    return f"{f:.{digits}f}"


#: 按**百分比**展示的指标。其余（夏普、卡玛等无量纲比率）按数值展示。
#: 口径必须集中在一处 —— 散落在各分支里就会出现"同一张表两种口径"。
PCT_METRICS = frozenset({
    "annual_return", "total_return", "max_drawdown", "annual_cost_ratio",
    "neighbor_mean", "win_rate", "turnover_ratio", "volatility",
    "avg_win", "avg_loss", "max_single_loss", "profit_loss_ratio_abs",
})


def _fmt_metric(name: str, v: Any) -> str:
    """按指标口径格式化，避免"同表两种口径"。

    ⚠️ 踩过：目标指标 `annual_return` 落了 `_fmt_num` → 显示 `-0.05`，
       而紧邻的 `neighbor_mean`（**同一个指标**）走 `_fmt_pct` → 显示 `-8.20%`。
       同一张表两种口径，读的人会把 `-0.05` 当成"只亏 5 分钱"而不是 5%。
       修正方式是集中判定，而不是在每个分支里各写一次。
    """
    if name == "trade_count" or name.endswith("_count") or name.endswith("_days"):
        return "—" if v is None or pd.isna(v) else str(int(v))
    if name in PCT_METRICS or name.endswith("_ratio") or name.endswith("_rate"):
        return _fmt_pct(v)
    return _fmt_num(v)


def render_summary(
    *,
    sweep_id: str,
    cfg: dict[str, Any],
    interval: dict[str, Any],
    df: pd.DataFrame,
    sens: dict[str, Any],
    objective: str,
    plateau: dict[str, Any] | None,
    single: dict[str, Any] | None,
    combos: list[dict[str, Any]],
    elapsed: float,
    debug_symbols: list[str] | None,
) -> str:
    """生成 `summary.md`。目标是**能直接用来下判断**，不是堆数字。"""
    L: list[str] = []
    a = L.append
    # ★ 用**展开后**的参数名，而不是 `cfg["grid"]` 的原始键 ——
    #   联合维度（如 `take_profit`）在结果表里展开成 `take_profit_gross_main/_gem`，
    #   按原始键取会让推荐组合与全网格表**丢掉止盈档位**，
    #   还会把"只差止盈的两组"误判成"完全一致"（v1.4-sweep 踩到）。
    param_names = grid_param_names(cfg["grid"])

    a(f"# 参数寻优报告 · {sweep_id}")
    a("")
    a(f"> 策略 `{cfg['strategy']}`｜区间 `{interval['start']} ~ {interval['end']}`"
      f"（{interval['name']}）｜目标指标 `{objective}`")
    a(f"> 网格 {len(combos)} 组｜完成 {len(df)} 组｜耗时 {elapsed:.1f}s")
    a(f"> 生成时间 {datetime.now().isoformat(timespec='seconds')}")
    a("")

    if debug_symbols:
        a("## ⚠️ 调试模式")
        a("")
        a(f"本次只用了 **{len(debug_symbols)} 只标的**（{', '.join(debug_symbols[:8])}"
          f"{'…' if len(debug_symbols) > 8 else ''}）。")
        a("截面选股被严重削弱，**结论不可用于评估策略**，仅验证流水线通不通。")
        a("")

    a("## 结论")
    a("")
    pk = {k: plateau[k] for k in param_names if k in plateau} if plateau else None
    sk = {k: single[k] for k in param_names if k in single} if single else None
    if pk is not None:
        a(f"**推荐组合（高原）**：`{_j(pk)}`")
        a("")
        a(f"  目标指标 `{objective}` = **{_fmt_pct(plateau.get(objective))}**"
          f"｜邻域平均 = {_fmt_pct(plateau.get('neighbor_mean'))}")
        a("")
    if sk is not None:
        if pk is not None and sk == pk:
            a("单点最优与高原推荐**一致**，说明该点周围稳，信号相对可信。")
        else:
            a(f"单点最优为 `{_j(sk)}`"
              f"（{_fmt_pct(single.get(objective))}），**但不推荐** ——")
            a("单点最优几乎必然过拟合，见下方敏感性分析。")
        a("")

    # ★ 目标指标全为负时，「尖峰/高原」的比值口径不适用（它是按"邻居均值 ÷ 本组值、
    #   默认收益为正"设计的）。不说清楚的话，"0 个尖峰"会被读成"没有噪声点"。
    if objective in df.columns and not df.empty:
        vals = pd.to_numeric(df[objective], errors="coerce").dropna()
        if not vals.empty and (vals <= 0).all():
            a("> ⚠️ **本网格目标指标全为负**：此时「尖峰」判定用的比值口径不适用"
              "（它假设收益为正）。故「0 个尖峰」**不等于没有噪声点** ——"
              "请结合下方全网格表人工读数，别只看推荐组合。")
            a("")

    n_spike = int(df["is_spike"].sum()) if "is_spike" in df.columns and not df.empty else 0
    if n_spike:
        spikes = df[df["is_spike"]]
        a(f"### 检出 {n_spike} 个「尖峰」组合（噪声拟合，不可用）")
        a("")
        a("| " + " | ".join([*param_names, f"{objective}", "邻域均值", "落差倍数"]) + " |")
        a("|" + "---|" * (len(param_names) + 3))
        for _, r in spikes.iterrows():
            own, nm = r.get(objective), r.get("neighbor_mean")
            ratio = "—"
            try:
                if nm and float(nm) > 0 and own is not None and pd.notna(own):
                    ratio = f"{float(own) / float(nm):.2f}×"
            except (TypeError, ValueError):
                pass
            cells = [f"`{r.get(k)}`" for k in param_names]
            a("| " + " | ".join([*cells, _fmt_pct(own), _fmt_pct(nm), ratio]) + " |")
        a("")

    a("## 全网格结果")
    a("")
    # 只看结果表里**真实存在**的参数列（联合维度已展开成 _main/_gem）
    pcols = [c for c in param_names if c in df.columns]
    cols = [*pcols, objective, "max_drawdown", "annual_cost_ratio",
            "sharpe", "trade_count", "neighbor_mean"]
    cols = [c for c in cols if c in df.columns]
    a("| " + " | ".join(str(c) for c in cols) + " |")
    a("|" + "---|" * len(cols))
    show = df.sort_values(objective, ascending=False) if objective in df.columns else df
    for _, r in show.iterrows():
        cells = []
        for c in cols:
            v = r.get(c)
            # 网格参数原样展示；其余按指标口径统一格式化
            cells.append(f"`{v}`" if c in param_names else _fmt_metric(c, v))
        a("| " + " | ".join(cells) + " |")
    a("")

    a("## 敏感性分析")
    a("")
    # 轴信息从 `sens` 里取（`analyse_sensitivity` 已经带回来了），
    # 不再单独加参数 —— 两个来源会漂移。
    _render_sensitivity(a, df=df, sens=sens, cfg=cfg, objective=objective,
                        x_axis=str(sens.get("x") or ""),
                        series_axis=sens.get("series"))

    a("## 怎么读这份报告")
    a("")
    a("1. **看高原不看尖峰** —— 推荐组合选的是邻域平均最高者，不是单点最高者。")
    a("2. **看相邻列的变化幅度** —— 参数挪一格收益就腰斩，说明该参数是在拟合噪声。")
    a("3. **年化成本随持有期下降** —— 持有期越长换手越低，这是本策略最主要的成本杠杆。")
    a("4. ⚠️ 样本内选出来的参数**必须**在规则冻结后只跑一次样本外验证。")
    a("")
    return "\n".join(L)


def _render_sensitivity(
    a: Any,
    *,
    df: pd.DataFrame,
    sens: dict[str, Any],
    cfg: dict[str, Any],
    objective: str,
    x_axis: str,
    series_axis: str | None,
) -> None:
    """把敏感性分析**渲染出来** —— 这是纪律 2 的落点。

    ⚠️ 踩过：`sens` 一路传进 `render_summary` 却从未被引用，
       报告里只有"看相邻列的变化幅度"这句空话。纪律 2 写着"强制敏感性分析"，
       代码也算了，但**结论根本没呈现给读者** —— 等于没做。
       计算了却不呈现 = 白算，而且是最危险的那种白算（看着像做了）。
    """
    if not sens.get("available"):
        a("网格里没有可用于敏感性分析的轴，本节省略。")
        a("")
        return

    x_vals = sorted(df[x_axis].dropna().unique().tolist()) if x_axis in df.columns else []
    if series_axis and series_axis in df.columns:
        s_vals = sorted(df[series_axis].dropna().unique().tolist())
    else:
        s_vals = [None]

    a(f"轴：`{x_axis}`（横向）"
      + (f" × `{series_axis}`（分组）" if series_axis else "")
      + f"｜指标 `{objective}`（单元格内为**样本内**数值）")
    a("")

    # 透视表：行 = series，列 = x。空位（该组失败）留 `—`，不补零
    head = [" " + (f"`{series_axis}`" if series_axis else "组")] + [f"`{v}`" for v in x_vals]
    a("| " + " | ".join(head) + " |")
    a("|" + "---|" * len(head))
    for sv in s_vals:
        cells = []
        for xv in x_vals:
            m = df[df[x_axis] == xv] if x_axis in df.columns else df.iloc[0:0]
            if sv is not None and series_axis in df.columns:
                m = m[m[series_axis] == sv]
            if m.empty or m[objective].isna().all():
                cells.append("—")
            else:
                cells.append(_fmt_metric(objective, m[objective].iloc[0]))
        label = f"`{sv}`" if sv is not None else objective
        a(f"| {label} | " + " | ".join(cells) + " |")
    a("")

    # 逐轴给"挪一格的变化幅度" —— 这才是判断"高原还是尖峰"的直接依据
    a("**参数挪一格的变化幅度**（越小越稳）：")
    a("")
    for axis in [x_axis, series_axis]:
        if not axis or axis not in df.columns:
            continue
        vals = sorted(df[axis].dropna().unique().tolist())
        if len(vals) < 2:
            continue
        vals_f = [float(v) for v in vals]
        steps = [vals_f[i + 1] - vals_f[i] for i in range(len(vals_f) - 1)]
        # 相邻两档之间，objective 的平均绝对变化
        deltas: list[float] = []
        for i in range(len(vals) - 1):
            seg_a = df[df[axis] == vals[i]][objective].dropna()
            seg_b = df[df[axis] == vals[i + 1]][objective].dropna()
            if seg_a.empty or seg_b.empty:
                continue
            deltas.append(abs(float(seg_b.mean()) - float(seg_a.mean())))
        if not deltas:
            continue
        avg_delta = fmean(deltas)
        spread = max(float(v) for v in df[objective].dropna()) - min(
            float(v) for v in df[objective].dropna()
        ) if df[objective].notna().any() else 0.0
        share = (avg_delta / spread * 100) if spread > 0 else 0.0
        verdict = "平稳" if share < 25 else ("偏敏感" if share < 50 else "**很敏感**")
        steps_txt = "、".join(f"{s:g}" for s in steps)
        a(f"- `{axis}`（步长 {steps_txt}）：相邻档平均变化 "
          f"**{avg_delta * 100:.2f}%**，占全网格极差的 {share:.0f}% → {verdict}")
    a("")
    a(f"尖峰判定阈值：`自己 / 邻域均值 ≥ {sens.get('spike_ratio', SPIKE_RATIO):.1f}` "
      f"或「邻域亏损而自己为正」。")
    a("")


# ======================================================================
# 主流程
# ======================================================================
def main() -> int:
    parser = argparse.ArgumentParser(description="策略参数寻优 CLI")
    parser.add_argument("--config", default="hold_tp.yaml", help="网格配置（sweeps/ 下）")
    parser.add_argument("--tag", default="", help="标签，便于在页面区分")
    parser.add_argument("--sweep-id", default=None, help="自定义 sweep_id（默认时间戳-策略名）")
    parser.add_argument(
        "--symbols", default=None,
        help="只跑指定标的（逗号分隔）—— 通线验证用，结论不可用于评估策略",
    )
    parser.add_argument("--list", action="store_true", help="列出历史 sweep")
    args = parser.parse_args()

    setup_logging()
    ensure_dirs()

    if args.list:
        rows = sweep_service.list_sweeps(force=True)
        if not rows:
            print("暂无参数寻优记录。跑一次：python -m strategies.sweep --config hold_tp.yaml")
            return 0
        print(f"{'sweep_id':<28s} {'策略':<10s} {'组数':>6s} {'区间':<26s} 完整")
        for r in rows:
            print(
                f"{r['sweep_id']:<28s} {r['strategy']:<10s} "
                f"{r['done_count']:>3d}/{r['combo_count']:<3d}"
                f"  {r['start_date']!s} ~ {r['end_date']!s:<12s} "
                f"{'是' if r['is_complete'] else '否'}"
            )
        return 0

    if not db.is_initialized():
        logger.error("meta.db 尚未初始化，请先执行：python -m tools.init_db")
        return 2

    cfg = _load_grid_config(args.config)
    bt = settings.backtest

    # ★ 纪律 1：样本内硬约束。写在最前面 —— 别等跑完 12 组才发现区间不对。
    assert_in_sample(bt.in_sample_start, bt.in_sample_end)
    interval = {
        "name": "in_sample",
        "start": bt.in_sample_start,
        "end": bt.in_sample_end,
        "preheat_start": PREHEAT_START,
        "is_out_sample": False,
    }

    combos = expand_combos(cfg["grid"])
    # 展开后的**真实参数名**：联合维度（如 take_profit）会展开成多个键
    # （take_profit_gross_main/_gem），敏感性轴必须按展开名匹配。
    param_names = grid_param_names(cfg["grid"])
    debug_symbols: list[str] | None = None
    if args.symbols:
        # ★ 必须过 `symbols.normalize()`，不能只 strip：
        #   ① 文件若带 UTF-8 BOM（记事本/Excel/PS Out-File 默认都带），
        #      它粘在第一个代码上，strip() 去不掉，直接报"格式无法识别"；
        #   ② 顺手允许 `600519` / `SH600519` 等写法，README 里那条
        #      `--symbols "$(cat sweeps/smoke_subset.txt)"` 才不会因换行/空格炸掉。
        debug_symbols = [
            symbols.normalize(s) for s in args.symbols.replace("\n", ",").split(",") if s.strip()
        ]

    sens_cfg = cfg["sensitivity"] or {}
    x_axis = str(sens_cfg.get("x") or param_names[0])
    series_axis = sens_cfg.get("series")
    if series_axis is not None:
        series_axis = str(series_axis)
        if series_axis not in param_names:
            raise AppError(f"sensitivity.series={series_axis} 不在网格参数里：{param_names}")
    if x_axis not in param_names:
        raise AppError(f"sensitivity.x={x_axis} 不在网格参数里：{param_names}")
    if series_axis is None and len(param_names) > 1:
        series_axis = next(p for p in param_names if p != x_axis)

    sweep_id = args.sweep_id or make_sweep_id(cfg["strategy"])
    out_dir = SWEEP_DIR / sweep_id
    out_dir.mkdir(parents=True, exist_ok=True)

    logger.info("=" * 70)
    logger.info(f"参数寻优开始：{cfg['strategy']}｜{sweep_id}")
    logger.info(f"  网格配置  : {cfg['path']}")
    logger.info(f"  区间      : {interval['start']} ~ {interval['end']}（样本内）")
    logger.info(f"  网格      : {len(combos)} 组｜{json.dumps(cfg['grid'], ensure_ascii=False)}")
    logger.info(f"  目标指标  : {cfg['objective']}")
    logger.info(f"  敏感性轴  : x={x_axis} series={series_axis}")
    logger.info(f"  落盘位置  : {relative_to_project(out_dir)}")
    logger.info("=" * 70)

    if debug_symbols:
        logger.warning(
            f"⚠️ 调试模式：只跑 {len(debug_symbols)} 只标的 → {debug_symbols}。"
            f"截面选股被严重削弱，**结论不可用于评估策略**。"
        )

    grid_payload = {
        "sweep_id": sweep_id,
        "strategy": cfg["strategy"],
        "description": cfg["description"],
        "objective": cfg["objective"],
        "params": cfg["grid"],           # 名字对齐前端/规格：grid.json 里叫 params
        "param_names": param_names,
        "sensitivity": {"x": x_axis, "series": series_axis},
        "combos": combos,
        "interval": interval,
        "meta": {
            "tag": args.tag,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "symbols": debug_symbols,
            "config_path": str(cfg["path"]),
            "objectives": list(OBJECTIVES),
            "spike_ratio": SPIKE_RATIO,
        },
    }
    (out_dir / "grid.json").write_text(
        json.dumps(grid_payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )

    # ★ 行情矩阵只加载一次，12 组共用（`MarketData` 只读且与策略参数无关）。
    #   不这么做的话每组都白读一遍全市场盘，光 IO 就烧掉半小时。
    t_load = time.perf_counter()
    market = load_market(
        start=interval["preheat_start"], end=interval["end"], symbols=debug_symbols
    )
    logger.info(
        f"行情矩阵已加载并锁定：{len(market.dates)} 交易日 × {len(market.symbols)} 标的"
        f"（{time.perf_counter() - t_load:.1f}s）—— 本轮 {len(combos)} 组共用"
    )

    rows: list[dict[str, Any]] = []
    runs: dict[str, str] = {}
    t0 = time.perf_counter()
    for i, combo in enumerate(combos, 1):
        logger.info(f"[{i}/{len(combos)}] 参数 {combo}")
        try:
            res = run_backtest(
                cfg["strategy"],
                start=interval["start"],
                end=interval["end"],
                interval_name="in_sample",
                is_out_sample=False,
                params=combo,
                tag=f"{args.tag or 'sweep'}:{i}",
                symbols=debug_symbols,
                market=market,
                # 寻优只关心指标：不写 trades/positions/orders，
                # 12 组 × 几百笔能省下大量磁盘。净值仍写（前端要画）。
                # ★ 产物里会记 `_run.detail_written=False`，下游据此区分
                #   "真的 0 笔交易"与"没落盘"。
                write_trades=False,
                # 标记来源：这些 run 属于某个 sweep，不是独立的回测运行。
                # 前端可据此把它们从回测列表页里折叠/标记，避免污染。
                extra_run={"sweep_id": sweep_id, "combo_index": i},
            )
        except Exception as exc:
            logger.error(f"  第 {i} 组失败（已跳过，不中断整轮）：{type(exc).__name__}: {exc}")
            row: dict[str, Any] = {**combo, "combo_index": i, "run_id": None, "failed": True}
            rows.append(row)
            continue

        m = res["metrics"]
        row = {
            **combo,
            "combo_index": i,
            "run_id": res["run_id"],
            "failed": False,
            "total_return": m.get("total_return"),
            "annual_return": m.get("annual_return"),
            "max_drawdown": m.get("max_drawdown"),
            "sharpe": m.get("sharpe"),
            "calmar": m.get("calmar"),
            "annual_cost_ratio": m.get("annual_cost_ratio"),
            "trade_count": m.get("trade_count"),
            "win_rate": m.get("win_rate"),
            "payoff_ratio": m.get("payoff_ratio"),
            "avg_hold_days": m.get("avg_hold_days"),
            "max_single_loss": m.get("max_single_loss"),
            "cost_seconds": res["cost_seconds"],
        }
        rows.append(row)
        runs[str(i)] = res["run_id"]
        logger.info(
            f"  → run {res['run_id']}｜年化 {_fmt_pct(m.get('annual_return'))}"
            f"｜回撤 {_fmt_pct(m.get('max_drawdown'))}"
            f"｜成本 {_fmt_pct(m.get('annual_cost_ratio'))}"
            f"｜{res['cost_seconds']}s"
        )

        # 中途落盘：跑到一半断电也不用重来（与回补的断点续传同一思路）
        pd.DataFrame(rows).to_parquet(out_dir / "results.parquet", index=False)
        (out_dir / "runs.json").write_text(
            json.dumps(runs, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    elapsed = time.perf_counter() - t0
    df = pd.DataFrame(rows)

    # ★ 纪律 2：强制敏感性分析（不是可选项）
    df, sens = analyse_sensitivity(
        df, x=x_axis, series=series_axis, objective=cfg["objective"]
    )
    df.to_parquet(out_dir / "results.parquet", index=False)

    # ★ 纪律 3：推荐高原而非尖峰
    plateau = pick_plateau(df, cfg["objective"])
    single = best_single(df, cfg["objective"])

    (out_dir / "summary.md").write_text(
        render_summary(
            sweep_id=sweep_id,
            cfg=cfg,
            interval=interval,
            df=df,
            sens=sens,
            objective=cfg["objective"],
            plateau=plateau,
            single=single,
            combos=combos,
            elapsed=elapsed,
            debug_symbols=debug_symbols,
        ),
        encoding="utf-8",
    )

    n_failed = int(df["failed"].sum()) if "failed" in df.columns else 0
    print()
    print("=" * 70)
    print(f"  参数寻优完成｜{sweep_id}｜{len(combos) - n_failed}/{len(combos)} 组成功")
    print(f"  耗时 {elapsed:.1f}s｜结果目录 {relative_to_project(out_dir)}")
    print("=" * 70)
    if plateau:
        # ★ 用**展开后**的参数名（联合维度如 take_profit → _main/_gem），
        #   否则打印出来的"推荐组合"会丢掉止盈档位。
        pk = {k: plateau[k] for k in param_names if k in plateau}
        print(f"  推荐组合（高原）: {_j(pk)}")
        print(
            f"  {cfg['objective']:<16s}: {_fmt_pct(plateau.get(cfg['objective']))}"
            f"｜邻域平均 {_fmt_pct(plateau.get('neighbor_mean'))}"
        )
    if single and plateau:
        sk = {k: single[k] for k in param_names if k in single}
        pk = {k: plateau[k] for k in param_names if k in plateau}
        if sk != pk:
            print(
                f"  单点最优（不推荐）: {_j(sk)}"
                f"（{_fmt_pct(single.get(cfg['objective']))}）"
            )
    if "is_spike" in df.columns:
        print(f"  检出尖峰组合: {int(df['is_spike'].sum())} 个")
    print("=" * 70)
    print(f"  详细结论：{relative_to_project(out_dir / 'summary.md')}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
