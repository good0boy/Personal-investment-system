"""交易成本模型测试。

★ 本文件盯的是**回测里最容易悄悄算错、且错了不容易发现**的一组数字：
印花税分段、最低佣金、整手取整。它们错了不会报错，只会让回测收益虚高或虚低。

规格依据：`策略规格说明书.md` v1.2 §2.4（成本表）与 §7.1 第 2 条
（止盈必须扣成本，门槛换算约毛涨 10.22% / 10.28%）。
"""
from __future__ import annotations

import pytest

from app.core.errors import AppError
from app.services import cost_model as cm

#: 印花税减半的分界点两侧各取一个交易日
BEFORE_CUT = "2023-08-25"   # 0.1%
AFTER_CUT = "2024-01-02"    # 0.05%


# ======================================================================
# 费率（纯比例口径）—— 规格文档里那三个数字必须能复现
# ======================================================================
def test_buy_rate_matches_spec():
    """单边买入成本率 = 佣金 0.025% + 过户费 0.001% + 滑点 0.05% = 0.076%。"""
    assert cm.buy_rate() == pytest.approx(0.00076, abs=1e-9)


def test_sell_rate_matches_spec_both_tax_regimes():
    """单边卖出成本率：0.126%（2023-08 后）/ 0.176%（之前）。"""
    assert cm.sell_rate(AFTER_CUT) == pytest.approx(0.00126, abs=1e-9)
    assert cm.sell_rate(BEFORE_CUT) == pytest.approx(0.00176, abs=1e-9)


def test_round_trip_rate_matches_spec():
    """往返成本率：0.202% / 0.252% —— 这是本策略"最大的隐性风险"。"""
    assert cm.round_trip_rate(AFTER_CUT) == pytest.approx(0.00202, abs=1e-9)
    assert cm.round_trip_rate(BEFORE_CUT) == pytest.approx(0.00252, abs=1e-9)


def test_stamp_tax_is_segmented_at_2023_08_28():
    """★ 回测区间横跨印花税减半，必须分段。混用会让一半区间少算/多算一倍。"""
    assert cm.stamp_tax_rate("2020-06-01") == pytest.approx(0.001)
    assert cm.stamp_tax_rate(BEFORE_CUT) == pytest.approx(0.001)
    assert cm.stamp_tax_rate("2023-08-28") == pytest.approx(0.0005)
    assert cm.stamp_tax_rate(AFTER_CUT) == pytest.approx(0.0005)


def test_stamp_tax_raises_when_outside_configured_tiers():
    """★ 档位缺失必须报错，**不能静默返回 0** —— 那会让这部分区间白送收益。"""
    with pytest.raises(AppError) as ei:
        cm.stamp_tax_rate("1999-01-04")
    assert "印花税档位" in ei.value.message


# ======================================================================
# 金额口径 —— 最低佣金与整手
# ======================================================================
def test_commission_min_applies_below_20k():
    """佣金 max(rate×金额, 5)：阈值正好是 5 / 0.00025 = 2 万元。

    规格 §2.4：20% 仓位 = 20 万元时佣金 50 元、最低值不生效；
    但当净值跌到 10 万以下（单笔 < 2 万元）时最低值会生效。
    """
    small = cm.buy_cost(10_000.0)
    assert small.commission == pytest.approx(5.0), "1 万元按比例只有 2.5 元，必须抬到 5 元"

    exact = cm.buy_cost(20_000.0)
    assert exact.commission == pytest.approx(5.0), "2 万元是临界点（比例值恰好 = 5）"

    big = cm.buy_cost(200_000.0)
    assert big.commission == pytest.approx(50.0), "20 万元时佣金 50 元，最低值不生效"


def test_buy_cost_has_no_stamp_tax():
    """印花税是卖出单边，买入恒为 0。"""
    assert cm.buy_cost(100_000.0).stamp_tax == 0.0


def test_sell_cost_includes_stamp_tax_by_date():
    a = cm.sell_cost(100_000.0, AFTER_CUT)
    b = cm.sell_cost(100_000.0, BEFORE_CUT)
    assert a.stamp_tax == pytest.approx(50.0)
    assert b.stamp_tax == pytest.approx(100.0)
    assert b.total - a.total == pytest.approx(50.0)


def test_cost_breakdown_total_is_sum_of_parts():
    c = cm.sell_cost(123_456.0, AFTER_CUT)
    assert c.total == pytest.approx(
        c.commission + c.transfer_fee + c.stamp_tax + c.slippage
    )


def test_buy_paid_and_sell_net_round_trip():
    """买入实付 > 金额，卖出净得 < 金额 —— 两头都被摩擦吃掉一块。"""
    assert cm.buy_paid(10.0, 1000) > 10_000.0
    assert cm.sell_net(10.0, 1000, AFTER_CUT) < 10_000.0


