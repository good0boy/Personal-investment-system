"""参数寻优网格展开（`strategies.runner.expand_grid`）测试。

★ 盯的是 v1.4 新增的「**联合维度**」：某个 key 的候选值是 dict 时，展开成多个
参数键 —— 用来表达"止盈主板/创业板成对联动"。

这类逻辑一旦写错，后果是**静默少跑或互相覆盖**（参数变成"写了没被读"），
不会报错，只会让寻优结论不可信 —— 所以这里把边界全部钉死。
"""
from __future__ import annotations

import pytest
import yaml

from app.core.errors import AppError
from app.core.paths import SWEEPS_DIR
from strategies.runner import MAX_GRID_COMBOS, expand_grid, grid_param_names


def test_expand_grid_scalar_only_backward_compatible():
    """普通网格（全标量）行为不变：笛卡尔积。"""
    combos = expand_grid({"a": [1, 2], "b": [10, 20, 30]})
    assert len(combos) == 6
    assert {"a": 1, "b": 10} in combos
    assert all(set(c) == {"a", "b"} for c in combos)


def test_expand_grid_joint_dimension():
    """联合维度：每个 dict 展开成多个参数键，并按档位成对联动。"""
    combos = expand_grid(
        {
            "hold": [10, 20],
            "take_profit": [
                {"tp_main": 0.04, "tp_gem": 0.08},
                {"tp_main": 0.06, "tp_gem": 0.10},
            ],
        }
    )
    assert len(combos) == 2 * 2 == 4
    pairs = sorted({(c["tp_main"], c["tp_gem"]) for c in combos})
    assert pairs == [(0.04, 0.08), (0.06, 0.10)], "主板/创业板必须成对联动，不做全交叉"
    assert grid_param_names({"take_profit": [{"tp_main": 1, "tp_gem": 2}]}) == [
        "tp_gem",
        "tp_main",
    ]


def test_expand_grid_rejects_mixed_scalar_and_dict():
    with pytest.raises(AppError, match="不得混用"):
        expand_grid({"a": [1, {"x": 2}]})


def test_expand_grid_rejects_cross_dimension_collision():
    """联合 dict 里的键与其它维度重名 → 必须报错（否则 update 会静默覆盖）。"""
    with pytest.raises(AppError, match="重复"):
        expand_grid({"joint": [{"p": 1}], "p": [9]})


def test_expand_grid_rejects_inconsistent_key_sets():
    """同一联合维度的候选键集必须一致（否则是残缺交叉）。"""
    with pytest.raises(AppError, match="键集不一致"):
        expand_grid({"joint": [{"p": 1}, {"q": 2}]})


def test_expand_grid_enforces_max_combos():
    with pytest.raises(AppError, match="超过上限"):
        expand_grid({"a": list(range(MAX_GRID_COMBOS + 1))})


def test_shipped_hold_tp_grid_is_192_combos_and_paired():
    """随包发布的 `sweeps/hold_tp.yaml` 形状必须符合规格 v1.5 §5.2。"""
    raw = yaml.safe_load((SWEEPS_DIR / "hold_tp.yaml").read_text(encoding="utf-8"))
    combos = expand_grid(raw["params"])
    assert len(combos) == 3 * 4 * 4 * 4 == 192

    # 两条联合维度必须**两板块同值**（v1.5 起不再区分），且各 4 档
    tp_pairs = sorted({(c["take_profit_gross_main"], c["take_profit_gross_gem"]) for c in combos})
    assert tp_pairs == [(0.04, 0.04), (0.06, 0.06), (0.08, 0.08), (0.10, 0.10)]
    sl_pairs = sorted({(c["stop_loss_amp_mult_main"], c["stop_loss_amp_mult_gem"]) for c in combos})
    assert sl_pairs == [(1.0, 1.0), (1.2, 1.2), (1.5, 1.5), (2.0, 2.0)]
    # 闸门 5 窗口必须含 0（内置消融对照）
    assert sorted({c["no_limit_up_days"] for c in combos}) == [0, 5, 10, 20]
    # 联合维度展开后主板/创业板必须恒等
    assert all(c["take_profit_gross_main"] == c["take_profit_gross_gem"] for c in combos)
    assert all(c["stop_loss_amp_mult_main"] == c["stop_loss_amp_mult_gem"] for c in combos)
    # 仓位/持仓数**不再进网格**（v1.4 已证明它们只缩放暴露）→ 用默认值
    assert all("max_weight_per_stock" not in c for c in combos)
    assert all("max_positions" not in c for c in combos)

    # 敏感性轴必须是展开后的真实参数名，且都在结果列里
    names = grid_param_names(raw["params"])
    sens = raw["sensitivity"]
    assert sens["x"] in names
    assert sens["series"] in names


