"""股票池（Universe）过滤规则测试。

★ 这是"数据正确性红线"之一。回测收益如果被"理论上买不到的股票"污染，
  整条链路的结论都是废的。因此每条过滤规则都要有测试。

注意两个容易混淆的参数：
  · 新股入池门槛   90 交易日（股票**入池**）
  · 卖出后禁买窗口 60 交易日（个股**卖出后**）
"""
from __future__ import annotations

import pandas as pd

from tools.universe import (
    LISTED_DAYS_MIN,
    NO_REBUY_DAYS,
    at_limit_down,
    at_limit_up,
    build_universe,
    derive_first_trade_date,
    has_valid_price,
    is_limit_locked,
    is_target_board_symbol,
    is_trading,
    listed_days_enough,
    not_st,
    rebuy_allowed,
)

# ======================================================================
# 单条规则
# ======================================================================


def _row(**kw) -> pd.Series:
    base = {
        "symbol": "sh.600519",
        "date": pd.Timestamp("2024-06-28"),
        "open": 10.0,
        "high": 10.5,
        "low": 9.8,
        "close": 10.2,
        "preclose": 10.0,
        "pct_chg": 2.0,
        "trade_status": 1,
        "is_st": 0,
    }
    base.update(kw)
    return pd.Series(base)


def test_not_st():
    assert not_st(_row(is_st=0)) is True
    assert not_st(_row(is_st=1)) is False


def test_not_st_missing_treated_as_untradable():
    """is_st 缺失时保守处理为不可交易 —— 宁可漏掉，不可误买。"""
    assert not_st(_row(is_st=pd.NA)) is False


def test_is_trading():
    assert is_trading(_row(trade_status=1)) is True
    assert is_trading(_row(trade_status=0)) is False


def test_has_valid_price():
    assert has_valid_price(_row()) is True
    assert has_valid_price(_row(close=0.0)) is False
    assert has_valid_price(_row(close=pd.NA)) is False
    assert has_valid_price(_row(preclose=0.0)) is False


def test_is_limit_locked_main_board():
    """主板一字涨停：开=高=低=收 且涨幅 >= 9.8%。"""
    assert is_limit_locked(_row(open=11.0, high=11.0, low=11.0, close=11.0, pct_chg=10.0)) is True
    assert is_limit_locked(_row(open=9.0, high=9.0, low=9.0, close=9.0, pct_chg=-10.0)) is True
    # 有振幅 → 不是一字板
    assert is_limit_locked(_row(open=10.0, high=11.0, low=10.0, close=11.0, pct_chg=10.0)) is False
    # 一字但未达涨跌幅限制
    assert is_limit_locked(_row(open=10.1, high=10.1, low=10.1, close=10.1, pct_chg=1.0)) is False


def test_is_limit_locked_gem_20pct():
    """创业板 2020-08-24 起 ±20%：涨 10% 的一字板不算封死。"""
    row = _row(symbol="sz.300750", open=11.0, high=11.0, low=11.0, close=11.0, pct_chg=10.0)
    assert is_limit_locked(row) is False                       # 20% 制度下还有空间
    row20 = _row(symbol="sz.300750", open=12.0, high=12.0, low=12.0, close=12.0, pct_chg=20.0)
    assert is_limit_locked(row20) is True


def test_is_limit_locked_gem_before_2020():
    """2020-08-24 之前创业板仍是 ±10%。"""
    row = _row(
        symbol="sz.300750", date=pd.Timestamp("2020-06-01"),
        open=11.0, high=11.0, low=11.0, close=11.0, pct_chg=10.0,
    )
    assert is_limit_locked(row) is True


def test_is_limit_locked_st_is_5pct():
    row = _row(open=10.5, high=10.5, low=10.5, close=10.5, pct_chg=5.0, is_st=1)
    assert is_limit_locked(row) is True