# ======================================================================
# 整手与可买股数
# ======================================================================
def test_lots_for_amount_rounds_down_to_lot():
    """整手 100 股，向下取整（多出来的钱留着，不能买 150 股）。"""
    assert cm.lots_for_amount(1_000.0, 10.0) == 100
    assert cm.lots_for_amount(1_099.0, 10.0) == 100
    assert cm.lots_for_amount(1_199.0, 10.0) == 100
    assert cm.lots_for_amount(999.0, 10.0) == 0


def test_lots_for_amount_rejects_bad_input():
    assert cm.lots_for_amount(10_000.0, 0) == 0
    assert cm.lots_for_amount(0.0, 10.0) == 0
    assert cm.lots_for_amount(-1.0, 10.0) == 0


def test_max_affordable_shares_never_overspends():
    """★ 现金能买多少股 —— 必须**连费用一起买得起**，否则下单会因现金不足失败。

    只按比例估算会在小额时超支（佣金最低 5 元抬高了实际成本率）。
    """
    for cash in (500.0, 1_000.0, 5_000.0, 19_999.0, 20_001.0, 1_000_000.0):
        for price in (3.27, 10.0, 68.88, 1_700.5):
            shares = cm.max_affordable_shares(cash, price)
            assert shares % 100 == 0, "必须整手"
            if shares > 0:
                assert cm.buy_paid(price, shares) <= cash + 1e-6, (
                    f"cash={cash} price={price} shares={shares} "
                    f"实付={cm.buy_paid(price, shares):.2f} 超支"
                )
                # 再多买一手就一定买不起了（说明没有过分保守）
                assert cm.buy_paid(price, shares + 100) > cash


# ======================================================================
# 净收益率 —— 止盈判定的唯一实现
# ======================================================================
def test_net_return_equals_spec_formula_for_large_position():
    """★ 关键一致性：精确版（含最低佣金）在大额下单时必须与规格公式等价。

    规格公式：`close_hfq×(1-卖出成本率) / (entry×(1+买入成本率)) - 1`
    本项目 20% 仓位 ≈ 20 万元 > 2 万元 → 最低佣金不生效 → 两者应当一致。
    """
    entry, exit_, shares = 10.0, 10.5, 20_000   # 20 万元一档
    gross = exit_ / entry - 1

    precise = cm.net_return(entry, exit_, AFTER_CUT, shares)
    formula = (1 + gross) * (1 - cm.sell_rate(AFTER_CUT)) / (1 + cm.buy_rate()) - 1

    assert precise == pytest.approx(formula, abs=1e-9)


def test_gross_return_threshold_matches_spec_table():
    """★ 规格 §2.3 的换算表：净 10% ⇔ 毛涨约 10.28%（之前）/ 10.22%（之后）。

    这两个数字是"止盈门槛被成本抬高了多少"的唯一凭证，必须与文档一致。
    """
    assert cm.gross_return_for_net(0.10, BEFORE_CUT) == pytest.approx(0.1028, abs=1e-4)
    assert cm.gross_return_for_net(0.10, AFTER_CUT) == pytest.approx(0.1022, abs=1e-4)


def test_net_return_threshold_roundtrip():
    """反向自洽：按换算出的毛涨幅成交，净收益应当正好是目标值。"""
    for day in (BEFORE_CUT, AFTER_CUT):
        gross = cm.gross_return_for_net(0.10, day)
        entry = 100.0
        exit_ = entry * (1 + gross)
        got = cm.net_return(entry, exit_, day, 20_000)
        assert got == pytest.approx(0.10, abs=1e-9)


def test_net_return_is_negative_when_price_flat():
    """价格不动也是亏 —— 摩擦力本身就是成本。"""
    assert cm.net_return(10.0, 10.0, AFTER_CUT, 20_000) < 0


def test_net_return_rejects_non_positive_shares():
    with pytest.raises(AppError):
        cm.net_return(10.0, 11.0, AFTER_CUT, 0)


def test_net_return_handles_small_position_with_higher_cost():
    """小额下单时最低佣金生效 → 净收益比按比例算的更差（成本占比更高）。"""
    shares = 500                      # 5000 元一档 < 2 万元
    entry, exit_ = 10.0, 11.0
    precise = cm.net_return(entry, exit_, AFTER_CUT, shares)
    formula = (exit_ / entry) * (1 - cm.sell_rate(AFTER_CUT)) / (1 + cm.buy_rate()) - 1
    assert precise < formula, "最低佣金生效时，实际净收益必须低于纯比例估算"


def test_net_return_accepts_multiple_date_types():
    """日期参数要能吃 str / date / datetime / Timestamp（引擎里四种都会出现）。"""
    from datetime import date as _date
    from datetime import datetime as _dt

    import pandas as pd

    same = [
        cm.net_return(10.0, 11.0, AFTER_CUT, 10_000),
        cm.net_return(10.0, 11.0, _date(2024, 1, 2), 10_000),
        cm.net_return(10.0, 11.0, _dt(2024, 1, 2, 15, 0, 0), 10_000),
        cm.net_return(10.0, 11.0, pd.Timestamp("2024-01-02"), 10_000),
    ]
    assert all(abs(v - same[0]) < 1e-12 for v in same)
