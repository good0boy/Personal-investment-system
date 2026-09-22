"""同步交易日历到 data/meta/trade_calendar.parquet。

用法：
    cd backend
    python -m tools.sync_trade_calendar
    python -m tools.sync_trade_calendar --source akshare    # 备用源
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.errors import AppError
from app.core.logging import logger, setup_logging
from app.core.paths import ensure_dirs
from app.repos import db
from app.services import calendar_service


def main() -> int:
    parser = argparse.ArgumentParser(description="同步交易日历")
    parser.add_argument(
        "--source",
        default="baostock",
        choices=["baostock", "akshare"],
        help="数据源（默认 baostock，含 is_trading_day 标记更完整）",
    )
    args = parser.parse_args()

    setup_logging()
    ensure_dirs()

    if not db.is_initialized():
        logger.error("meta.db 尚未初始化，请先执行：python -m tools.init_db")
        return 1

    try:
        if args.source == "akshare":
            # akshare 的日历只返回交易日，需自行补齐 is_trading_day 列
            import pandas as pd  # noqa: PLC0415

            from app.services import data_store  # noqa: PLC0415
            from app.services.data_source import akshare_client  # noqa: PLC0415

            dates = akshare_client.fetch_trade_dates(
                calendar_service.CALENDAR_START, calendar_service.CALENDAR_END
            )
            if not dates:
                logger.error("akshare 返回的交易日历为空")
                return 2
            df = pd.DataFrame({"date": pd.to_datetime(dates), "is_trading_day": 1})
            data_store.save_trade_calendar(df)
            calendar_service.calendar.load(force=True)
            info = calendar_service.calendar.stats()
        else:
            info = calendar_service.calendar.refresh()

    except AppError as exc:
        logger.error(f"同步失败：{exc.message}")
        return 2
    except Exception as exc:
        logger.exception(f"同步失败（未预期异常）：{exc}")
        return 3

    warn = calendar_service.calendar.coverage_warning()

    print()
    print("=" * 62)
    print("  交易日历同步完成")
    print("=" * 62)
    print(f"  总日历天数  : {info.get('total_days')}")
    print(f"  交易日天数  : {info.get('trading_days')}")
    print(f"  最早日期    : {info.get('first_date')}")
    print(f"  覆盖至      : {info.get('coverage_end')}")
    print(f"  最近交易日  : {info.get('last_date') or info.get('latest_trading_day')}")
    print(f"  今天        : {info.get('today')}")
    print(f"  今天是否交易日: {'是' if info.get('today_is_trading_day') else '否'}")
    if warn:
        print()
        print(f"  ⚠️  {warn}")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
