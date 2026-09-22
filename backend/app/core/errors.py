"""统一异常与错误码。

约定：
- 业务异常一律继承 AppError，携带 code / message / detail
- code 为 0 表示成功；非 0 表示错误
- message 必须是可以直接展示给用户的中文
"""
from __future__ import annotations

from typing import Any


class AppError(Exception):
    """所有业务异常的基类。"""

    code: int = 1000
    http_status: int = 400
    message: str = "未知错误"

    def __init__(
        self,
        message: str | None = None,
        *,
        detail: Any = None,
        code: int | None = None,
        http_status: int | None = None,
    ) -> None:
        self.message = message or self.__class__.message
        self.detail = detail
        if code is not None:
            self.code = code
        if http_status is not None:
            self.http_status = http_status
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "detail": self.detail,
        }


# ---------------- 通用 ----------------
class NotFoundError(AppError):
    code = 1404
    http_status = 404
    message = "资源不存在"


class InvalidParamError(AppError):
    code = 1400
    http_status = 400
    message = "参数不合法"


class ConfigError(AppError):
    code = 1500
    http_status = 500
    message = "配置错误"


class NotConfiguredError(AppError):
    code = 1501
    http_status = 503
    message = "该功能尚未配置完成"


# ---------------- 数据源 ----------------
class DataSourceError(AppError):
    code = 2000
    http_status = 502
    message = "数据源调用失败"


class DataSourceRateLimited(DataSourceError):
    code = 2001
    http_status = 429
    message = "数据源触发限频，请稍后重试"


class DataSourceTimeout(DataSourceError):
    code = 2002
    http_status = 504
    message = "数据源响应超时"


class SymbolFormatError(DataSourceError):
    code = 2003
    http_status = 400
    message = "股票代码格式不正确"


class DataNotReadyError(AppError):
    code = 2100
    http_status = 503
    message = "本地数据尚未就绪，请先执行数据初始化"


# ---------------- 存储 ----------------
class StorageError(AppError):
    code = 3000
    http_status = 500
    message = "本地存储读写失败"


class DatabaseError(AppError):
    code = 3001
    http_status = 500
    message = "元数据库操作失败"


# ---------------- 业务 ----------------
class BacktestError(AppError):
    code = 4000
    http_status = 500
    message = "回测执行失败"


class SweepError(AppError):
    code = 4001
    http_status = 500
    message = "参数寻优执行失败"


class PaperError(AppError):
    code = 4100
    http_status = 500
    message = "模拟盘执行失败"


class AlertError(AppError):
    code = 4200
    http_status = 502
    message = "消息推送失败"


# ---------------- 读取保护 ----------------
class ReadUntilViolation(AppError):
    """违反数据时间锁：试图读取未来数据。"""

    code = 5000
    http_status = 400
    message = "违反数据时间锁：不允许读取未来数据"
