"""模块 4 · 模拟仿真的测试。

夹具纪律（conftest 教训）：
- 全部合成数据 + tmp 目录 + tmp meta.db —— 不碰真实 data/，可重复；
- **数量断言前场地必为空**：paper_env 里 db 是全新 tmp 库、quote_dir 是
  全新 tmp 目录，无公共夹具预置数据，无需再手动清场。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.config import settings  # noqa: E402
from app.core.errors import DataNotReadyError, PaperError  # noqa: E402
from app.repos import db as db_module  # noqa: E402
from app.repos import meta_repo  # noqa: E402
from app.services import data_store, paper_service  # noqa: E402
from tests.conftest import SAMPLE_DATES  # noqa: E402


# ======================================================================
# 夹具
# ======================================================================
@pytest.fixture
def paper_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, quote_dir: Path) -> Path:
    """模拟盘测试环境：tmp 行情 + tmp 指数 + tmp meta.db + tmp 产物目录。"""
    # 1) tmp meta.db（连接惰性创建 —— 先重定向再关旧连接）
    db_module.close_connection()
    monkeypatch.setattr(db_module, "META_DB", tmp_path / "meta" / "meta.db")
    conn = db_module.get_connection()
    schema = (BACKEND_ROOT / "sql" / "schema.sql").read_text(encoding="utf-8")
    conn.executescript(schema)

    # 2) 指数目录（择时闸门信号源）：12 天，close=3000+i
    idx_dir = tmp_path / "quote_index"
    idx_dir.mkdir(parents=True, exist_ok=True)
    idx = pd.DataFrame(
        {
            "date": pd.to_datetime(SAMPLE_DATES),
            "symbol": "sh.000001",
            "open": [3000.0 + i for i in range(len(SAMPLE_DATES))],
            "high": [3001.0 + i for i in range(len(SAMPLE_DATES))],
            "low": [2999.0 + i for i in range(len(SAMPLE_DATES))],
            "close": [3000.5 + i for i in range(len(SAMPLE_DATES))],
            "preclose": [2999.5 + i for i in range(len(SAMPLE_DATES))],
            "volume": [1.0e9] * len(SAMPLE_DATES),
            "amount": [1.0e11] * len(SAMPLE_DATES),
            "turn": [0.8] * len(SAMPLE_DATES),
            "pct_chg": [0.5] * len(SAMPLE_DATES),
            "trade_status": [1] * len(SAMPLE_DATES),
            "is_st": [0] * len(SAMPLE_DATES),
        }
    )
    data_store.write_parquet(idx, idx_dir / "sh.000001.parquet")
    monkeypatch.setattr(data_store, "QUOTE_INDEX_DIR", idx_dir)

    # 3) 模拟盘产物目录 + 起始日（夹具数据在 2024 上半年）
    paper_dir = tmp_path / "paper"
    monkeypatch.setattr(paper_service, "PAPER_DIR", paper_dir)
    monkeypatch.setattr(paper_service, "PAPER_START_DATE", "2024-01-01")

    # 4) 数据同步编排必须隔离：单测绝不能真去跑 baostock 子进程。
    #    三个子步骤打成"已跳过"；覆盖率阈值降到 1（夹具只有几只标的，
    #    否则会被 2026-10-08 新增的全市场覆盖率闸门误拦 —— 那是生产语义）。
    import app.services.data_sync_service as data_sync_module

    monkeypatch.setattr(
        data_sync_module, "sync_quotes_incremental",
        lambda: {"step": "行情增量回补", "ok": True, "skipped": True, "reason": "test"},
    )
    monkeypatch.setattr(
        data_sync_module, "sync_universe",
        lambda run_date: {"step": f"股票池快照 {run_date}", "ok": True, "skipped": True, "reason": "test"},
    )
    monkeypatch.setattr(
        data_sync_module, "sync_factors",
        lambda: {"step": "因子重算", "ok": True, "skipped": True, "reason": "test"},
    )
    monkeypatch.setattr(settings.data_sync, "min_daily_symbols", 1)
    return paper_dir


@pytest.fixture
def _close_db_after(paper_env: Path):
    yield
    db_module.close_connection()


# ======================================================================
# 账户
# ======================================================================
@pytest.mark.usefixtures("_close_db_after")
def test_ensure_default_account_idempotent(paper_env: Path) -> None:
    paper_service.ensure_default_account()
    paper_service.ensure_default_account()  # 幂等：INSERT OR IGNORE
    rows = meta_repo.list_paper_accounts()
    assert len(rows) == 1
    assert rows[0]["id"] == "default"
    assert rows[0]["init_cash"] == pytest.approx(1_000_000)


@pytest.mark.usefixtures("_close_db_after")
def test_require_account_404(paper_env: Path) -> None:
    from app.core.errors import NotFoundError

    with pytest.raises(NotFoundError):
        paper_service.require_account("no-such-account")


# ======================================================================
# 跑批：跳过 / 幂等 / 完整链路
# ======================================================================
@pytest.mark.usefixtures("_close_db_after")
def test_run_paper_day_skips_non_trading_day(paper_env: Path) -> None:
    result = paper_service.run_paper_day("2024-01-06")  # 周六
    assert result == {"skipped": True, "reason": "2024-01-06 非交易日"}
    assert not (paper_env / "default").exists()


@pytest.mark.usefixtures("_close_db_after")
def test_run_paper_day_idempotent_skips_when_done(paper_env: Path) -> None:
    """净值已更新到 >= run_date 的日期 → 跳过（防止补跑旧日期覆盖新净值）。"""
    acc = paper_service.ensure_default_account()
    nav = pd.DataFrame({"date": ["2024-06-28"], "nav": [1.0]})
    data_store.write_parquet(nav, paper_env / "default" / "nav.parquet", allow_empty=True)
    result = paper_service.run_paper_day("2024-06-28")
    assert result["skipped"] is True
    assert "已更新至 2024-06-28" in result["reason"]
    assert meta_repo.get_paper_account(acc["id"])["last_run_date"] is None  # 未碰状态


@pytest.mark.usefixtures("_close_db_after")
def test_assert_quotes_ready_raises_when_missing(paper_env: Path, quote_dir: Path) -> None:
    # 场地清空：个股与指数都无数据（quote_dir 保留目录但删文件）
    for f in quote_dir.glob("*.parquet"):
        f.unlink()
    with pytest.raises(DataNotReadyError):
        paper_service.assert_quotes_ready("2024-06-28")


@pytest.mark.usefixtures("_close_db_after")
def test_run_paper_day_full_pipeline(paper_env: Path) -> None:
    """重算 → 落盘四件套 → 更新 last_run_date（0 笔交易也是正常产物）。"""
    result = paper_service.run_paper_day("2024-06-28", trigger="manual")
    assert "skipped" not in result  # 成功路径返回 summary，没有 skipped 键
    assert result["data_as_of"] == "2024-06-28"
    assert result["trade_count"] == 0  # 合成数据不满 20 日动量，无信号 → 0 笔

    out = paper_env / "default"
    assert (out / "nav.parquet").exists()
    assert (out / "trades.parquet").exists()  # ⭐ 0 笔也必须落盘（带列结构）
    assert (out / "positions.parquet").exists()
    assert (out / "config.json").exists()

    nav = data_store.query_duck(f"SELECT * FROM read_parquet('{(out / 'nav.parquet').as_posix()}')")
    assert len(nav) == 6  # SAMPLE_DATES 上半年 6 天
    assert nav["nav"].nunique() == 1  # 无交易 → 净值恒 1.0

    trades = data_store.query_duck(
        f"SELECT * FROM read_parquet('{(out / 'trades.parquet').as_posix()}')"
    )
    assert len(trades) == 0
    assert "symbol" in trades.columns  # 空表带契约列，下游不 KeyError（契约无 side 列，勿臆断）

    assert meta_repo.get_paper_account("default")["last_run_date"] == "2024-06-28"

    cfg = json.loads((out / "config.json").read_text(encoding="utf-8"))
    assert cfg["data_as_of"] == "2024-06-28"
    assert cfg["metrics"]["trade_count"] == 0


@pytest.mark.usefixtures("_close_db_after")
def test_run_paper_day_second_run_appends_incrementally(
    paper_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 增量撮合语义（2026-10-10）：首跑全量（冷启动写 state）→ 再跑更晚日期
    = **逐日补齐**（缺口感知），历史行不动；state.as_of 与 nav 最后日连续。

    日历 monkeypatch 成夹具口径（每月最后一个交易日）—— 真实日历与夹具
    稀疏数据不同轴，不 mock 会触发"回退全量"（那是另一条自愈路径，由
    test_incremental_falls_back_on_calendar_gap 单独验证）。
    """
    from tests.conftest import SAMPLE_DATES

    monkeypatch.setattr(
        paper_service.calendar, "trading_days",
        lambda s, e: [d for d in SAMPLE_DATES if s <= d <= e],
    )
    monkeypatch.setattr(
        paper_service.calendar, "next_trading_day",
        lambda d, n=1: next(x for x in SAMPLE_DATES if x > d),
    )

    paper_service.run_paper_day("2024-03-29")   # 冷启动：state 不存在 → 自动全量
    out = paper_env / "default"
    assert (out / "state.json").exists(), "全量跑批必须落 state.json（增量续跑依据）"
    nav1 = data_store.query_duck(
        f"SELECT * FROM read_parquet('{(out / 'nav.parquet').as_posix()}')"
    )
    n1 = len(nav1)

    paper_service.run_paper_day("2024-06-28")   # 中间隔 04-30、05-31 两个未撮合日
    nav2 = data_store.query_duck(
        f"SELECT * FROM read_parquet('{(out / 'nav.parquet').as_posix()}')"
    )
    assert str(nav2["date"].max())[:10] == "2024-06-28"
    assert len(nav2) == n1 + 3, "增量逐日补齐：+3 行（04-30 / 05-31 / 06-28），历史行不动"
    # 历史行逐位未变（追加语义的硬要求）
    pd.testing.assert_frame_equal(
        nav1.reset_index(drop=True), nav2.iloc[:n1].reset_index(drop=True)
    )
    # state 连续性
    state = json.loads((out / "state.json").read_text(encoding="utf-8"))
    assert state["as_of"] == "2024-06-28"


