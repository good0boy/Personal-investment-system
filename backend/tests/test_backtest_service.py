"""模块 3 回测读取服务（`backtest_service`）的回归测试。

★ 这里守的是**"文件契约 → API 响应"这一段**的四个脆点：

1. **JSON 里的 NaN/Infinity**：Python 能读写，浏览器 `JSON.parse` 直接抛
   `SyntaxError`，一个 NaN 让详情页整体白屏。所以响应里绝不允许出现
   非有限浮点字面量。
2. **空表**：零成交是正常结果（`mock-empty` 就是），但 `pd.DataFrame()`
   是 0 行 0 列，消费者一取列就 KeyError。
3. **缺文件**：进程被杀会留下没有 `metrics.json` 的目录。列表页必须能
   展示它并标成 `incomplete`，而不是整个 500。
4. **调试运行的伪装**：只跑了 3 只标的的 run，产物结构与全市场 run 一致。
   必须带 `debug_symbols` 与醒目 notes，否则会被当成策略结论。

另外还有两条排序/对齐语义：
  · 无指标的 run **永远排最后**（不然"没跑完"会霸榜看起来像收益最好）
  · 多 run 对比按日期**并集**对齐，区间外为 None（不是交集）
"""
from __future__ import annotations

import json

import pandas as pd
import pytest

from app.core.errors import NotFoundError
from app.services import backtest_service as svc
from app.services import data_store
from strategies.base import RunLayout, write_run

STRATEGY = "momentum"


# ======================================================================
# 夹具
# ======================================================================
@pytest.fixture
def bt_dir(tmp_path, monkeypatch):
    """把回测根目录指向临时目录，并清掉扫盘缓存。"""
    monkeypatch.setattr(data_store, "BACKTEST_DIR", tmp_path / "backtest")
    svc.invalidate_cache()
    yield tmp_path / "backtest"
    svc.invalidate_cache()


def _config(
    *,
    start: str = "2024-01-02",
    end: str = "2024-06-28",
    is_out_sample: bool = False,
    debug_symbols: list[str] | None = None,
) -> dict:
    return {
        "strategy": {
            "name": STRATEGY,
            "version": "1.2",
            "benchmark": "sh.000001",
            "params": {"hold_days_max": 10, "take_profit_gross_main": 0.10},
        },
        "interval": {
            "name": "in_sample",
            "start": start,
            "end": end,
            "preheat_start": "2023-08-01",
            "is_out_sample": is_out_sample,
        },
        "capital": {"initial_cash": 1_000_000.0, "max_positions": 5},
        "universe": {"listed_days_min": 90},
        "cost": {"commission_rate": 0.00025},
        "data": {"adjust": "hfq"},
        "debug_symbols": debug_symbols,
    }


def _equity(n: int = 40, with_benchmark: bool = True) -> pd.DataFrame:
    dates = pd.bdate_range("2024-01-02", periods=n)
    nav = [1.0 + 0.01 * i for i in range(n)]
    peak = 0.0
    dd = []
    for v in nav:
        peak = max(peak, v)
        dd.append(v / peak - 1.0)
    data = {
        "date": dates,
        "cash": [500_000.0] * n,
        "market_value": [500_000.0] * n,
        "total_asset": [1_000_000.0 * v for v in nav],
        "position_count": [3] * n,
        "nav": nav,
        "drawdown": dd,
    }
    if with_benchmark:
        data["benchmark_nav"] = [1.0 + 0.002 * i for i in range(n)]
    return pd.DataFrame(data)


def _trades(*, wins: int = 2, loses: int = 3) -> pd.DataFrame:
    rows = []
    for i in range(wins + loses):
        win = i < wins
        entry = 10.0
        exit_ = 11.5 if win else 9.4
        shares = 1000
        entry_amount = entry * shares
        exit_amount = exit_ * shares
        cost = 60.0
        gross = exit_amount - entry_amount
        net = gross - cost
        rows.append(
            {
                "symbol": f"sh.60000{i % 10}",
                "entry_date": f"2024-0{1 + i // 5}-{2 + i:02d}",
                "entry_price": entry,
                "shares": shares,
                "entry_amount": entry_amount,
                "entry_cost": 30.0,
                "exit_date": f"2024-0{1 + i // 5}-{12 + i:02d}",
                "exit_price": exit_,
                "exit_amount": exit_amount,
                "exit_cost": 30.0,
                "cost": cost,
                "gross_pnl": gross,
                "net_pnl": net,
                "net_return": net / (entry_amount + 30.0),
                "hold_days": 10,
                "entry_reason": "momentum_top",
                "exit_reason": "take_profit" if win else "time_stop",
            }
        )
    return pd.DataFrame(rows)


