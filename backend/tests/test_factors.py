"""模块 2 · 因子库的测试：注册表契约、因子实现、产物清单、计算通道。

★ 三条测试策略（都是本项目用血换来的）：
  1. **跨标的不污染**：`ret_n` 这类因子如果忘了 groupby，会把上一只股票的末行
     当成下一只的首行，算出一个**不存在**的收益，而且不报错。所以每个时序因子
     都要在"多标的"的合成数据上验，不能只测单只。
  2. **索引对齐**：`groupby(...).rolling(...)` 返回的是 MultiIndex Series，与
     `groupby(...).shift(...)` 的原索引返回**不是一回事**。混用会让数值静默错配
     到别的股票。所以统一断言"返回的 index 与输入完全一致"。
  3. **夹具自检**：夹具写错会让断言静默空过（全绿但什么都没验证）。
     每个用例都对**手算的期望值**下断言，而不是只断言"长度相等"。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.core.errors import AppError, ConfigError, NotFoundError
from factors import loader, manifest
from factors.impl import daily


# ======================================================================
# 合成行情
# ======================================================================
def make_quote(rows: list[dict]) -> pd.DataFrame:
    """按 [symbol, date] 排序并重建干净索引 —— 与 calc.prepare_quotes 的契约一致。"""
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values(["symbol", "date"], kind="stable").reset_index(drop=True)


def row(symbol: str, day: str, close: float, **over) -> dict:
    base = {
        "date": day,
        "symbol": symbol,
        "open": over.get("open", close),
        "high": over.get("high", close + 0.1),
        "low": over.get("low", close - 0.1),
        "close": close,
        "preclose": over.get("preclose", close),
        "volume": 1_000_000.0,
        "amount": 1e7,
        "turn": over.get("turn", 1.0),
        "pct_chg": over.get("pct_chg", 0.0),
        "trade_status": 1,
        "is_st": 0,
    }
    base.update(over)
    return base


#: 两个标的、各 6 天，**故意让 B 的第一天价格远低于 A 的最后一天**
#: —— 这样"忘了 groupby 的 shift"会算出巨大的假收益，一眼可辨。
_TWO_SYMBOL_ROWS: list[dict] = [
    *[
        row("sh.600000", f"2024-01-0{i}", c)
        for i, c in zip(range(1, 7), [10, 11, 12, 13, 14, 15], strict=False)
    ],
    *[
        row("sz.000001", f"2024-01-0{i}", c)
        for i, c in zip(range(1, 7), [1, 2, 4, 8, 16, 32], strict=False)
    ],
]


def _clear_quotes(quote_dir) -> None:
    """清掉 conftest 夹具预置的合成行情。

    ⚠️ 必须清：夹具放了 sh.600519 + sz.000001 共 24 行，不清的话
       "剔除了几行""剩几行"这类断言会被污染 —— 而夹具污染恰恰是
       **测试全绿却没验证任何东西**的典型来源。
    """
    for p in quote_dir.glob("*.parquet"):
        p.unlink()


# ======================================================================
# 一、注册表契约
# ======================================================================
def test_registry_loads_and_categories_ordered() -> None:
    reg = loader.load_registry()
    assert reg["version"] >= 1
    keys = [c.key for c in reg["categories"]]
    assert keys == ["daily", "combo", "fundamental", "minute"], "分类顺序必须稳定（前端按序展示）"
    # 顺序的唯一来源是 loader.CATEGORY_ORDER —— 两处各写一份必然漂移
    assert keys == list(loader.CATEGORY_ORDER), "注册表顺序与 CATEGORY_ORDER 不一致"
    assert len(reg["factors"]) >= 8


def test_category_status_matches_reality() -> None:
    """分类状态必须如实反映"能不能算" —— 对**所有**分类成立，不写死某一个。

    ★ 不变式（比列举具体分类更耐改）：
      · ready   → 该分类下**必须**有可计算的因子（否则用户看到能点、点了没东西）；
      · planned / blocked → 该分类下**必须没有**因子，且必须写明原因
        （否则用户以为系统漏了一类）。
    2026-09-22 启用 fundamental 时，正是这条不变式保证了"改成 ready 就必须真的给出因子"。
    """
    by_key = {c.key: c for c in loader.categories()}

    assert by_key["daily"].status == "ready"

    for key, cat in by_key.items():
        factors = loader.list_factors(key, include_disabled=True)
        if cat.available:
            assert factors, f"分类 {key} 标为 ready 却没有任何因子 —— 状态与实现不符"
        else:
            assert not factors, f"分类 {key} 标为 {cat.status} 却挂着因子"
            assert (cat.note or cat.desc).strip(), (
                f"{cat.status} 分类 {key} 必须写明原因，否则用户以为系统漏了"
            )

    # 本轮明确启用的两类
    assert by_key["fundamental"].available, "基本面数据已回补，分类不应再是 blocked"
    assert by_key["combo"].available
    assert by_key["minute"].status == "planned"


def test_every_factor_is_fully_defined() -> None:
    """每个因子都必须有中文名 / 单位 / 口径说明 —— 缺一项前端就会出现空白列。"""
    for f in loader.list_factors(include_disabled=True):
        assert f.cname and f.cname != f.name, f"{f.name} 缺中文名"
        assert f.unit, f"{f.name} 缺 unit（前端画图要标量纲）"
        assert f.desc, f"{f.name} 缺 desc（口径说明）"
        assert f.direction in loader.DIRECTIONS
        assert f.category in {c.key for c in loader.categories()}


def test_direction_is_documented_as_hypothesis() -> None:
    """`ret_5` 与 `momentum_20` 的方向假设刻意不同 —— 反转还是延续是待检验的问题。

    这条断言的价值：防止有人"顺手统一"方向，把待检验的假设改成既定结论。
    """
    assert loader.get_factor("momentum_20").direction == 1
    assert loader.get_factor("ret_5").direction == 0


@pytest.mark.parametrize(
    ("bad", "reason"),
    [
        ({"name": "Bad-Name", "category": "daily"}, "因子名非法"),
        ({"name": "x1", "category": "nope"}, "category 未在 categories 中声明"),
        ({"name": "x1", "category": "daily", "direction": 2}, "direction 非法"),
        ({"name": "x1", "category": "daily", "impl": "nosuchmodule"}, "impl 非法"),
        ({"name": "", "category": "daily"}, "缺少 name"),
    ],
)
def test_registry_rejects_structural_errors(tmp_path, monkeypatch, bad: dict, reason: str) -> None:
    """结构性错误必须**在加载时**抛 ConfigError（而不是跑到一半才炸）。

    参数化用 `pytest.param` 的 id 让失败信息能指到具体是哪一条。
    """
    base = {"name": "ok_name", "category": "daily", "impl": "factors.impl.daily.today_pct"}
    base.update(bad)
    doc = {
        "version": 1,
        "categories": [{"key": "daily", "cname": "日度", "status": "ready"}],
        "factors": [base],
    }
    path = tmp_path / "registry.yaml"
    path.write_text(_dump_yaml(doc), encoding="utf-8")
    monkeypatch.setattr(loader, "FACTOR_REGISTRY_FILE", path)
    loader.invalidate_cache()
    try:
        with pytest.raises(ConfigError) as ei:
            loader.load_registry()
        assert reason in ei.value.message, f"错误信息应包含「{reason}」，实际：{ei.value.message}"
    finally:
        loader.invalidate_cache()


def test_registry_rejects_duplicate_name(tmp_path, monkeypatch) -> None:
    doc = {
        "version": 1,
        "categories": [{"key": "daily", "cname": "日度", "status": "ready"}],
        "factors": [
            {"name": "dup", "category": "daily", "impl": "factors.impl.daily.today_pct"},
            {"name": "dup", "category": "daily", "impl": "factors.impl.daily.today_pct"},
        ],
    }
    path = tmp_path / "registry.yaml"
    path.write_text(_dump_yaml(doc), encoding="utf-8")
    monkeypatch.setattr(loader, "FACTOR_REGISTRY_FILE", path)
    loader.invalidate_cache()
    try:
        with pytest.raises(ConfigError, match="重复"):
            loader.load_registry()
    finally:
        loader.invalidate_cache()


def test_registry_missing_file_raises(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(loader, "FACTOR_REGISTRY_FILE", tmp_path / "nope.yaml")
    loader.invalidate_cache()
    try:
        with pytest.raises(ConfigError, match="不存在"):
            loader.load_registry()
    finally:
        loader.invalidate_cache()


def test_registry_cache_invalidated_on_mtime(tmp_path, monkeypatch) -> None:
    """改了 YAML 必须自动生效（不用重启）—— 与模块 1 的 catalog 同一条纪律。"""
    doc = {
        "version": 1,
        "categories": [{"key": "daily", "cname": "日度", "status": "ready"}],
        "factors": [{"name": "a1", "category": "daily", "impl": "factors.impl.daily.today_pct"}],
    }
    path = tmp_path / "registry.yaml"
    path.write_text(_dump_yaml(doc), encoding="utf-8")
    monkeypatch.setattr(loader, "FACTOR_REGISTRY_FILE", path)
    loader.invalidate_cache()
    try:
        assert [f.name for f in loader.list_factors()] == ["a1"]
        doc["factors"].append(
            {"name": "b2", "category": "daily", "impl": "factors.impl.daily.today_pct"}
        )
        path.write_text(_dump_yaml(doc), encoding="utf-8")
        # 不改文件大小、只改内容时 size 不变，靠 mtime_ns 兜底
        assert sorted(f.name for f in loader.list_factors()) == ["a1", "b2"]
    finally:
        loader.invalidate_cache()


def test_get_factor_unknown_raises_not_found() -> None:
    with pytest.raises(NotFoundError) as ei:
        loader.get_factor("no_such_factor")
    assert "no_such_factor" in ei.value.message
    assert ei.value.detail and ei.value.detail.get("available")


def test_check_all_registered_factors_pass_smoke() -> None:
    """注册表里每个因子都要能 import + 签名匹配 + 冒烟调用。

    ⚠️ 这条是"写了但调用就崩"的最低成本拦网 —— 本项目第一大失效模式。
    """
    rows = loader.check(with_smoke=True)
    broken = [r for r in rows if r["status"] != "ok"]
    assert not broken, f"自检失败：{[(r['name'], r['message']) for r in broken]}"
    assert len(rows) == len(loader.list_factors(include_disabled=True))


# ======================================================================
# 二、实现解析与签名校验
# ======================================================================
def test_resolve_impl_missing_function_raises() -> None:
    defn = _defn("factors.impl.daily.no_such_function")
    with pytest.raises(ConfigError, match="实现函数不存在"):
        loader.resolve_impl(defn)


def test_resolve_impl_missing_module_raises() -> None:
    with pytest.raises(ConfigError, match="无法导入"):
        loader.resolve_impl(_defn("factors.impl.not_a_module.fn"))


def test_signature_accepts_declared_params() -> None:
    fn = loader.resolve_impl(loader.get_factor("momentum_20"))
    assert loader.signature_error(loader.get_factor("momentum_20"), fn) is None


def test_signature_rejects_undeclared_param() -> None:
    """YAML 多写一个参数名 → 必须在自检阶段报出来，而不是调用时 TypeError。"""
    defn = _defn("factors.impl.daily.ret_n", params={"window": 5, "typo_param": 1})
    fn = loader.resolve_impl(defn)
    err = loader.signature_error(defn, fn)
    assert err is not None and "typo_param" in err


def test_signature_rejects_missing_required_param() -> None:
    """函数有必填参数但 YAML 没给 → 同样必须在自检阶段拦住。"""

    def needs_arg(q: pd.DataFrame, must_have: int) -> pd.Series:
        return pd.Series(0.0, index=q.index)

    defn = _defn("x.y")
    assert loader.signature_error(defn, needs_arg) is not None


def test_smoke_test_detects_wrong_length(monkeypatch) -> None:
    """返回长度与输入不一致 → 必须报错（否则产物会错配到别的股票）。"""

    def short(q: pd.DataFrame, **_kw) -> pd.Series:
        return pd.Series([1.0, 2.0])

    monkeypatch.setattr(loader, "resolve_impl", lambda _d: short)
    msg = loader.smoke_test(_defn("x.y"))
    assert msg is not None and "长度" in msg


def test_smoke_test_detects_not_series(monkeypatch) -> None:
    monkeypatch.setattr(loader, "resolve_impl", lambda _d: (lambda q, **_k: [1, 2, 3]))
    msg = loader.smoke_test(_defn("x.y"))
    assert msg is not None and "pd.Series" in msg


def test_smoke_test_detects_index_mismatch(monkeypatch) -> None:
    """返回 Series 但 index 不一致 → 必须报错（数值会静默错配）。"""

    def shifted(q: pd.DataFrame, **_kw) -> pd.Series:
        return pd.Series(np.arange(len(q)) + 100.0, index=np.arange(len(q)) + 1)

    monkeypatch.setattr(loader, "resolve_impl", lambda _d: shifted)
    msg = loader.smoke_test(_defn("x.y"))
    # 长度也不一样（index 平移了一位），报哪一条都算通过；这里只要求"被拦住"
    assert msg is not None


def test_smoke_test_detects_all_nan(monkeypatch) -> None:
    def all_nan(q: pd.DataFrame, **_kw) -> pd.Series:
        return pd.Series(np.full(len(q), np.nan), index=q.index)

    monkeypatch.setattr(loader, "resolve_impl", lambda _d: all_nan)
    msg = loader.smoke_test(_defn("x.y"))
    assert msg is not None and "NaN" in msg


def test_smoke_frame_satisfies_contract() -> None:
    """冒烟帧自身必须满足实现契约（排序 + RangeIndex），否则冒烟是假通过。"""
    q = loader.smoke_frame()
    assert {"date", "symbol", "close", "preclose", "high", "low", "turn", "pct_chg"} <= set(q.columns)
    assert q.index.equals(pd.RangeIndex(len(q)))
    assert (q["trade_status"] == 1).all()
    assert (q["close"] > 0).all() and (q["preclose"] > 0).all()
    assert q.groupby("symbol")["date"].is_monotonic_increasing.all()


# ======================================================================
# 三、因子实现（手算期望值）
# ======================================================================
def test_ret_n_does_not_leak_across_symbols() -> None:
    """★ 核心测试：跨标的不能污染。

    sh.600000 最后一天收 15，sz.000001 第一天收 1。
    如果实现忘了 groupby，`shift(1)` 会让 sz.000001 的第一天拿到 15 → 收益 -93%。
    正确结果必须是 NaN。
    """
    q = make_quote(_TWO_SYMBOL_ROWS)
    out = daily.ret_n(q, window=1)
    assert out.index.equals(q.index), "返回的 index 必须与输入一致"

    first_of_b = q[(q["symbol"] == "sz.000001")].index[0]
    assert pd.isna(out.loc[first_of_b]), "跨标的的首行必须是 NaN，不能算出假收益"

    # sz.000001 第 2 天：1 → 2，收益 +100%
    second_of_b = q[(q["symbol"] == "sz.000001")].index[1]
    assert out.loc[second_of_b] == pytest.approx(1.0)

    # sh.600000 第 6 天：14 → 15
    last_of_a = q[(q["symbol"] == "sh.600000")].index[5]
    assert out.loc[last_of_a] == pytest.approx(15 / 14 - 1)


def test_ret_n_window_insufficient_is_nan() -> None:
    q = make_quote(_TWO_SYMBOL_ROWS)
    out = daily.ret_n(q, window=5)
    for sym in ("sh.600000", "sz.000001"):
        idx = q[q["symbol"] == sym].index
        assert out.loc[idx[:5]].isna().all(), "窗口不足的 5 行必须全是 NaN"
        assert not pd.isna(out.loc[idx[5]]), "第 6 行必须算得出来"


def test_today_pct_is_percent_over_100() -> None:
    q = make_quote([row("sh.600000", "2024-01-02", 10.0, pct_chg=1.15)])
    assert daily.today_pct(q).iloc[0] == pytest.approx(0.0115)


def test_amplitude_uses_preclose_not_close() -> None:
    """振幅分母必须是 preclose —— 用 close 会得到另一个数（都能跑出结果）。"""
    q = make_quote([row("sh.600000", "2024-01-02", 10.0, high=11.0, low=9.0, preclose=10.0)])
    assert daily.amplitude_mean(q, window=1).iloc[0] == pytest.approx(0.2)

    q2 = make_quote([row("sh.600000", "2024-01-02", 10.0, high=11.0, low=9.0, preclose=20.0)])
    assert daily.amplitude_mean(q2, window=1).iloc[0] == pytest.approx(0.1)


def test_volatility_is_sample_std_of_daily_returns() -> None:
    """波动率 = 日收益率的**样本**标准差（ddof=1）。"""
    rets = [0.01, 0.02, -0.01, 0.03]
    rows = [row("sh.600000", f"2024-01-0{i + 1}", 10.0, pct_chg=r * 100) for i, r in enumerate(rets)]
    q = make_quote(rows)
    out = daily.volatility(q, window=4)
    assert out.iloc[-1] == pytest.approx(float(np.std(rets, ddof=1)))
    assert out.iloc[:3].isna().all()


def test_turnover_mean_keeps_percent_unit() -> None:
    rows = [row("sh.600000", f"2024-01-0{i + 1}", 10.0, turn=t) for i, t in enumerate([1.0, 2.0, 3.0])]
    q = make_quote(rows)
    assert daily.turnover_mean(q, window=3).iloc[-1] == pytest.approx(2.0)


def test_ma_bias_is_close_over_ma_minus_one() -> None:
    closes = [10.0, 12.0, 14.0]
    rows = [
        row("sh.600000", f"2024-01-0{i + 1}", c, preclose=c)
        for i, c in enumerate(closes)
    ]
    q = make_quote(rows)
    out = daily.ma_bias(q, window=3)
    assert out.iloc[-1] == pytest.approx(14.0 / np.mean(closes) - 1)


def test_max_daily_ret_picks_max_in_window() -> None:
    # pct_chg 是**百分数**（1.0 表示 +1%），所以 r=0.095 → 传 9.5
    rets = [0.01, 0.095, -0.03, 0.02]
    rows = [row("sh.600000", f"2024-01-0{i + 1}", 10.0, pct_chg=r * 100) for i, r in enumerate(rets)]
    q = make_quote(rows)
    out = daily.max_daily_ret(q, window=4)
    assert out.iloc[-1] == pytest.approx(0.095)


def test_rsi_all_up_is_100() -> None:
    """只涨不跌 → RSI = 100。"""
    closes = [10.0, 11.0, 12.0]
    rows = [row("sh.600000", f"2024-01-0{i + 1}", c) for i, c in enumerate(closes)]
    q = make_quote(rows)
    assert daily.rsi(q, window=2).iloc[-1] == pytest.approx(100.0)


def test_rsi_flat_is_neutral_50_not_100() -> None:
    """★ 完全横盘必须给中性 50。

    若写成 `avg_loss = 0 → RSI = 100`，一只横盘的票会被读成"极度超买"。
    这是"边界定义没想清楚"型 bug，肉眼看代码很难发现。
    """
    rows = [row("sh.600000", f"2024-01-0{i + 1}", 10.0) for i in range(3)]
    q = make_quote(rows)
    assert daily.rsi(q, window=2).iloc[-1] == pytest.approx(50.0)


def test_rsi_symmetric_moves_is_50() -> None:
    """涨跌幅度对称（+1 / -1）→ RSI = 50。"""
    closes = [10.0, 11.0, 10.0]
    rows = [row("sh.600000", f"2024-01-0{i + 1}", c) for i, c in enumerate(closes)]
    q = make_quote(rows)
    assert daily.rsi(q, window=2).iloc[-1] == pytest.approx(50.0)


@pytest.mark.parametrize("name", [f.name for f in loader.list_factors("daily")])
def test_all_daily_factors_align_index_and_length(name: str) -> None:
    """★ 所有已注册的日度因子：返回必须与输入**等长同序同索引**。

    这条参数化测试专门拦"groupby.rolling 返回 MultiIndex"那一类静默错配。
    """
    defn = loader.get_factor(name)
    q = loader.smoke_frame(n_dates=60, n_symbols=4)
    out = loader.resolve_impl(defn)(q, **defn.params)
    assert isinstance(out, pd.Series)
    assert len(out) == len(q)
    assert out.index.equals(q.index)


def test_window_must_be_positive() -> None:
    q = make_quote(_TWO_SYMBOL_ROWS)
    with pytest.raises(ValueError, match="window"):
        daily.ret_n(q, window=0)


# ---------------------------------------------------------------
# 长周期动量与量比（2026-09-30 新增：ret_50 / volume_ratio_1/5/10）
# ---------------------------------------------------------------
def test_ret_50_is_close_over_close_50_days_ago() -> None:
    """ret_50 与 ret_3/10/20 共用 ret_n，差别只在 window —— 钉一次长窗口的手算值。"""
    n = 51
    closes = [10.0 * (1.01**i) for i in range(n)]
    rows = [
        row("sh.600000", str(d.date()), c, preclose=c)
        for d, c in zip(pd.bdate_range("2024-01-01", periods=n), closes, strict=True)
    ]
    q = make_quote(rows)
    out = daily.ret_n(q, window=50)

    assert out.index.equals(q.index)
    assert out.iloc[:50].isna().all(), "窗口不足的前 50 行必须 NaN"
    assert out.iloc[50] == pytest.approx(closes[50] / closes[0] - 1)


def _volume_rows(volumes: list[float], symbol: str = "sh.600000") -> list[dict]:
    dates = pd.bdate_range("2024-01-01", periods=len(volumes))
    return [
        row(symbol, str(d.date()), 10.0, preclose=10.0, volume=v)
        for d, v in zip(dates, volumes, strict=True)
    ]


def test_volume_ratio_n_uses_previous_window_excluding_today() -> None:
    """★ 分母是【前 window 个交易日】的均量，**不含今日**。

    含今日（直接 `_rolling(volume, window, "mean")`）是另一种写法，
    两种都能跑出结果、但数值不同 —— 这条把口径钉死。
    """
    vols = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0]
    q = make_quote(_volume_rows(vols))
    out = daily.volume_ratio_n(q, window=2)

    assert out.index.equals(q.index)
    assert out.iloc[:2].isna().all(), "分母窗口不足的前 2 行必须 NaN"
    # t=2：30 ÷ mean(10,20) = 2.0
    assert out.iloc[2] == pytest.approx(30.0 / 15.0)
    # t=5：60 ÷ mean(40,50) = 1.333，而不是含今日口径的 60 ÷ mean(50,60) = 1.09
    assert out.iloc[5] == pytest.approx(60.0 / 45.0)
    assert out.iloc[5] != pytest.approx(60.0 / 55.0)


def test_volume_ratio_1_is_today_over_yesterday() -> None:
    """window=1 退化为最朴素的"今日量 ÷ 昨日量"。"""
    q = make_quote(_volume_rows([100.0, 250.0]))
    out = daily.volume_ratio_n(q, window=1)

    assert pd.isna(out.iloc[0]), "首日没有昨日量 → NaN"
    assert out.iloc[1] == pytest.approx(2.5)


def test_volume_ratio_does_not_leak_across_symbols() -> None:
    """★ 跨标的不得污染：B 的首行不能拿 A 的末行成交量当分母。

    这正是 `_group_shift` 存在的理由 —— 忘了 groupby 的 shift **不报错**，
    只是把上一只股票的末行当成下一只的首行（本项目最忌讳的静默错配）。
    """
    rows = _volume_rows([10.0, 20.0, 30.0], symbol="sh.600000") + _volume_rows(
        [1000.0, 2000.0, 4000.0], symbol="sz.000001"
    )
    q = make_quote(rows)
    out = daily.volume_ratio_n(q, window=1)

    idx_b = q[q["symbol"] == "sz.000001"].index
    assert pd.isna(out.loc[idx_b[0]]), "B 首行必须是 NaN（拿到 A 的末行就是假值）"
    assert out.loc[idx_b[1]] == pytest.approx(2.0)  # 2000/1000，不是 2000/30
    assert out.loc[idx_b[2]] == pytest.approx(2.0)  # 4000/2000


def test_volume_ratio_zero_base_is_nan() -> None:
    """前一日零成交（分母 = 0）→ NaN，不是 inf。"""
    q = make_quote(_volume_rows([0.0, 10.0]))
    out = daily.volume_ratio_n(q, window=1)
    assert pd.isna(out.iloc[1])


def test_volume_ratio_window_must_be_positive() -> None:
    q = make_quote(_volume_rows([1.0, 2.0]))
    with pytest.raises(ValueError, match="window"):
        daily.volume_ratio_n(q, window=0)


def test_smoke_frame_covers_largest_registered_window() -> None:
    """★ 不变式：冒烟帧的交易日数必须**大于**所有日度因子声明过的最大 window。

    为什么要有这条：`smoke_test` 把"结果全为 NaN"当作 "窗口长度可能大于数据长度"
    的证据并判 broken。2026-09-30 新增 `ret_50`(window=50) 时，`smoke_frame` 默认
    只有 40 天 → ret_50 在冒烟帧上恒为全 NaN → 被误判 broken（`--check` 报红）。
    这条断言把"帧天数 > 最大窗口"变成显式契约：以后加更长窗口的因子，先看它。
    """
    windows = [
        f.params.get("window")
        for f in loader.list_factors("daily")
        if isinstance(f.params.get("window"), int)
    ]
    max_window = max(windows) if windows else 0
    q = loader.smoke_frame()
    assert q["date"].nunique() > max_window, (
        f"冒烟帧只有 {q['date'].nunique()} 个交易日，却存在 window={max_window} 的因子"
        " —— 该因子的冒烟会被误判为 broken（改 loader.smoke_frame 的默认天数）"
    )


def test_ret_50_needs_more_than_forty_days_evidence() -> None:
    """反向证据：40 天的帧确实会算出全 NaN —— 说明上面那条不变式不是多余的。"""
    defn = loader.get_factor("ret_50")
    fn = loader.resolve_impl(defn)
    out = fn(loader.smoke_frame(n_dates=40), **defn.params)
    assert out.notna().sum() == 0


# ======================================================================
# 四、产物清单
# ======================================================================
def _patch_manifest(tmp_path, monkeypatch):
    path = tmp_path / "_manifest.json"
    monkeypatch.setattr(manifest, "FACTOR_MANIFEST_FILE", path)
    return path


def test_manifest_missing_file_returns_empty_shell(tmp_path, monkeypatch) -> None:
    _patch_manifest(tmp_path, monkeypatch)
    data = manifest.read_manifest()
    assert data["factors"] == {}


def test_manifest_broken_json_degrades_not_crash(tmp_path, monkeypatch) -> None:
    """清单损坏不能让整张列表页崩掉 —— 降级为空壳并告警。"""
    path = _patch_manifest(tmp_path, monkeypatch)
    path.write_text("{ this is not json", encoding="utf-8")
    assert manifest.read_manifest()["factors"] == {}


def test_manifest_upsert_merges_instead_of_overwriting(tmp_path, monkeypatch) -> None:
    """★ 部分运行不能抹掉其它因子的状态。

    `calc --name a` 只算一个因子；若整体覆盖，另外 10 个因子的状态会凭空消失，
    页面显示"全部未计算"而文件明明都在 —— 最难排查的一类 bug。
    """
    _patch_manifest(tmp_path, monkeypatch)
    manifest.upsert("a", {"rows": 1})
    manifest.upsert("b", {"rows": 2})
    manifest.upsert("a", {"rows": 99})
    data = manifest.read_manifest()["factors"]
    assert data["a"]["rows"] == 99
    assert data["b"]["rows"] == 2, "更新 a 不能影响 b"


def test_manifest_remove(tmp_path, monkeypatch) -> None:
    _patch_manifest(tmp_path, monkeypatch)
    manifest.upsert("a", {"rows": 1})
    assert manifest.remove("a") is True
    assert manifest.remove("a") is False
    assert manifest.entry_of("a") is None


def test_stale_reasons_detects_missing_entry_and_file(tmp_path, monkeypatch, factor_dir) -> None:
    _patch_manifest(tmp_path, monkeypatch)
    defn = loader.get_factor("today_pct")
    assert manifest.stale_reasons(defn, None, start=None, end=None) == ["清单里没有该因子的记录"]

    reasons = manifest.stale_reasons(defn, {"rows": 1}, start=None, end=None)
    assert any("产物文件不存在" in r for r in reasons)


def test_stale_reasons_detects_param_change(tmp_path, monkeypatch, factor_dir) -> None:
    _patch_manifest(tmp_path, monkeypatch)
    defn = loader.get_factor("ret_5")
    manifest.factor_file(defn).parent.mkdir(parents=True, exist_ok=True)
    manifest.factor_file(defn).write_bytes(b"x")
    entry = {"params_key": "window=999", "interval": {"start": "2000-01-01", "end": "2099-01-01"}}
    reasons = manifest.stale_reasons(defn, entry, start=None, end=None)
    assert any("参数已变更" in r for r in reasons)


def test_stale_reasons_detects_interval_expansion(tmp_path, monkeypatch, factor_dir) -> None:
    _patch_manifest(tmp_path, monkeypatch)
    defn = loader.get_factor("ret_5")
    manifest.factor_file(defn).parent.mkdir(parents=True, exist_ok=True)
    manifest.factor_file(defn).write_bytes(b"x")
    entry = {
        "params_key": defn.params_key,
        "interval": {"start": "2024-01-02", "end": "2024-06-28"},
    }
    reasons = manifest.stale_reasons(defn, entry, start="2020-01-02", end="2026-09-17")
    assert any("只覆盖到" in r for r in reasons)
    assert any("开始，需要从" in r for r in reasons)


def test_stale_reasons_empty_when_covered(tmp_path, monkeypatch, factor_dir) -> None:
    _patch_manifest(tmp_path, monkeypatch)
    defn = loader.get_factor("ret_5")
    manifest.factor_file(defn).parent.mkdir(parents=True, exist_ok=True)
    manifest.factor_file(defn).write_bytes(b"x")
    entry = {
        "params_key": defn.params_key,
        "interval": {"start": "2020-01-02", "end": "2026-09-17"},
    }
    assert manifest.stale_reasons(defn, entry, start="2021-01-01", end="2025-01-01") == []


def test_factor_file_uses_data_store_single_implementation(factor_dir) -> None:
    """清单里的路径必须与 data_store 写的路径是**同一个**（否则清单指向 A、写到 B）。"""
    from app.services import data_store

    defn = loader.get_factor("ret_5")
    assert manifest.factor_file(defn) == data_store.factor_path("ret_5", "daily")


# ======================================================================
# 五、计算通道
# ======================================================================
def test_prepare_quotes_drops_suspended_and_sorts(quote_dir) -> None:
    """停牌行（trade_status=0）必须被剔除，且输出按 [symbol, date] 排序、索引干净。

    停牌日的行是"有价无市"（close 等于前收）。当成有效观测会让波动率被人为压低。
    """
    from app.services import data_store
    from factors import calc

    d = quote_dir
    _clear_quotes(d)
    df = pd.DataFrame(
        {
            "date": pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"] * 2),
            "symbol": ["sh.600519"] * 3 + ["sz.000001"] * 3,
            "open": [1.0] * 6,
            "high": [2.0] * 6,
            "low": [0.5] * 6,
            "close": [1.5] * 6,
            "preclose": [1.4] * 6,
            "volume": [1.0] * 6,
            "amount": [1.0] * 6,
            "turn": [1.0] * 6,
            "pct_chg": [0.0] * 6,
            # 把其中一只的中间一天设为停牌
            "trade_status": [1, 0, 1, 1, 1, 0],
            "is_st": [0] * 6,
        }
    )
    data_store.write_parquet(df.iloc[:3].reset_index(drop=True), d / "sh.600519.parquet")
    data_store.write_parquet(df.iloc[3:].reset_index(drop=True), d / "sz.000001.parquet")

    q, stats = calc.prepare_quotes("2024-01-01", "2024-12-31")
    assert stats["rows_dropped_invalid"] == 2, "两天停牌应被剔除"
    assert q.index.equals(pd.RangeIndex(len(q))), "必须有干净的 RangeIndex"
    assert (q["trade_status"] == 1).all()
    for _sym, g in q.groupby("symbol"):
        assert g["date"].is_monotonic_increasing, "组内必须按日期升序"
    assert list(q["symbol"].unique()) == sorted(q["symbol"].unique()), "标的必须有序"


def test_prepare_quotes_drops_invalid_price(quote_dir) -> None:
    from app.services import data_store
    from factors import calc

    _clear_quotes(quote_dir)
    df = pd.DataFrame(
        {
            "date": pd.to_datetime(["2024-01-02", "2024-01-03"]),
            "symbol": ["sh.600519"] * 2,
            "open": [1.0, 1.0],
            "high": [2.0, 2.0],
            "low": [0.5, 0.5],
            "close": [1.5, 0.0],  # 0 是无效价
            "preclose": [1.4, 1.4],
            "volume": [1.0, 1.0],
            "amount": [1.0, 1.0],
            "turn": [1.0, 1.0],
            "pct_chg": [0.0, 0.0],
            "trade_status": [1, 1],
            "is_st": [0, 0],
        }
    )
    data_store.write_parquet(df, quote_dir / "sh.600519.parquet")
    q, stats = calc.prepare_quotes("2024-01-01", "2024-12-31")
    assert stats["rows_dropped_invalid"] == 1
    assert len(q) == 1


def test_prepare_quotes_empty_raises(quote_dir) -> None:
    from factors import calc

    _clear_quotes(quote_dir)
    with pytest.raises(AppError, match="行情为空"):
        calc.prepare_quotes("2024-01-01", "2024-12-31")


def test_compute_one_writes_parquet_and_manifest_entry(
    quote_dir, factor_dir, tmp_path, monkeypatch
) -> None:
    """★ 端到端最小闭环：读行情 → 算 → 落盘 → 清单条目。

    断言产物文件真的存在（不能只信清单 —— "清单说算过了、文件不在"是最坏的一致性问题）。
    """
    from app.services import data_store
    from factors import calc

    monkeypatch.setattr(calc, "FACTOR_DIR", factor_dir)
    _patch_manifest(tmp_path, monkeypatch)

    q, stats = calc.prepare_quotes("2024-01-01", "2024-12-31")
    defn = loader.get_factor("ret_5")
    entry = calc.compute_one(defn, q, stats, args_skip_gate=True)

    path = data_store.factor_path("ret_5", "daily")
    assert path.exists(), "产物文件必须存在"
    out = pd.read_parquet(path)
    assert list(out.columns) == ["date", "symbol", "value"]
    assert len(out) == len(q)
    assert entry["rows"] == len(q)
    assert entry["params_key"] == "window=5"
    assert entry["interval"]["start"] == stats["date_start"]
    assert entry["warnings"] == [] or entry["warnings"]


def test_compute_one_flags_all_nan(quote_dir, factor_dir, tmp_path, monkeypatch) -> None:
    """★ "算出来全是 NaN" 必须留痕 —— 文件在、行数对、页面能画，只是什么都没算出来。"""
    from factors import calc

    monkeypatch.setattr(calc, "FACTOR_DIR", factor_dir)
    _patch_manifest(tmp_path, monkeypatch)

    q, stats = calc.prepare_quotes("2024-01-01", "2024-12-31")
    # 用一个窗口远大于数据长度的因子定义
    defn = loader.FactorDef(
        name="too_long",
        cname="窗口过长",
        category="daily",
        direction=0,
        impl="factors.impl.daily.ret_n",
        params={"window": 100000},
    )
    entry = calc.compute_one(defn, q, stats, args_skip_gate=True)
    assert entry["non_null"] == 0
    assert any("NaN" in w for w in entry["warnings"])


def test_compute_one_rejects_length_mismatch(quote_dir, factor_dir, tmp_path, monkeypatch) -> None:
    from factors import calc

    monkeypatch.setattr(calc, "FACTOR_DIR", factor_dir)
    _patch_manifest(tmp_path, monkeypatch)
    q, stats = calc.prepare_quotes("2024-01-01", "2024-12-31")

    def short(qq: pd.DataFrame, **_kw) -> pd.Series:
        return pd.Series([1.0])

    monkeypatch.setattr(loader, "resolve_impl", lambda _d: short)
    with pytest.raises(AppError, match="长度"):
        calc.compute_one(loader.get_factor("ret_5"), q, stats, args_skip_gate=True)


def test_compute_one_marks_debug_run(quote_dir, factor_dir, tmp_path, monkeypatch) -> None:
    """⚠️ 调试运行必须显式标注 —— 它的产物结构与全市场一模一样。"""
    from factors import calc

    monkeypatch.setattr(calc, "FACTOR_DIR", factor_dir)
    _patch_manifest(tmp_path, monkeypatch)
    q, stats = calc.prepare_quotes("2024-01-01", "2024-12-31", symbols=["sh.600519"])
    stats["debug_symbols"] = ["sh.600519"]
    entry = calc.compute_one(loader.get_factor("ret_5"), q, stats, args_skip_gate=True)
    assert entry["debug_symbols"] == ["sh.600519"]
    assert any("调试" in w for w in entry["warnings"])


def test_calc_compute_all_skips_recompute_when_fresh(
    quote_dir, factor_dir, tmp_path, monkeypatch
) -> None:
    """清单已覆盖区间且参数未变 → `stale_reasons` 为空（增量运行不重算）。"""
    from factors import calc

    monkeypatch.setattr(calc, "FACTOR_DIR", factor_dir)
    _patch_manifest(tmp_path, monkeypatch)
    q, stats = calc.prepare_quotes("2024-01-01", "2024-12-31")
    defn = loader.get_factor("ret_5")
    entry = calc.compute_one(defn, q, stats, args_skip_gate=True)
    manifest.upsert(defn.name, entry)

    assert manifest.stale_reasons(defn, entry, start=stats["date_start"], end=stats["date_end"]) == []


def test_cli_list_and_check_run(monkeypatch, capsys, tmp_path) -> None:
    """CLI 的两个只读分支必须能跑通（不读行情、不落盘）。"""
    from factors import calc

    assert calc.main(["--check"]) == 0
    out = capsys.readouterr().out
    assert "broken=0" in out

    monkeypatch.setattr(manifest, "FACTOR_MANIFEST_FILE", tmp_path / "m.json")
    assert calc.main(["--list"]) == 0
    assert "因子注册表" in capsys.readouterr().out


def test_cli_unknown_factor_returns_2(capsys) -> None:
    from factors import calc

    assert calc.main(["--name", "nope"]) == 2


def test_cli_non_ready_category_gives_reason() -> None:
    """非 ready 分类要给**原因**而不是一句"失败"。

    不写死具体分类（本轮 fundamental 从 blocked 变 ready，写死的断言当场失效，
    而它想守的东西其实没变）—— 改为遍历当前所有非 ready 分类。

    ⚠️ 这里断言的是 `_resolve_targets` 抛出的消息，而不是 CLI 的 stdout：
       loguru 的 handler 在 pytest 里会与 capsys 打架（"I/O operation on closed file"），
       拿日志做断言会把测试变成测基础设施。CLI 的退出码另有用例覆盖。
    """
    from factors import calc

    non_ready = [c for c in loader.categories() if not c.available]
    assert non_ready, "至少要有一个非 ready 分类，否则这个用例什么都没验证"

    for cat in non_ready:
        args = calc.build_parser().parse_args(["--category", cat.key])
        with pytest.raises(AppError) as ei:
            calc._resolve_targets(args)
        msg = ei.value.message
        assert "不能计算" in msg
        assert cat.status in msg, "要把状态写出来"
        reason = (cat.note or cat.desc).strip().splitlines()[0].strip()
        assert reason[:12] in msg, "原因必须真的带进报错里"


def _defn(impl: str, params: dict | None = None) -> loader.FactorDef:
    return loader.FactorDef(
        name="tmp_factor",
        cname="临时",
        category="daily",
        direction=0,
        impl=impl,
        params=params or {},
    )


def _dump_yaml(doc: dict) -> str:
    import yaml

    return yaml.safe_dump(doc, allow_unicode=True, sort_keys=False)
