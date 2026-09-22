"""data_store 读取入口的时间锁与退化保护测试。

★ 本文件是 **P0-1 / P0-2 的回归防线**。

背景（2026-09-17 自查报告）：
  · P0-1 时间锁可被绕过 —— 调用点写成 `if end_date: _apply_read_until(...)`，
    `end_date=None` 时整条校验被跳过，实测可读到时间锁之后 6 个月的数据。
    这类 bug **不报错、不警告**，只会让回测"看到未来"后收益虚高，
    所以必须有测试盯着。
  · P0-2 `read_quotes(symbols=[])` 落入全市场 glob 分支 ——
    候选池为空的日子会从"今天不该买"变成"今天买一切"。
"""
from __future__ import annotations

from datetime import date

import pandas as pd
import pytest
from conftest import MID_YEAR_LAST, SAMPLE_DATES

from app.core.errors import DataNotReadyError, ReadUntilViolation
from app.services import data_store

SYM = "sh.600519"
SYM2 = "sz.000001"
FIRST_DAY = SAMPLE_DATES[0]
LAST_DAY = SAMPLE_DATES[-1]
HALF = len([d for d in SAMPLE_DATES if d <= MID_YEAR_LAST])   # 应为 6


def _max_date(df: pd.DataFrame) -> str | None:
    if df is None or df.empty:
        return None
    return pd.to_datetime(df["date"]).max().strftime("%Y-%m-%d")


# ======================================================================
# P0-1 时间锁：end_date=None 不得绕过校验
# ======================================================================
def test_end_none_is_clamped_by_lock(quote_dir):
    """★ P0-1 核心回归：end=None + 锁生效 → 必须收敛到锁，不得返回锁之后的数据。

    修复前：返回全部 12 行（泄漏到 2024-12-31）。
    修复后：收敛到 2024-06-28，返回 6 行。
    """
    with data_store.read_until(MID_YEAR_LAST):
        df = data_store.read_quotes(SYM, FIRST_DAY, None)

    assert not df.empty, "应当返回锁内数据，而不是空"
    assert len(df) == HALF, f"锁 {MID_YEAR_LAST} 内应为 {HALF} 行，实际 {len(df)} 行"
    assert _max_date(df) == MID_YEAR_LAST
    assert _max_date(df) <= MID_YEAR_LAST


def test_end_none_fully_open_when_no_lock(quote_dir):
    """无锁时 end=None 仍应返回全量（修复不能改变正常行为）。"""
    data_store.set_read_until(None)
    df = data_store.read_quotes(SYM, FIRST_DAY, None)
    assert len(df) == len(SAMPLE_DATES)
    assert _max_date(df) == LAST_DAY


def test_load_symbol_history_is_clamped(quote_dir):
    """load_symbol_history 是另一条读取入口，同样必须过锁。"""
    with data_store.read_until(MID_YEAR_LAST):
        df = data_store.load_symbol_history(SYM)

    assert len(df) == HALF
    assert _max_date(df) == MID_YEAR_LAST


def test_both_bounds_omitted_still_locked(quote_dir):
    """start 与 end 都不传时，也必须收敛（这是最容易被漏掉的调用形态）。"""
    with data_store.read_until(MID_YEAR_LAST):
        df = data_store.read_quotes(SYM)
    assert len(df) == HALF


def test_explicit_future_end_raises(quote_dir):
    """显式索要锁之后的数据 → 必须大声报错，而不是静默截断。"""
    with data_store.read_until(MID_YEAR_LAST), pytest.raises(ReadUntilViolation):
        data_store.read_quotes(SYM, FIRST_DAY, LAST_DAY)


def test_explicit_end_equal_to_lock_is_allowed(quote_dir):
    """end 恰好等于锁 → 放行（边界不应误伤）。"""
    with data_store.read_until(MID_YEAR_LAST):
        df = data_store.read_quotes(SYM, FIRST_DAY, MID_YEAR_LAST)
    assert len(df) == HALF