def _make_run(
    root,
    run_id: str,
    *,
    tag: str = "",
    metrics: dict | None = None,
    write_metrics: bool = True,
    equity: pd.DataFrame | None = None,
    trades: pd.DataFrame | None = None,
    debug_symbols: list[str] | None = None,
    strategy: str = STRATEGY,
) -> None:
    """按真实契约落一份产物（走 `write_run`，不手搓 parquet）。"""
    layout = RunLayout(strategy=strategy, run_id=run_id, root=root / strategy / run_id)
    eq = _equity() if equity is None else equity
    tr = _trades() if trades is None else trades
    if metrics is None:
        metrics = {
            "trading_days": len(eq),
            "total_return": 0.12,
            "annual_return": 0.25,
            "max_drawdown": -0.08,
            "sharpe": 1.4,
            "win_rate": 0.4,
            "trade_count": len(tr),
            "annual_cost_ratio": 0.064,
            "max_single_loss": -0.11,
        }
    write_run(
        layout,
        config=_config(debug_symbols=debug_symbols),
        equity=eq,
        trades=tr,
        metrics=metrics,
        tag=tag,
    )
    # 缺 metrics.json 的场景：落盘后手动删掉（模拟"进程被杀"）
    if not write_metrics:
        layout.metrics_path.unlink()


# ======================================================================
# 列表
# ======================================================================
def test_empty_dir_returns_empty_not_error(bt_dir):
    """没有任何回测产物时，列表页要能正常打开（而不是 500）。"""
    out = svc.list_runs()
    assert out["total"] == 0
    assert out["items"] == []
    assert out["strategies"], "策略列表应当仍有内容（注册表与产物无关）"


def test_list_reads_tag_and_interval_from_artifact(bt_dir):
    """★ `tag` 必须从**产物**里读出来，不能只依赖 meta.db。

    此前 tag 只写进数据库，扫目录这条读取路径拿不到它 ——
    于是服务层被迫依赖 DB 状态，而目录才是真相源。
    """
    _make_run(bt_dir, "r1", tag="baseline")
    item = svc.list_runs()["items"][0]
    assert item["tag"] == "baseline"
    assert item["start_date"] == "2024-01-02"
    assert item["end_date"] == "2024-06-28"
    assert item["benchmark"] == "sh.000001"
    assert item["strategy_version"] == "1.2"
    assert item["has_metrics"] is True
    assert item["status"] == svc.STATUS_SUCCESS


def test_missing_metrics_is_incomplete_and_still_listed(bt_dir):
    _make_run(bt_dir, "r-broken", write_metrics=False)
    item = svc.list_runs()["items"][0]
    assert item["has_metrics"] is False
    assert item["status"] in (svc.STATUS_INCOMPLETE, svc.STATUS_RUNNING)


def test_broken_json_file_does_not_break_listing(bt_dir):
    """半截 JSON（进程被杀时正在写）按缺失处理，不能拖垮整个列表页。"""
    _make_run(bt_dir, "r-ok")
    bad = bt_dir / STRATEGY / "r-corrupt"
    bad.mkdir(parents=True)
    (bad / "config.json").write_text('{"interval": {"start": "2024', encoding="utf-8")

    out = svc.list_runs()
    assert out["total"] == 2, "坏 run 仍应出现在列表里（只是信息不全）"


