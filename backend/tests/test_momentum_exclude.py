"""闸门 3.5「极端动量剔除」（规格 v1.3）的策略级测试。

测试对象：`MomentumStrategy._apply_extreme_momentum_exclusion` 及其在
`_buy_side` 里的接入效果。盯的是四件最要命的事：

1. **剔除确实生效**：momentum_20 截面最高分位的候选不得进入任何订单。
   （断言要同时看 trades **和** orders —— 只看 trades 会被"挂了单但
   没成交"糊弄过去。）
2. **exclude_top_pct = 0 必须是纯 v1.2 行为**：不读因子文件、不剔除。
   现有全部引擎测试都不装因子数据 —— 默认参数若去碰因子，全量测试就红了。
3. **数据缺失必须降级而不是崩**：因子文件缺失 / 当日截面缺失时跳过剔除，
   回测照常跑完 —— 但绝不允许悄悄变成"假装剔过了"。
4. **缺值不剔**：个股没有因子值是"无法判定"，不是"该剔"。

分位数构造说明（4 只标的、pct=0.25）：
    截面值 [F=0.00, A=0.10, B=0.11, C=0.12]
    q75（线性插值）= 0.11 + 0.25 × (0.12 − 0.11) = 0.1125
    → 只有 C(0.12) 被剔，A/B 保留。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app.services import data_store
from strategies.engine import BacktestEngine
from strategies.impl.momentum import MomentumStrategy

# ----------------------------------------------------------------------
# 合成行情（与 test_engine 同思路，独立成文避免跨测试文件 import 的脆依赖）
# ----------------------------------------------------------------------
DATES = pd.bdate_range("2020-01-01", periods=200)
DATE_STRS = [d.strftime("%Y-%m-%d") for d in DATES]
N = len(DATES)

FLAT_UNTIL = 100
SIGNAL_IDX = 125            # 信号日：当日 +4%，近 3 日累计 ≈ 5.4%，过开仓门槛
BT_START = DATE_STRS[101]
BT_END = DATE_STRS[-1]
PREHEAT = DATE_STRS[0]
INDEX_SYM = "sh.000001"

A_SYM = "sh.600001"         # ramp，momentum_20 = 0.10
B_SYM = "sh.600002"         # ramp，momentum_20 = 0.11
C_SYM = "sh.600003"         # ramp，momentum_20 = 0.12（最极端 → 应被剔）
F_SYM = "sh.600004"         # 横盘，momentum_20 = 0.00（本来就不会被买）

RAMP_SYMS = (A_SYM, B_SYM, C_SYM)


def _ramp(base: float, *, daily: float, signal_pct: float, tail_daily: float) -> np.ndarray:
    out = np.full(N, float(base))
    for i in range(FLAT_UNTIL + 1, SIGNAL_IDX):
        out[i] = out[i - 1] * (1 + daily)
    out[SIGNAL_IDX] = out[SIGNAL_IDX - 1] * (1 + signal_pct)
    for i in range(SIGNAL_IDX + 1, N):
        out[i] = out[i - 1] * (1 + tail_daily)
    return out


def _frame(symbol: str, closes: np.ndarray, *, spread: float = 0.02) -> pd.DataFrame:
    closes = np.asarray(closes, dtype=float)
    preclose = np.concatenate([[closes[0]], closes[:-1]])
    pct = np.where(preclose > 0, closes / preclose - 1.0, 0.0) * 100.0
    return pd.DataFrame(
        {
            "date": DATES,
            "symbol": symbol,
            "open": closes,
            "high": closes * (1 + spread),
            "low": closes * (1 - spread),
            "close": closes,
            "preclose": preclose,
            "pct_chg": pct,
            "trade_status": 1,
            "is_st": 0,
        }
    )


def _install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """4 只标的 + 基准指数（每日 +0.3% → 择时闸门通过）。"""
    daily = tmp_path / "quote_daily"
    index = tmp_path / "quote_index"
    daily.mkdir(parents=True, exist_ok=True)
    index.mkdir(parents=True, exist_ok=True)
    ramp = _ramp(10.0, daily=0.007, signal_pct=0.04, tail_daily=0.007)
    frames = {
        A_SYM: _frame(A_SYM, ramp),
        B_SYM: _frame(B_SYM, ramp),
        C_SYM: _frame(C_SYM, ramp),
        F_SYM: _frame(F_SYM, np.full(N, 10.0)),
    }
    for sym, df in frames.items():
        data_store.write_parquet(df, daily / f"{sym}.parquet")
    idx = _frame(INDEX_SYM, 3000.0 * np.cumprod(1 + np.full(N, 0.003)))
    data_store.write_parquet(idx, index / f"{INDEX_SYM}.parquet")
    monkeypatch.setattr(data_store, "QUOTE_DAILY_DIR", daily)
    monkeypatch.setattr(data_store, "QUOTE_INDEX_DIR", index)


def _install_factor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    values: dict[str, float | None],
    *,
    day: str | None = None,
) -> None:
    """把 momentum_20 因子写进临时 FACTOR_DIR。

    values: {symbol: value}；value=None 表示该标的**不写行**（模拟缺值）。
    day=None 表示整个文件都不写（模拟因子数据缺失）。
    """
    if day is None:
        monkeypatch.setattr(data_store, "FACTOR_DIR", tmp_path / "factor_empty")
        (tmp_path / "factor_empty").mkdir(parents=True, exist_ok=True)
        return
    fdir = tmp_path / "factor"
    fdir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(data_store, "FACTOR_DIR", fdir)
    rows = [
        {"date": pd.Timestamp(day), "symbol": sym, "value": val}
        for sym, val in values.items()
        if val is not None
    ]
    if rows:
        data_store.save_factor("momentum_20", pd.DataFrame(rows))


def _engine(params: dict | None = None) -> BacktestEngine:
    return BacktestEngine(
        MomentumStrategy(params),
        start=BT_START,
        end=BT_END,
        preheat_start=PREHEAT,
        initial_cash=1_000_000.0,
        max_positions=5,
        max_weight_per_stock=0.20,
        benchmark=INDEX_SYM,
    )


def _touched(res) -> set[str]:
    """出现过的标的 = 已成交交易 ∪ 委托流水。"""
    return set(res.trades["symbol"]).union(set(res.orders["symbol"]))


# ======================================================================
# 用例
# ======================================================================
def test_extreme_momentum_candidate_is_excluded(tmp_path, monkeypatch):
    """★ 最高分位候选不得出现在任何订单里；其余候选照买。"""
    _install(tmp_path, monkeypatch)
    _install_factor(
        tmp_path,
        monkeypatch,
        {A_SYM: 0.10, B_SYM: 0.11, C_SYM: 0.12, F_SYM: 0.00},
        day=DATE_STRS[SIGNAL_IDX],
    )

    res = _engine({"exclude_top_pct": 0.25}).run()

    touched = _touched(res)
    assert C_SYM not in touched, "截面最极端的候选应被闸门 3.5 剔除"
    assert A_SYM in touched and B_SYM in touched, "分位线以下的候选应保留"
    assert not res.trades.empty, "剔除后仍应有交易（A/B 照买）—— 否则是夹具失效"


def test_zero_pct_is_pure_v12_and_never_touches_factor(tmp_path, monkeypatch):
    """★ exclude_top_pct=0 是纯 v1.2 行为：因子文件不存在也照常跑、照常买。"""
    _install(tmp_path, monkeypatch)
    _install_factor(tmp_path, monkeypatch, {}, day=None)   # 目录都不建完整内容

    res = _engine(None).run()                              # 默认参数

    assert C_SYM in _touched(res), "默认参数下不应有剔除（v1.2 兼容）"
    assert not res.trades.empty


def test_missing_factor_file_degrades_gracefully(tmp_path, monkeypatch):
    """★ 因子文件缺失：跳过剔除 + 回测照常完成，绝不崩、绝不静默变 v1.3。"""
    _install(tmp_path, monkeypatch)
    _install_factor(tmp_path, monkeypatch, {}, day=None)

    res = _engine({"exclude_top_pct": 0.25}).run()

    assert C_SYM in _touched(res), "数据缺失时应降级为 v1.2 行为（不剔除）"
    assert not res.trades.empty, "降级后回测必须照常产生交易"


def test_missing_day_in_factor_skips_exclusion(tmp_path, monkeypatch):
    """★ 因子文件在但信号日不在：该日跳过剔除（首次告警），交易照常。"""
    _install(tmp_path, monkeypatch)
    _install_factor(
        tmp_path,
        monkeypatch,
        {A_SYM: 0.10, B_SYM: 0.11, C_SYM: 0.12, F_SYM: 0.00},
        day=DATE_STRS[SIGNAL_IDX - 1],                     # 截面缺信号日当天
    )

    res = _engine({"exclude_top_pct": 0.25}).run()

    assert C_SYM in _touched(res), "当日截面缺失应跳过剔除，而不是把全员误剔"


def test_missing_value_keeps_symbol_not_excludes_it(tmp_path, monkeypatch):
    """★ 个股缺因子值 = 无法判定 → 保留，绝不当成"该剔"处理。

    构造：C 的因子行不写。剩余截面 [0, 0.10, 0.11] 的 q75 = 0.105
    → B(0.11) 被剔、A(0.10) 留、C 缺值留。
    """
    _install(tmp_path, monkeypatch)
    _install_factor(
        tmp_path,
        monkeypatch,
        {A_SYM: 0.10, B_SYM: 0.11, C_SYM: None, F_SYM: 0.00},
        day=DATE_STRS[SIGNAL_IDX],
    )

    res = _engine({"exclude_top_pct": 0.25}).run()

    touched = _touched(res)
    assert C_SYM in touched, "缺因子值的候选不得被剔除"
    assert B_SYM not in touched, "缺值缩小截面后，分位线之上的候选仍应被剔"
    assert A_SYM in touched


def test_config_snapshot_records_v13_contract(tmp_path, monkeypatch):
    """config.json 快照必须能回答"当时跑的是哪套规则"。"""
    _install(tmp_path, monkeypatch)

    default = MomentumStrategy(None)
    assert default.params["exclude_top_pct"] == 0.0, "代码默认必须是 0（v1.2 兼容）"
    snap = default.config_snapshot()
    assert "v1.3" in snap["rules_version"]
    assert snap["exclude_factor"] == "momentum_20"
    assert snap["params"]["exclude_top_pct"] == 0.0

    explicit = MomentumStrategy({"exclude_top_pct": 0.2})
    assert explicit.params["exclude_top_pct"] == 0.2, "显式参数必须透传（v1.3 回测靠它）"
