"""交易日历服务。

所有"判断是否交易日""取上一/下一交易日""列区间交易日"的逻辑都走这里，
不要在业务代码里用自然日做判断。

数据来源：baostock `query_trade_dates`（含 is_trading_day 标记）
落盘位置：data/meta/trade_calendar.parquet
"""
from __future__ import annotations

import threading
from datetime import date, datetime
from typing import Any

import pandas as pd

from app.core.errors import DataNotReadyError, InvalidParamError
from app.core.logging import logger
from app.repos import meta_repo
from app.services import data_store
from app.services.data_source import fetch_all_trade_dates
from app.services.data_source.base import normalize_date

_local = threading.local()

#: 日历同步的**请求上界**。
#
# ★ 为什么拉到未来（2026-09-18 定稿，主人拍板）：日历是**日程表**，交易所提前
#   公布了未来排期，物理上可得。拉到未来有三个好处：
#     1. 回测/仿真在样本最后一个交易日算"T+1 成交日"不会因为日历到头而崩；
#     2. 不用每天早上重同步（此前 end 默认 today，导致每个交易日开盘前
#        健康检查必然 degraded、启动补跑必然抛 DataNotReadyError）；
#     3. 与 `tools/sync_trade_calendar --source akshare` 分支共用同一常量
#        （此前它写死 2030-12-31、baostock 分支按 today 走 —— 两条路径各说各话）。
#
# ⚠️ **实测（2026-09-18）：数据源其实只给到"次年年底"。**
#      传 end=2030-12-31 时，baostock `query_trade_dates` 只返回到
#      **2026-12-31**（13162 自然日 / 8797 交易日）；akshare 的
#      `tool_trade_date_hist_sina` 同样是 2026-12-31。
#      所以这个常量是"请求上界"，不是"实际覆盖上界" —— 实际覆盖以
#      `stats()["coverage_end"]` 为准，且**每年年底需要重同步一次**
#      （数据源会滚动发布下一年排期）。
#
# ⚠️ 代价：日历里从此**存在未来日期**。因此 `data_store` 的读取上界被
#    收紧为 min(时间锁, 今天)，保证业务侧读不到未来行情
#    （见 data_store._upper_bound）。
CALENDAR_END = "2030-12-31"

#: 日历最小起点（baostock 的覆盖起点）
CALENDAR_START = "1990-01-01"


