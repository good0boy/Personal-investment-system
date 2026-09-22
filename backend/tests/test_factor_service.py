"""模块 2 · 因子有效性分析（analyze）与只读服务层（factor_service）的测试。

★ 重点验证三件容易错且**不报错**的事：
  1. 前瞻收益的**时点**（T 日因子 → T+1 开盘买入 → T+1+h 开盘卖出）——
     差一天就是未来函数，而回测里这种偏差会把收益从负变成正。
  2. 分组与 IC 的**方向**是否正确（构造一个"完美因子"，IC 必须 = 1）。
  3. 多空组合的**换手口径** —— 用错口径（可交易池而非组合）会让年化成本
     从 10% 掉到 0.1%，结论方向完全反转。这是阶段 3 开发时真实踩到的 bug。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from app.core.errors import DataNotReadyError, InvalidParamError, NotFoundError
from app.services import data_store, factor_service
from factors import analyze, loader, manifest


# ======================================================================
# 夹具
# ======================================================================
@pytest.fixture
def factor_env(tmp_path, monkeypatch):
    """把因子相关路径全部指向临时目录。

    ⚠️ 必须**同时**改四处，缺一处就会出现"测试写进了真实 data/ 目录"
       （本项目此前在 `data/factor/daily/` 留下过一个 `_test_lock.parquet`）：
         data_store.FACTOR_DIR          —— 产物与清单的根
         manifest.FACTOR_MANIFEST_FILE  —— 清单文件
         analyze.FACTOR_ANALYSIS_DIR    —— 分析产物目录
         factor_service 的清单缓存       —— 否则读到上一个用例的缓存
    """
    fdir = tmp_path / "factor"
    (fdir / "daily").mkdir(parents=True)
    (fdir / "analysis").mkdir(parents=True)
    monkeypatch.setattr(data_store, "FACTOR_DIR", fdir)
    monkeypatch.setattr(manifest, "FACTOR_MANIFEST_FILE", fdir / "_manifest.json")
    monkeypatch.setattr(analyze, "FACTOR_ANALYSIS_DIR", fdir / "analysis")
    factor_service.invalidate_cache()
    yield fdir
    factor_service.invalidate_cache()


def _write_factor(name: str, rows: list[tuple[str, str, float]], category: str = "daily") -> None:
    df = pd.DataFrame(rows, columns=["date", "symbol", "value"])
    df["date"] = pd.to_datetime(df["date"])
    data_store.save_factor(name, df, category)


def _market(dates: list[str], symbols: list[str], opens: np.ndarray) -> SimpleNamespace:
    """`analyze` 只用到 dates / symbols / open 三个属性，不需要真的 MarketData。"""
    return SimpleNamespace(
        dates=dates,
        date_index={d: i for i, d in enumerate(dates)},
        symbols=symbols,
        symbol_index={s: i for i, s in enumerate(symbols)},
        open=opens,
    )


# ======================================================================
# 一、统计原语
# ======================================================================
def test_average_rank_uses_mean_for_ties() -> None:
    """并列值必须共享平均名次（与 scipy.stats.spearmanr 口径一致）。"""
    ranks = analyze._average_rank(np.array([1.0, 2.0, 2.0, 3.0]))
    assert ranks.tolist() == [1.0, 2.5, 2.5, 4.0]


@pytest.mark.parametrize(
    ("a", "b", "expect"),
    [
        ([1, 2, 3, 4], [10, 20, 30, 40], 1.0),
        ([1, 2, 3, 4], [40, 30, 20, 10], -1.0),
        ([1, 1, 1, 1], [1, 2, 3, 4], None),  # 常数序列无秩相关
    ],
)
def test_spearman(a: list[float], b: list[float], expect: float | None) -> None:
    got = analyze._spearman(np.array(a, dtype=float), np.array(b, dtype=float))
    if expect is None:
        assert got is None
    else:
        assert got == pytest.approx(expect)


def test_spearman_handles_ties_like_scipy() -> None:
    """[1,2,2,3] vs [1,2,3,4] 的秩相关 = 0.9487（手算）。"""
    got = analyze._spearman(np.array([1.0, 2, 2, 3]), np.array([1.0, 2, 3, 4]))
    assert got == pytest.approx(0.9486833, abs=1e-6)


def test_group_index_balanced_and_ordered() -> None:
    """秩分组：组内均衡、组号随因子值单调。"""
    g = analyze._group_index(np.arange(100, dtype=float), 5)
    assert g.min() == 0 and g.max() == 4
    assert np.bincount(g).tolist() == [20, 20, 20, 20, 20]
    # 值最大的必须是最后一组
    assert g[-1] == 4 and g[0] == 0


def test_group_index_survives_heavy_ties() -> None:
    """大量重复值（如涨跌幅为 0）不能让分组崩掉 —— qcut 在这种输入上会抛错/减组。"""
    g = analyze._group_index(np.array([1.0] * 90 + [2.0] * 10), 5)
    assert set(np.unique(g)).issubset({0, 1, 2, 3, 4})


# ======================================================================
# 二、前瞻收益的时点
# ======================================================================
def test_forward_returns_timing_is_next_open() -> None:
    """★ fwd[i] 必须 = open[i+1+h] / open[i+1] − 1（T+1 开盘买入）。

    差一天就是未来函数 —— 回测里这种偏差足以把负收益做成正收益。
    """
    opens = np.array([[10.0], [11.0], [12.0], [13.0], [14.0]])
    m = _market(["d0", "d1", "d2", "d3", "d4"], ["s"], opens)
    fwd = analyze.forward_returns(m, horizon=1)

    assert fwd[0, 0] == pytest.approx(12.0 / 11.0 - 1)
    assert fwd[1, 0] == pytest.approx(13.0 / 12.0 - 1)
    assert fwd[2, 0] == pytest.approx(14.0 / 13.0 - 1)
    assert np.isnan(fwd[3, 0]) and np.isnan(fwd[4, 0]), "末 h+1 行必须为 NaN"


def test_forward_returns_horizon_5() -> None:
    opens = np.arange(1, 11, dtype=float).reshape(-1, 1)  # 1..10
    dates = [f"d{i}" for i in range(10)]
    m = _market(dates, ["s"], opens)
    fwd = analyze.forward_returns(m, horizon=5)
    assert fwd[0, 0] == pytest.approx(7.0 / 2.0 - 1)  # open[6] / open[1]
    assert np.isnan(fwd[4, 0])


def test_forward_returns_rejects_zero_horizon() -> None:
    m = _market(["d0", "d1"], ["s"], np.ones((2, 1)))
    with pytest.raises(Exception, match="horizon"):
        analyze.forward_returns(m, horizon=0)


def test_forward_returns_too_short_returns_all_nan() -> None:
    m = _market(["d0"], ["s"], np.ones((1, 1)))
    assert np.isnan(analyze.forward_returns(m, horizon=1)).all()


# ======================================================================
# 三、analyze_one：方向与换手口径
# ======================================================================
def _perfect_case(
    n_dates: int = 12,
    n_symbols: int = 30,
    rotate: bool = False,
    opens: np.ndarray | None = None,
):
    """构造一个"完美因子"：fmat 越大，下一期收益越高（IC 应为 1）。"""
    dates = [f"2024-01-{i + 2:02d}" for i in range(n_dates)]
    symbols = [f"sz.{i:06d}" for i in range(n_symbols)]
    fmat = np.tile(np.arange(n_symbols, dtype=float), (n_dates, 1))
    if rotate:
        # 每天轮换一次排序 → 分组成员大幅变动，用于验证换手口径
        for t in range(n_dates):
            fmat[t] = np.roll(fmat[t], t)
    fwd = np.tile(np.arange(n_symbols, dtype=float) * 0.001, (n_dates, 1))
    masks = np.ones((n_dates, n_symbols), dtype=bool)
    if opens is None:
        opens = np.ones((n_dates, n_symbols))
    return _market(dates, symbols, opens), fmat, fwd, masks


def _monotone_opens(n_dates: int, n_symbols: int) -> np.ndarray:
    """构造"下一期收益随标的序号单调递增"的开盘价矩阵。

    ⚠️ 为什么不能直接用 `np.ones`：那样前瞻收益恒为 0，
       Spearman 的分母为 0 → IC 全为 None（**代码是对的**，
       但这种输入下测不出任何东西，属于"夹具写错导致断言静默空过"）。
    """
    return np.array(
        [[100.0 + s + 0.1 * t * s for s in range(n_symbols)] for t in range(n_dates)],
        dtype=float,
    )


def test_analyze_one_perfect_factor_has_ic_one() -> None:
    """★ 完美正向因子：IC 必须 = 1、单调性 = 1、最高组收益最高。

    这条测试同时验证了分组方向没写反、IC 的符号没反、净值累乘没反。
    """
    market, fmat, fwd, masks = _perfect_case()
    res = analyze.analyze_one(
        loader.get_factor("ret_5"), market, fmat, fwd, masks, groups=5, horizon=1
    )
    assert res["ic"]["mean"] == pytest.approx(1.0)
    assert res["ic"]["win_rate"] == pytest.approx(1.0)
    assert res["layered"]["monotonicity"] == pytest.approx(1.0)

    groups = res["layered"]["group_summary"]
    assert [g["group"] for g in groups] == [1, 2, 3, 4, 5]
    cums = [g["cum_return"] for g in groups]
    assert cums == sorted(cums), "分组累计收益必须随组号单调递增"
    assert res["layered"]["long_short"]["avg_daily"] > 0

    # 每组等权：30 只 / 5 组 = 6 只
    assert all(g["avg_size"] == pytest.approx(6.0) for g in groups)


def test_analyze_one_reversed_factor_has_ic_minus_one() -> None:
    """把因子取负 → IC 必须变成 -1（对称性检查，防止符号写反还"看起来合理"）。"""
    market, fmat, fwd, masks = _perfect_case()
    res = analyze.analyze_one(
        loader.get_factor("ret_5"), market, -fmat, fwd, masks, groups=5, horizon=1
    )
    assert res["ic"]["mean"] == pytest.approx(-1.0)
    assert res["layered"]["monotonicity"] == pytest.approx(-1.0)
    assert res["layered"]["long_short"]["avg_daily"] < 0


def test_analyze_one_skips_last_date_without_forward_return() -> None:
    """最后一行没有 T+1 → 不可入场，不能进统计。"""
    market, fmat, fwd, masks = _perfect_case(n_dates=6)
    res = analyze.analyze_one(
        loader.get_factor("ret_5"), market, fmat, fwd, masks, groups=5, horizon=1
    )
    assert res["interval"]["dates_used"] == 5
    assert market.dates[-1] not in [r["date"] for r in res["ic"]["series"]]


def test_analyze_one_requires_t1_in_universe() -> None:
    """★ 入场可行性：T+1 不在股票池里（开盘涨停/停牌）必须排除该样本。

    模块 3 曾因漏判这一点，让"买不到的却买入了"，虚增 53 个百分点。

    ⚠️ 缩池幅度要**大于** `MIN_SYMBOLS_PER_GROUP × groups = 25`，
       否则那一天会被"样本太少"整条跳过（那是另一个正确的行为），
       这条用例就测不到"缩池"本身了。所以取 26 只。
    """
    market, fmat, fwd, masks = _perfect_case(n_dates=6, n_symbols=30)
    masks[2, 26:] = False  # 26 只 > 25 的地板
    res = analyze.analyze_one(
        loader.get_factor("ret_5"), market, fmat, fwd, masks, groups=5, horizon=1
    )
    sizes = res["layered"]["group_summary"]
    assert any(g["avg_size"] < 6.0 for g in sizes), f"缩池应该体现在组均规模上：{sizes}"
    assert res["settings"]["avg_universe_size"] < 30


def test_analyze_one_ls_turnover_uses_portfolio_not_eligible_set() -> None:
    """★ 多空换手必须用**组合成员**的变动，不能用可交易池的变动。

    踩过的坑：可交易池每天只变动 ~0.18%（ST/停牌/新股进出），而按因子重排的
    多空组合每天换手 20%+ —— 用错口径让"年化成本"从 10% 掉到 0.1%，
    结论从"成本吃掉三分之一的多空收益"变成"成本可以忽略"，方向完全反转。
    """
    market, fmat, fwd, masks = _perfect_case(n_dates=12, n_symbols=30, rotate=True)
    res = analyze.analyze_one(
        loader.get_factor("ret_5"), market, fmat, fwd, masks, groups=5, horizon=1
    )
    ls = res["layered"]["long_short"]
    # 每天整体轮换 → 换手率必然很高（远高于可交易池的 0.2%）
    assert ls["turnover_daily"] > 0.2, f"换手率过小，很可能用了可交易池口径：{ls}"
    assert ls["cost_annual"] > 0.02, f"年化成本过小，口径可疑：{ls}"


def test_analyze_one_returns_empty_when_too_few_symbols() -> None:
    """截面标的数不足 → 明确说明跳过原因，而不是给出一堆噪声数字。"""
    market, fmat, fwd, masks = _perfect_case(n_dates=6, n_symbols=8)  # < 5×5
    res = analyze.analyze_one(
        loader.get_factor("ret_5"), market, fmat, fwd, masks, groups=5, horizon=1
    )
    assert res.get("dates_used") == 0
    assert any("无法分析" in w for w in res["warnings"])


def test_analyze_one_warns_on_low_ic_and_monotonicity() -> None:
    """低 IC + 低单调性必须出警告（否则页面会看着"正常"）。"""
    market, fmat, fwd, masks = _perfect_case(n_dates=12, n_symbols=30)
    rng = np.random.default_rng(7)
    noise = rng.normal(size=fwd.shape)
    res = analyze.analyze_one(
        loader.get_factor("ret_5"), market, fmat, noise, masks, groups=5, horizon=1
    )
    assert res["warnings"]


def test_analyze_one_notes_are_present() -> None:
    """口径说明必须随产物一起落盘 —— 数字脱离口径没法解读。"""
    market, fmat, fwd, masks = _perfect_case()
    res = analyze.analyze_one(
        loader.get_factor("ret_5"), market, fmat, fwd, masks, groups=5, horizon=1
    )
    notes = " ".join(res["notes"])
    assert "T+1 开盘" in notes
    assert "未强制" in notes, "出场可行性的近似必须如实标注"
    assert "算术年化" in notes


def test_analyze_run_writes_artifact(factor_env, monkeypatch) -> None:
    """`analyze.run` 必须真的把 JSON 写出来，并且能被服务层读回。"""
    n_d, n_s = 12, 30
    market, fmat, _fwd, masks = _perfect_case(
        n_dates=n_d, n_symbols=n_s, opens=_monotone_opens(n_d, n_s)
    )
    monkeypatch.setattr(analyze, "load_market", lambda *a, **k: market)
    monkeypatch.setattr(analyze, "build_investable_masks", lambda *a, **k: (masks, {"calendar": "test"}))
    _write_factor(
        "ret_5",
        [
            (d, s, float(j))
            for d, row in zip(market.dates, fmat, strict=False)
            for j, s in enumerate(market.symbols)
        ],
    )

    out = analyze.run(names=["ret_5"], start="2024-01-01", end="2024-12-31", groups=5, horizon=1)
    assert len(out) == 1
    path = analyze.analysis_path("ret_5")
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    # 因子值 = 标的序号，前瞻收益随序号单调递增 → IC 必须 = 1
    assert data["ic"]["mean"] == pytest.approx(1.0)
    assert data["settings"]["groups"] == 5
    assert data["settings"]["universe"]["calendar"] == "test"
    assert data["layered"]["nav_columns"] == ["G1", "G2", "G3", "G4", "G5", "LS"]


# ======================================================================
# 四、服务层
# ======================================================================
def test_list_factors_reports_states(factor_env) -> None:
    """三态必须分清楚：已计算 / 产物存在但无清单 / 清单有记录但文件缺失。"""
    _write_factor("ret_5", [("2024-01-02", "sh.600000", 0.05)])
    manifest.upsert("ret_5", {"rows": 1, "interval": {"start": "2024-01-02", "end": "2024-01-02"}})
    # 清单有记录但文件不在 → orphan
    manifest.upsert("today_pct", {"rows": 5, "interval": {"start": "2024-01-02", "end": "2024-01-02"}})

    factor_service.invalidate_cache()
    data = factor_service.list_factors()
    by_name = {f["name"]: f for f in data["factors"]}

    assert by_name["ret_5"]["state"] == "calculated"
    assert by_name["today_pct"]["state"] == "orphan"
    assert any("文件不存在" in w for w in by_name["today_pct"]["warnings"])
    assert by_name["rsi_14"]["state"] == "not_calculated"
    assert data["total"] == len(loader.list_factors(include_disabled=True))
    assert data["calculated"] == 1


def test_list_factors_unindexed_state(factor_env) -> None:
    """文件在但清单没记录 → 不能让页面显示"未计算"（那会让人白跑一遍）。"""
    _write_factor("ret_5", [("2024-01-02", "sh.600000", 0.05)])
    factor_service.invalidate_cache()
    item = {f["name"]: f for f in factor_service.list_factors()["factors"]}["ret_5"]
    assert item["state"] == "unindexed"
    assert item["calculated"] is True
    assert any("清单里没有记录" in w for w in item["warnings"])


def test_list_factors_category_counts_not_zeroed_by_filter(factor_env) -> None:
    """★ 按 category 过滤时，其它分类的**已计算数**不能变成 0。

    这类"过滤后被下游误用"是统计口径漂移的常见来源，看起来像"数据丢了"。
    """
    _write_factor("ret_5", [("2024-01-02", "sh.600000", 0.05)])
    manifest.upsert("ret_5", {"rows": 1, "interval": {"start": "2024-01-02", "end": "2024-01-02"}})
    factor_service.invalidate_cache()
    data = factor_service.list_factors(category="daily")
    daily = {c["key"]: c for c in data["categories"]}["daily"]
    assert daily["calculated_count"] == 1
    assert daily["factor_count"] == len(loader.list_factors("daily"))


def test_list_factors_unknown_category_raises() -> None:
    with pytest.raises(InvalidParamError):
        factor_service.list_factors(category="nope")


def test_list_factors_non_ready_category_exposes_note(factor_env) -> None:
    """非 ready 分类必须带着 note 返回 —— 否则用户以为系统漏了一类。

    不写死分类：本轮 fundamental 由 blocked 变 ready，写死的断言当场失效，
    而它要守的东西（"不能算的分类要说清为什么"）没变。改为遍历所有非 ready 分类。
    """
    cats = {c["key"]: c for c in factor_service.list_factors()["categories"]}
    non_ready = {k: c for k, c in cats.items() if not c["available"]}
    assert non_ready, "至少要有一个非 ready 分类，否则这个用例什么都没验证"
    for key, c in non_ready.items():
        assert c["note"].strip(), f"{key}（{c['status']}）必须带原因"

    # 本轮的状态与实现一致性
    assert cats["fundamental"]["available"], "基本面分类已启用"
    assert cats["combo"]["available"], "合成因子分类已启用"
    assert cats["minute"]["status"] == "planned"


def test_get_factor_unknown_raises_not_found(factor_env) -> None:
    """未注册的因子 → 404 而不是 500（"你问的因子不存在"≠"服务器配置坏了"）。"""
    with pytest.raises(NotFoundError):
        factor_service.get_factor("no_such_factor")


def test_get_series_aggregate_mean_and_median(factor_env) -> None:
    """★ 截面聚合必须给出正确的均值/中位数（手算核对）。"""
    _write_factor(
        "ret_5",
        [
            ("2024-01-02", "sh.600000", 1.0),
            ("2024-01-02", "sh.600001", 2.0),
            ("2024-01-02", "sh.600002", 3.0),
            ("2024-01-03", "sh.600000", 2.0),
            ("2024-01-03", "sh.600001", 4.0),
            ("2024-01-03", "sh.600002", 6.0),
        ],
    )
    r = factor_service.get_series("ret_5")
    assert r["mode"] == "cross_section_aggregate"
    assert r["dates"] == ["2024-01-02", "2024-01-03"]
    assert r["series"]["mean"] == [pytest.approx(2.0), pytest.approx(4.0)]
    assert r["series"]["median"] == [pytest.approx(2.0), pytest.approx(4.0)]
    assert r["count"] == [3, 3]


def test_get_series_symbols_mode(factor_env) -> None:
    _write_factor(
        "ret_5",
        [
            ("2024-01-02", "sh.600000", 0.1),
            ("2024-01-03", "sh.600000", 0.2),
            ("2024-01-02", "sz.000001", 0.3),
            ("2024-01-03", "sz.000001", 0.4),
        ],
    )
    r = factor_service.get_series("ret_5", symbols=["sh.600000", "sz.000001"])
    assert r["mode"] == "symbols"
    assert r["symbols"] == ["sh.600000", "sz.000001"]
    assert r["series"]["sh.600000"] == [pytest.approx(0.1), pytest.approx(0.2)]


def test_get_series_rejects_too_many_symbols(factor_env) -> None:
    syms = [f"sh.{i:06d}" for i in range(factor_service.MAX_SYMBOLS_PER_QUERY + 1)]
    with pytest.raises(InvalidParamError):
        factor_service.get_series("ret_5", symbols=syms)


def test_get_series_missing_factor_raises_not_found(factor_env) -> None:
    with pytest.raises(NotFoundError):
        factor_service.get_series("no_such_factor")


def test_get_cross_section_stats(factor_env) -> None:
    """截面统计：分位数、直方图计数之和必须等于样本数。"""
    rows = [("2024-01-02", f"sz.{i:06d}", float(i + 1)) for i in range(100)]
    _write_factor("ret_5", rows)
    manifest.upsert(
        "ret_5", {"rows": 100, "interval": {"start": "2024-01-02", "end": "2024-01-02"}}
    )
    factor_service.invalidate_cache()

    cs = factor_service.get_cross_section("ret_5", bins=10)
    assert cs["day"] == "2024-01-02"
    assert cs["count"] == 100
    assert cs["stats"]["mean"] == pytest.approx(50.5)
    assert cs["stats"]["median"] == pytest.approx(50.5)
    assert cs["quantiles"]["p50"] == pytest.approx(50.5)
    assert sum(cs["histogram"]["counts"]) == 100
    assert cs["extremes"]["top"][0]["symbol"] == "sz.000099"
    assert cs["extremes"]["bottom"][0]["symbol"] == "sz.000000"


def test_get_cross_section_default_day_uses_manifest_interval(factor_env) -> None:
    """不给 day 时取产物的**最后一天**（不是"今天"）—— 今天是周末就返回空表。"""
    _write_factor("ret_5", [("2024-01-02", "sh.600000", 1.0), ("2024-01-05", "sh.600000", 2.0)])
    manifest.upsert(
        "ret_5", {"rows": 2, "interval": {"start": "2024-01-02", "end": "2024-01-05"}}
    )
    factor_service.invalidate_cache()
    assert factor_service.get_cross_section("ret_5")["day"] == "2024-01-05"


def test_get_cross_section_non_trading_day_raises_not_ready(factor_env) -> None:
    _write_factor("ret_5", [("2024-01-02", "sh.600000", 1.0)])
    manifest.upsert(
        "ret_5", {"rows": 1, "interval": {"start": "2024-01-02", "end": "2024-01-02"}}
    )
    factor_service.invalidate_cache()
    with pytest.raises(DataNotReadyError, match="2024-01-06"):
        factor_service.get_cross_section("ret_5", day="2024-01-06")


def test_get_cross_section_rejects_bad_bins(factor_env) -> None:
    _write_factor("ret_5", [("2024-01-02", "sh.600000", 1.0)])
    with pytest.raises(InvalidParamError):
        factor_service.get_cross_section("ret_5", bins=2)


def test_layered_and_ic_raise_with_command_when_missing(factor_env) -> None:
    """★ 分析没跑过时，错误信息里必须带**可执行命令**，不能只说"不存在"。"""
    _write_factor("ret_5", [("2024-01-02", "sh.600000", 1.0)])
    for fn in (factor_service.get_layered, factor_service.get_ic):
        with pytest.raises(NotFoundError) as ei:
            fn("ret_5")
        assert "factors.analyze" in ei.value.message
        assert "ret_5" in ei.value.message


def test_layered_reads_artifact(factor_env) -> None:
    payload = {
        "name": "ret_5",
        "interval": {"start": "2024-01-02", "end": "2024-01-31", "dates_used": 20},
        "settings": {"groups": 5, "horizon": 1},
        "warnings": [],
        "notes": ["口径说明"],
        # ⚠️ 必须嵌在 "layered" 下 —— 与服务层/analyze 的产物结构一致。
        #    平铺写在这里会让 `group_summary` 变空，而断言会以"索引越界"的形式
        #    报出来，看起来像服务层的 bug。
        "layered": {
            "group_summary": [{"group": i + 1, "cum_return": 0.1 * i} for i in range(5)],
            "monotonicity": 1.0,
            "long_short": {"annual_gross": 0.2, "annual_net": 0.15},
            "dates": ["2024-01-02"],
            "nav": [[1.0, 1.0, 1.0, 1.0, 1.0, 1.0]],
            "nav_columns": ["G1", "G2", "G3", "G4", "G5", "LS"],
        },
    }
    analyze.analysis_path("ret_5").write_text(json.dumps(payload), encoding="utf-8")
    r = factor_service.get_layered("ret_5")
    assert r["group_summary"][4]["group"] == 5
    assert r["monotonicity"] == 1.0
    assert r["nav_columns"][-1] == "LS"
    assert r["notes"] == ["口径说明"]


def test_ic_downsamples_but_keeps_signs(factor_env) -> None:
    series = [{"date": f"2024-01-{i + 1:02d}", "ic": 0.1 if i % 2 else -0.1} for i in range(20)]
    payload = {
        "name": "ret_5",
        "interval": {"start": "2024-01-01", "end": "2024-01-20"},
        "settings": {},
        "ic": {"n_dates": 20, "mean": 0.0, "series": series},
        "warnings": [],
        "notes": [],
    }
    analyze.analysis_path("ret_5").write_text(json.dumps(payload), encoding="utf-8")
    r = factor_service.get_ic("ret_5", max_points=5)
    assert len(r["series"]) <= 5
    assert r["summary"]["mean"] == 0.0
    assert all(s["ic"] in (-0.1, 0.1) for s in r["series"]), "降采样不能改变符号"


def test_broken_analysis_json_raises_app_error(factor_env) -> None:
    analyze.analysis_path("ret_5").write_text("{oops", encoding="utf-8")
    from app.core.errors import AppError

    with pytest.raises(AppError, match="损坏"):
        factor_service.get_layered("ret_5")


def test_get_factor_detail_includes_analysis_summary(factor_env) -> None:
    _write_factor("ret_5", [("2024-01-02", "sh.600000", 1.0)])
    manifest.upsert(
        "ret_5",
        {
            "rows": 1,
            "non_null": 1,
            "nan_ratio": 0.0,
            "symbols": 1,
            "interval": {"start": "2024-01-02", "end": "2024-01-02"},
            "computed_at": "2026-09-20T09:00:00+08:00",
        },
    )
    analyze.analysis_path("ret_5").write_text(
        json.dumps(
            {
                "interval": {"start": "2024-01-02", "end": "2024-01-02"},
                "settings": {"groups": 5},
                "ic": {"mean": -0.03, "ir": -0.2, "series": []},
                "layered": {"group_summary": [], "long_short": {}, "monotonicity": 0.4},
                "warnings": ["测试警告"],
            }
        ),
        encoding="utf-8",
    )
    factor_service.invalidate_cache()
    d = factor_service.get_factor("ret_5")
    assert d["has_analysis"] is True
    assert d["analysis_summary"]["ic"]["mean"] == pytest.approx(-0.03)
    assert d["analysis_summary"]["monotonicity"] == pytest.approx(0.4)
    assert d["warnings"] == []


def test_list_factors_injects_analysis_summary(factor_env) -> None:
    """★ 列表行必须带轻量 analysis_summary —— 前端"IC 均值 / ICIR"两列的数据源。

    没有它两列永远显示 "—"（这正是 2026-09-20 前端反馈的问题）。
    """
    _write_factor("ret_5", [("2024-01-02", "sh.600000", 1.0)])
    analyze.analysis_path("ret_5").write_text(
        json.dumps(
            {
                "name": "ret_5",
                "settings": {"groups": 5, "horizon": 1},
                "ic": {"mean": -0.03, "ir": -0.2, "series": [{"date": "d", "ic": 0.1}]},
                "warnings": [],
            }
        ),
        encoding="utf-8",
    )
    factor_service.invalidate_cache()
    items = {f["name"]: f for f in factor_service.list_factors()["factors"]}

    s = items["ret_5"]["analysis_summary"]
    assert s["ic"]["mean"] == pytest.approx(-0.03)
    assert s["ic"]["ir"] == pytest.approx(-0.2)
    assert "series" not in s["ic"], "列表摘要不能带 IC 序列（白费带宽）"
    assert s["settings"]["horizon"] == 1

    # 没有分析产物的因子 → 空 dict（前端显示 "—"），不能报错
    assert items["rsi_14"]["analysis_summary"] == {}


def test_list_factors_summary_cache_respects_invalidate(factor_env) -> None:
    """摘要走 TTL 缓存：TTL 内改文件不生效，invalidate 后立即可见。"""
    path = analyze.analysis_path("ret_5")
    path.write_text(json.dumps({"name": "ret_5", "ic": {"mean": 0.1}}), encoding="utf-8")
    factor_service.invalidate_cache()
    got = {f["name"]: f for f in factor_service.list_factors()["factors"]}["ret_5"]
    assert got["analysis_summary"]["ic"]["mean"] == pytest.approx(0.1)

    path.write_text(json.dumps({"name": "ret_5", "ic": {"mean": 0.2}}), encoding="utf-8")
    got2 = {f["name"]: f for f in factor_service.list_factors()["factors"]}["ret_5"]
    assert got2["analysis_summary"]["ic"]["mean"] == pytest.approx(0.1), "TTL 内不应重读"

    factor_service.invalidate_cache()
    got3 = {f["name"]: f for f in factor_service.list_factors()["factors"]}["ret_5"]
    assert got3["analysis_summary"]["ic"]["mean"] == pytest.approx(0.2)


def test_list_factors_summary_skips_broken_json(factor_env) -> None:
    """损坏的分析产物只跳过该文件（摘要留空），不能拖垮整个列表接口。"""
    analyze.analysis_path("ret_5").write_text("{oops", encoding="utf-8")
    factor_service.invalidate_cache()
    items = {f["name"]: f for f in factor_service.list_factors()["factors"]}
    assert items["ret_5"]["analysis_summary"] == {}


def test_analysis_path_horizon_naming(factor_env) -> None:
    """h1 落 {name}.json（历史兼容），h≠1 落 {name}_h{n}.json（与 h1 不同文件）。"""
    assert analyze.analysis_path("ret_5").name == "ret_5.json"
    assert analyze.analysis_path("ret_5", 1).name == "ret_5.json"
    assert analyze.analysis_path("ret_5", 5).name == "ret_5_h5.json"
    assert analyze.analysis_path("ret_5", 10).name == "ret_5_h10.json"


def test_list_factors_summary_ignores_other_horizons(factor_env) -> None:
    """★ h5/h10 扫描产物与 h1 同名（产物内 name 相同）：列表摘要必须锚定 h1。"""
    analyze.analysis_path("ret_5").write_text(
        json.dumps(
            {"name": "ret_5", "settings": {"horizon": 1}, "ic": {"mean": -0.03}}
        ),
        encoding="utf-8",
    )
    analyze.analysis_path("ret_5", 5).write_text(
        json.dumps(
            {"name": "ret_5", "settings": {"horizon": 5}, "ic": {"mean": 0.99}}
        ),
        encoding="utf-8",
    )
    factor_service.invalidate_cache()
    items = {f["name"]: f for f in factor_service.list_factors()["factors"]}
    s = items["ret_5"]["analysis_summary"]
    assert s["settings"]["horizon"] == 1
    assert s["ic"]["mean"] == pytest.approx(-0.03), "h5 产物不能覆盖 h1 摘要"


def test_check_merges_definition_and_state(factor_env) -> None:
    data = factor_service.check(with_smoke=True)
    assert data["total"] == len(loader.list_factors(include_disabled=True))
    assert data["broken"] == 0
    assert all("state" in item for item in data["items"])
    assert data["checked_at"]


def test_search_symbols_filters(quote_dir) -> None:
    """按代码子串过滤（走文件名列举，不扫 Parquet）。"""
    assert "sh.600519" in factor_service.search_symbols("600519")
    assert factor_service.search_symbols("no_such_code") == []
    assert factor_service.search_symbols(None, limit=1)
    with pytest.raises(InvalidParamError):
        factor_service.search_symbols(None, limit=0)


def test_search_symbols_skips_underscore_files(quote_dir) -> None:
    """`_` 前缀是内部/临时文件的约定（如 `_test_lock.parquet`），不能出现在标的列表里。"""
    junk = quote_dir / "_test_lock.parquet"
    junk.write_bytes(b"x")
    assert not any(s.startswith("_") for s in factor_service.search_symbols(None, limit=500))