# ----------------------------------------------------------------------
# 涨跌停触及判定（比例容差，复权安全）
# ----------------------------------------------------------------------
def test_at_limit_up_down_exact_hits():
    """比值恰好等于限幅 → 触及。"""
    assert at_limit_up(11.0, 10.0, 0.10) is True
    assert at_limit_down(9.0, 10.0, 0.10) is True
    assert at_limit_up(10.9, 10.0, 0.10) is False      # 9% ≠ 涨停
    assert at_limit_down(9.5, 10.0, 0.10) is False     # 跌 5% ≠ 跌停


def test_at_limit_tolerates_rounding_on_raw_scale():
    """不复权口径的取整偏差（≤ 半分钱）在比例容差内 → 仍判触及。"""
    # pre=3.07 元（低价股），真实涨停价 = round2(3.07×1.1) = 3.38
    # 3.38/3.07 - 1 = 10.097% → 偏差 0.097% ≤ 0.5% → 触及
    assert at_limit_up(3.38, 3.07, 0.10) is True
    assert at_limit_down(2.76, 3.07, 0.10) is True


def test_at_limit_works_on_hfq_scale():
    """★ 复权尺度（旧绝对容差失效的现场）：hfq 价格 ×15 后取整错位被放大。

    旧实现 |price − round2(pre×(1±limit))| < 0.005 在 pre=150 的 hfq 尺度上
    会让真实涨停（open_raw=9.0005×15=135.0075）漏判 —— 绝对差 0.0075 > 0.005。
    比例口径与复权因子无关：偏离限幅 +0.005% 仍在容差内。
    """
    pre = 150.0
    up_open = pre * (1 + 0.10) + 0.0075      # hfq 涨停价偏上半分多钱
    down_open = pre * (1 - 0.10) - 0.0075    # hfq 跌停价偏下半分多钱
    assert abs(up_open - round(pre * 1.10, 2)) > 0.005, "构造必须落在旧口径的漏判区"
    assert at_limit_up(up_open, pre, 0.10) is True
    assert at_limit_down(down_open, pre, 0.10) is True


def test_at_limit_boundary_outside_tol():
    """偏差超过 0.5% 容差 → 不算触及（错杀安全，不放行不可能的成交）。"""
    assert at_limit_up(10.0 * (1 + 0.10 - 0.0051), 10.0, 0.10) is False
    assert at_limit_down(10.0 * (1 - 0.10 + 0.0051), 10.0, 0.10) is False


def test_at_limit_rejects_invalid_input():
    assert at_limit_up(float("nan"), 10.0, 0.10) is False
    assert at_limit_up(11.0, 0.0, 0.10) is False
    assert at_limit_down(9.0, float("nan"), 0.10) is False
    assert at_limit_down(9.0, -1.0, 0.10) is False


def test_listed_days_enough_natural_day_approx():
    first = {"sh.600519": "2024-01-01"}
    # 约 1.45 自然日 / 交易日 → 90 交易日 ≈ 130 自然日
    assert listed_days_enough("sh.600519", "2024-06-28", first, min_days=90) is True
    assert listed_days_enough("sh.600519", "2024-02-01", first, min_days=90) is False


def test_listed_days_enough_unknown_symbol():
    assert listed_days_enough("sh.999999", "2024-06-28", {}, min_days=90) is False


def test_listed_days_enough_before_listing():
    first = {"sh.600519": "2024-06-01"}
    assert listed_days_enough("sh.600519", "2024-01-01", first, min_days=90) is False


def test_listed_days_enough_with_calendar_fn():
    days = [f"2024-01-{d:02d}" for d in range(1, 32)]

    def cal(_s: str, _e: str) -> list[str]:
        return days

    first = {"sh.600519": "2024-01-01"}
    assert listed_days_enough("sh.600519", "2024-01-31", first, min_days=30, calendar_days_fn=cal) is True
    assert listed_days_enough("sh.600519", "2024-01-31", first, min_days=31, calendar_days_fn=cal) is True
    assert listed_days_enough("sh.600519", "2024-01-31", first, min_days=32, calendar_days_fn=cal) is False


