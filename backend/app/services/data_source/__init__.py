"""数据源抽象层 —— 业务层取数的唯一入口。

纪律（见 技术架构.md 第 4 章）：
    业务层**不得**直接 `import baostock` 或 `import akshare`，
    一律通过本模块暴露的函数取数。这样换源/加缓存/改限流只改这一层。

分工：
    baostock = 行情主源（模块 3 回测、模块 4 模拟盘；唯一提供逐日 isST/停牌）
    akshare  = 广度源（模块 1 数据分类、基本面、以及**同花顺板块** `*_ths`）
    fuyao    = 特色数据源（同花顺官方 REST：涨跌停池/连板天梯/板块成分/快照）

    ★ 为什么 fuyao 不占 `primary` / `secondary` 槽位（2026-10-01 决策）：
      那两个槽位的语义是"日线主源 / 广度源"，决定 `fetch_daily` 的默认走向；
      而 fuyao 的价值在**独占端点**（板块成分、按历史交易日取涨跌停池），
      不在"再挂一个通用日线源"。所以给它独立的 `get_fuyao()` 访问器。

用法：
    from app.services.data_source import fetch_daily, fetch_index_daily
    from app.services.data_source import get_fuyao
    df = fetch_daily("sh.600519", "2024-01-01", "2024-12-31")
    pool = get_fuyao().limit_up_pool("20260929")
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any

import pandas as pd

from app.core.config import settings
from app.core.errors import DataSourceError
from app.core.logging import logger
from app.services.data_source.akshare_client import akshare_client
from app.services.data_source.baostock_client import baostock_client
from app.services.data_source.base import (
    INDEX_COLUMNS,
    QUOTE_COLUMNS,
    BaseDataSource,
    DataSourceStatus,
    TTLCache,
    call_with_retry,
    normalize_date,
    to_compact_date,
    with_retry,
)
from app.services.data_source.fuyao_client import fuyao_client
from app.services.data_source.symbols import (
    TARGET_BOARDS,
    board_of,
    csi_index_code,
    from_thscode,
    is_board_thscode,
    is_target_board,
    normalize,
    split_exchange,
    to_akshare,
    to_baostock,
    to_board_thscode,
    to_plain,
    to_prefixed,
    to_suffixed,
    to_thscode,
)
from app.services.data_source.throttle import (
    CircuitBreaker,
    GroupedCircuitBreaker,
    SlidingWindowRateLimiter,
    looks_rate_limited,
)

__all__ = [
    # 客户端
    "baostock_client",
    "akshare_client",
    "fuyao_client",
    "get_primary",
    "get_secondary",
    "get_fuyao",
    "shutdown_all",
    # 便捷取数
    "fetch_daily",
    "fetch_index_daily",
    "fetch_trade_dates",
    "fetch_all_trade_dates",
    "fetch_spot",
    "fetch_spot_fuyao",
    "fetch_all_stock",
    "fetch_index_constituents",
    "fetch_stock_industry",
    "call_akshare",
    "health_all",
    "throttle_stats_all",
    "last_data_date",
    # 类型与常量
    "BaseDataSource",
    "DataSourceStatus",
    "QUOTE_COLUMNS",
    "INDEX_COLUMNS",
    "TTLCache",
    # 节流与熔断
    "SlidingWindowRateLimiter",
    "CircuitBreaker",
    "GroupedCircuitBreaker",
    "looks_rate_limited",
    "with_retry",
    "call_with_retry",
    # 代码格式工具
    "normalize",
    "split_exchange",
    "to_baostock",
    "to_plain",
    "to_prefixed",
    "to_suffixed",
    "to_akshare",
    "to_thscode",
    "from_thscode",
    "to_board_thscode",
    "is_board_thscode",
    "board_of",
    "is_target_board",
    "TARGET_BOARDS",
    "csi_index_code",
    "normalize_date",
    "to_compact_date",
]


@lru_cache(maxsize=1)
def get_primary() -> BaseDataSource:
    """行情主源（按 config.datasource.primary 选择）。"""
    key = settings.datasource.primary.lower()
    if key == "baostock":
        return baostock_client
    if key == "akshare":
        logger.warning("主数据源被配置为 akshare —— 将失去 isST/停牌/退市股精度，不推荐")
        return akshare_client
    raise DataSourceError(f"未知的主数据源：{key!r}")


@lru_cache(maxsize=1)
def get_secondary() -> BaseDataSource:
    """广度源。"""
    key = settings.datasource.secondary.lower()
    if key == "akshare":
        return akshare_client
    if key == "baostock":
        return baostock_client
    raise DataSourceError(f"未知的广度数据源：{key!r}")


@lru_cache(maxsize=1)
def get_fuyao() -> BaseDataSource:
    """特色数据源（同花顺 fuyao REST）。

    ★ 刻意**不放进 `primary`/`secondary` 槽位** —— 见本模块 docstring。
    """
    return fuyao_client


def shutdown_all() -> None:
    """关闭所有数据源会话（应用退出时调用）。"""
    for client in (baostock_client, akshare_client, fuyao_client):
        try:
            client.logout()
        except Exception as exc:
            logger.debug(f"关闭 {client.name} 会话时忽略异常：{exc}")


# ======================================================================
# 便捷取数（业务层用这些，不要直接用 client）
# ======================================================================
def fetch_daily(
    symbol: str,
    start_date: str,
    end_date: str,
    *,
    adjust: str | None = None,
    source: str | None = None,
) -> pd.DataFrame:
    """个股日线。默认走后复权 + 主源。"""
    client = _pick(source)
    return client.fetch_daily(
        symbol,
        start_date,
        end_date,
        adjust=adjust if adjust is not None else settings.data.adjust,
    )


def fetch_index_daily(
    symbol: str,
    start_date: str,
    end_date: str,
    *,
    source: str | None = None,
) -> pd.DataFrame:
    """指数日线（symbol 传 sh.000001 或 csi_index_code("上证指数")）。"""
    client = _pick(source)
    return client.fetch_index_daily(symbol, start_date, end_date)


def fetch_trade_dates(
    start_date: str,
    end_date: str,
    *,
    source: str | None = None,
    force_refresh: bool = False,
) -> list[str]:
    """区间交易日列表。`force_refresh=True` 跳过缓存（探测/刷新场景必须用）。"""
    client = _pick(source)
    return client.fetch_trade_dates(start_date, end_date, force_refresh=force_refresh)


def fetch_all_trade_dates(
    start_date: str = "1990-01-01",
    end_date: str | None = None,
    *,
    source: str | None = None,
    force_refresh: bool = False,
) -> pd.DataFrame:
    """全量日历（含非交易日）。优先用 baostock。

    ⚠️ `force_refresh=True` 是"刷新交易日历"的**必需**参数，否则会命中
       1 小时缓存而变成"假刷新"。
    """
    if source is None or source == "baostock":
        return baostock_client.fetch_all_trade_dates(
            start_date, end_date, force_refresh=force_refresh
        )
    raise DataSourceError("fetch_all_trade_dates 目前仅支持 baostock")


def fetch_spot() -> pd.DataFrame:
    """全市场实时快照（akshare `stock_zh_a_spot_em`，走东财 push2）。

    ⚠️ 2026-10-01 实测：东财 `push2.eastmoney.com` 在本机被网络掐断，
       本函数会抛 `DataSourceRateLimited`（soft 档）。**它不是唯一选择** ——
       需要可用快照时请用 `fetch_spot_fuyao()`（同花顺 fuyao REST）。

       保留本函数是为了"换台机器/换网络可能又能用"，并让失败点集中可见。
    """
    return akshare_client.fetch_spot()


def fetch_spot_fuyao(
    thscodes: str | None = None, limit: int = 100, offset: int = 0
) -> pd.DataFrame:
    """实时快照（同花顺 fuyao REST）。

    ⚠️ 列名与 `fetch_spot()` **不同**（这里是 fuyao 原始字段：
       `thscode/ticker/last_price/price_change_ratio_pct/...`，全英文），
       两者不可互换 —— 所以没有做成"自动降级"，避免调用方拿到两套 schema。
    """
    return fuyao_client.snapshot(thscodes=thscodes, limit=limit, offset=offset)


def fetch_all_stock(day: str | None = None) -> pd.DataFrame:
    """某日全部证券列表（仅 baostock 支持历史日期）。"""
    return baostock_client.fetch_all_stock(day)


def fetch_index_constituents(index: str, day: str | None = None) -> pd.DataFrame:
    """历史指数成分股（仅 baostock 支持）。"""
    return baostock_client.fetch_index_constituents(index, day)


def fetch_stock_industry() -> pd.DataFrame:
    """证监会行业分类。"""
    return baostock_client.fetch_stock_industry()


def call_akshare(func_name: str, params: dict[str, Any] | None = None) -> pd.DataFrame:
    """模块 1 的通用试跑入口。"""
    return akshare_client.call(func_name, params)


def last_data_date() -> str | None:
    """本地行情数据的最新日期（用于 /api/health 显示数据新鲜度）。

    只读 meta 表，不触发网络请求。
    """
    # 延迟导入以打破循环依赖
    from app.repos.meta_repo import get_meta_value  # noqa: PLC0415

    try:
        return get_meta_value("quote_last_date")
    except Exception:
        return None


def health_all(*, deep: bool = False) -> list[dict[str, Any]]:
    """所有数据源的健康状态。

    Args:
        deep: True 时**真实请求探测**（慢；akshare 还受 1 秒调用间隔约束）；
              False 时只读版本号 + 熔断状态（零网络请求）。

    返回项含 `deep` 字段，标明该条状态是"真的探测了"还是"只读了版本号"。
    **两者含义完全不同** —— 后者只说明"库装上了"，不代表"能连上数据源"，
    而"库能导入但连不上网"恰恰是最需要被发现的故障。前端据此区分。
    """
    out: list[dict[str, Any]] = []
    for client in (baostock_client, akshare_client, fuyao_client):
        try:
            status = client.health(deep=deep)
        except Exception as exc:
            status = DataSourceStatus(
                name=client.name, available=False, message=repr(exc), deep=deep
            )
        d = status.to_dict()
        d["throttle"] = client.throttle_stats()
        out.append(d)
    return out


def throttle_stats_all() -> list[dict[str, Any]]:
    """所有数据源的限流 / 熔断 / 缓存状态（供系统状态页展示）。"""
    return [
        client.throttle_stats()
        for client in (baostock_client, akshare_client, fuyao_client)
    ]


def _pick(source: str | None) -> BaseDataSource:
    if source is None:
        return get_primary()
    key = source.lower()
    if key == "baostock":
        return baostock_client
    if key == "akshare":
        return akshare_client
    if key == "fuyao":
        return fuyao_client
    raise DataSourceError(f"未知数据源：{source!r}（可选 baostock / akshare / fuyao）")