def test_lock_context_manager_restores_previous(quote_dir):
    """时间锁上下文退出后必须恢复原值，不能永久污染。"""
    data_store.set_read_until("2024-03-29")
    with data_store.read_until(MID_YEAR_LAST):
        assert data_store.get_read_until() == MID_YEAR_LAST
    assert data_store.get_read_until() == "2024-03-29"

    # 恢复后仍按原锁生效
    df = data_store.read_quotes(SYM, None, None)
    assert _max_date(df) == "2024-03-29"


def test_lock_not_leaked_after_violation(quote_dir):
    """抛异常后锁状态不能泄漏（异常路径同样要走 finally）。"""
    with pytest.raises(ReadUntilViolation), data_store.read_until(MID_YEAR_LAST):
        data_store.read_quotes(SYM, FIRST_DAY, LAST_DAY)
    assert data_store.get_read_until() is None


def test_read_factor_clamped_by_lock(factor_dir):
    """因子也是行情衍生数据，同样受锁约束。"""
    df = pd.DataFrame(
        {
            "date": pd.to_datetime(SAMPLE_DATES),
            "symbol": [SYM] * len(SAMPLE_DATES),
            "value": list(range(len(SAMPLE_DATES))),
        }
    )
    data_store.save_factor("amp5", df, category="daily")

    with data_store.read_until(MID_YEAR_LAST):
        out = data_store.read_factor("amp5", FIRST_DAY, None)
    assert len(out) == HALF

    with data_store.read_until(MID_YEAR_LAST), pytest.raises(ReadUntilViolation):
        data_store.read_factor("amp5", FIRST_DAY, LAST_DAY)


def test_read_factor_missing_raises_not_ready(factor_dir):
    with pytest.raises(DataNotReadyError):
        data_store.read_factor("no_such_factor")


def test_read_universe_respects_lock(universe_dir):
    """股票池快照隐含"某日哪些股票可交易"，属未来信息，必须过锁。"""
    data_store.save_universe(LAST_DAY, [SYM, SYM2])

    with data_store.read_until(MID_YEAR_LAST), pytest.raises(ReadUntilViolation):
        data_store.read_universe(LAST_DAY)

    with data_store.read_until(MID_YEAR_LAST):
        assert data_store.read_universe(FIRST_DAY) == []   # 未落盘 → 空，且不报错


# ======================================================================
# P0-3 "今天"是兜底上界 —— 即使调用方忘了设锁也越不过去
#
# 背景（2026-09-18）：交易日历的覆盖被拉到 2030-12-31（日历是日程表，
# 允许看到未来），于是"忘了设锁的回测/仿真"就成了唯一的防线缺口。
# 若读取上界只看显式锁，一个忘记设锁的调用就能读到尚未发生的数据
# **而不报任何错** —— 本项目最怕的静默失效。
# ======================================================================
FUTURE_DAY = "2030-01-01"


def test_explicit_future_end_raises_even_without_lock(quote_dir):
    """★ 回归：无锁时显式索要未来日期，仍必须抛 ReadUntilViolation。"""
    data_store.set_read_until(None)
    with pytest.raises(ReadUntilViolation):
        data_store.read_quotes(SYM, FIRST_DAY, FUTURE_DAY)


def test_end_none_is_clamped_to_today_without_lock(quote_dir):
    """无锁时 end=None → 收敛到今天（不是"不限"）。"""
    data_store.set_read_until(None)
    with pytest.raises(ReadUntilViolation):
        # 显式索要 2030 年区间必须报错
        data_store.read_quotes(SYM, FIRST_DAY, FUTURE_DAY)

    _, bound = data_store._apply_read_until(FIRST_DAY, None)
    assert bound == date.today().strftime("%Y-%m-%d")


def test_lock_later_than_today_still_clamped_by_today(quote_dir):
    """★ 上界 = min(锁, 今天)：锁设成未来时，"今天"才是真正的天花板。"""
    data_store.set_read_until("2027-01-01")
    with pytest.raises(ReadUntilViolation):
        data_store.read_quotes(SYM, FIRST_DAY, "2027-01-01")

    _, bound = data_store._apply_read_until(FIRST_DAY, None)
    assert bound == date.today().strftime("%Y-%m-%d")


