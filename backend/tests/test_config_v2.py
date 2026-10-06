"""策略 v2.0 独立配置节（`strategy_defaults_v2`）的加载测试。

盯的是本项目头号坑：**配了键却没被读**——`YamlSettings` 不是 `extra=forbid`，
往 `config.yaml` 塞新键不会报错，只会被 pydantic **静默丢弃**。
所以必须同时验证：新节可读、且**没动** v1.6 的 `strategy_defaults`。
"""
from __future__ import annotations

from app.core.config import StrategyDefaultsV2, settings


def test_strategy_v2_is_loadable_and_independent():
    v2 = settings.strategy_v2
    assert isinstance(v2, StrategyDefaultsV2)

    # v2.0 的关键默认值（与 config.yaml::strategy_defaults_v2 对齐）
    assert v2.max_positions == 10
    assert v2.max_weight_per_stock == 0.10
    assert v2.hold_days_max == 30
    assert v2.rebalance_every == 5
    assert v2.reversal_pct == 0.5
    assert v2.use_quality_gate is True
    assert v2.min_amount_mean_20 == 5.0e7
    # v2.1：止损/止盈默认全 0（关闭）→ 不改变 M1 基线行为
    assert v2.stop_loss_p_mult == 0.0
    assert v2.take_profit_p_mult == 0.0
    assert v2.take_profit_abs == 0.0


def test_v16_defaults_untouched():
    """★ 加了 v2 配置节后，现役 v1.6 的默认值必须原样不变。"""
    v16 = settings.strategy
    assert v16.max_positions == 5
    assert v16.hold_days_max == 10
    assert v16.max_weight_per_stock == 0.20
    assert v16.take_profit_gross_main == 0.06
    assert v16.benchmark == "sh.000001"
    # v2 专有字段不得出现在 v1.6 模型上（防止两节被合并成一份）
    assert not hasattr(v16, "rebalance_every")