def test_incremental_falls_back_on_calendar_gap(
    paper_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 自愈路径：真实日历下中间日无夹具数据（跨多日 + 数据缺失）→
    增量撮合抛错 → 自动回退全量重算，跑批不失败、产物完整。"""
    # 不 mock 日历：03-29 → 06-28 之间是真实交易日，但夹具行情只有每月一天
    # → PaperEngine 起点日无数据 → 异常 → 回退全量
    paper_service.run_paper_day("2024-03-29")
    paper_service.run_paper_day("2024-06-28")
    out = paper_env / "default"
    nav = data_store.query_duck(
        f"SELECT * FROM read_parquet('{(out / 'nav.parquet').as_posix()}')"
    )
    assert str(nav["date"].max())[:10] == "2024-06-28"
    assert len(nav) == 6, "回退全量 = 整段重算（6 个夹具交易日）"
    state = json.loads((out / "state.json").read_text(encoding="utf-8"))
    assert state["as_of"] == "2024-06-28"


# ======================================================================
# 只读路径
# ======================================================================
@pytest.mark.usefixtures("_close_db_after")
def test_read_overview_empty(paper_env: Path) -> None:
    paper_service.ensure_default_account()
    overview = paper_service.read_overview("default")
    assert overview["empty"] is True
    assert "23:00" in overview["note"]
    assert overview["job"]["schedule"] is not None  # 空态也带调度信息（前端空态卡片用）


@pytest.mark.usefixtures("_close_db_after")
def test_read_overview_with_data(paper_env: Path) -> None:
    paper_service.run_paper_day("2024-06-28")
    overview = paper_service.read_overview("default")
    assert overview["empty"] is False
    assert overview["data_as_of"] == "2024-06-28"
    assert len(overview["nav_series"]) == 6
    assert overview["latest_positions"] == []  # 无交易 → 无持仓
    assert overview["recent_trades"] == []
    assert overview["metrics"]["trade_count"] == 0
    assert overview["job"]["schedule"] is not None  # 任务状态接通了调度器
    # 直接调 service 没走 job 包装 → 无日志；且必须是 None 而非 list
    assert overview["job"]["last_log"] is None


@pytest.mark.usefixtures("_close_db_after")
def test_list_account_summaries(paper_env: Path) -> None:
    rows = paper_service.list_account_summaries()
    assert len(rows) == 1
    assert rows[0]["id"] == "default"
    assert rows[0]["start_date"] == "2024-01-01"
    assert "nav" not in rows[0]  # 无产物时不带净值字段


# ======================================================================
# 手动触发
# ======================================================================
@pytest.mark.usefixtures("_close_db_after")
def test_trigger_run_now_raises_when_scheduler_off(paper_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """调度器未运行时手动触发 → 明确报错，而不是静默什么都不发生。"""
    paper_service.ensure_default_account()
    with pytest.raises(PaperError, match="调度器未运行"):
        paper_service.trigger_run_now()


def test_paper_account_strategy_switch(paper_env: Path, monkeypatch):
    """★ config.yaml paper.strategy 与账户行不一致 → 自动切换并写入参数。"""
    from app.core.config import settings
    from app.repos import meta_repo

    monkeypatch.setattr(settings.paper, "strategy", "quality_reversal")
    monkeypatch.setattr(
        settings.paper, "params",
        {"ma_rise_confirm_window": 10, "max_positions": 10},
    )

    row = paper_service.ensure_default_account()

    assert row["strategy"] == "quality_reversal"
    import json as _json
    assert _json.loads(row["param_json"])["ma_rise_confirm_window"] == 10
    # 再跑一次幂等（不应报错，也不应重复改）
    row2 = paper_service.ensure_default_account()
    assert row2["strategy"] == "quality_reversal"
    assert meta_repo.get_paper_account("default")["param_json"] == row["param_json"]


def test_paper_strategy_switch_invalidates_artifacts(paper_env: Path, monkeypatch):
    """★ 策略切换必须作废旧策略产物（nav/positions/trades/orders/config）。

    幂等闸（last_nav_date >= run_date → 跳过）只看日期不看策略：
    不删产物的话，旧 momentum 净值会一直展示在前端净值曲线上，
    且 nightly 跑批也会被幂等闸拦住不重算。
    """
    from app.core.config import settings

    pdir = paper_env / "default"     # account_dir = PAPER_DIR / account_id
    pdir.mkdir(parents=True, exist_ok=True)
    for name in ("nav.parquet", "positions.parquet", "trades.parquet", "orders.parquet", "config.json"):
        (pdir / name).write_bytes(b"stale")

    monkeypatch.setattr(settings.paper, "strategy", "quality_reversal")
    monkeypatch.setattr(settings.paper, "params", {"ma_rise_confirm_window": 10})

    paper_service.ensure_default_account()

    leftovers = [f.name for f in pdir.iterdir() if f.name != "placeholder"]
    assert leftovers == [], f"旧策略产物应全部作废，残留：{leftovers}"


@pytest.mark.usefixtures("_close_db_after")
def test_read_overview_nav_consistent_and_open_positions(paper_env: Path) -> None:
    """★ 两项前端契约（主人 2026-10-09）：

    1. 「当前净值」卡 = 净值曲线最后一点（账户行没有 nav 列，此前显示为空）；
       最新交易日未跑批时自然显示前一日净值 —— 与曲线永远一致。
    2. 「最近交易」= 已平仓 ∪ 当前持仓（持仓行卖出日/原因/盈亏为空），
       按买入日降序，最多 100 笔。
    """
    paper_service.ensure_default_account()
    d = paper_env / "default"
    d.mkdir(parents=True, exist_ok=True)

    nav = pd.DataFrame(
        {
            "date": pd.to_datetime(["2024-06-27", "2024-06-28"]),
            "cash": [90.0, 80.0],
            "market_value": [10.0, 20.0],
            "total_asset": [100.0, 100.0],
            "position_count": [1, 1],
            "nav": [1.05, 1.10],
            "drawdown": [0.0, -0.02],
            "benchmark_nav": [1.0, 1.01],
        }
    )
    data_store.write_parquet(nav, d / "nav.parquet", allow_empty=True)

    trades = pd.DataFrame(
        {
            "symbol": ["sh.600000", "sh.600001"],
            "entry_date": pd.to_datetime(["2024-06-20", "2024-06-25"]),
            "entry_price": [10.0, 11.0],
            "shares": [100, 200],
            "exit_date": pd.to_datetime(["2024-06-26", "2024-06-27"]),
            "net_pnl": [5.0, -3.0],
            "net_return": [0.05, -0.03],
            "exit_reason": ["time_stop", "time_stop"],
        }
    )
    data_store.write_parquet(trades, d / "trades.parquet", allow_empty=True)

    positions = pd.DataFrame(
        {
            "date": pd.to_datetime(["2024-06-28"]),
            "symbol": ["sh.600002"],
            "shares": [300],
            "entry_date": pd.to_datetime(["2024-06-27"]),   # 比两笔平仓都新
            "entry_price": [12.0],
            "close": [12.5],
            "market_value": [3750.0],
            "unrealized_pnl": [150.0],
            "hold_days": [1],
        }
    )
    data_store.write_parquet(positions, d / "positions.parquet", allow_empty=True)
    (d / "config.json").write_text("{}", encoding="utf-8")

    ov = paper_service.read_overview("default")

    # 1) 当前净值 = 曲线最后一点
    assert ov["empty"] is False
    assert ov["account"]["nav"] == 1.10
    assert ov["account"]["nav_date"] == "2024-06-28"
    assert ov["account"]["total_asset"] == 100.0

    # 2) 最近交易：2 平仓 + 1 持仓，按买入日降序，持仓行卖出字段为空
    rt = ov["recent_trades"]
    assert [r["symbol"] for r in rt] == ["sh.600002", "sh.600001", "sh.600000"]
    head = rt[0]
    assert head["holding"] is True
    assert head["exit_date"] is None and head["exit_reason"] is None
    assert head["net_pnl"] is None and head["net_return"] is None
