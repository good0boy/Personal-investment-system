"""基本面因子（P0-1）与合成因子（P0-2）的测试。

★ 本文件的头号目标不是"函数能跑"，而是钉死**两条静默失效**：
  1. 财务数据按报告期对齐 → 回测读到尚未公告的业绩，**不报错、只让 IC 虚高**；
  2. 因子产物与行情行序错配 → 把 A 股票的值贴到 B 股票上，**同样不报错**。
  这两类错误都不会抛异常，只会在结果里体现为"看起来还不错的收益"。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.core.errors import AppError
from app.services import data_store
from factors.impl import combo as combo_impl
from factors.impl import fundamental as fund_impl
from tests.conftest import FUND_NOTICE_DATES, SAMPLE_DATES


@pytest.fixture(autouse=True)
def _clear_impl_caches():
    fund_impl.clear_caches()
    combo_impl.clear_caches()
    yield
    fund_impl.clear_caches()
    combo_impl.clear_caches()


def _quotes() -> pd.DataFrame:
    """用合成行情夹具的日期造一个 q（两只标的 × SAMPLE_DATES）。"""
    rows = []
    for sym in ("sh.600519", "sz.000001"):
        for d in SAMPLE_DATES:
            rows.append({"date": pd.Timestamp(d), "symbol": sym})
    return pd.DataFrame(rows).sort_values(["symbol", "date"], kind="stable").reset_index(drop=True)


# ======================================================================
# 一、as-of 对齐（本项目最危险的静默失效）
# ======================================================================
def test_fin_value_is_nan_before_first_notice(fundamental_dir):
    """首个公告日之前必须整段 NaN —— 这就是"未来函数"防线。"""
    q = _quotes()
    first_notice = pd.Timestamp(FUND_NOTICE_DATES[0])

    val = fund_impl.fin_value(q, "ROEJQ")

    before = q["date"] < first_notice
    assert before.any(), "夹具应当包含公告日之前的交易日，否则本用例没验证任何东西"
    assert val[before].isna().all(), "公告日之前拿到了财务数据 = 读到了未来"


def test_fin_value_switches_exactly_on_notice_date(fundamental_dir):
    """切换点必须**正好**落在公告日：公告日当天就能用，前一天还不能。"""
    q = _quotes()
    val = fund_impl.fin_value(q, "ROEJQ")
    n0, n1 = pd.Timestamp(FUND_NOTICE_DATES[0]), pd.Timestamp(FUND_NOTICE_DATES[1])

    sh = (q["symbol"] == "sh.600519").to_numpy()
    dates = q["date"].to_numpy()

    # 第一期（ROEJQ=10.0）覆盖 [n0, n1)
    in_first = sh & (dates >= n0.to_datetime64()) & (dates < n1.to_datetime64())
    assert in_first.any()
    assert (val[in_first] == 10.0).all()

    # 第二期（ROEJQ=11.0）覆盖 [n1, ∞)
    in_second = sh & (dates >= n1.to_datetime64())
    assert in_second.any()
    assert (val[in_second] == 11.0).all()


def test_fin_value_matches_manual_asof(fundamental_dir):
    """逐日值必须与手工 as-of（取 notice_date <= 交易日的最后一行）完全一致。"""
    q = _quotes()
    val = fund_impl.fin_value(q, "ROEJQ")
    raw = data_store.read_fundamental("financial_indicator", ["sh.600519"], columns=["ROEJQ"])

    expect = []
    for _, row in q[q["symbol"] == "sh.600519"].iterrows():
        cand = raw[raw["notice_date"] <= row["date"]]
        expect.append(float(cand.iloc[-1]["ROEJQ"]) if len(cand) else np.nan)
    got = val[(q["symbol"] == "sh.600519").to_numpy()].to_numpy()
    assert np.allclose(got, np.array(expect), equal_nan=True)


def test_fin_value_annualize_only_when_asked(fundamental_dir):
    """annualize 参数必须真的生效，且系数取决于**该期自己的报告期**。

    口径：×4/已过季度数 —— 2023-12-31（年报，4 个季度）→ ×1；
          2024-06-30（中报，2 个季度）→ ×2。
    """
    q = _quotes()
    raw = fund_impl.fin_value(q, "ROEJQ")
    ann = fund_impl.fin_value(q, "ROEJQ", annualize=True)

    n1 = pd.Timestamp(FUND_NOTICE_DATES[1])
    second = (q["date"] >= n1).to_numpy()
    first = (q["date"] < n1).to_numpy()
    assert second.any() and first.any(), "夹具要同时覆盖两期，否则测不出系数差异"

    # 第一期是年报 → 系数 1（取值不变）；第二期中报 → 系数 2
    assert np.allclose(ann[first].to_numpy(), raw[first].to_numpy(), equal_nan=True)
    assert np.allclose(ann[second].to_numpy(), raw[second].to_numpy() * 2.0, equal_nan=True)
    # 反例守卫：若实现错把"所有期都乘 2"，上面第一条就会红


def test_fin_value_raises_with_actionable_message_on_unknown_field(fundamental_dir):
    """字段不存在时要给出"指向该做什么"的报错，而不是裸 KeyError。"""
    with pytest.raises(AppError) as ei:
        fund_impl.fin_value(_quotes(), "NOT_A_FIELD")
    msg = ei.value.message
    assert "NOT_A_FIELD" in msg
    assert "WHITELISTS" in msg or "可用字段" in msg


def test_fin_value_empty_dataset_raises(monkeypatch, tmp_path):
    """数据集为空时要提示回补命令，而不是返回一列 NaN。"""
    empty = tmp_path / "fundamental"
    (empty / "financial_indicator").mkdir(parents=True)
    monkeypatch.setattr(data_store, "FUNDAMENTAL_DIR", empty)
    with pytest.raises(AppError) as ei:
        fund_impl.fin_value(_quotes(), "ROEJQ")
    assert "backfill_fundamental" in ei.value.message


# ======================================================================
# 二、data_store.read_fundamental 的时间锁与回退
# ======================================================================
def test_read_fundamental_falls_back_when_notice_missing(tmp_path, monkeypatch):
    """公告日缺失 → 回退 report_date + 90 天（宁晚不早，不得当成报告日可用）。"""
    d = tmp_path / "fundamental"
    (d / "financial_indicator").mkdir(parents=True)
    df = pd.DataFrame(
        {
            "symbol": ["sh.600519"],
            "report_date": pd.to_datetime(["2024-06-30"]),
            "notice_date": pd.to_datetime([pd.NaT]),
            "ROEJQ": [5.0],
        }
    )
    data_store.write_parquet(df, d / "financial_indicator" / "sh.600519.parquet")
    monkeypatch.setattr(data_store, "FUNDAMENTAL_DIR", d)

    out = data_store.read_fundamental("financial_indicator", ["sh.600519"])
    assert not out.empty
    assert out["notice_date"].iloc[0] == pd.Timestamp("2024-09-28")


def test_read_fundamental_truncates_by_time_lock(fundamental_dir):
    """未公告的财报必须读不出来（时间锁防线）。"""
    full = data_store.read_fundamental("financial_indicator")
    n_all = len(full)
    assert n_all > 0

    with data_store.read_until("2024-06-01"):
        cut = data_store.read_fundamental("financial_indicator")
    assert len(cut) < n_all, "时间锁没有截断掉任何一行 —— 防线失效"
    assert cut["notice_date"].max() <= pd.Timestamp("2024-06-01")


def test_read_fundamental_empty_symbol_list_returns_empty(fundamental_dir):
    """显式空列表 = 无标的可读，不可退化成全量（read_quotes 的同一条纪律）。"""
    out = data_store.read_fundamental("financial_indicator", [])
    assert out.empty


def test_read_fundamental_unknown_dataset_returns_empty(fundamental_dir):
    assert data_store.read_fundamental("no_such_dataset").empty


# ======================================================================
# 三、冒烟自检的数据替换钩子
# ======================================================================
def test_loader_smoke_uses_synthetic_data_hook():
    """有 `synthetic_data()` 钩子的实现，自检必须走合成数据（不依赖盘上真实数据）。"""
    from factors import loader

    defn = loader.get_factor("roe_annualized")
    assert loader.smoke_test(defn) is None, "基本面因子在无真实数据的环境下也应能通过自检"


def test_loader_smoke_hook_is_generic_not_hardcoded():
    """钩子按"实现模块是否提供"发现，不按分类硬编码（否则新数据依赖就得改 loader）。"""
    import inspect

    from factors import loader

    src = inspect.getsource(loader.smoke_test)
    # 去掉 docstring 再断言 —— 说明文字里提到具体分类是被允许的，
    # 不允许的是**代码里**出现 `if category == "fundamental"` 这类分支。
    code = src.split('"""')[2] if src.count('"""') >= 2 else src
    assert "synthetic_data" in code
    assert "fundamental" not in code


# ======================================================================
# 四、合成因子
# ======================================================================
def _write_factor(factor_dir, name: str, values: pd.DataFrame, category: str = "daily") -> None:
    (factor_dir / category).mkdir(parents=True, exist_ok=True)
    data_store.write_parquet(values, factor_dir / category / f"{name}.parquet",
                             dedup_keys=("date", "symbol"))


SYMS3 = ("sh.600519", "sh.600000", "sz.000001")


def _q3() -> pd.DataFrame:
    """3 只标的 × SAMPLE_DATES 的 q（3 只才能让截面秩出现 1/3、2/3、1 三档）。"""
    rows = [{"date": pd.Timestamp(d), "symbol": s} for d in SAMPLE_DATES for s in SYMS3]
    return pd.DataFrame(rows).sort_values(["symbol", "date"], kind="stable").reset_index(drop=True)


def _factor_frame(values: list[float]) -> pd.DataFrame:
    """每天按 SYMS3 的顺序给 3 个值（`values` 长度必须是 3）。"""
    return pd.DataFrame(
        [
            {"date": pd.Timestamp(d), "symbol": s, "value": v}
            for d in SAMPLE_DATES
            for s, v in zip(SYMS3, values)
        ]
    )


def _first_day_values(q: pd.DataFrame, out: pd.Series) -> np.ndarray:
    """取首个交易日的合成值，按 SYMS3 的顺序返回。

    ⚠️ `q` 是按 (symbol, date) 排序的，所以 `out.iloc[:3]` 是"同一只股票的前 3 天"，
    不是"同一天的前 3 只股票" —— 按行号取会让断言看起来对、其实测错了东西。
    """
    d0 = pd.Timestamp(SAMPLE_DATES[0])
    sub = q.assign(_v=out.to_numpy())
    sub = sub[sub["date"] == d0].set_index("symbol")
    return sub.loc[list(SYMS3), "_v"].to_numpy()


def test_rank_combo_equals_component_rank_when_aligned(factor_dir, monkeypatch):
    """两个组件排序完全一致 → 合成值 = 该排序的百分位秩（1.0 / 0.667 / 0.333）。"""
    q = _q3()
    _write_factor(factor_dir, "fa", _factor_frame([3.0, 2.0, 1.0]))
    _write_factor(factor_dir, "fb", _factor_frame([9.0, 5.0, 1.0]))
    monkeypatch.setattr(combo_impl, "_component_category", lambda n: "daily")

    out = combo_impl.rank_combo(q, ["fa", "fb"])
    # pandas rank(pct=True) = 平均秩 / 个数 → 1/3、2/3、1（**不是** 0、0.5、1）
    assert np.allclose(_first_day_values(q, out), [1.0, 2 / 3, 1 / 3])


def test_rank_combo_is_constant_when_components_are_anti_aligned(factor_dir, monkeypatch):
    """完全反向的两个组件 → 合成值恒定（秩的和与排序无关）。这钉死了秩的定义。"""
    q = _q3()
    _write_factor(factor_dir, "fa", _factor_frame([3.0, 2.0, 1.0]))
    _write_factor(factor_dir, "fb", _factor_frame([1.0, 2.0, 3.0]))
    monkeypatch.setattr(combo_impl, "_component_category", lambda n: "daily")

    out = combo_impl.rank_combo(q, ["fa", "fb"])
    assert np.allclose(_first_day_values(q, out), [2 / 3, 2 / 3, 2 / 3])


def test_rank_combo_signs_flip_values(factor_dir, monkeypatch):
    q = _q3()
    _write_factor(factor_dir, "fa", _factor_frame([3.0, 2.0, 1.0]))
    monkeypatch.setattr(combo_impl, "_component_category", lambda n: "daily")

    pos = combo_impl.rank_combo(q, ["fa"])
    neg = combo_impl.rank_combo(q, ["fa"], signs=[-1])
    assert np.allclose(pos.to_numpy(), -neg.to_numpy(), equal_nan=True)


def test_rank_combo_nan_component_makes_row_nan(factor_dir, monkeypatch):
    """任一组件缺值 → 整行 NaN（**不能用 0 顶替**：0 在秩空间里是最低分）。"""
    q = _q3()
    frame = _factor_frame([3.0, 2.0, 1.0])
    frame.loc[0, "value"] = np.nan  # 首个交易日 × SYMS3[0]
    _write_factor(factor_dir, "fa", frame)
    _write_factor(factor_dir, "fb", _factor_frame([1.0, 2.0, 3.0]))
    monkeypatch.setattr(combo_impl, "_component_category", lambda n: "daily")

    out = combo_impl.rank_combo(q, ["fa", "fb"])
    d0, s0 = pd.Timestamp(SAMPLE_DATES[0]), SYMS3[0]
    missing_row = (q["date"] == d0) & (q["symbol"] == s0)
    assert missing_row.sum() == 1
    assert np.isnan(out[missing_row.to_numpy()]).all()
    # 其余行不受影响（别把整列都搞成 NaN）
    assert out[~missing_row.to_numpy()].notna().all()


def test_rank_combo_raises_on_length_mismatch(factor_dir, monkeypatch):
    """长度不一致时必须显式报错 —— 绝不能"能用就用"地错配行。"""
    q = _quotes()
    short = pd.DataFrame(
        [{"date": pd.Timestamp(SAMPLE_DATES[0]), "symbol": "sh.600519", "value": 1.0}]
    )
    _write_factor(factor_dir, "fa", short)
    monkeypatch.setattr(combo_impl, "_component_category", lambda n: "daily")

    with pytest.raises(AppError) as ei:
        combo_impl.rank_combo(q, ["fa"])
    assert "factors.calc" in ei.value.message  # 报错要指向该跑什么命令


def test_rank_combo_raises_on_same_length_wrong_keys(factor_dir, monkeypatch):
    """长度相同但 (date, symbol) 对不上 → 也必须报错（最危险的情况）。"""
    q = _quotes()  # 2 只标的 × 12 天 = 24 行
    wrong = pd.DataFrame(
        [
            {"date": pd.Timestamp(d), "symbol": s, "value": 1.0}
            for d in SAMPLE_DATES
            for s in ("sh.600000", "sz.000002")  # 与 q 完全不重叠的标的
        ]
    )
    assert len(wrong) == len(q), "本用例的前提是长度相同、只有键不同"
    _write_factor(factor_dir, "fa", wrong)
    monkeypatch.setattr(combo_impl, "_component_category", lambda n: "daily")

    with pytest.raises(AppError) as ei:
        combo_impl.rank_combo(q, ["fa"])
    assert "对不上" in ei.value.message


def test_rank_combo_rejects_mismatched_argument_lengths(factor_dir, monkeypatch):
    q = _quotes()
    rows = [
        {"date": pd.Timestamp(d), "symbol": s, "value": 1.0}
        for d in SAMPLE_DATES
        for s in ("sh.600519", "sz.000001")
    ]
    _write_factor(factor_dir, "fa", pd.DataFrame(rows))
    monkeypatch.setattr(combo_impl, "_component_category", lambda n: "daily")
    with pytest.raises(AppError):
        combo_impl.rank_combo(q, ["fa"], weights=[1.0, 2.0])


def test_rank_combo_rejects_empty_components():
    with pytest.raises(AppError):
        combo_impl.rank_combo(_quotes(), [])


# ======================================================================
# 五、注册表层面：新增的两个分类必须真的可发现
# ======================================================================
def test_registry_exposes_new_categories_and_factors():
    from factors import loader

    keys = [c.key for c in loader.categories()]
    assert "fundamental" in keys and "combo" in keys
    fund = loader.category_of("fundamental")
    assert fund is not None and fund.status == "ready", "基本面分类应当已启用（不再是 blocked）"
    assert len(loader.list_factors("fundamental")) >= 6
    assert len(loader.list_factors("combo")) >= 2


def test_all_registered_factors_pass_smoke():
    """全文自检：任何新增因子都不许"写得进去、调不起来"。"""
    from factors import loader

    bad = [r for r in loader.check(with_smoke=True) if r["status"] != "ok"]
    assert bad == [], f"因子自检未通过：{[(r['name'], r['message']) for r in bad]}"
