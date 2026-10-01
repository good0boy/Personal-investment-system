"""P1-3：限流 / 熔断状态必须**真的**出现在健康响应里。

★ 这个测试针对的是一个已经发生过两次的失效模式：
  代码往 dict 里塞了字段，但响应模型没声明 → Pydantic **静默丢掉**，
  接口看起来 200、字段却不存在，前端拿到 undefined。
  项目问题清单第 16 条（日历的 coverage_end 凭空消失）就是同一个坑。
  所以这里断言的是"响应里真的有这个字段"，而不是"函数返回了它"。
"""
from __future__ import annotations

from app.api.v1.system import _build_health
from app.schemas.common import (
    CircuitBreakerStats,
    DatasourceHealth,
    RateLimiterStats,
    ThrottleStats,
)


def test_health_payload_contains_throttle_for_every_datasource():
    """每个数据源都必须带上 throttle，且含限流 + 熔断两块。"""
    raw = _build_health(deep=False).model_dump()
    ds = raw["datasources"]
    assert ds, "健康检查至少要有一个数据源，否则本用例没验证任何东西"

    for item in ds:
        throttle = item.get("throttle")
        assert throttle, f"{item['name']} 的 throttle 在响应模型里被丢掉了"
        assert throttle["rate_limiter"], f"{item['name']} 缺 rate_limiter"
        assert throttle["circuit_breaker"], f"{item['name']} 缺 circuit_breaker"
        cb = throttle["circuit_breaker"]
        assert "open" in cb and "remaining_seconds" in cb
        assert "soft_strikes" in cb and "soft_threshold" in cb
        rl = throttle["rate_limiter"]
        assert "calls_in_window" in rl and "total_calls" in rl


def test_throttle_submodels_pass_through_unknown_keys():
    """限流/熔断是**诊断信息**：底层 `stats()` 新增字段必须透传，不许被模型吃掉。

    这是刻意开的 `extra="allow"` —— 多一个字段远比少一个字段危险得多。
    """
    payload = {
        "datasource": "baostock",
        "rate_limiter": {
            "name": "baostock",
            "min_interval": 0.12,
            "calls_in_window": 3,
            "future_field_added_later": 42,  # 模拟底层将来新增
        },
        "circuit_breaker": {
            "name": "baostock",
            "open": True,
            "remaining_seconds": 12.5,
            "another_future_field": "x",
        },
    }
    out = ThrottleStats.model_validate(payload).model_dump()
    assert out["rate_limiter"]["future_field_added_later"] == 42
    assert out["circuit_breaker"]["another_future_field"] == "x"
    assert out["circuit_breaker"]["open"] is True


def test_throttle_models_tolerate_partial_payload():
    """字段不全（老版本/异常路径）时不能抛校验错 —— 健康检查永远要出得来。"""
    ok = ThrottleStats.model_validate({}).model_dump()
    assert ok["datasource"] == ""
    assert ok["rate_limiter"] is None

    dh = DatasourceHealth.model_validate({"name": "x", "available": True}).model_dump()
    assert dh["throttle"] is None  # 老后端不返回该字段 → 允许为空，前端做兜底
    assert RateLimiterStats.model_validate({}).calls_in_window == 0
    assert CircuitBreakerStats.model_validate({}).soft_threshold == 0


# ======================================================================
# 熔断分组字段必须真的出现在响应里（2026-10-01）
# ======================================================================
def test_breaker_groups_survive_response_model():
    """★ 与文件头同一个坑：往 dict 里塞了字段，模型没声明就会被静默丢掉。

    前端系统页要靠 `groups` / `open_groups` 逐线路展示熔断状态；
    字段一旦被 Pydantic 吞掉，页面就会永远显示"没有熔断"。
    """
    from app.services.data_source.throttle import GroupedCircuitBreaker

    g = GroupedCircuitBreaker(cooldown=10, name="akshare")
    g.record_failure("炸了", tier="soft", key="em_push2")
    g.record_failure("炸了", tier="soft", key="em_push2")
    g.record_failure("炸了", tier="soft", key="em_push2")

    model = CircuitBreakerStats.model_validate(g.stats())
    assert model.open is True
    assert model.open_groups == ["em_push2"]
    assert [x.key for x in model.groups] == ["em_push2"]
    assert model.groups[0].open is True
    assert model.groups[0].remaining_seconds > 0


def test_breaker_group_tolerates_partial_payload():
    """上游只给部分字段时不能炸（老版本/别的数据源可能没有 groups）。"""
    model = CircuitBreakerStats.model_validate({"open": True})
    assert model.open is True
    assert model.groups == []
    assert model.open_groups == []


# ======================================================================
# 密钥脱敏（fuyao API Key 绝不能随 /system/config 泄露）
# ======================================================================
def test_system_config_never_leaks_fuyao_key(monkeypatch):
    """★ 硬约束：fuyao 的 API Key 只允许出现在 .env 与请求头里。

    `GET /api/v1/system/config` 会把 `settings.datasource.model_dump()` 整体
    返回 —— 所以 Key **不能**放进 `DatasourceConfig`，只能放 `EnvSettings`，
    并且接口只给脱敏值。这条断言就是钉这个边界。
    """
    import json

    from app.api.v1.system import current_config
    from app.core.config import settings

    secret = "sk-testkey-abcdefghijklmnopqrstuvwxyz"
    monkeypatch.setattr(settings.env, "fuyao_api_key", secret)

    body = json.dumps(current_config().model_dump(), ensure_ascii=False, default=str)
    assert secret not in body, "明文 API Key 泄露到了 /system/config 响应里！"
    assert "sk-t****wxyz" in body, "应当给出脱敏值供主人确认是否配置正确"


def test_datasource_config_has_no_secret_field():
    """配置模型里只允许有"环境变量名"，不允许有 Key 真值字段。"""
    from app.core.config import DatasourceConfig

    fields = set(DatasourceConfig.model_fields["fuyao"].annotation.model_fields)
    assert "api_key_env" in fields
    assert not (fields & {"api_key", "key", "secret", "token"}), (
        f"DatasourceConfig.fuyao 里出现了疑似密钥字段：{fields}"
    )