def test_rebuy_window():
    last = {"sh.600519": "2024-01-01"}
    assert rebuy_allowed("sh.600519", "2024-06-28", last, window=60) is True
    assert rebuy_allowed("sh.600519", "2024-01-10", last, window=60) is False
    assert rebuy_allowed("sh.600519", "2024-06-28", {}, window=60) is True   # 从未买过


def test_two_params_are_independent():
    """⚠️ 90（入池门槛）与 60（禁买窗口）是两个独立参数，不能混用。"""
    assert LISTED_DAYS_MIN == 90
    assert NO_REBUY_DAYS == 60


def test_is_target_board_symbol():
    assert is_target_board_symbol("sh.600519") is True
    assert is_target_board_symbol("sz.300750") is True
    assert is_target_board_symbol("sh.688981") is False
    assert is_target_board_symbol("bj.830799") is False
    assert is_target_board_symbol("not-a-code") is False


def test_is_target_board_symbol_excludes_indices():
    """★ 回归：`sh.000xxx` 是**上证指数**，不是深主板股票。

    历史 bug：判定只看 6 位码（`board_of("000001") == "sz_main"`），
    于是 `sh.000001`（上证综合指数）被判成深主板股票混进股票池。
    实测全市场回补清单 5014 只里混进了 **223 个 `sh.000xxx` 指数**（4.4%），
    会污染回测候选池且不报任何错。
    """
    # 同上交所指数段 —— 必须排除
    for s in ("sh.000001", "sh.000300", "sh.000905", "sh.000016", "sh.000688"):
        assert is_target_board_symbol(s) is False, f"{s} 是上证指数，不该入池"

    # 深交所同 6 位码的**股票**仍然要保留
    assert is_target_board_symbol("sz.000001") is True, "sz.000001 是平安银行，必须保留"
    assert is_target_board_symbol("sz.000002") is True

    # 深证成指 / 创业板指也是指数
    assert is_target_board_symbol("sz.399001") is False
    assert is_target_board_symbol("sz.399006") is False


def test_is_target_board_symbol_excludes_funds_and_bonds():
    """ETF / 基金段也不能入池（它们常与股票代码段相邻，最容易混进来）。"""
    for s in ("sh.510300", "sh.512880", "sh.588000", "sz.159915", "sz.150019"):
        assert is_target_board_symbol(s) is False, f"{s} 是基金/ETF，不该入池"


def test_mask_target_board_uses_full_symbol():
    """★ 向量化路径同样不能只看 6 位码（两条路径必须一致）。"""
    from tools.universe import _mask_target_board

    df = pd.DataFrame({"symbol": ["sh.000001", "sz.000001", "sh.600519", "sz.300750"]})
    mask = _mask_target_board(df).tolist()
    assert mask == [False, True, True, True]