class TradingCalendar:
    """内存缓存的交易日历。线程安全（每线程一份只读副本）。"""

    def __init__(self) -> None:
        self._df: pd.DataFrame | None = None
        self._trading: list[str] = []

    # ------------------------------------------------------------------
    # 加载
    # ------------------------------------------------------------------
    def load(self, *, force: bool = False) -> None:
        """从 Parquet 加载（force=True 时强制重读磁盘）。"""
        if self._df is not None and not force:
            return
        try:
            df = data_store.read_trade_calendar()
        except DataNotReadyError:
            logger.warning("交易日历未初始化，尝试从数据源同步...")
            self.refresh()
            df = data_store.read_trade_calendar()

        df["date"] = pd.to_datetime(df["date"])
        self._df = df.sort_values("date").reset_index(drop=True)
        self._trading = (
            self._df.loc[self._df["is_trading_day"] == 1, "date"]
            .dt.strftime("%Y-%m-%d")
            .tolist()
        )

    def refresh(self) -> dict[str, Any]:
        """从数据源重新同步并落盘（拉取区间 `CALENDAR_START` ~ `CALENDAR_END`）。

        ⚠️ **无条件绕过缓存**（`force_refresh=True`）。

        本方法的语义就是"重新拉取"。`fetch_all_trade_dates` 被 `_invoke` 以
        1 小时 TTL 缓存，若不加 `force_refresh`，1 小时内重复调用会直接命中缓存 ——
        接口报"已同步"、`calendar_synced_at` 时间戳照更新，但**根本没连数据源**，
        即"假刷新"。实测确认过：不加时连续两次调用的真实网络次数不递增。

        缓存只用于"普通读取"路径；凡是**语义上承诺了动作**的方法（刷新/探测）
        都必须强制绕过缓存。
        """
        logger.info(
            f"开始同步交易日历（baostock query_trade_dates，"
            f"{CALENDAR_START} ~ {CALENDAR_END}，强制刷新）..."
        )
        df = fetch_all_trade_dates(CALENDAR_START, CALENDAR_END, force_refresh=True)
        if df.empty:
            raise DataNotReadyError("交易日历拉取结果为空，数据源可能异常")

        data_store.save_trade_calendar(df)

        last = pd.to_datetime(df["date"]).max()
        meta_repo.set_meta_bulk(
            {
                "calendar_last_date": last.strftime("%Y-%m-%d"),
                "calendar_synced_at": datetime.now().isoformat(timespec="seconds"),
                "calendar_total_days": len(df),
            }
        )
        self.load(force=True)

        n_trading = int((df["is_trading_day"] == 1).sum())
        logger.info(
            f"交易日历同步完成：{len(df)} 天（其中交易日 {n_trading} 天），"
            f"覆盖至 {last.date()}"
        )
        # ★ 统一返回 `stats()` 的结构，不要另造一份。
        #   历史上这里返回的是 {total_days, trading_days, first_date, last_date}，
        #   与 stats() 字段名不一致（`last_date` 在两边含义还不同），
        #   结果 CLI 打印出 "最近交易日: None"、今天是否交易日: 否 —— 静默错。
        #   同一件事只有一份结构，就不会再有这种偏差。
        return self.stats()

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    @property
    def df(self) -> pd.DataFrame:
        self.load()
        assert self._df is not None
        return self._df

    @property
    def all_trading_days(self) -> list[str]:
        self.load()
        return self._trading

    def is_trading_day(self, day: str | date | datetime) -> bool:
        d = normalize_date(day)
        try:
            row = self.df.loc[self.df["date"] == pd.Timestamp(d)]
        except Exception:
            return False
        return bool(len(row) and int(row.iloc[0]["is_trading_day"]) == 1)

    def is_weekend(self, day: str | date | datetime) -> bool:
        return pd.Timestamp(normalize_date(day)).weekday() >= 5

    def latest_trading_day(self, on_or_before: str | date | datetime | None = None) -> str:
        """不晚于给定日期的最近一个交易日。"""
        limit = normalize_date(on_or_before) if on_or_before else date.today().strftime("%Y-%m-%d")
        days = self.all_trading_days
        if not days:
            raise DataNotReadyError("交易日历为空")
        candidates = [d for d in days if d <= limit]
        if not candidates:
            raise DataNotReadyError(f"交易日历中没有早于 {limit} 的交易日")
        return candidates[-1]

    def next_trading_day(self, day: str | date | datetime, n: int = 1) -> str:
        """之后第 n 个交易日。"""
        d = normalize_date(day)
        days = self.all_trading_days
        later = [x for x in days if x > d]
        if len(later) < n:
            raise DataNotReadyError(
                f"{d} 之后只剩 {len(later)} 个交易日，不足 {n} 个。"
                f"请同步最新的交易日历。"
            )
        return later[n - 1]

    def prev_trading_day(self, day: str | date | datetime, n: int = 1) -> str:
        """之前第 n 个交易日。"""
        d = normalize_date(day)
        days = self.all_trading_days
        earlier = [x for x in days if x < d]
        if len(earlier) < n:
            raise DataNotReadyError(f"{d} 之前只剩 {len(earlier)} 个交易日，不足 {n} 个")
        return earlier[-n]

    def trading_days(
        self,
        start: str | date | datetime,
        end: str | date | datetime,
    ) -> list[str]:
        """闭区间内的交易日列表。"""
        s, e = normalize_date(start), normalize_date(end)
        if s > e:
            raise InvalidParamError(f"起始日期晚于结束日期：{s} > {e}")
        return [d for d in self.all_trading_days if s <= d <= e]

    def count_trading_days(
        self,
        start: str | date | datetime,
        end: str | date | datetime,
    ) -> int:
        return len(self.trading_days(start, end))

    def shift(self, day: str | date | datetime, n: int) -> str:
        """按交易日偏移（n 可正可负）。"""
        if n == 0:
            return normalize_date(day)
        return self.next_trading_day(day, n) if n > 0 else self.prev_trading_day(day, -n)

    def trading_day_offset(self, start: str | date | datetime, end: str | date | datetime) -> int:
        """两个日期之间的交易日跨度（end - start，单位：交易日）。

        用于"持有满 N 个交易日""卖出后 N 个交易日禁买"等判断。
        """
        s, e = normalize_date(start), normalize_date(end)
        if s > e:
            return -self.trading_day_offset(e, s)
        return len([d for d in self.all_trading_days if s <= d <= e])

    def range_days(self, start: str | date | datetime, n: int) -> list[str]:
        """从 start 起（含）连续 n 个交易日。"""
        s = normalize_date(start)
        days = [d for d in self.all_trading_days if d >= s]
        if len(days) < n:
            raise DataNotReadyError(f"从 {s} 起只有 {len(days)} 个交易日，不足 {n} 个")
        return days[:n]

    def calendar_days(self, start: str, end: str) -> list[str]:
        """闭区间内全部自然日（用于需要自然日对齐的场景）。"""
        s = pd.Timestamp(normalize_date(start))
        e = pd.Timestamp(normalize_date(end))
        return [d.strftime("%Y-%m-%d") for d in pd.date_range(s, e, freq="D")]

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        """日历状态。

        ★ 三个"日期"必须分清（否则前端会显示成"最近交易日 = 2030-12-31"）：
          · `coverage_end`       日历**覆盖到**哪一天（含未来，可能是 2030-12-31）
          · `last_date`          最后一个**已发生**的交易日（≤ 今天）—— 业务可用上界
          · `latest_trading_day` 同 last_date，保留给既有调用方
        """
        self.load()
        assert self._df is not None
        trading = self._trading
        today_s = date.today().strftime("%Y-%m-%d")
        realised = [d for d in trading if d <= today_s]
        coverage_end = pd.to_datetime(self._df["date"]).max().strftime("%Y-%m-%d")
        return {
            "total_days": len(self._df),
            "trading_days": len(trading),
            "first_date": trading[0] if trading else None,
            "coverage_end": coverage_end,
            "future_trading_days": len(trading) - len(realised),
            "last_date": realised[-1] if realised else None,
            "today": today_s,
            "today_is_trading_day": self.is_trading_day(today_s),
            "latest_trading_day": realised[-1] if realised else None,
            "synced_at": meta_repo.get_meta_value("calendar_synced_at"),
        }

    def coverage_warning(self, need_until: str | None = None) -> str | None:
        """返还覆盖度警告（None 表示无问题）。

        ★ 判定对象是**日历覆盖上界**（含非交易日的最后一天），不是"最后一个交易日"。

        为什么不比"最后一个交易日 vs 今天"（旧实现）：
          那样**每逢周末/节假日都必然误报** —— 周五收盘后到周一开盘前，
          最后一个交易日永远早于今天，健康检查会长期挂着一条假告警。

        为什么也不能把基准换成 `latest_trading_day()`：
          那样日历**真过期时会漏报** —— 日历只覆盖到 2025 年时，
          "最后一个交易日" 2025-12-31 与基准相等，判定为通过。

        用覆盖上界判断，恰好等价于"最近需要的那个交易日是否已被日历覆盖"：
        日历是连续自然日（含 `is_trading_day=0` 的行），所以覆盖上界 ≥ 今天
        就说明今天（或今天之前最近的那个交易日）一定在覆盖范围内。
        """
        self.load()
        if self._df is None or self._df.empty:
            return "交易日历为空"
        coverage_end = pd.to_datetime(self._df["date"]).max().strftime("%Y-%m-%d")
        target = normalize_date(need_until) if need_until else date.today().strftime("%Y-%m-%d")
        if coverage_end < target:
            return (
                f"交易日历只覆盖到 {coverage_end}，早于需要的 {target}。"
                f"请执行 python -m tools.sync_trade_calendar 更新。"
            )
        return None


#: 全局单例
calendar = TradingCalendar()


def get_calendar() -> TradingCalendar:
    return calendar