def test_sort_puts_missing_metric_last(bt_dir):
    """★ 无该指标的 run 永远排最后 —— 否则"没跑完"会霸榜，看起来像收益最好。"""
    _make_run(bt_dir, "r-low", metrics={"total_return": 0.05, "trade_count": 1})
    _make_run(bt_dir, "r-none", write_metrics=False)
    _make_run(bt_dir, "r-high", metrics={"total_return": 0.42, "trade_count": 1})

    ids = [r["run_id"] for r in svc.list_runs(sort="total_return", desc=True)["items"]]
    assert ids == ["r-high", "r-low", "r-none"]

    ids_asc = [r["run_id"] for r in svc.list_runs(sort="total_return", desc=False)["items"]]
    assert ids_asc == ["r-low", "r-high", "r-none"], "升序时 None 同样在最后"


def test_unknown_sort_field_falls_back_to_created_at(bt_dir):
    _make_run(bt_dir, "r1")
    out = svc.list_runs(sort="not_a_field")
    assert out["total"] == 1 and out["sort"] == "not_a_field"


# ======================================================================
# JSON 合法性（前端白屏的头号原因）
# ======================================================================
def test_metrics_with_nan_is_cleaned_before_serving(bt_dir):
    """★ NaN / Infinity 绝不能出现在响应里。

    Python 能读写它们，浏览器 `JSON.parse` 会抛 `SyntaxError` —— 一个 NaN
    让整个详情页打不开，代价极不对称。
    """
    nan = float("nan")
    inf = float("inf")
    _make_run(
        bt_dir,
        "r-nan",
        metrics={
            "trading_days": 40,
            "total_return": nan,
            "annual_return": inf,
            "max_drawdown": -inf,
            "sharpe": None,
            "trade_count": 3,
        },
    )
    detail = svc.get_run(STRATEGY, "r-nan")
    rendered = json.dumps(detail, ensure_ascii=False)

    assert "NaN" not in rendered
    assert "Infinity" not in rendered
    assert detail["metrics"]["total_return"] is None
    assert detail["metrics"]["annual_return"] is None
    assert detail["metrics"]["max_drawdown"] is None


def test_written_metrics_file_is_valid_json(bt_dir):
    """落盘那一刻就要保证合法（不只是响应层净化）。"""
    _make_run(bt_dir, "r-nan2", metrics={"total_return": float("nan"), "trade_count": 0})
    raw = (bt_dir / STRATEGY / "r-nan2" / "metrics.json").read_text(encoding="utf-8")
    assert "NaN" not in raw
    assert json.loads(raw)["total_return"] is None


# ======================================================================
# 详情
# ======================================================================
def test_get_run_not_found_raises(bt_dir):
    _make_run(bt_dir, "r1")
    with pytest.raises(NotFoundError):
        svc.get_run(STRATEGY, "does-not-exist")


def test_detail_returns_files_and_rows(bt_dir):
    _make_run(bt_dir, "r1")
    d = svc.get_run(STRATEGY, "r1")
    names = {f["name"] for f in d["files"]}
    assert {"config.json", "metrics.json", "equity.parquet", "trades.parquet"} <= names
    assert all(f["bytes"] > 0 for f in d["files"])
    assert d["rows"]["equity"] == 40
    assert d["rows"]["trades"] == 5


def test_debug_run_is_flagged_loudly(bt_dir):
    """★ 调试运行必须被标出来 —— 它与全市场 run 的产物结构一模一样。"""
    _make_run(bt_dir, "r-debug", debug_symbols=["sh.600519"])
    d = svc.get_run(STRATEGY, "r-debug")
    assert d["debug_symbols"] == ["sh.600519"]
    levels = {n["level"] for n in d["notes"]}
    assert "danger" in levels, f"调试运行必须给 danger 级提示，实际 {d['notes']}"


def test_out_sample_run_is_flagged(bt_dir):
    layout = RunLayout(strategy=STRATEGY, run_id="r-oos", root=bt_dir / STRATEGY / "r-oos")
    write_run(
        layout,
        config=_config(is_out_sample=True),
        equity=_equity(),
        trades=_trades(),
        metrics={"trading_days": 40, "trade_count": 5},
    )
    d = svc.get_run(STRATEGY, "r-oos")
    assert d["is_out_sample"] is True
    assert any("样本外" in n["text"] for n in d["notes"])