def test_future_universe_day_raises_without_lock(universe_dir):
    """单日读取的派生数据同样被"今天"挡住。"""
    data_store.set_read_until(None)
    data_store.save_universe(FUTURE_DAY, [SYM])
    with pytest.raises(ReadUntilViolation):
        data_store.read_universe(FUTURE_DAY)


def test_trade_calendar_is_exempt_from_lock_and_today(tmp_path, monkeypatch):
    """★ 交易日历**刻意**不过锁、也不受"今天"约束 —— 它是日程表。

    必须能读到未来，否则回测在样本最后一个交易日算 T+1 成交日就会崩：
        next_trading_day("2024-12-31")  # 需要 2025-01-02 存在
    """
    path = tmp_path / "trade_calendar.parquet"
    pd.DataFrame(
        {
            "date": pd.to_datetime([FIRST_DAY, LAST_DAY, FUTURE_DAY]),
            "is_trading_day": [1, 1, 1],
        }
    ).to_parquet(path, index=False)
    monkeypatch.setattr(data_store, "TRADE_CALENDAR_PARQUET", path)

    data_store.set_read_until(MID_YEAR_LAST)
    df = data_store.read_trade_calendar()

    assert len(df) == 3, "日历读取得能看到全部日期（含未来），不受锁/今天约束"


# ======================================================================
# P0-2 空标的列表不得退化为全市场
# ======================================================================
def test_empty_symbol_list_returns_empty(quote_dir):
    """★ P0-2 核心回归：`[]` 必须返回空，绝不可退化成"读全市场"。"""
    df = data_store.read_quotes([], FIRST_DAY, LAST_DAY)
    assert df.empty, f"空列表应返回空，实际返回 {len(df)} 行"


def test_empty_symbol_list_keeps_column_names(quote_dir):
    df = data_store.read_quotes(
        [], FIRST_DAY, LAST_DAY, columns=["date", "symbol", "close"]
    )
    assert df.empty
    assert list(df.columns) == ["date", "symbol", "close"]


def test_empty_symbol_list_still_respects_lock(quote_dir):
    """空列表也要过锁 —— 不能因为"没数据"就跳过校验（保持路径一致）。"""
    with data_store.read_until(MID_YEAR_LAST), pytest.raises(ReadUntilViolation):
        data_store.read_quotes([], FIRST_DAY, LAST_DAY)


def test_none_means_all_market(quote_dir):
    """`None` 才是全市场语义，必须与 `[]` 区分开。"""
    df = data_store.read_quotes(None, FIRST_DAY, LAST_DAY)
    assert set(df["symbol"].unique()) == {SYM, SYM2}
    assert len(df) == len(SAMPLE_DATES) * 2


def test_single_string_symbol_is_accepted(quote_dir):
    df = data_store.read_quotes(SYM, FIRST_DAY, LAST_DAY)
    assert set(df["symbol"].unique()) == {SYM}


def test_missing_symbol_file_returns_empty_with_columns(quote_dir):
    df = data_store.read_quotes("sh.999999", FIRST_DAY, LAST_DAY, columns=["date", "close"])
    assert df.empty
    assert list(df.columns) == ["date", "close"]


# ======================================================================
# 读写基本一致性
# ======================================================================
def test_write_read_roundtrip(tmp_path):
    df = pd.DataFrame(
        {
            "date": pd.to_datetime(["2024-01-02", "2024-01-03"]),
            "symbol": ["sh.600000", "sh.600000"],
            "close": [10.0, 11.0],
        }
    )
    path = tmp_path / "x.parquet"
    info = data_store.write_parquet(df, path)
    assert info["written"] is True
    assert info["rows"] == 2
    back = pd.read_parquet(path)
    assert back["close"].tolist() == [10.0, 11.0]


def test_write_parquet_empty_is_noop(tmp_path):
    info = data_store.write_parquet(pd.DataFrame(), tmp_path / "empty.parquet")
    assert info["written"] is False
    assert not (tmp_path / "empty.parquet").exists()


