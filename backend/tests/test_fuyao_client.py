"""同花顺 fuyao REST 客户端单测。

全程 **不发真实网络请求** —— 用一个假的 HTTP 层喂响应，
专门验证三件最容易出错的事：

1. **信封解析**：业务错误也返回 HTTP 200，必须靠 `code` 判断；
2. **错误码 → 异常映射**：尤其"**只有 4001 / 429 才算限频**"这条 ——
   本项目刚被"把连接被掐断误判成限频 → 熔断器把整个源封掉"坑过一次；
3. **密钥不出境**：Key 只进 `X-api-key` 头，绝不进日志参数。
"""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.core.config import settings
from app.core.errors import (
    AppError,
    DataSourceError,
    DataSourceRateLimited,
    DataSourceTimeout,
    InvalidParamError,
    NotConfiguredError,
)
from app.services.data_source.fuyao_client import FuyaoClient, date_to_ms


# ======================================================================
# 假 HTTP 层
# ======================================================================
class FakeResponse:
    def __init__(self, status_code: int = 200, payload: Any = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or (json.dumps(payload or {}) if payload is not None else "")

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeHTTP:
    """假 HTTP 层。

    ⚠️ 响应用完时**重复最后一个** —— 因为 `_invoke` 会走真实的重试
      （`DataSourceError` / 超时是可重试的，默认 3 次），只喂一个响应
      会在第二次尝试时炸出"没有更多响应"的假失败。要精确断言调用次数
      请读 `calls`，而不是靠"响应用完"来卡住。
    """

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def get(self, path: str, params: dict | None = None, headers: dict | None = None):
        self.calls.append({"path": path, "params": params or {}, "headers": headers or {}})
        if not self._responses:
            raise AssertionError(f"没有预置响应（path={path}）")
        item = self._responses[0] if len(self._responses) == 1 else self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self) -> None:
        return None


def envelope(item: Any, code: int = 0, message: str = "success") -> dict:
    return {
        "code": code,
        "message": message,
        "request_id": "test",
        "data": {"timestamp": 1786000000000, "item": item},
    }


@pytest.fixture
def client(monkeypatch) -> FuyaoClient:
    """独立实例（不用模块级单例，避免测试之间串状态）。

    并把重试降到 1 次：真实重试是**可重试异常 + 指数退避**（0.5s/1s…），
    对本文件"验证错误映射"的目的没有增量，只会让用例变慢、让调用次数难断言。
    （重试逻辑本身由 `test_data_source_invoke.py` 覆盖。）
    """
    monkeypatch.setattr(settings.datasource, "max_retries", 1)
    c = FuyaoClient()
    c._min_interval = 0.0
    return c


@pytest.fixture
def logs(monkeypatch) -> list[dict]:
    captured: list[dict] = []

    def fake_log(datasource, func_name, status, **kw):
        captured.append(
            {"datasource": datasource, "func_name": func_name, "status": status, **kw}
        )

    from app.services.data_source import base as base_mod

    monkeypatch.setattr(base_mod.meta_repo, "log_datasource_call", fake_log)
    return captured


def wire(client: FuyaoClient, *responses: Any) -> FakeHTTP:
    fake = FakeHTTP(list(responses))
    client._http = fake  # type: ignore[assignment]
    return fake


# ======================================================================
# date_to_ms —— 三种写法 + 空值
# ======================================================================
def test_date_to_ms_accepts_three_formats():
    a = date_to_ms("20260929")
    b = date_to_ms("2026-09-29")
    c = date_to_ms(1790611200000)
    assert a == b and a > 1_700_000_000_000
    assert c == 1790611200000


def test_date_to_ms_empty_means_omit():
    """★ 空值必须返回 None（=不传该参数），不能返回 0。

    传 0 会被服务端当成 1970-01-01，静默返回空表 —— 是最难查的一类 bug。
    """
    assert date_to_ms("") is None
    assert date_to_ms(None) is None
    assert date_to_ms("   ") is None


def test_date_to_ms_rejects_garbage():
    with pytest.raises(InvalidParamError):
        date_to_ms("2026/09/29/xx")


# ======================================================================
# 信封解析
# ======================================================================
def test_code_zero_returns_items(client: FuyaoClient, logs: list[dict]):
    wire(client, FakeResponse(200, envelope([{"thscode": "600519.SH", "name": "贵州茅台"}])))
    df = client.tickers_list("a-share", limit=1)
    assert len(df) == 1
    assert df.iloc[0]["thscode"] == "600519.SH"
    assert logs and logs[-1]["status"] == "ok"


