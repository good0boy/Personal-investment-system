"""pytest 公共夹具。

★ 纪律：
  1. 测试用**合成数据**，不依赖 `data/` 下的真实行情 —— 真实数据会变，
     测试必须可重复。需要真实数据的地方单独打 skip 标记。
  2. 全局状态（时间锁、限流窗口、熔断）在每个用例前后都要重置，
     否则用例顺序会互相污染，出现"单跑通过、全跑失败"的假象。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services import data_store  # noqa: E402

#: 合成行情的交易日（覆盖 2024 全年，每月最后一个交易日）
SAMPLE_DATES: list[str] = [
    "2024-01-31", "2024-02-29", "2024-03-29", "2024-04-30",
    "2024-05-31", "2024-06-28", "2024-07-31", "2024-08-30",
    "2024-09-30", "2024-10-31", "2024-11-29", "2024-12-31",
]

#: 上半年的最后一个交易日 —— 时间锁边界测试用
MID_YEAR_LAST = "2024-06-28"


@pytest.fixture(autouse=True)
def _isolate_global_state():
    """隔离每个用例的全局状态。

    1. 时间锁：前后都清空，避免用例间互相污染。
    2. DuckDB 连接：用例结束**必须关闭**。
       原因：`get_duck()` 是进程内单例，它会持有临时 parquet 的文件句柄；
       Windows 下被占用的目录删不掉，pytest 清理 tmp_path 时会抛
       `PermissionError: [Errno 13]`，表现为**随机某个用例的 teardown 报错**。
       这是测试基础设施缺陷，不是被测代码的问题，但同样会让 CI 变红。
    """
    data_store.set_read_until(None)
    yield
    data_store.set_read_until(None)
    data_store.close_duck()


@pytest.fixture
def quote_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """合成日线目录，并把它替换为 `data_store.QUOTE_DAILY_DIR`。

    生成两个标的：
      · sh.600519 —— 12 个交易日，逐年覆盖全年（用于时间锁边界测试）
      · sz.000001 —— 同上
    """
    d = tmp_path / "quote_daily"
    d.mkdir(parents=True, exist_ok=True)

    for sym, base in (("sh.600519", 1700.0), ("sz.000001", 12.0)):
        df = pd.DataFrame(
            {
                "date": pd.to_datetime(SAMPLE_DATES),
                "symbol": sym,
                "open": [base + i for i in range(len(SAMPLE_DATES))],
                "high": [base + i + 1 for i in range(len(SAMPLE_DATES))],
                "low": [base + i - 1 for i in range(len(SAMPLE_DATES))],
                "close": [base + i + 0.5 for i in range(len(SAMPLE_DATES))],
                "preclose": [base + i - 0.5 for i in range(len(SAMPLE_DATES))],
                "volume": [1_000_000.0] * len(SAMPLE_DATES),
                "amount": [1.0e8] * len(SAMPLE_DATES),
                "turn": [1.0] * len(SAMPLE_DATES),
                "pct_chg": [0.5] * len(SAMPLE_DATES),
                "trade_status": [1] * len(SAMPLE_DATES),
                "is_st": [0] * len(SAMPLE_DATES),
            }
        )
        data_store.write_parquet(df, d / f"{sym}.parquet")

    monkeypatch.setattr(data_store, "QUOTE_DAILY_DIR", d)
    return d


@pytest.fixture
def universe_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """合成股票池快照目录。"""
    d = tmp_path / "universe"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(data_store, "UNIVERSE_DIR", d)
    return d


@pytest.fixture
def factor_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """合成因子目录（含 daily 子目录），用于时间锁测试。"""
    d = tmp_path / "factor"
    (d / "daily").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(data_store, "FACTOR_DIR", d)
    return d


#: 合成财务数据的公告日（故意让第二个公告日**落在 SAMPLE_DATES 中间**，
#: 这样 as-of 归并的"切换点"会被真的走到）
FUND_NOTICE_DATES: tuple[str, str] = ("2024-03-15", "2024-08-15")


@pytest.fixture
def fundamental_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """合成基本面目录（financial_indicator 逐只 + 一个批量集）。

    两只标的各两期报表，公告日见 `FUND_NOTICE_DATES`。
    数值刻意为"可手算"的整数，便于断言。
    """
    d = tmp_path / "fundamental"
    fin = d / "financial_indicator"
    yjbb = d / "yjbb"
    fin.mkdir(parents=True, exist_ok=True)
    yjbb.mkdir(parents=True, exist_ok=True)

    rows = []
    for sym, base in (("sh.600519", 10.0), ("sz.000001", 20.0)):
        for i, notice in enumerate(FUND_NOTICE_DATES):
            report = "2023-12-31" if i == 0 else "2024-06-30"
            rows.append(
                {
                    "symbol": sym,
                    "report_date": pd.Timestamp(report),
                    "notice_date": pd.Timestamp(notice),
                    # 一季报口径（÷1）与中报口径（÷2），用于验证年度化
                    "ROEJQ": base + i,
                    "ZCFZL": base * 4 + i,
                }
            )
    for sym, base in (("sh.600519", 10.0), ("sz.000001", 20.0)):
        sub = pd.DataFrame([r for r in rows if r["symbol"] == sym])
        data_store.write_parquet(sub, fin / f"{sym}.parquet")

    batch = pd.DataFrame(
        {
            "symbol": ["sh.600519", "sz.000001"],
            "report_date": pd.to_datetime(["2024-06-30", "2024-06-30"]),
            "notice_date": pd.to_datetime(["2024-08-20", "2024-08-20"]),
            "PARENTNETPROFIT": [1.0, 2.0],
        }
    )
    data_store.write_parquet(batch, yjbb / "all.parquet")

    monkeypatch.setattr(data_store, "FUNDAMENTAL_DIR", d)
    return d
