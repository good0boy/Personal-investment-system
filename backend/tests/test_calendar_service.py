"""交易日历服务测试。

★ 本文件覆盖两个"看起来能用、其实在骗人"的缺陷：

1. **`refresh()` 假刷新**（2026-09-17 发现）
   `refresh()` 内部固定调 `fetch_all_trade_dates("1990-01-01")`，而该方法被
   `_invoke` 以 1 小时 TTL 缓存 → 1 小时内重复点"刷新交易日历"直接命中缓存，
   接口报"已同步"、时间戳照更新，但**根本没连数据源**。
   本文件断言它每次都要求强制刷新。

2. **`trading_day_offset` 是含首尾计数**（`offset(x, x) == 1`，不是 0）
   策略规格把时间止损口径写死为 `offset >= 10`，差一天就是 10% 的持有期误差。
   本文件把这条口径钉死。
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from app.core.errors import DataNotReadyError, InvalidParamError
from app.services import calendar_service
from app.services.calendar_service import TradingCalendar


@pytest.fixture
def fake_calendar_io(monkeypatch) -> list[dict]:
    """把 `refresh()` 的外部依赖全部换成内存桩：不联网、不落盘、不写库。

    返回一个列表，记录每次 `fetch_all_trade_dates` 的调用参数。
    """
    calls: list[dict] = []

    def fake_fetch_all(
        start_date: str = "1990-01-01",
        end_date: str | None = None,
        *,
        source: str | None = None,
        force_refresh: bool = False,
    ) -> pd.DataFrame:
        calls.append(
            {"start": start_date, "end": end_date, "source": source, "force_refresh": force_refresh}
        )
        return pd.DataFrame(
            {
                "date": pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"]),
                "is_trading_day": [1, 1, 1],
            }
        )

    monkeypatch.setattr(calendar_service, "fetch_all_trade_dates", fake_fetch_all)
    monkeypatch.setattr(
        calendar_service.data_store, "save_trade_calendar", lambda df: {"rows": len(df)}
    )
    monkeypatch.setattr(
        calendar_service.data_store,
        "read_trade_calendar",
        lambda *a, **k: pd.DataFrame(
            {
                "date": pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"]),
                "is_trading_day": [1, 1, 1],
            }
        ),
    )
    monkeypatch.setattr(calendar_service.meta_repo, "set_meta_bulk", lambda items: None)
    monkeypatch.setattr(
        calendar_service.meta_repo, "get_meta_value", lambda key: "2024-01-04T15:00:00"
    )
    return calls


# ======================================================================
# 1. refresh() 必须真的去拉数据
# ======================================================================
def test_refresh_always_requests_force_refresh(fake_calendar_io):
    """★ 核心回归：每次 refresh 都必须传 force_refresh=True。

    修复前这里会是 [False, False] —— 第二次就是读缓存。
    """
    cal = TradingCalendar()
    cal.refresh()
    cal.refresh()

    assert [c["force_refresh"] for c in fake_calendar_io] == [True, True], (
        "refresh() 必须每次要求强制刷新，否则会命中缓存变成假刷新"
    )


def test_refresh_fetches_from_datasource_every_call(fake_calendar_io):
    """★ 核心回归：两次 refresh = 两次真实拉取（修复前第二次不拉）。"""
    cal = TradingCalendar()
    cal.refresh()
    cal.refresh()
    cal.refresh()

    assert len(fake_calendar_io) == 3, (
        f"3 次 refresh 应有 3 次真实拉取，实际 {len(fake_calendar_io)} 次"
    )


def test_refresh_returns_calendar_info(fake_calendar_io):
    """`refresh()` 的返回必须与 `stats()` **同一份结构**。

    历史上两者字段名不一致（且 `last_date` 在两边含义不同），
    CLI 直接照着 refresh 的返回打印，于是打出 `最近交易日: None`、
    `今天是否交易日: 否` —— 静默错，没人报错。
    """
    cal = TradingCalendar()
    info = cal.refresh()

    assert info["total_days"] == 3
    assert info["trading_days"] == 3
    assert info["first_date"] == "2024-01-02"
    assert info["coverage_end"] == "2024-01-04"
    assert info.keys() == cal.stats().keys(), "refresh() 与 stats() 必须是同一份结构"
    assert info["today"] is not None
    assert info["today_is_trading_day"] is not None


def test_refresh_pulls_full_horizon_not_today(fake_calendar_io):
    """★ 回归：拉取上界必须是 `CALENDAR_END`（2030-12-31），不是 `date.today()`。

    旧实现 `fetch_all_trade_dates("1990-01-01")` 的 end 默认 today，于是日历
    永远只覆盖到昨天 → 每个交易日开盘前：健康检查必然 degraded、
    启动补跑必然抛 `DataNotReadyError`。实测 2026-09-18 早上就是这么报的。
    同时这也是与 `--source akshare` 分支（写死 2030-12-31）不一致的根源。
    """
    TradingCalendar().refresh()
    call = fake_calendar_io[-1]
    assert call["start"] == calendar_service.CALENDAR_START
    assert call["end"] == calendar_service.CALENDAR_END == "2030-12-31"
    assert call["force_refresh"] is True


def test_refresh_raises_when_source_returns_empty(monkeypatch):
    """数据源返回空 → 必须报错，不能把空日历写进本地。"""
    monkeypatch.setattr(
        calendar_service,
        "fetch_all_trade_dates",
        lambda *a, **k: pd.DataFrame(columns=["date", "is_trading_day"]),
    )
    with pytest.raises(DataNotReadyError):
        TradingCalendar().refresh()


# ======================================================================
# 1b. fetch_all_trade_dates 的 force_refresh 必须一路透传到 _query
# ======================================================================
def test_fetch_all_trade_dates_forwards_force_refresh(monkeypatch):
    """★ 回归：门面函数不得吞掉 force_refresh。

    链路：calendar.refresh() → data_source.fetch_all_trade_dates()
          → client.fetch_all_trade_dates() → client._query() → _invoke()
    任何一环丢掉 force_refresh，假刷新就会复现。
    """
    from app.services.data_source import baostock_client

    seen: list[bool] = []

    def fake_query(func_name, call, **kwargs):
        seen.append(kwargs.get("force_refresh", False))
        return pd.DataFrame({"calendar_date": ["2024-01-02"], "is_trading_day": ["1"]})

    monkeypatch.setattr(baostock_client, "_query", fake_query)

    baostock_client.fetch_all_trade_dates("1990-01-01", force_refresh=True)
    baostock_client.fetch_all_trade_dates("1990-01-01")
    baostock_client.fetch_trade_dates("2024-01-01", "2024-12-31", force_refresh=True)

    assert seen == [True, False, True], f"force_refresh 未正确透传：{seen}"


def test_data_source_facade_forwards_force_refresh(monkeypatch):
    """门面层（services.data_source）同样不得吞掉 force_refresh。"""
    from app.services import data_source as ds
    from app.services.data_source import baostock_client

    seen: list[bool] = []
    monkeypatch.setattr(
        baostock_client,
        "fetch_all_trade_dates",
        lambda s="1990-01-01", e=None, *, force_refresh=False: (
            seen.append(force_refresh) or pd.DataFrame({"date": [], "is_trading_day": []})
        ),
    )
    monkeypatch.setattr(
        baostock_client,
        "fetch_trade_dates",
        lambda s, e, *, force_refresh=False: (seen.append(force_refresh) or []),
    )

    ds.fetch_all_trade_dates("1990-01-01", force_refresh=True)
    ds.fetch_all_trade_dates("1990-01-01")
    ds.fetch_trade_dates("2024-01-01", "2024-12-31", force_refresh=True)

    assert seen == [True, False, True]


# ======================================================================
# 2. trading_day_offset 含首尾 —— 策略时间止损口径依赖它
# ======================================================================
def _cal_with(days: list[str]) -> TradingCalendar:
    cal = TradingCalendar()
    cal._df = pd.DataFrame({"date": pd.to_datetime(days), "is_trading_day": 1})
    cal._trading = days
    return cal


JAN_DAYS = [f"2024-01-{d:02d}" for d in range(2, 21)]   # 19 个交易日


def test_trading_day_offset_is_inclusive():
    """★ 含首尾：`offset(x, x) == 1`，不是 0。写引擎时必须知道这一点。"""
    cal = _cal_with(JAN_DAYS)
    assert cal.trading_day_offset(JAN_DAYS[0], JAN_DAYS[0]) == 1
    assert cal.trading_day_offset(JAN_DAYS[0], JAN_DAYS[1]) == 2
    assert cal.trading_day_offset(JAN_DAYS[0], JAN_DAYS[9]) == 10
    assert cal.trading_day_offset(JAN_DAYS[0], JAN_DAYS[10]) == 11


def test_time_stop_threshold_means_ten_trading_days_held():
    """把策略规格 2.3 的口径钉死。

    T+1 开盘买入（entry）→ `offset(entry, T) >= 10` 时触发卖出。
    实测：触发日就是"从 entry 起算第 10 个交易日"，即实际持有 10 个交易日。
    """
    cal = _cal_with(JAN_DAYS)
    entry = JAN_DAYS[0]        # T+1 买入成交日

    trigger = next(d for d in JAN_DAYS if cal.trading_day_offset(entry, d) >= 10)

    assert trigger == JAN_DAYS[9], f"应在持有第 10 个交易日触发，实际 {trigger}"
    assert cal.trading_day_offset(entry, trigger) == 10

    # 前 9 个交易日都不该触发
    for d in JAN_DAYS[:9]:
        assert cal.trading_day_offset(entry, d) < 10


def test_offset_would_be_wrong_with_strict_greater():
    """反证：若误用 `> 10`，实际持有会多一天（11 个交易日）。"""
    cal = _cal_with(JAN_DAYS)
    entry = JAN_DAYS[0]

    wrong_trigger = next(d for d in JAN_DAYS if cal.trading_day_offset(entry, d) > 10)
    right_trigger = next(d for d in JAN_DAYS if cal.trading_day_offset(entry, d) >= 10)

    assert wrong_trigger != right_trigger
    assert cal.trading_day_offset(entry, wrong_trigger) == 11


def test_trading_day_offset_negative_for_reversed():
    cal = _cal_with(JAN_DAYS)
    assert cal.trading_day_offset(JAN_DAYS[5], JAN_DAYS[0]) == -6


def test_trading_days_closed_interval():
    cal = _cal_with(JAN_DAYS)
    got = cal.trading_days(JAN_DAYS[0], JAN_DAYS[4])
    assert got == JAN_DAYS[:5]
    assert len(got) == 5


def test_trading_days_rejects_reversed_range():
    cal = _cal_with(JAN_DAYS)
    with pytest.raises(InvalidParamError):
        cal.trading_days(JAN_DAYS[5], JAN_DAYS[0])


def test_shift_moves_by_trading_days():
    cal = _cal_with(JAN_DAYS)
    assert cal.shift(JAN_DAYS[0], 1) == JAN_DAYS[1]
    assert cal.shift(JAN_DAYS[5], -1) == JAN_DAYS[4]
    assert cal.shift(JAN_DAYS[5], 0) == JAN_DAYS[5]


def test_is_trading_day_uses_calendar_not_weekday():
    """★ 交易日 ≠ 工作日。用日历判断，不要用 weekday()。"""
    days = ["2024-10-08"]   # 周二（真实交易日）
    cal = _cal_with(days)
    assert cal.is_trading_day("2024-10-08") is True
    # 2024-10-07 是周一但是国庆假期调休/放假，不在日历里 → 非交易日
    assert cal.is_trading_day("2024-10-07") is False


# ======================================================================
# 3. 覆盖度告警：以"日历覆盖上界"为准，不是"最后一个交易日"
# ======================================================================
def _cal_split(trading_days: list[str], coverage_end: str) -> TradingCalendar:
    """交易日与"覆盖上界"分开构造（`_cal_with` 把两者绑在一起，测不了这个）。"""
    trading = sorted(set(trading_days))
    all_days = sorted({*trading, coverage_end})
    cal = TradingCalendar()
    cal._df = pd.DataFrame(
        {"date": pd.to_datetime(all_days), "is_trading_day": [1] * len(all_days)}
    )
    cal._trading = trading
    return cal


def test_coverage_warning_silent_when_coverage_reaches_today():
    """★ 回归：旧实现每逢周末/节假日必报 degraded。

    场景：最后一个**交易日**是昨天，但日历已经覆盖到今天（含非交易日行、
    节假日同理）。旧实现比的是"最后一个交易日 vs 今天" → 必然 `昨天 < 今天`
    → 长期挂着一条假告警；新实现比覆盖上界 → 通过。
    """
    today = date.today().strftime("%Y-%m-%d")
    yesterday = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
    cal = _cal_split([yesterday], coverage_end=today)
    assert cal.coverage_warning() is None


def test_coverage_warning_fires_when_calendar_really_stale():
    """反证：日历真没覆盖到今天就**必须**告警（新实现不能把这条也吞掉）。"""
    yesterday = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
    cal = _cal_split(["2024-01-02"], coverage_end=yesterday)
    warn = cal.coverage_warning()
    assert warn is not None
    assert yesterday in warn


def test_coverage_warning_respects_explicit_need_until():
    cal = _cal_split(["2030-12-30"], coverage_end="2030-12-30")
    assert cal.coverage_warning("2030-12-30") is None
    assert cal.coverage_warning("2030-12-31") is not None


def test_stats_separates_coverage_end_from_last_realised():
    """★ `stats()` 的三个日期不能混：coverage_end 允许是未来，
    last_date / latest_trading_day 必须是"已发生"（≤ 今天）。

    混了的后果：系统状态页会把 2030-12-31 显示成"最近交易日"。
    """
    today = date.today().strftime("%Y-%m-%d")
    cal = _cal_split(["2024-01-02", today, "2030-12-31"], coverage_end="2030-12-31")
    s = cal.stats()

    assert s["first_date"] == "2024-01-02"
    assert s["coverage_end"] == "2030-12-31"
    assert s["last_date"] == today
    assert s["latest_trading_day"] == today
    assert s["future_trading_days"] == 1
    assert s["today"] == today


def test_next_trading_day_works_at_sample_end_with_future_coverage():
    """★ 这正是 2026-09-18 启动补跑崩掉的场景。

    日历覆盖到未来时，样本最后一个交易日的"下一个交易日"必须取得出来 ——
    回测在样本末日算 T+1 成交、补跑扫描上次成功日到今天的缺口，都依赖它。
    旧实现（日历只到 today）在这里抛 DataNotReadyError。
    """
    cal = _cal_split(["2024-12-30", "2024-12-31", "2025-01-02"], coverage_end="2030-12-31")
    assert cal.next_trading_day("2024-12-31") == "2025-01-02"