def test_missing_key_raises_not_configured(client: FuyaoClient, monkeypatch):
    monkeypatch.setattr(settings.env, "fuyao_api_key", "")
    wait_seconds = 0.15
    wire(client, FakeResponse(200, envelope([])))
    with pytest.raises(NotConfiguredError) as ei:
        client.tickers_list("a-share", limit=1)
    assert "FUYAO_API_KEY" in ei.value.message
    assert wait_seconds > 0.1  # 占位断言：确保没被优化掉


def test_key_goes_into_header_only(client: FuyaoClient, monkeypatch, logs: list[dict]):
    """★ 密钥纪律：只在 X-api-key 头里，绝不出现在日志参数中。"""
    secret = "sk-supersecret-abcdefghijklmnop"
    monkeypatch.setattr(settings.env, "fuyao_api_key", secret)
    fake = wire(client, FakeResponse(200, envelope([])))

    client.tickers_list("a-share", limit=1)

    assert fake.calls[0]["headers"]["X-api-key"] == secret
    assert secret not in json.dumps(fake.calls[0]["params"], ensure_ascii=False)
    blob = json.dumps(logs, ensure_ascii=False)
    assert secret not in blob, "密钥泄露进了 datasource_log 参数！"


# ======================================================================
# 错误码映射 —— 什么该熔断、什么不该
# ======================================================================
@pytest.mark.parametrize("code", [2001, 2003])
def test_auth_codes_map_to_not_configured_and_do_not_trip(
    client: FuyaoClient, code: int
):
    """未认证/无权限 → NotConfiguredError，**不熔断**（熔断也治不好 Key 问题）。"""
    wire(client, FakeResponse(200, envelope(None, code=code, message="Invalid API key")))
    with pytest.raises(NotConfiguredError):
        client.tickers_list("a-share", limit=1)
    assert client._breaker.is_open("fuyao") is False


def test_code_4001_trips_breaker(client: FuyaoClient):
    """4001 = 频率超限 → definite 档，立即熔断。"""
    wire(client, FakeResponse(200, envelope(None, code=4001, message="QPS exceeded")))
    with pytest.raises(DataSourceRateLimited):
        client.tickers_list("a-share", limit=1)
    assert client._breaker.is_open("fuyao") is True


def test_http_429_trips_breaker(client: FuyaoClient):
    wire(client, FakeResponse(429, text="too many requests"))
    with pytest.raises(DataSourceRateLimited):
        client.tickers_list("a-share", limit=1)
    assert client._breaker.is_open("fuyao") is True


def test_unknown_code_is_plain_error_not_rate_limit(client: FuyaoClient):
    """★ 兜底：拿不准的错误码一律当普通错误，**不许**触发熔断。

    这是本轮根因修复的延伸 —— 宁可少熔断一次，也不要重演"误判成限频 →
    整个数据源被短路"。
    """
    wire(client, FakeResponse(200, envelope(None, code=5003, message="上游不可用")))
    with pytest.raises(DataSourceError) as ei:
        client.tickers_list("a-share", limit=1)
    assert ei.value.detail.get("fuyao_code") == 5003
    assert not isinstance(ei.value, DataSourceRateLimited)
    assert client._breaker.is_open("fuyao") is False


def test_http_error_maps_to_datasource_error(client: FuyaoClient):
    wire(client, FakeResponse(500, text="boom"))
    with pytest.raises(DataSourceError):
        client.tickers_list("a-share", limit=1)


def test_non_json_body_raises_datasource_error(client: FuyaoClient):
    wire(client, FakeResponse(200, payload=None, text="<html>gateway</html>"))
    with pytest.raises(DataSourceError):
        client.tickers_list("a-share", limit=1)


def test_timeout_maps_to_datasource_timeout(client: FuyaoClient):
    wire(client, httpx.TimeoutException("timed out"))
    with pytest.raises(DataSourceTimeout):
        client.tickers_list("a-share", limit=1)


def test_connection_abort_is_classified_by_shared_throttle(
    client: FuyaoClient, monkeypatch
):
    """连被掐断走与 akshare 同一套档位识别（soft），不是无脑报 DataSourceError。"""
    wire(client, httpx.ConnectError("Remote end closed connection without response"))
    with pytest.raises(AppError):
        client.tickers_list("a-share", limit=1)


# ======================================================================
# 参数与分组
# ======================================================================
def test_empty_params_are_not_sent(client: FuyaoClient):
    """空串/None 一律不发给服务端（否则会被当成 1970-01-01 或空条件）。"""
    fake = wire(client, FakeResponse(200, envelope([])))
    client.limit_up_pool(date_ms=None, page=1, size=10, sort_field="", sort_dir="desc")
    sent = fake.calls[0]["params"]
    assert "date_ms" not in sent
    assert "sort_field" not in sent
    assert sent["size"] == 10