def test_merge_parquet_dedups_by_key(tmp_path):
    path = tmp_path / "m.parquet"
    d1 = pd.DataFrame({"date": pd.to_datetime(["2024-01-02"]), "close": [10.0]})
    d2 = pd.DataFrame({"date": pd.to_datetime(["2024-01-02"]), "close": [99.0]})
    data_store.merge_parquet(d1, path, keys=("date",))
    data_store.merge_parquet(d2, path, keys=("date",))
    out = pd.read_parquet(path)
    assert len(out) == 1
    assert out["close"].iloc[0] == 99.0     # keep="last"


def test_lttb_preserves_endpoints():
    pts = [(float(i), float(i % 7)) for i in range(500)]
    out = data_store.lttb_downsample(pts, 50)
    assert out[0] == pts[0]
    assert out[-1] == pts[-1]
    assert len(out) <= 50 + 2


def test_lttb_returns_input_when_small():
    pts = [(1.0, 2.0), (3.0, 4.0)]
    assert data_store.lttb_downsample(pts, 50) == pts


# ======================================================================
# downsample_series —— DataFrame 层
#
# ★ 这几条是 2026-09-18 补的，补的原因本身就是教训：
#   上面两条只测了 `lttb_downsample`（纯函数、返回点），
#   而 `downsample_series`（返回 DataFrame）**从未被任何调用方使用**，
#   于是它把 lttb 返回的 x 值（纳秒时间戳 ≈1.6e18）当成 iloc 下标，
#   一路活到回测 API 第一次真正调用它才炸 `IndexError: positional indexers
#   are out-of-bounds`。
#   结论：**只测底层纯函数不算测过这条路径** —— 端到端跑一次才算。
# ======================================================================
def _long_series(n: int = 1600) -> pd.DataFrame:
    """造一条带尖峰的长净值序列（尖峰用于验证 LTTB 真的保形态）。"""
    dates = pd.bdate_range("2020-01-01", periods=n)
    nav = [1.0 + i * 0.0005 for i in range(n)]
    nav[n // 2] = 0.7           # 深回撤尖峰
    nav[n // 3] = 1.9           # 向上尖峰
    return pd.DataFrame({"date": dates, "nav": nav, "benchmark_nav": 1.0})


def test_downsample_series_hits_target_and_keeps_endpoints():
    df = _long_series(1600)
    out = data_store.downsample_series(df, x_col="date", y_cols=("nav",), max_points=200)

    assert len(out) == 200, f"应降到 200 点，实际 {len(out)}"
    assert out["date"].iloc[0] == df["date"].iloc[0], "首点必须保留（否则曲线起点会变）"
    assert out["date"].iloc[-1] == df["date"].iloc[-1], "末点必须保留"
    assert out.attrs["downsampled_from"] == 1600


def test_downsample_series_dates_stay_monotonic_and_unique():
    """降采样后日期必须**严格递增** —— 重复或乱序会让前端折线来回跳。"""
    out = data_store.downsample_series(_long_series(1600), max_points=200)
    dates = pd.to_datetime(out["date"])
    assert dates.is_monotonic_increasing
    assert dates.is_unique


def test_downsample_series_keeps_other_columns():
    """降采样只选行，**不能丢列** —— 基准净值/回撤都靠它带过去。"""
    out = data_store.downsample_series(_long_series(1600), max_points=200)
    assert set(out.columns) == {"date", "nav", "benchmark_nav"}


def test_downsample_series_preserves_extremes():
    """LTTB 的存在意义就是保极值 —— 等间隔抽样会把回撤尖峰抹平。"""
    df = _long_series(1600)
    out = data_store.downsample_series(df, max_points=200)
    assert out["nav"].min() == pytest.approx(df["nav"].min()), "最深回撤必须被保留"
    assert out["nav"].max() == pytest.approx(df["nav"].max()), "最高点必须被保留"


def test_downsample_series_returns_input_when_short():
    df = _long_series(120)
    out = data_store.downsample_series(df, max_points=2000)
    assert len(out) == 120 and "downsampled_from" not in out.attrs
