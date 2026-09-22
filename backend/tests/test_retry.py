"""重试策略测试。

★ 重点覆盖一个**已经踩过的坑**：
  旧实现把内置 `ConnectionError` 写进重试白名单，而 requests 抛的是
  `requests.exceptions.ConnectionError` —— 两者没有继承关系，
  导致所有真实网络错误都静默地不重试。现已改为黑名单策略。
"""
from __future__ import annotations

import pytest
import requests

import app.services.data_source.base as base_mod
from app.core.errors import (
    ConfigError,
    DataSourceError,
    DataSourceRateLimited,
    DataSourceTimeout,
    InvalidParamError,
    StorageError,
    SymbolFormatError,
)
from app.services.data_source.base import call_with_retry, is_retryable, with_retry


@pytest.fixture
def no_sleep(monkeypatch):
    """跳过退避等待，让测试快速跑完。"""
    monkeypatch.setattr(base_mod.time, "sleep", lambda _s: None)


# ======================================================================
# 重试判定
# ======================================================================
@pytest.mark.parametrize(
    "exc",
    [
        requests.exceptions.ConnectionError("boom"),
        requests.exceptions.ConnectTimeout("boom"),
        requests.exceptions.ReadTimeout("boom"),
        requests.exceptions.ChunkedEncodingError("boom"),
        ConnectionError("builtin"),
        TimeoutError(),
        OSError("io"),
        ValueError("weird"),
        DataSourceError("数据源挂了"),
        DataSourceTimeout("超时"),
    ],
)
def test_retryable(exc):
    assert is_retryable(exc) is True


@pytest.mark.parametrize(
    "exc",
    [
        DataSourceRateLimited("限频"),
        InvalidParamError("参数错"),
        SymbolFormatError("代码错"),
        ConfigError("配置错"),
        StorageError("写盘失败"),
    ],
)
def test_not_retryable(exc):
    assert is_retryable(exc) is False


def test_requests_connection_error_is_retryable():
    """★ 回归：requests.ConnectionError 与内置 ConnectionError 无继承关系。"""
    exc = requests.exceptions.ConnectionError("x")
    assert not isinstance(exc, ConnectionError)
    assert is_retryable(exc) is True


# ======================================================================
# 重试行为
# ======================================================================
def test_retry_succeeds_after_transient_failures(no_sleep):
    state = {"n": 0}

    def flaky() -> str:
        state["n"] += 1
        if state["n"] < 3:
            raise ValueError("临时故障")
        return "ok"

    assert call_with_retry(flaky, max_attempts=3) == "ok"
    assert state["n"] == 3


def test_retry_gives_up_after_max_attempts(no_sleep):
    state = {"n": 0}

    def always_fail() -> str:
        state["n"] += 1
        raise ValueError("一直失败")

    with pytest.raises(ValueError):
        call_with_retry(always_fail, max_attempts=3)
    assert state["n"] == 3


def test_rate_limit_is_not_retried(no_sleep):
    """★ 限频不重试 —— 越重试撞得越狠，交给熔断器静默。"""
    state = {"n": 0}

    def limited() -> str:
        state["n"] += 1
        raise DataSourceRateLimited("限频了")

    with pytest.raises(DataSourceRateLimited):
        call_with_retry(limited, max_attempts=5)
    assert state["n"] == 1


def test_invalid_param_is_not_retried(no_sleep):
    state = {"n": 0}

    def bad_param() -> str:
        state["n"] += 1
        raise InvalidParamError("参数错了")

    with pytest.raises(InvalidParamError):
        call_with_retry(bad_param, max_attempts=5)
    assert state["n"] == 1


def test_retryable_error_is_not_retried(no_sleep):
    state = {"n": 0}

    def storage_fail() -> str:
        state["n"] += 1
        raise StorageError("磁盘满了")

    with pytest.raises(StorageError):
        call_with_retry(storage_fail, max_attempts=4)
    assert state["n"] == 1


def test_rate_limit_signal_is_converted_and_not_retried(no_sleep):
    """带限频特征的普通异常 → 转成 DataSourceRateLimited 且不重试。"""
    state = {"n": 0}

    def net_error() -> str:
        state["n"] += 1
        raise requests.exceptions.ConnectionError("HTTP 429 Too Many Requests")

    with pytest.raises(DataSourceRateLimited) as ei:
        call_with_retry(net_error, max_attempts=5)
    assert state["n"] == 1
    assert ei.value.detail["tier"] == "definite"


def test_connection_abort_marked_soft(no_sleep):
    def aborted() -> str:
        raise requests.exceptions.ConnectionError(
            "RemoteDisconnected('Remote end closed connection without response')"
        )

    with pytest.raises(DataSourceRateLimited) as ei:
        call_with_retry(aborted, max_attempts=3)
    assert ei.value.detail["tier"] == "soft"


def test_first_attempt_success_calls_once(no_sleep):
    state = {"n": 0}

    def good() -> int:
        state["n"] += 1
        return 42

    assert call_with_retry(good, max_attempts=3) == 42
    assert state["n"] == 1


def test_with_retry_decorator():
    state = {"n": 0}

    @with_retry(max_attempts=3)
    def flaky(a: int, b: int = 1) -> int:
        state["n"] += 1
        if state["n"] < 2:
            raise ValueError("boom")
        return a + b

    assert flaky(1, b=2) == 3
    assert state["n"] == 2
    assert flaky.__name__ == "flaky"      # functools.wraps 生效