def test_monthly_returns_starts_from_nav_of_first_month(bt_dir):
    """首月收益不能缺 —— 起点净值恒为 1，所以首月 = 该月末净值 - 1。"""
    _make_run(bt_dir, "r1", equity=_equity(60))  # 覆盖 3 个月
    monthly = svc.get_run(STRATEGY, "r1")["monthly_returns"]
    assert len(monthly) >= 2
    assert monthly[0]["return"] is not None, "首月收益不能是 None"
    assert monthly[0]["year"] == 2024 and monthly[0]["month"] == 1


# ======================================================================
# 净值
# ======================================================================
def test_equity_exposes_all_chart_columns(bt_dir):
    """★ `benchmark_nav` 必须真的落盘。

    它一度被 `ensure_contract` 的列白名单静默丢掉 —— `equity_curve()` 算了，
    文件里却没有，前端"策略 vs 基准"双线于是永远画不出来。
    """
    _make_run(bt_dir, "r1")
    d = svc.get_equity(STRATEGY, "r1")
    assert set(d["series"]) == {
        "date", "nav", "benchmark_nav", "drawdown", "cash", "market_value", "position_count",
    }
    assert len(d["series"]["date"]) == 40
    assert d["downsampled"] is False


def test_equity_downsampled_when_too_many_points(bt_dir):
    """★ 降采样的端到端路径（这里曾是"写了但从未被调用"的死代码，
    第一次真正调用时抛 `IndexError: positional indexers are out-of-bounds`）。"""
    _make_run(bt_dir, "r-long", equity=_equity(1200))
    d = svc.get_equity(STRATEGY, "r-long", max_points=200)
    s = d["series"]

    assert d["count"] == 200
    assert d["downsampled_from"] == 1200
    assert d["downsampled"] is True
    assert len(s["nav"]) == 200
    assert s["date"][0] == "2024-01-02", "首点必须保留"
    assert s["date"] == sorted(s["date"]), "日期必须单调递增"


def test_equity_missing_file_raises(bt_dir):
    from app.core.errors import DataNotReadyError

    _make_run(bt_dir, "r1")
    (bt_dir / STRATEGY / "r1" / "equity.parquet").unlink()
    with pytest.raises(DataNotReadyError):
        svc.get_equity(STRATEGY, "r1")


# ======================================================================
# 交易记录
# ======================================================================
def test_trades_summary_covers_all_filtered_rows_not_page(bt_dir):
    """★ `summary` 统计**筛选后的全量**，不是当前页 —— 否则翻页时数字会变。"""
    _make_run(bt_dir, "r1")
    out = svc.get_trades(STRATEGY, "r1", page=1, page_size=2)
    assert out["total"] == 5
    assert len(out["items"]) == 2
    assert out["summary"]["trade_count"] == 5, "统计口径应为全量 5 笔，而非当页 2 笔"
    assert out["pages"] == 3


def test_trades_filter_by_win(bt_dir):
    _make_run(bt_dir, "r1")
    out = svc.get_trades(STRATEGY, "r1", win="win")
    assert out["total"] == 2
    assert all(t["net_pnl"] > 0 for t in out["items"])


def test_trades_filter_by_exit_reason(bt_dir):
    _make_run(bt_dir, "r1")
    out = svc.get_trades(STRATEGY, "r1", exit_reasons=["time_stop"])
    assert out["total"] == 3
    assert all("time_stop" in t["exit_reason"] for t in out["items"])
    assert set(out["filters"]["exit_reasons"]) == {"time_stop", "take_profit"}


def test_trades_multi_value_reason_is_matched_by_part(bt_dir):
    """`exit_reason` 可能是 `time_stop|take_profit`，按 `|` 拆开逐个匹配。

    夹具：5 笔 = 2 笔 take_profit + 3 笔 time_stop；把第 1 笔改成双值
    `time_stop|take_profit`。于是
      · 筛 take_profit → 2 笔（该笔 + 原本那笔）
      · 筛 time_stop   → 4 笔（原本 3 笔 + 该笔）
    若实现是按整串相等匹配，会得到 1 / 3 —— 数字不同，正好暴露实现方式。
    """
    tr = _trades()
    tr.loc[0, "exit_reason"] = "time_stop|take_profit"
    _make_run(bt_dir, "r1", trades=tr)

    assert svc.get_trades(STRATEGY, "r1", exit_reasons=["take_profit"])["total"] == 2
    assert svc.get_trades(STRATEGY, "r1", exit_reasons=["time_stop"])["total"] == 4
    assert svc.get_trades(STRATEGY, "r1", exit_reasons=["market_panic"])["total"] == 0