def test_shipped_v14_recon_grid_is_72_combos() -> None:
    """v1.4 **参数重建**网格：72 组，且三处固定值必须把 v1.5 代码调回 v1.4 语义。

    这是本轮"三版对比"的关键保险：如果哪天真把 `no_limit_up_days` / 止损倍数
    的默认值改了，这条用例会立刻炸，而不是让"v1.4 重建"悄悄变成别的东西。
    """
    raw = yaml.safe_load((SWEEPS_DIR / "hold_tp_v14.yaml").read_text(encoding="utf-8"))
    combos = expand_grid(raw["params"])
    assert len(combos) == 4 * 3 * 3 * 2 == 72
    assert raw["strategy"] == "momentum"

    # 止盈是**成对**扫描（v1.4 是板块化 6%/10%），不是同值
    tp_pairs = sorted({(c["take_profit_gross_main"], c["take_profit_gross_gem"]) for c in combos})
    assert tp_pairs == [(0.04, 0.08), (0.06, 0.10), (0.09, 0.12), (0.12, 0.15)]

    # 三处"调回 v1.4"的固定值
    assert sorted({c["no_limit_up_days"] for c in combos}) == [0]
    assert sorted({c["stop_loss_amp_mult_main"] for c in combos}) == [2.0]
    assert sorted({c["stop_loss_amp_mult_gem"] for c in combos}) == [3.0]

    # 仓位与持仓数**本组要扫**（与现役 192 组网格相反）
    assert sorted({c["max_weight_per_stock"] for c in combos}) == [0.20, 0.25, 0.50]
    assert sorted({c["max_positions"] for c in combos}) == [2, 5]

    names = grid_param_names(raw["params"])
    assert raw["sensitivity"]["x"] in names
    assert raw["sensitivity"]["series"] in names


def test_shipped_v13_grid_is_72_combos_and_uses_frozen_strategy() -> None:
    """v1.3 冻结快照网格：72 组，且必须指向 `momentum_v13` 那份实现。

    v1.3 的规则（闸门 1 累计口径 / 无闸门 4 / 闸门 3.5 启用 / 止盈净口径 / 无止损）
    是**代码分支**，靠参数调不出来 —— 所以 `strategy` 字段写错就等于跑成别的版本，
    这条断言专门钉住它。
    """
    raw = yaml.safe_load((SWEEPS_DIR / "hold_tp_v13.yaml").read_text(encoding="utf-8"))
    combos = expand_grid(raw["params"])
    assert len(combos) == 4 * 3 * 3 * 2 == 72
    assert raw["strategy"] == "momentum_v13", "v1.3 必须走冻结实现，不能靠参数近似"

    # 止盈是**净口径**的 take_profit_net（不是毛口径的两个字段）
    assert sorted({c["take_profit_net"] for c in combos}) == [0.06, 0.08, 0.10, 0.12]
    assert all("take_profit_gross_main" not in c for c in combos)
    assert all("stop_loss_amp_mult_main" not in c for c in combos)   # v1.3 不设个股止损

    # 闸门 3.5 在 v1.3 是**启用**的（0.2），与现役网格默认 0 相反
    assert sorted({c["exclude_top_pct"] for c in combos}) == [0.2]
    # v1.3 没有闸门 5
    assert all("no_limit_up_days" not in c for c in combos)

    names = grid_param_names(raw["params"])
    assert raw["sensitivity"]["x"] in names
    assert raw["sensitivity"]["series"] in names


def test_v13_frozen_strategy_loads_with_v13_defaults() -> None:
    """`momentum_v13` 必须能独立构造，且默认参数是 v1.3 的冻结值。

    它的默认值**不能**读现役 `settings.strategy` —— `take_profit_net` /
    `entry.today_pct_max` / `entry.ret10_min` 这些字段在 v1.4 之后已被改名或删除，
    读了就直接崩。这条用例把这个"不依赖现役配置"的契约钉住。
    """
    from strategies.registry import get_strategy

    s = get_strategy("momentum_v13")
    assert s.name == "momentum_v13"
    assert s.version == "1.3"
    assert s.params["take_profit_net"] == 0.10
    assert s.params["exclude_top_pct"] == 0.2      # v1.3 启用闸门 3.5
    assert s.params["entry_today_pct_max"] == 0.05
    assert s.params["entry_ret10_min"] == 0.05
    snap = s.config_snapshot()
    assert "v1.3" in snap["rules_version"]
    assert snap["params"]["take_profit_net"] == 0.10
