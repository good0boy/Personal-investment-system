"""日志配置（loguru）。

用法：
    from app.core.logging import logger
    logger.info("...")

设计要点：
- 控制台 + 滚动文件双输出
- 文件按天分目录，便于事后排查
- 敏感信息（Webhook key、密钥）在写入前被脱敏
"""
from __future__ import annotations

import contextlib
import re
import sys
from pathlib import Path

from loguru import logger

from app.core.config import settings
from app.core.paths import LOGS_DIR

_CONFIGURED = False

#: 需要脱敏的模式：企微 webhook key、各种 secret/token
_MASK_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(key=)[0-9a-fA-F\-]{8,}"), r"\1***MASKED***"),
    (re.compile(r"(\"?(?:secret|token|password|passwd)\"?\s*[:=]\s*)['\"]?[^\s'\"]{6,}"),
     r"\1***MASKED***"),
    (re.compile(r"(https://qyapi\.weixin\.qq\.com/\S+?key=)[0-9a-fA-F\-]+"),
     r"\1***MASKED***"),
]


def mask_secrets(text: str) -> str:
    """把日志文本里的密钥替换为 ***MASKED***。"""
    for pattern, repl in _MASK_PATTERNS:
        text = pattern.sub(repl, text)
    return text


class _SecretMaskFilter:
    """loguru 过滤器：在消息进入 sink 前脱敏。"""

    def __call__(self, record: dict) -> bool:
        # 脱敏失败不能影响正常日志
        with contextlib.suppress(Exception):
            record["message"] = mask_secrets(str(record["message"]))
        return True


def setup_logging(log_dir: Path | None = None) -> None:
    """初始化 loguru。幂等，重复调用无副作用。"""
    global _CONFIGURED
    if _CONFIGURED:
        return

    cfg = settings.logging
    target_dir = log_dir or LOGS_DIR
    target_dir.mkdir(parents=True, exist_ok=True)

    logger.remove()

    fmt = (
        "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"
    )

    if cfg.console:
        logger.add(
            sys.stderr,
            level=cfg.level,
            format=fmt,
            colorize=True,
            filter=_SecretMaskFilter(),
            enqueue=False,
        )

    logger.add(
        target_dir / "{time:YYYY-MM-DD}" / "{time:HH}.log",
        level=cfg.level,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{line} - {message}",
        rotation=cfg.rotation,
        retention=cfg.retention,
        encoding="utf-8",
        filter=_SecretMaskFilter(),
        enqueue=True,          # 多进程/多线程写安全
        backtrace=False,
        diagnose=False,        # 不输出变量值，避免密钥泄露
    )

    _CONFIGURED = True
    logger.debug(f"日志已初始化，输出目录：{target_dir}")


__all__ = ["logger", "mask_secrets", "setup_logging"]