def test_empty_trades_returns_zero_not_error(bt_dir):
    """★ 零成交是正常结果（`mock-empty` 就是），不能让前端崩。"""
    _make_run(bt_dir, "r-empty", trades=_trades(wins=0, loses=0), metrics={"trade_count": 0})
    out = svc.get_trades(STRATEGY, "r-empty")
    assert out["total"] == 0
    assert out["items"] == []
    assert out["summary"]["trade_count"] == 0
    assert out["summary"]["win_rate"] is None, "零成交时胜率应为 None 而不是 NaN"


def test_trades_page_size_is_clamped(bt_dir):
    _make_run(bt_dir, "r1")
    out = svc.get_trades(STRATEGY, "r1", page=1, page_size=99999)
    assert out["page_size"] == 500


# ======================================================================
# 对比
# ======================================================================
def test_compare_aligns_on_union_of_dates(bt_dir):
    """★ 用**并集**对齐：区间外的 run 留 None，前端断线。

    若用交集，"全区间基线 vs 半年期新版本"的对比会退化成只剩半年，
    看的人会以为两条线从同一天开始。
    """
    _make_run(bt_dir, "r-long", equity=_equity(60))
    _make_run(bt_dir, "r-short", equity=_equity(20))

    out = svc.compare_runs([(STRATEGY, "r-long"), (STRATEGY, "r-short")])
    assert len(out["dates"]) == 60
    long_s, short_s = out["series"][0]["nav"], out["series"][1]["nav"]
    assert all(v is not None for v in long_s)
    assert sum(1 for v in short_s if v is not None) == 20
    assert all(v is None for v in short_s[20:]), "区间外必须是 None（前端断线），不能补数"


def test_compare_caps_number_of_series(bt_dir):
    for i in range(svc.MAX_COMPARE + 3):
        _make_run(bt_dir, f"r{i}")
    refs = [(STRATEGY, f"r{i}") for i in range(svc.MAX_COMPARE + 3)]
    assert svc.compare_runs(refs)["count"] == svc.MAX_COMPARE


def test_compare_with_empty_refs(bt_dir):
    assert svc.compare_runs([]) == {"dates": [], "series": [], "count": 0}


# ======================================================================
# 委托流水
# ======================================================================
def test_orders_filter_by_status(bt_dir):
    _make_run(bt_dir, "r1")
    all_orders = svc.get_orders(STRATEGY, "r1")
    assert all_orders["total"] == 0 or "columns" in all_orders

    # 手工塞一份 orders（`write_run` 未传 orders 时是 0 行空表）
    from strategies.base import ORDER_COLUMNS

    rows = pd.DataFrame(
        [
            {
                "signal_date": "2024-01-05", "exec_date": "2024-01-08",
                "symbol": "sh.600519", "side": "sell", "shares": 100, "price": None,
                "amount": None, "status": "pending", "reason": "time_stop",
                "note": "一字跌停无买盘，顺延",
            },
            {
                "signal_date": "2024-01-08", "exec_date": "2024-01-09",
                "symbol": "sh.600519", "side": "sell", "shares": 100, "price": 10.0,
                "amount": 1000.0, "status": "filled", "reason": "time_stop", "note": "",
            },
        ],
        columns=list(ORDER_COLUMNS),
    )
    layout = RunLayout(strategy=STRATEGY, run_id="r-orders", root=bt_dir / STRATEGY / "r-orders")
    write_run(
        layout,
        config=_config(),
        equity=_equity(),
        trades=_trades(),
        orders=rows,
        metrics={"trading_days": 40, "trade_count": 5},
    )

    pending = svc.get_orders(STRATEGY, "r-orders", status="pending")
    assert pending["total"] == 1
    assert pending["items"][0]["note"].startswith("一字跌停")
