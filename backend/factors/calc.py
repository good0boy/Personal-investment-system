"""因子计算通道 CLI —— `python -m factors.calc`。

为什么要走 CLI
------------
与模块 3 同一条纪律：**计算在 CLI，Web 只读结果**。
全市场 11 个因子 ≈ 4800 只 × 1600 天 ≈ 700 万行/因子，放进 HTTP 请求链路
会让服务卡死；而落盘成普通文件后，前端扫盘即可，页面永远是毫秒级。

用法
----
    cd backend
    python -m factors.calc --list                     # 列注册表 + 已有产物
    python -m factors.calc --check                    # 自检（导入/签名/冒烟，不读行情）
    python -m factors.calc --category daily           # 算全部分类（增量：已算过且未变则跳过）
    python -m factors.calc --name ret_5 --name ret_10 # 只算指定的
    python -m factors.calc --category daily --force   # 强制重算
    python -m factors.calc --category daily --analysis # 算完顺带跑分层回测 + IC
    python -m factors.calc --name ret_5 --symbols sh.600519,sz.000001   # ⚠️ 调试：结论不可用

产物
----
    data/factor/{category}/{name}.parquet   列 [date, symbol, value]
    data/factor/_manifest.json              状态清单（行数/覆盖区间/参数/耗时）
    data/factor/analysis/{name}.json        分层回测 + IC（--analysis 时）

================================ 重要 ================================
**调试运行必须显式标注**（`debug_symbols` 写进清单，详情页会标红）。
一个只跑了 2 只标的的因子，产物结构与全市场**一模一样**，
不标就会有人拿它下结论 —— 这是模块 3 用血换来的教训（§2.4 设计要点 3）。
=====================================================================
"""
from __future__ import annotations

import argparse
import time
from datetime import datetime
from typing import Any

import pandas as pd

from app.core.config import settings
from app.core.errors import AppError
from app.core.logging import logger, setup_logging
from app.core.paths import FACTOR_DIR, relative_to_project
from app.services import data_store
from factors import loader, manifest

#: 因子计算需要的行情列（列裁剪，别用 SELECT * —— 少读 3 列能省 1/4 时间）
QUOTE_COLUMNS: tuple[str, ...] = (
    "date",
    "symbol",
    "open",
    "high",
    "low",
    "close",
    "preclose",
    "volume",
    "amount",
    "turn",
    "pct_chg",
    "trade_status",
    "is_st",
)