# ======================================================================
# build_universe 主入口
# ======================================================================
def _snapshot(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame([_row(**r) for r in rows])


def test_build_universe_keeps_normal_stocks():
    df = _snapshot([
        {"symbol": "sh.600519"},
        {"symbol": "sz.000001", "close": 12.0, "preclose": 11.8},
    ])
    res = build_universe("2024-06-28", df)
    assert set(res.symbols) == {"sh.600519", "sz.000001"}
    assert res.total_candidates == 2


def test_build_universe_drops_st():
    df = _snapshot([{"symbol": "sh.600519"}, {"symbol": "sz.000001", "is_st": 1}])
    res = build_universe("2024-06-28", df)
    assert res.symbols == ["sh.600519"]
    assert res.dropped["ST"] == 1


def test_build_universe_drops_suspended():
    df = _snapshot([{"symbol": "sh.600519"}, {"symbol": "sz.000001", "trade_status": 0}])
    res = build_universe("2024-06-28", df)
    assert res.symbols == ["sh.600519"]
    assert res.dropped["停牌"] == 1


def test_build_universe_drops_non_target_boards():
    df = _snapshot([
        {"symbol": "sh.600519"},
        {"symbol": "sh.688981"},      # 科创板
        {"symbol": "bj.830799"},      # 北交所
        {"symbol": "sz.200011"},      # B 股
        {"symbol": "sh.510300"},      # ETF
    ])
    res = build_universe("2024-06-28", df)
    assert res.symbols == ["sh.600519"]
    assert res.dropped["非目标板块"] == 4


def test_build_universe_drops_limit_locked():
    df = _snapshot([
        {"symbol": "sh.600519"},
        {"symbol": "sz.000001", "open": 11.0, "high": 11.0, "low": 11.0, "close": 11.0, "pct_chg": 10.0},
    ])
    res = build_universe("2024-06-28", df)
    assert res.symbols == ["sh.600519"]
    assert res.dropped["一字涨跌停"] == 1


def test_build_universe_drops_invalid_price():
    df = _snapshot([{"symbol": "sh.600519"}, {"symbol": "sz.000001", "close": 0.0}])
    res = build_universe("2024-06-28", df)
    assert res.symbols == ["sh.600519"]
    assert res.dropped["价格无效"] == 1


def test_build_universe_new_stock_threshold():
    """上市不足 90 交易日（自然日近似）→ 剔除。"""
    df = _snapshot([{"symbol": "sh.600519"}, {"symbol": "sz.000001"}])
    first = {"sh.600519": "2020-01-01", "sz.000001": "2024-06-20"}
    res = build_universe("2024-06-28", df, first_trade_date=first, min_listed_days=90)
    assert res.symbols == ["sh.600519"]
    assert res.dropped["上市不足90交易日"] == 1


def test_build_universe_90_is_default_threshold():
    """默认门槛必须是 90（不是旧的 60）。"""
    df = _snapshot([{"symbol": "sh.600519"}])
    first = {"sh.600519": "2024-04-01"}
    res = build_universe("2024-06-28", df, first_trade_date=first)
    assert res.symbols == []
    assert "上市不足90交易日" in res.dropped


def test_build_universe_rebuy_window():
    df = _snapshot([{"symbol": "sh.600519"}])
    last = {"sh.600519": "2024-06-01"}
    res = build_universe("2024-06-28", df, last_sell_date=last)
    assert res.symbols == []
    assert "卖出后60交易日内" in res.dropped


def test_build_universe_empty_snapshot():
    res = build_universe("2024-06-28", pd.DataFrame())
    assert res.symbols == []
    assert res.total_candidates == 0


def test_build_universe_none_snapshot():
    res = build_universe("2024-06-28", None)
    assert res.symbols == []


def test_build_universe_summary_and_dict():
    df = _snapshot([{"symbol": "sh.600519"}, {"symbol": "sz.000001", "is_st": 1}])
    res = build_universe("2024-06-28", df)
    d = res.to_dict()
    assert d["day"] == "2024-06-28"
    assert d["symbol_count"] == 1
    assert d["total_candidates"] == 2
    assert "ST" in d["dropped"]
    assert "股票池" in res.summary()


def test_build_universe_verbose_does_not_crash():
    res = build_universe("2024-06-28", _snapshot([{"symbol": "sh.600519"}]), verbose=True)
    assert res.symbols == ["sh.600519"]


def test_derive_first_trade_date():
    df = pd.DataFrame({
        "symbol": ["sh.600519", "sh.600519", "sz.000001"],
        "date": pd.to_datetime(["2024-01-02", "2024-01-03", "2024-02-01"]),
    })
    out = derive_first_trade_date(df)
    assert out["sh.600519"] == "2024-01-02"
    assert out["sz.000001"] == "2024-02-01"


def test_derive_first_trade_date_empty():
    assert derive_first_trade_date(pd.DataFrame()) == {}


# ======================================================================
# 性能约束（防止行级 apply 回归）
# ======================================================================
def test_build_universe_performance_is_vectorized():
    """★ 全市场 4500 只单日必须远快于 0.5 秒。

    历史实现用 `df.apply(fn, axis=1)` 逐行处理，实测 0.77ms/只；
    回测 1115 天会累积到 ~65 分钟，属于不可接受的性能缺陷。
    本测试用 4500 行数据把这条底线钉死。
    """
    import time as _t

    n = 4500
    # ⚠️ 交易所前缀必须与代码段**配对正确**：`000/001/002/003/300/301` 是深市，
    #    沪市只有 `600/601/603/605`。写成 `sh.000xxx` 会被判成"上证指数"而排除
    #    （这正是 2026-09-18 修掉的那个 bug），那样就测不出性能了。
    sh_prefixes = ["600", "601", "603", "605"]
    sz_prefixes = ["000", "001", "002", "003", "300", "301"]
    prefixes = sh_prefixes + sz_prefixes
    symbols = [
        f"{'sh' if prefixes[i % 10] in sh_prefixes else 'sz'}.{prefixes[i % 10]}{i // 10:03d}"
        for i in range(n)
    ]
    df = pd.DataFrame({
        "symbol": symbols,
        "date": pd.Timestamp("2024-06-28"),
        "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2,
        "preclose": 10.0, "amount": 1e8, "pct_chg": 2.0,
        "trade_status": 1, "is_st": 0,
    })
    t0 = _t.perf_counter()
    res = build_universe("2024-06-28", df)
    elapsed = _t.perf_counter() - t0

    assert len(res.symbols) == n, f"应全部保留，实际 {len(res.symbols)}，剔除 {res.dropped}"
    assert elapsed < 0.5, f"4500 只单日耗时 {elapsed * 1000:.0f}ms，应 < 500ms"


def test_build_universe_does_not_crash_when_all_filtered():
    """★ 回归：候选集被清空时必须返回空池，而不是抛 KeyError。

    历史 bug：`df.apply(fn, axis=1)` 在空 DataFrame 上返回 DataFrame 而非 Series，
    后续 `df["symbol"]` 直接 KeyError —— 某天全市场都停牌/ST 时回测会崩。
    """
    df = _snapshot([
        {"symbol": "sh.600519", "is_st": 1},
        {"symbol": "sz.000001", "trade_status": 0},
    ])
    res = build_universe("2024-06-28", df)
    assert res.symbols == []


def test_build_universe_does_not_crash_when_filtered_midway():
    """在每个过滤步骤后都可能变空，每处都要能安全短路。"""
    # 全部被新股门槛剔除 —— 触达第 5 步之后的空表分支
    df = _snapshot([{"symbol": "sh.600519"}, {"symbol": "sz.000001"}])
    first = {"sh.600519": "2024-06-20", "sz.000001": "2024-06-21"}
    res = build_universe("2024-06-28", df, first_trade_date=first, min_listed_days=90)
    assert res.symbols == []

    # 全部被禁买窗口剔除
    res2 = build_universe(
        "2024-06-28", df,
        last_sell_date={"sh.600519": "2024-06-25", "sz.000001": "2024-06-25"},
    )
    assert res2.symbols == []

    # 全部被一字板剔除
    df3 = _snapshot([
        {"symbol": "sh.600519", "open": 11.0, "high": 11.0, "low": 11.0, "close": 11.0, "pct_chg": 10.0},
    ])
    res3 = build_universe("2024-06-28", df3)
    assert res3.symbols == []


def test_vectorized_masks_match_single_row_functions():
    """向量化掩码必须与单行纯函数结果完全一致（防止重构引入语义漂移）。"""
    rows = [
        {"symbol": "sh.600519"},
        {"symbol": "sz.000001", "is_st": 1},
        {"symbol": "sz.000002", "trade_status": 0},
        {"symbol": "sh.688981"},
        {"symbol": "bj.830799"},
        {"symbol": "sz.000003", "close": 0.0},
        {"symbol": "sz.300750", "open": 12.0, "high": 12.0, "low": 12.0, "close": 12.0, "pct_chg": 20.0},
        {"symbol": "sh.600520", "open": 11.0, "high": 11.0, "low": 11.0, "close": 11.0, "pct_chg": 10.0},
    ]
    df = _snapshot(rows)

    from tools.universe import (
        _mask_not_limit_locked,
        _mask_not_st,
        _mask_target_board,
        _mask_trading,
        _mask_valid_price,
    )

    board_expected = df.apply(lambda r: is_target_board_symbol(r["symbol"]), axis=1)
    st_expected = df.apply(not_st, axis=1)
    trade_expected = df.apply(is_trading, axis=1)
    price_expected = df.apply(has_valid_price, axis=1)
    limit_expected = ~df.apply(is_limit_locked, axis=1)

    assert _mask_target_board(df).tolist() == board_expected.tolist()
    assert _mask_not_st(df).tolist() == st_expected.tolist()
    assert _mask_trading(df).tolist() == trade_expected.tolist()
    assert _mask_valid_price(df).tolist() == price_expected.tolist()
    assert _mask_not_limit_locked(df).tolist() == limit_expected.tolist()


def test_listed_days_mask_matches_single_row_with_calendar():
    """带交易日历的新股门槛，向量化结果必须与逐只计算一致。

    ⚠️ 夹具必须**遵守传入区间** —— 否则逐只实现（按区间调用）
       与向量化实现（一次性取全区间再 searchsorted）会因夹具失真而"看似不一致"。
    """
    from tools.universe import _mask_listed_days

    cal = [f"2024-01-{d:02d}" for d in range(1, 32)] + [f"2024-02-{d:02d}" for d in range(1, 29)]

    def cal_fn(s: str, e: str) -> list[str]:
        return [d for d in cal if s <= d <= e]

    first = {
        "sh.600519": "2024-01-01",   # [01-01, 02-28] = 59 天 → 达标
        "sz.000001": "2024-02-20",   # [02-20, 02-28] = 9 天  → 不达标
        "sz.000002": "2024-06-01",   # 未来 → 不达标
    }
    df = _snapshot([{"symbol": k} for k in first])

    got = _mask_listed_days(df, "2024-02-28", first, min_days=30, calendar_days_fn=cal_fn)
    expected = [
        listed_days_enough(s, "2024-02-28", first, min_days=30, calendar_days_fn=cal_fn)
        for s in first
    ]
    assert got.tolist() == expected
    assert expected == [True, False, False], "夹具本身要能产生正反两类结果"


def test_rebuy_mask_matches_single_row_with_calendar():
    from tools.universe import _mask_rebuy_allowed

    cal = [f"2024-01-{d:02d}" for d in range(1, 32)] + [f"2024-02-{d:02d}" for d in range(1, 29)]

    def cal_fn(s: str, e: str) -> list[str]:
        return [d for d in cal if s <= d <= e]

    last = {
        "sh.600519": "2024-01-01",   # 很久以前 → 允许
        "sz.000001": "2024-02-25",   # 4 天 ≤ 5 → 禁止
        "sz.000002": "2024-02-20",   # 9 天 > 5 → 允许
        "sz.000003": "2024-03-01",   # 未来 → 允许
    }
    df = _snapshot([{"symbol": k} for k in last])

    got = _mask_rebuy_allowed(df, "2024-02-28", last, window=5, calendar_days_fn=cal_fn)
    expected = [
        rebuy_allowed(s, "2024-02-28", last, window=5, calendar_days_fn=cal_fn)
        for s in last
    ]
    assert got.tolist() == expected
    assert expected == [True, False, True, True], "夹具本身要能产生正反两类结果"