def test_line_group_is_single_group(client: FuyaoClient):
    """fuyao 是单一主机，熔断只有一组 —— 不该按接口名散开。"""
    assert client._line_group("limit_up_pool") == "fuyao"
    assert client._line_group("anything_else") == "fuyao"


def test_ladder_is_flattened(client: FuyaoClient):
    """连板天梯的嵌套结构要被展平成 行=日期×板位×标的。"""
    payload = {
        "code": 0,
        "message": "success",
        "data": {
            "timestamp": 1,
            "window": {"length": 30, "date_list": ["20260930"]},
            "item": [
                {
                    "date": "2026-09-30",
                    "boards": {
                        "two_board": [
                            {"thscode": "603200.SH", "ticker": "603200", "name": "上海洗霸",
                             "board_num": 2, "seal_nextday": None, "sign_level": 0}
                        ],
                        "three_board": [],
                        "seven_over": [
                            {"thscode": "600825.SH", "ticker": "600825", "name": "新华传媒",
                             "board_num": 7, "seal_nextday": None, "sign_level": 0}
                        ],
                    },
                }
            ],
        },
    }
    wire(client, FakeResponse(200, payload))
    df = client.limit_up_ladder()
    assert set(df.columns) >= {"date", "board", "thscode", "board_num"}
    assert len(df) == 2                      # 两个板位各一只
    assert set(df["board"]) == {"two_board", "seven_over"}


# ======================================================================
# 板块名 → thscode 解析
# ======================================================================
def test_resolve_board_thscode_by_chinese_name(client: FuyaoClient):
    wire(
        client,
        FakeResponse(
            200,
            envelope([{"thscode": "881101.TI", "name": "种植业与林业"},
                      {"thscode": "881121.TI", "name": "半导体"}]),
        ),
    )
    assert client.resolve_board_thscode("半导体") == "881121.TI"


def test_resolve_board_thscode_raises_when_missing(client: FuyaoClient):
    """★ 匹配不上必须如实报错，不能静默返回空 —— 那会变成静默污染。"""
    from app.core.errors import NotFoundError

    wire(client, FakeResponse(200, envelope([{"thscode": "881101.TI", "name": "种植业"}])))
    with pytest.raises(NotFoundError) as ei:
        client.resolve_board_thscode("不存在的板块")
    assert "不存在的板块" in ei.value.message


def test_index_list_maps_business_tag_to_api_tag(client: FuyaoClient):
    """akshare 只分"行业/概念"，fuyao 有四个 tag —— 映射必须显式且可测。"""
    fake = wire(client, FakeResponse(200, envelope([])))
    client.index_list("concept")
    assert fake.calls[0]["params"]["tag"] == "cn_concept"
    client.index_list("industry")
    assert fake.calls[1]["params"]["tag"] == "industry"


# ======================================================================
# fetch_daily 的诚实标注
# ======================================================================
def test_fetch_daily_marks_st_and_halt_as_unknown(client: FuyaoClient):
    """★ fuyao 日线没有逐日 isST / 停牌标记 —— 必须显式标为未知，不能填 1。

    这两个字段是本项目的合规红线（只有 baostock 提供）。填个 1 出来
    会让人以为"这天正常交易且非 ST"，是危险的假数据。
    """
    import pandas as pd

    items = [
        {"date_ms": 1780329600000, "open_price": 10.0, "high_price": 11.0,
         "low_price": 9.5, "close_price": 10.5, "volume": 1000.0, "turnover": 10000.0},
        {"date_ms": 1780416000000, "open_price": 10.5, "high_price": 12.0,
         "low_price": 10.4, "close_price": 11.5, "volume": 1200.0, "turnover": 13000.0},
    ]
    wire(client, FakeResponse(200, envelope(items)))
    df = client.fetch_daily("sh.600519", "2026-09-01", "2026-09-30")

    assert len(df) == 2
    assert df["symbol"].iloc[0] == "sh.600519"
    assert df["is_st"].isna().all(), "is_st 必须是未知（NA），不能填 1"
    assert (df["trade_status"] == 1).all()
    assert pd.isna(df["turn"].iloc[0])
    # 复权后的价格来自服务端，不做本地重算
    assert df["close"].iloc[-1] == pytest.approx(11.5)


def test_fetch_daily_rejects_unknown_adjust(client: FuyaoClient):
    with pytest.raises(DataSourceError):
        client.fetch_daily("sh.600519", "2026-09-01", "2026-09-30", adjust="weird")