# ======================================================================
# 行情准备（口径统一在这里收口）
# ======================================================================
def prepare_quotes(
    start: str | None,
    end: str | None,
    symbols: list[str] | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """读全市场行情并整理成因子实现要求的形态。

    ★ 统一口径（**所有因子共享，实现里不再各自处理**）：
      1. 只保留 `trade_status == 1`（正常交易）的行 —— 停牌日的行是"有价无市"
         （close 等于前收），当成有效观测会让波动率被人为压低、动量被拉长；
      2. 剔除 close / preclose 无效（NaN 或 ≤ 0）的行；
      3. 按 `[symbol, date]` **稳定排序**并 `reset_index(drop=True)`
         —— 让 index 干净且唯一，杜绝 `groupby.rolling` 的索引错位隐患。

    Returns:
        (整理后的长表, 统计信息)
    """
    # ★ 先判"目录里有没有行情文件"再读。
    #   否则 DuckDB 会抛 `IO Error: No files found that match the pattern ...`
    #   —— 一条把用户指向 SQL 而不是指向"你还没回补数据"的报错。
    #   （首次部署 / 清空 data/ 之后是**很常见**的状态，不能让用户自己猜。）
    if not data_store.list_quote_symbols():
        raise AppError(
            "行情为空：data/raw/quote_daily/ 下没有任何日线文件。"
            "请先执行：python -m tools.backfill_quotes"
        )

    raw = data_store.read_quotes(None, start, end, columns=list(QUOTE_COLUMNS))
    stats: dict[str, Any] = {"rows_raw": len(raw)}
    if raw.empty:
        raise AppError(
            f"行情为空：{start or '起点不限'} ~ {end or '今天'}。"
            f"请先执行 python -m tools.backfill_quotes"
        )

    raw = raw.copy()
    raw["date"] = pd.to_datetime(raw["date"])

    if symbols:
        allow = {str(s).strip() for s in symbols if str(s).strip()}
        raw = raw[raw["symbol"].astype(str).isin(allow)]
        if raw.empty:
            raise AppError(f"按 --symbols 过滤后行情为空，请检查代码格式：{sorted(allow)}")

    ts = pd.to_numeric(raw["trade_status"], errors="coerce")
    close = pd.to_numeric(raw["close"], errors="coerce")
    preclose = pd.to_numeric(raw["preclose"], errors="coerce")
    mask = (ts == 1) & (close > 0) & close.notna() & (preclose > 0) & preclose.notna()

    stats["rows_dropped_invalid"] = int(len(raw) - int(mask.sum()))
    q = (
        raw[mask]
        .sort_values(["symbol", "date"], kind="stable")
        .reset_index(drop=True)
    )
    if q.empty:
        raise AppError("剔除停牌/无效价格后行情为空 —— 请检查数据源与过滤口径")

    stats["rows"] = len(q)
    stats["symbols"] = int(q["symbol"].nunique())
    stats["date_start"] = q["date"].min().strftime("%Y-%m-%d")
    stats["date_end"] = q["date"].max().strftime("%Y-%m-%d")
    return q, stats


# ======================================================================
# 单个因子
# ======================================================================
def compute_one(defn: loader.FactorDef, q: pd.DataFrame, stats: dict[str, Any]) -> dict[str, Any]:
    """计算并落盘一个因子，返回清单条目。异常由调用方处理（不在这里吞掉）。"""
    fn = loader.resolve_impl(defn)
    t0 = time.monotonic()
    values = fn(q, **defn.params)
    seconds = time.monotonic() - t0

    # ---- 契约校验：宁可在这里报错，也不要落一份"行对不上股票"的产物 ----
    if not isinstance(values, pd.Series):
        raise AppError(
            f"因子 {defn.name} 的实现返回了 {type(values).__name__}，契约要求 pd.Series"
        )
    if len(values) != len(q):
        raise AppError(
            f"因子 {defn.name} 返回长度 {len(values)} 与输入 {len(q)} 不一致（必须逐行对齐）"
        )

    frame = pd.DataFrame(
        {
            "date": q["date"].to_numpy(),
            "symbol": q["symbol"].to_numpy(),
            "value": pd.to_numeric(values, errors="coerce").to_numpy(dtype="float64"),
        }
    )
    written = data_store.save_factor(defn.name, frame, defn.category)

    non_null = int(frame["value"].notna().sum())
    rows = len(frame)
    nan_ratio = 1.0 - (non_null / rows) if rows else 1.0

    # ★ "算出来全是空的"是最危险的一类静默失效：文件在、行数对、页面能画，
    #   只是什么都没算出来。所以这里必须显式留痕，不能只看"退出码 0"。
    warnings: list[str] = []
    if non_null == 0:
        warnings.append("全部为 NaN —— 窗口长度可能大于数据长度，或实现有误")
    elif nan_ratio > 0.5:
        warnings.append(f"非空占比仅 {1 - nan_ratio:.1%}，请确认窗口参数是否过大")

    entry: dict[str, Any] = {
        "name": defn.name,
        "cname": defn.cname,
        "category": defn.category,
        "direction": defn.direction,
        "params": defn.params,
        "params_key": defn.params_key,
        "tags": list(defn.tags),
        "file": factor_rel_path(defn),
        "rows": rows,
        "non_null": non_null,
        "nan_ratio": round(nan_ratio, 6),
        "symbols": int(frame["symbol"].nunique()),
        "date_start": stats["date_start"],
        "date_end": stats["date_end"],
        "interval": {"start": stats["date_start"], "end": stats["date_end"]},
        "requested_interval": {"start": stats.get("requested_start"), "end": stats.get("requested_end")},
        "rows_input": stats["rows"],
        "rows_dropped_invalid": stats["rows_dropped_invalid"],
        "computed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "seconds": round(seconds, 3),
        "size_bytes": int(written.get("size_bytes") or 0),
        "value_min": _safe_stat(frame["value"].min()),
        "value_max": _safe_stat(frame["value"].max()),
        "warnings": warnings,
    }
    if stats.get("debug_symbols"):
        # ⚠️ 调试运行：只用了少量标的。产物结构与全市场一样，必须显式标注。
        entry["debug_symbols"] = list(stats["debug_symbols"])
        entry["warnings"] = [
            *warnings,
            f"⚠️ 调试运行：只用了 {len(stats['debug_symbols'])} 只标的，"
            f"截面/IC 结论**不可用**于评估因子",
        ]
    return entry


def factor_rel_path(defn: loader.FactorDef) -> str:
    """产物相对 `data/factor/` 的路径（清单里存相对路径，换机器也不失效）。"""
    p = manifest.factor_file(defn)
    try:
        return p.resolve().relative_to(FACTOR_DIR.resolve()).as_posix()
    except ValueError:
        return p.name


def _safe_stat(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) != float("inf") else None


# ======================================================================
# 主流程
# ======================================================================
def _resolve_targets(args: argparse.Namespace) -> list[loader.FactorDef]:
    names = [str(n).strip() for n in (args.name or []) if str(n).strip()]
    if names:
        return [loader.get_factor(n) for n in names]

    category = args.category
    if category:
        cat = loader.category_of(category)
        if cat is None:
            raise AppError(f"未知分类：{category}（已声明 {[c.key for c in loader.categories()]}）")
        if not cat.available:
            raise AppError(
                f"分类「{cat.cname}」当前状态为 {cat.status}，不能计算。"
                f"原因：{cat.note or cat.desc or '未说明'}"
            )
        targets = loader.list_factors(category)
    else:
        # 不指定分类 → 只算 ready 分类下的因子（blocked/planned 静默跳过，
        # 但要在下面打印出来，不能让人以为"全算了"）
        ready = set(loader.available_categories())
        targets = [f for f in loader.list_factors() if f.category in ready]

    if not targets:
        raise AppError("没有匹配到任何因子 —— 请检查 --name / --category")
    return targets


def _print_list() -> None:
    entries = manifest.read_manifest()["factors"]
    print()
    print("=" * 96)
    print("  因子注册表（定义来自 factors/registry.yaml）")
    print("=" * 96)
    for cat in loader.categories():
        items = loader.list_factors(cat.key, include_disabled=True)
        flag = {"ready": "✅", "planned": "🚧", "blocked": "⛔"}.get(cat.status, "?")
        print(f"\n  {flag} [{cat.key}] {cat.cname} —— {cat.status}，{len(items)} 个因子")
        if not items:
            note = (cat.note or cat.desc or "").strip().replace("\n", " ")
            print(f"         （无因子）{note[:110]}")
            continue
        print(f"      {'因子':<16s}{'中文名':<20s}{'方向':>4s}  {'行数':>10s}  {'覆盖区间':<24s}{'算于'}")
        for f in items:
            e = entries.get(f.name)
            if e:
                interval = f"{e.get('date_start') or '?'} ~ {e.get('date_end') or '?'}"
                rows = f"{e.get('rows') or 0:,}"
                at = (e.get("computed_at") or "")[:19].replace("T", " ")
                mark = " ⚠️调试" if e.get("debug_symbols") else ""
                if e.get("warnings"):
                    mark += " ⚠️告警"
            else:
                interval, rows, at, mark = "（未计算）", "-", "-", ""
            print(
                f"      {f.name:<16s}{f.cname:<20s}{f.direction:>4d}  {rows:>10s}  "
                f"{interval:<24s}{at}{mark}"
            )
    print()
    print(f"  清单：{relative_to_project(manifest.FACTOR_MANIFEST_FILE)}")
    print("=" * 96)
    print()


def _print_check() -> int:
    rows = loader.check(with_smoke=True)
    bad = [r for r in rows if r["status"] != "ok"]
    print()
    print("=" * 84)
    print(f"  因子自检（导入 + 签名 + 冒烟调用）｜ok={len(rows) - len(bad)} broken={len(bad)}")
    print("=" * 84)
    for r in rows:
        flag = "✅" if r["status"] == "ok" else "❌"
        print(f"  {flag} {r['name']:<16s} [{r['category']:<11s}] {r['impl']}")
        if r["message"]:
            print(f"       └─ {r['message']}")
    print("=" * 84)
    print()
    return 1 if bad else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m factors.calc",
        description="模块 2 · 因子计算通道（CLI）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--name", action="append", default=None, help="因子名，可重复；与 --category 二选一")
    p.add_argument("--category", default=None, help="因子分类：daily | minute | fundamental")
    p.add_argument("--start", default=None, help="起始日 YYYY-MM-DD（默认取 config.data.start_date）")
    p.add_argument("--end", default=None, help="结束日 YYYY-MM-DD（默认到今天）")
    p.add_argument("--force", action="store_true", help="忽略已有产物，全部重算")
    p.add_argument("--symbols", default=None, help="逗号分隔的标的，⚠️ 调试模式，结论不可用")
    p.add_argument("--analysis", action="store_true", help="算完顺带跑分层回测 + RankIC")
    p.add_argument("--groups", type=int, default=5, help="--analysis 的分组数（默认 5）")
    p.add_argument("--horizon", type=int, default=1, help="--analysis 的前瞻持有天数（默认 1）")
    p.add_argument("--list", action="store_true", help="列出注册表与已有产物后退出")
    p.add_argument("--check", action="store_true", help="自检（不读行情，不落盘）后退出")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging()

    if args.list:
        _print_list()
        return 0
    if args.check:
        return _print_check()

    try:
        targets = _resolve_targets(args)
    except AppError as exc:
        logger.error(exc.message)
        return 2

    start = args.start or settings.data.start_date
    debug_symbols = [s.strip() for s in (args.symbols or "").split(",") if s.strip()] or None
    if args.end:
        # 显式给了终点就上锁 —— 越界会抛 ReadUntilViolation，让 bug 大声暴露
        data_store.set_read_until(args.end)

    print()
    print("=" * 96)
    print(f"  因子计算｜{len(targets)} 个因子｜请求区间 {start} ~ {args.end or '今天'}")
    if debug_symbols:
        print(f"  ⚠️ 调试模式：只算 {len(debug_symbols)} 只标的（{debug_symbols}），结论不可用于评估因子")
    print("=" * 96)

    try:
        t_read = time.monotonic()
        q, stats = prepare_quotes(start, args.end, debug_symbols)
        stats["requested_start"] = start
        stats["requested_end"] = args.end
        if debug_symbols:
            stats["debug_symbols"] = debug_symbols
        logger.info(
            f"行情就绪：{stats['rows']:,} 行 × {stats['symbols']} 只"
            f"（{stats['date_start']} ~ {stats['date_end']}）"
            f"｜剔除停牌/无效 {stats['rows_dropped_invalid']:,} 行"
            f"｜读取 {time.monotonic() - t_read:.1f}s"
        )
    except AppError as exc:
        logger.error(f"读取行情失败：{exc.message}")
        return 2

    entries = manifest.read_manifest()["factors"]
    computed: list[str] = []
    skipped: list[tuple[str, str]] = []
    failed: list[tuple[str, str]] = []
    t_all = time.monotonic()

    for i, defn in enumerate(targets, 1):
        if not args.force:
            reasons = manifest.stale_reasons(
                defn,
                entries.get(defn.name),
                start=stats["date_start"],
                end=stats["date_end"],
            )
            if not reasons:
                skipped.append((defn.name, "产物已覆盖区间且参数未变"))
                print(f"  [{i:>2}/{len(targets)}] ⏭  {defn.name:<16s} 跳过（--force 可强制重算）")
                continue

        try:
            entry = compute_one(defn, q, stats)
        except Exception as exc:
            failed.append((defn.name, f"{type(exc).__name__}: {exc}"))
            print(f"  [{i:>2}/{len(targets)}] ❌ {defn.name:<16s} 失败：{exc}")
            logger.exception(f"因子 {defn.name} 计算失败")
            continue

        # ★ 先落盘产物、再写清单条目。反过来会出现"清单说算过了、文件却不在"
        #   —— 与 sweep 报告里 detail_written 的教训同源：状态必须跟着产物走。
        manifest.upsert(defn.name, entry)
        computed.append(defn.name)
        warn = " ⚠️ " + "；".join(entry["warnings"]) if entry["warnings"] else ""
        print(
            f"  [{i:>2}/{len(targets)}] ✅ {defn.name:<16s} "
            f"{entry['rows']:>9,} 行｜非空 {entry['non_null'] / max(entry['rows'], 1):>6.1%}｜"
            f"{entry['seconds']:>6.2f}s｜{entry['size_bytes'] / 1e6:>6.1f} MB{warn}"
        )

    elapsed = time.monotonic() - t_all
    print("-" * 96)
    print(
        f"  完成：新算 {len(computed)}｜跳过 {len(skipped)}｜失败 {len(failed)}"
        f"｜总耗时 {elapsed:.1f}s｜产物目录 {relative_to_project(FACTOR_DIR)}"
    )
    if failed:
        for name, reason in failed:
            print(f"    ❌ {name}: {reason}")
    print("=" * 96)
    print()

    if args.analysis and computed:
        from factors import analyze  # noqa: PLC0415 - 只在需要时导入

        names = list(computed) if not args.name else [f.name for f in targets if f.name in set(computed)]
        print(f"  → 开始分层回测 + RankIC（groups={args.groups}, horizon={args.horizon}）")
        analyze.run(
            names=names,
            start=start,
            end=stats["date_end"],
            groups=args.groups,
            horizon=args.horizon,
            debug_symbols=debug_symbols,
        )

    # 有失败就给出非零退出码 —— 但**不是 1**：1 在别处表示"结果完好但打印崩了"
    # （见 tools/backfill_quotes 与 relative_to_project 的教训）。这里用 3。
    return 3 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
