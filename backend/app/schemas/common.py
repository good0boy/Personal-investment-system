"""统一响应模型。

前端约定：所有接口返回
    { "code": 0, "message": "ok", "data": {...}, "ts": "2026-09-17T08:42:50+08:00" }
code != 0 时 message 是可直接展示的中文。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class ApiResponse(BaseModel, Generic[T]):
    """统一响应包装。"""

    code: int = 0
    message: str = "ok"
    data: T | None = None
    ts: str = Field(default_factory=_now_iso)

    @classmethod
    def ok(cls, data: Any = None, message: str = "ok") -> ApiResponse[Any]:
        return cls(code=0, message=message, data=data)

    @classmethod
    def fail(cls, code: int, message: str, data: Any = None) -> ApiResponse[Any]:
        return cls(code=code, message=message, data=data)


class ErrorResponse(BaseModel):
    code: int
    message: str
    detail: Any = None
    ts: str = Field(default_factory=_now_iso)


# ======================================================================
# 系统相关
# ======================================================================
class DatasourceHealth(BaseModel):
    name: str
    available: bool
    version: str = ""
    message: str = ""
    checked_at: str = ""
    latency_ms: float | None = None
    cache: dict[str, int] = Field(default_factory=dict)


class CalendarStats(BaseModel):
    total_days: int | None = None
    trading_days: int | None = None
    first_date: str | None = None
    #: 日历**覆盖到**哪一天（含未来排期）—— 与 last_date 不是一回事
    coverage_end: str | None = None
    #: 覆盖范围内的未来交易日数量（仅参考，业务只能用到 last_date）
    future_trading_days: int | None = None
    #: 最后一个**已发生**的交易日（≤ 今天）—— 业务可用上界
    last_date: str | None = None
    today: str | None = None
    today_is_trading_day: bool | None = None
    latest_trading_day: str | None = None
    synced_at: str | None = None


class StorageSection(BaseModel):
    files: int = 0
    bytes: int = 0
    human: str = "0 B"


class StorageStats(BaseModel):
    data_root: str
    sections: dict[str, dict[str, Any]] = Field(default_factory=dict)
    total_bytes: int = 0
    total_human: str = "0 B"
    trade_calendar_ready: bool = False


class DatabaseStats(BaseModel):
    db_path: str
    initialized: bool
    size_bytes: int = 0
    size_human: str = "0 B"
    table_counts: dict[str, int] = Field(default_factory=dict)


class JobInfo(BaseModel):
    job_id: str
    name: str
    trigger: str = ""
    next_run_time: str | None = None
    enabled: bool = True


class HealthData(BaseModel):
    """GET /api/health 的 data 部分。"""

    status: str = "ok"                    # ok | degraded | error
    app_name: str = ""
    app_version: str = ""
    env: str = "dev"
    server_time: str = Field(default_factory=_now_iso)
    uptime_seconds: float = 0.0
    python_version: str = ""
    akshare_version: str = ""
    baostock_version: str = ""
    data_last_date: str | None = None
    datasources: list[DatasourceHealth] = Field(default_factory=list)
    calendar: CalendarStats | None = None
    storage: StorageStats | None = None
    database: DatabaseStats | None = None
    scheduler_enabled: bool = False
    jobs: list[JobInfo] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    wecom_configured: bool = False
    wecom_key_masked: str = ""
