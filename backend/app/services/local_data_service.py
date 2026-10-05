"""模块 1 · 本地数据清单 —— 「磁盘上现在到底有什么」。

为什么要有它
------------
`data/` 下已经堆了几个 GB、两万多个 parquet，但没有任何地方能一眼看出
"本地到底有哪些维度的数据、各多少、长什么样"。在此之前要确认某份数据是否
就位，只能去翻目录，或者读回补状态 JSON —— 而**状态文件与磁盘会不一致**
（本项目已经栽过一次：`data/raw/fundamental/` 有 2.1 GB 真实数据，因子分类却
还标着「本地没有财务数据」）。所以本服务只认一个真相：

    ★ 清单以【磁盘】为唯一真相。

  · 文件数 / 字节数 → 直接扫目录（`glob` + `stat`）
  · 行数 / 覆盖区间 → DuckDB 读 parquet **元信息**（不扫数据列，很快）
  · 示例数据        → 真读真表，取该维度最近 N 行

唯一例外是**因子产物**：它的状态按纪律记在 `data/factor/_manifest.json`
（见 `paths.py` 里 `FACTOR_MANIFEST_FILE` 的说明），这里直接读它，不重复扫 ——
扫了就有两个数，两个数就会有对不上的那一天。

「未落盘」也要如实说
------------------
计划内但磁盘上没有的数据集（如 `fund_hold`），不会被静默跳过 ——
它会以 `available=false` 出现在清单里，并带上**从回补状态文件里现读的**
失败原因（不写死在代码里，否则会像上一版 `registry.yaml` 那样过期）。

统计成本与缓存
------------
一次完整统计要读约 2.5 万个 parquet 的文件尾，实测 ~10 秒。因此结果做
进程内缓存（TTL 见 `_TTL_SECONDS`），`datasets(refresh=True)` 可强制重算。
"""
from __future__ import annotations

import glob as globlib
import json
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from app.core.errors import AppError, NotFoundError
from app.core.logging import logger
from app.core.paths import (
    ANOMALY_DIR,
    BACKFILL_FUND_STATE_FILE,
    BACKFILL_SPECIAL_STATE_FILE,
    BACKTEST_DIR,
    BOARD_CATALOG_DIR,
    BOARD_DIR,
    DATA_DIR,
    DRAGON_TIGER_DIR,
    FACTOR_ANALYSIS_DIR,
    FACTOR_DIR,
    FACTOR_MANIFEST_FILE,
    FUNDAMENTAL_DIR,
    HOT_LIST_DIR,
    LIMIT_POOL_DIR,
    META_DB,
    PAPER_DIR,
    QUOTE_DAILY_DIR,
    QUOTE_INDEX_DIR,
    SWEEP_DIR,
    TRADE_CALENDAR_PARQUET,
    UNIVERSE_DIR,
    relative_to_project,
)
from app.services import data_store

#: 统计结果的进程内缓存时长（秒）。
#: 首次统计 ~10s，不能每次点开页面都重扫；但也不能缓存太久 ——
#: 用户点"刷新"往往就是想知道刚跑完的回补到底落盘没有。
_TTL_SECONDS = 600

#: 示例数据最多展示多少列（最宽的真表 151 列，全铺出来没法看）
_MAX_SAMPLE_COLUMNS = 12

#: 单元格最长字符数（业绩变动原因这类长文本会撑爆表格）
_MAX_CELL_CHARS = 160

#: 数据来源语义。**必须如实标注** —— 原始下载与本地派生混在一起看会误导：
#: 前者是"数据源给的"，后者是"我们自己算的"，可信度与更新方式完全不同。
ORIGINS: dict[str, str] = {
    "downloaded": "原始下载",
    "derived": "本地派生",
    "state": "本地状态",
}

#: 分组（顺序即前端展示顺序）
GROUPS: tuple[dict[str, str], ...] = (
    {
        "key": "market",
        "label": "行情数据",
        "desc": "baostock 回补落盘，后复权口径。回测与模拟盘的数据基础。",
    },
    {
        "key": "universe",
        "label": "股票池",
        "desc": "逐日截面的可投标的池，已剔除 ST / 停牌 / 新股 / 一字板。",
    },
    {
        "key": "fundamental",
        "label": "基本面与财务",
        "desc": "akshare 回补落盘。含公告日 NOTICE_DATE，可按公告日 as-of 对齐（防未来函数）。",
    },
    {
        "key": "factor",
        "label": "因子产物",
        "desc": "`python -m factors.calc` 计算落盘；规模取自产物清单 _manifest.json。",
    },
    {
        "key": "research",
        "label": "回测与寻优",
        "desc": "CLI 跑出的产物（回测 / 寻优刻意不做 Web 入口）。",
    },
    {
        "key": "paper",
        "label": "模拟盘",
        "desc": "调度器每日盘后落盘，与回测共用同一套成本模型。",
    },
    {
        "key": "special",
        "label": "同花顺特色数据",
        "desc": (
            "同花顺官方 REST（fuyao）回补落盘：涨跌停池 / 板块指数历史 / 热榜 / "
            "龙虎榜 / 个股异动。⚠️ 各数据起点不同（见每个维度的说明）。"
        ),
    },
    {
        "key": "meta",
        "label": "元数据与状态",
        "desc": "交易日历与本地状态库（SQLite）。",
    },
)

#: SQLite 维度的示例取哪张表（白名单 —— 表名不由外部输入决定）
_SQLITE_SAMPLE_TABLE = "datasource_log"

#: 文件名是不是标的名（`sh.600519.parquet`）。用于把"这段示例是哪只票的"告诉前端。
_SYMBOL_NAME_RE = re.compile(r"^[a-z]{2}\.\d{6}$")


# ======================================================================
# 描述结构
# ======================================================================
@dataclass(frozen=True)
class SampleSpec:
    """一个维度的示例数据取法。

    三条路只走一条，优先级从上到下：
      · `sqlite` —— 读本地状态库里的某张表
      · `sql`    —— 自定义 SQL（需要横向拼接的场合：多个因子拼成一张宽表）
      · `glob`   —— 从匹配到的文件里挑一个，取它的若干行
    """

    caption: str = ""
    # ---- SQLite ----
    sqlite: bool = False
    # ---- 自定义 SQL ----
    sql: str | None = None
    # ---- 挑文件读 ----
    glob: str | None = None
    prefer: str | None = None      # 优先挑这个文件名（不含 .parquet）
    latest: bool = False           # 按文件名倒序挑最新（日期/时间戳命名的快照）
    columns: tuple[str, ...] = ()  # 想展示的列；实际不存在的会被自动剔除
    order_by: str | None = None    # 排序列（通常是时间列）
    desc: bool = True              # 排序方向：True = 最近的在前
    #: 路径里含这些片段的文件直接排除。
    #: 用于避开"通线验证"类产物 —— 例如 `sweep/smoke-165/`（只跑 165 只抽样标的，
    #: 项目自己写着"结论不可用于评估策略"），拿它当示例会误导。
    skip_contains: tuple[str, ...] = ()


@dataclass(frozen=True)
class Dimension:
    """一个本地数据维度（= `data/` 下的一个数据集合）。"""

    key: str
    name: str
    group: str
    origin: str
    desc: str
    grain: str                      # "一行代表什么" —— 看数字前先搞清单位
    producer: str                   # 谁把它落盘的（工具 / 服务）
    root: Path                      # 展示用根路径
    #: 统计**行数**用的 glob（必须是一组 schema 一致的表）。
    #: ⚠️ 别把 equity / orders / trades 混在一个 glob 里 —— DuckDB 会因 schema
    #: 不一致直接报错（同名不同列），这正是本服务第一版踩到的坑。
    pattern: str | None
    #: 统计**文件数 / 体积**用的 glob。默认同 `pattern`；当"一个维度 = 多个
    #: 子表"（回测 run 目录、寻优 tag 目录）时，要单独给一个更宽的 glob，
    #: 否则"体积"只算主表、看着比真实占用小。
    file_pattern: str | None = None
    #: 行数的口径说明（如"仅净值表"）。行数与文件数不同源时必须写清楚。
    rows_scope: str = ""
    date_col: str | None = None     # 时间列（用于覆盖区间）
    date_label: str = "覆盖区间"
    sample: SampleSpec | None = None
    stats_from_manifest: bool = False
    stats_from_sqlite: bool = False
    extras: tuple[tuple[str, str], ...] = ()


def _posix(p: Path | str) -> str:
    return Path(p).as_posix()


def _dir_glob(d: Path) -> str:
    return _posix(d) + "/*.parquet"


#: 逐只类财务表的公共列（每张表都有这三列）
_SHEET_ID_COLS = ("symbol", "report_date", "notice_date", "REPORT_DATE_NAME")

#: 示例取哪只标的（优先用它，取不到就退化成目录里的第一个文件）
_SAMPLE_SYMBOL = "sh.600519"


def _sheet_sample(dataset: str, columns: tuple[str, ...], *, caption: str) -> SampleSpec:
    """逐只类财务表的示例取法：固定挑一只标的，取它最近若干报告期。

    ★ 必须**按标的挑文件**，不能对整目录 glob 取 top-N ——
      后者取回来的是"全市场最新的 N 条"，混在多个标的中，看不出单只的时间序列。
    """
    return SampleSpec(
        caption=caption,
        glob=_dir_glob(FUNDAMENTAL_DIR / dataset),
        prefer=_SAMPLE_SYMBOL,
        columns=columns,
        order_by="report_date",
    )


def _batch_sample(dataset: str, columns: tuple[str, ...], *, caption: str) -> SampleSpec:
    """批量类（单张 all.parquet）的示例取法：按公告日取最新几条。"""
    return SampleSpec(
        caption=caption,
        glob=_dir_glob(FUNDAMENTAL_DIR / dataset),
        columns=columns,
        order_by="notice_date",
    )


def _definitions() -> tuple[Dimension, ...]:
    """全部维度定义 —— **唯一维护点**：加一个维度只改这一个函数。"""
    return (
        # ---------------- 行情 ----------------
        Dimension(
            key="quote_daily",
            name="个股日线行情",
            group="market",
            origin="downloaded",
            desc="全部 A 股（含已退市）的日线，baostock 后复权（hfq）。回测与因子的唯一行情来源。",
            grain="1 行 = 1 只标的 1 个交易日",
            producer="tools/backfill_quotes.py（断点续传，状态见 data/meta/backfill_state.json）",
            root=QUOTE_DAILY_DIR,
            pattern=_dir_glob(QUOTE_DAILY_DIR),
            date_col="date",
            sample=SampleSpec(
                caption="按日期倒序，最新的在最前",
                glob=_dir_glob(QUOTE_DAILY_DIR),
                prefer="sh.600519",
                columns=(
                    "date", "symbol", "open", "high", "low", "close",
                    "preclose", "volume", "amount", "turn", "pct_chg", "trade_status",
                ),
                order_by="date",
            ),
            extras=(("代码格式", "sh.600519 / sz.000001（baostock 风格，无歧义）"),),
        ),
        Dimension(
            key="quote_index",
            name="指数日线行情",
            group="market",
            origin="downloaded",
            desc="指数日线。策略的择时信号（上证指数）用的就是它。",
            grain="1 行 = 1 个指数 1 个交易日",
            producer="tools/backfill_quotes.py",
            root=QUOTE_INDEX_DIR,
            pattern=_dir_glob(QUOTE_INDEX_DIR),
            date_col="date",
            sample=SampleSpec(
                caption="按日期倒序，最新的在最前",
                glob=_dir_glob(QUOTE_INDEX_DIR),
                prefer="sh.000001",
                columns=(
                    "date", "symbol", "open", "high", "low", "close",
                    "volume", "amount", "pct_chg",
                ),
                order_by="date",
            ),
            extras=(("指数代码", "sh.000001（上证指数）"),),
        ),
        # ---------------- 股票池 ----------------
        Dimension(
            key="universe",
            name="逐日股票池快照",
            group="universe",
            origin="derived",
            desc=(
                "每个交易日一份快照，只落**当天可投的标的**（回测内选股与基准都按它对齐）。"
                "`kept` 列是当天的池子大小，`empty=true` 表示当天确实没有可投标的 —— "
                "空池也落盘，才能与「这天没跑过」区分开。"
            ),
            grain="1 行 = 1 个交易日 1 只可投标的",
            producer="tools/backfill_universe.py（一次性回补；回测时由 tools/universe.py 现场构建）",
            root=UNIVERSE_DIR,
            pattern=_dir_glob(UNIVERSE_DIR),
            date_col="date",
            sample=SampleSpec(
                caption="最新一天快照，按标的代码排序",
                glob=_dir_glob(UNIVERSE_DIR),
                latest=True,
                columns=("date", "symbol", "kept", "empty"),
                order_by="symbol",
                desc=False,
            ),
        ),
        # ---------------- 基本面 ----------------
        Dimension(
            key="balance_sheet",
            name="资产负债表",
            group="fundamental",
            origin="downloaded",
            desc="东财三大报表之一，逐只落盘。科目值与其同比（*_YOY）各一套列。",
            grain="1 行 = 1 只标的 1 个报告期",
            producer="tools/backfill_fundamental.py",
            root=FUNDAMENTAL_DIR / "balance_sheet",
            pattern=_dir_glob(FUNDAMENTAL_DIR / "balance_sheet"),
            date_col="report_date",
            date_label="覆盖（报告期）",
            sample=_sheet_sample(
                "balance_sheet",
                (*_SHEET_ID_COLS, "TOTAL_ASSETS", "TOTAL_LIABILITIES",
                 "TOTAL_EQUITY", "ASSET_BALANCE", "LIAB_BALANCE"),
                caption="按报告期倒序，最新的在最前",
            ),
        ),
        Dimension(
            key="profit_sheet",
            name="利润表",
            group="fundamental",
            origin="downloaded",
            desc="东财三大报表之一，逐只落盘。营业收入 / 净利润 / 每股收益在这张表。",
            grain="1 行 = 1 只标的 1 个报告期",
            producer="tools/backfill_fundamental.py",
            root=FUNDAMENTAL_DIR / "profit_sheet",
            pattern=_dir_glob(FUNDAMENTAL_DIR / "profit_sheet"),
            date_col="report_date",
            date_label="覆盖（报告期）",
            sample=_sheet_sample(
                "profit_sheet",
                (*_SHEET_ID_COLS, "OPERATE_INCOME", "OPERATE_PROFIT",
                 "TOTAL_PROFIT", "NETPROFIT", "PARENT_NETPROFIT", "BASIC_EPS"),
                caption="按报告期倒序，最新的在最前",
            ),
        ),
        Dimension(
            key="cashflow_sheet",
            name="现金流量表",
            group="fundamental",
            origin="downloaded",
            desc="东财三大报表之一，逐只落盘。列最宽（151 列），单只拉取也最慢。",
            grain="1 行 = 1 只标的 1 个报告期",
            producer="tools/backfill_fundamental.py",
            root=FUNDAMENTAL_DIR / "cashflow_sheet",
            pattern=_dir_glob(FUNDAMENTAL_DIR / "cashflow_sheet"),
            date_col="report_date",
            date_label="覆盖（报告期）",
            sample=_sheet_sample(
                "cashflow_sheet",
                (*_SHEET_ID_COLS, "NETCASH_OPERATE", "NETCASH_INVEST",
                 "NETCASH_FINANCE", "CCE_ADD", "BEGIN_CCE", "END_CCE"),
                caption="按报告期倒序，最新的在最前",
            ),
        ),
        Dimension(
            key="financial_indicator",
            name="财务指标摘要",
            group="fundamental",
            origin="downloaded",
            desc=(
                "ROE / 毛利率 / 负债率 / 周转率 / 同比增长等现成口径（81 列），"
                "基本面因子的主数据源 —— 不必自己从三大报表重算。"
            ),
            grain="1 行 = 1 只标的 1 个报告期",
            producer="tools/backfill_fundamental.py",
            root=FUNDAMENTAL_DIR / "financial_indicator",
            pattern=_dir_glob(FUNDAMENTAL_DIR / "financial_indicator"),
            date_col="report_date",
            date_label="覆盖（报告期）",
            sample=_sheet_sample(
                "financial_indicator",
                (*_SHEET_ID_COLS, "ROEJQ", "ZZCJLL", "XSMLL", "XSJLL", "ZCFZL",
                 "TOTALOPERATEREVETZ", "PARENTNETPROFITTZ", "EPSJB", "BPS"),
                caption="按报告期倒序，最新的在最前",
            ),
        ),
        Dimension(
            key="yjbb",
            name="业绩报表（全市场）",
            group="fundamental",
            origin="downloaded",
            desc="按报告期拉取的全市场快照：一次拿到所有股票的营收 / 净利 / ROE / 毛利率。",
            grain="1 行 = 1 只标的 1 个报告期",
            producer="tools/backfill_fundamental.py（批量类，落 all.parquet）",
            root=FUNDAMENTAL_DIR / "yjbb",
            pattern=_dir_glob(FUNDAMENTAL_DIR / "yjbb"),
            date_col="report_date",
            date_label="覆盖（报告期）",
            sample=_batch_sample(
                "yjbb",
                ("股票代码", "股票简称", "营业总收入-营业总收入", "营业总收入-同比增长",
                 "净利润-净利润", "净利润-同比增长", "净资产收益率", "销售毛利率",
                 "所处行业", "report_date", "notice_date"),
                caption="按公告日倒序，最新的在最前",
            ),
        ),
        Dimension(
            key="yjyg",
            name="业绩预告（全市场）",
            group="fundamental",
            origin="downloaded",
            desc="按报告期拉取的全市场业绩预告，含预告类型与变动原因（长文本）。",
            grain="1 行 = 1 只标的 1 个报告期",
            producer="tools/backfill_fundamental.py（批量类）",
            root=FUNDAMENTAL_DIR / "yjyg",
            pattern=_dir_glob(FUNDAMENTAL_DIR / "yjyg"),
            date_col="report_date",
            date_label="覆盖（报告期）",
            sample=SampleSpec(
                caption="按公告日倒序，最新的在最前",
                glob=_dir_glob(FUNDAMENTAL_DIR / "yjyg"),
                columns=(
                    "股票代码", "股票简称", "预测指标", "业绩变动", "预测数值",
                    "业绩变动幅度", "预告类型", "上年同期值", "公告日期", "notice_date",
                ),
                order_by="notice_date",
            ),
        ),
        Dimension(
            key="gdhs",
            name="股东户数（全市场）",
            group="fundamental",
            origin="downloaded",
            desc="按报告期拉取的全市场股东户数变化，含户均持股市值与增减比例。",
            grain="1 行 = 1 只标的 1 个统计截止日",
            producer="tools/backfill_fundamental.py（批量类）",
            root=FUNDAMENTAL_DIR / "gdhs",
            pattern=_dir_glob(FUNDAMENTAL_DIR / "gdhs"),
            date_col="report_date",
            date_label="覆盖（报告期）",
            sample=SampleSpec(
                caption="按公告日倒序，最新的在最前",
                glob=_dir_glob(FUNDAMENTAL_DIR / "gdhs"),
                columns=(
                    "代码", "名称", "股东户数-本次", "股东户数-上次", "股东户数-增减比例",
                    "户均持股市值", "户均持股数量", "股东户数统计截止日-本次",
                    "公告日期", "notice_date",
                ),
                order_by="notice_date",
            ),
        ),
        Dimension(
            key="fund_hold",
            name="基金持仓（全市场）",
            group="fundamental",
            origin="downloaded",
            desc="东财「基金持仓」批量接口，基本面五件套之一。",
            grain="1 行 = 1 只基金 1 个报告期持有的标的",
            producer="tools/backfill_fundamental.py（批量类）",
            root=FUNDAMENTAL_DIR / "fund_hold",
            pattern=_dir_glob(FUNDAMENTAL_DIR / "fund_hold"),
            date_col="notice_date",
            date_label="覆盖（公告日）",
            sample=_batch_sample(
                "fund_hold",
                (),
                caption="按公告日倒序，最新的在最前",
            ),
            extras=(("状态", "计划内但**未落盘**，详见卡片上的原因"),),
        ),
        Dimension(
            key="fhps",
            name="分红配送（全市场）",
            group="fundamental",
            origin="downloaded",
            desc="按报告期拉取的全市场分红送转方案，含股息率与除权除息日。",
            grain="1 行 = 1 只标的 1 个分红方案",
            producer="tools/backfill_fundamental.py（批量类）",
            root=FUNDAMENTAL_DIR / "fhps",
            pattern=_dir_glob(FUNDAMENTAL_DIR / "fhps"),
            date_col="report_date",
            date_label="覆盖（报告期）",
            sample=_batch_sample(
                "fhps",
                ("代码", "名称", "送转股份-送转总比例", "现金分红-现金分红比例",
                 "现金分红-股息率", "每股收益", "每股净资产", "预案公告日",
                 "除权除息日", "方案进度", "notice_date"),
                caption="按公告日倒序，最新的在最前",
            ),
        ),
        # ---------------- 因子产物 ----------------
        Dimension(
            key="factor_daily",
            name="日度行情因子",
            group="factor",
            origin="derived",
            desc="由日线算出的 15 个因子：收益 / 动量 / 波动 / 换手 / 振幅 / RSI。",
            grain="1 行 = 1 只标的 1 个交易日",
            producer="python -m factors.calc --category daily",
            root=FACTOR_DIR / "daily",
            pattern=None,
            date_col="date",
            stats_from_manifest=True,
            sample=SampleSpec(
                caption="按日期倒序；4 个代表因子横向拼成一张宽表",
                sql=(
                    'SELECT a."date", a."symbol", a."value" AS ret_5, b."value" AS ret_10,'
                    ' c."value" AS momentum_20, d."value" AS turnover_20'
                    " FROM read_parquet('{factor_dir}/daily/ret_5.parquet') a"
                    ' JOIN read_parquet(\'{factor_dir}/daily/ret_10.parquet\') b USING ("date", "symbol")'
                    " JOIN read_parquet('{factor_dir}/daily/momentum_20.parquet') c"
                    ' USING ("date", "symbol")'
                    " JOIN read_parquet('{factor_dir}/daily/turnover_20.parquet') d"
                    ' USING ("date", "symbol")'
                    ' WHERE a."symbol" = \'sh.600519\' ORDER BY a."date" DESC'
                ),
            ),
            extras=(("产物清单", "data/factor/_manifest.json（因子状态的唯一来源）"),),
        ),
        Dimension(
            key="factor_fundamental",
            name="基本面因子",
            group="factor",
            origin="derived",
            desc=(
                "由财务数据算出的 6 个因子，**按公告日 as-of 对齐**（未公告的财报读不出来，"
                "这是防未来函数的唯一防线）。换手成本比日度因子低两个数量级。"
            ),
            grain="1 行 = 1 只标的 1 个交易日",
            producer="python -m factors.calc --category fundamental",
            root=FACTOR_DIR / "fundamental",
            pattern=None,
            date_col="date",
            stats_from_manifest=True,
            sample=SampleSpec(
                caption="按日期倒序；4 个财务因子横向拼成一张宽表",
                sql=(
                    'SELECT a."date", a."symbol", a."value" AS roe_annualized,'
                    ' b."value" AS gross_margin, c."value" AS debt_ratio, d."value" AS revenue_yoy'
                    " FROM read_parquet('{factor_dir}/fundamental/roe_annualized.parquet') a"
                    " JOIN read_parquet('{factor_dir}/fundamental/gross_margin.parquet') b"
                    ' USING ("date", "symbol")'
                    " JOIN read_parquet('{factor_dir}/fundamental/debt_ratio.parquet') c"
                    ' USING ("date", "symbol")'
                    " JOIN read_parquet('{factor_dir}/fundamental/revenue_yoy.parquet') d"
                    ' USING ("date", "symbol")'
                    ' WHERE a."symbol" = \'sh.600519\' ORDER BY a."date" DESC'
                ),
            ),
        ),
        Dimension(
            key="factor_combo",
            name="合成因子",
            group="factor",
            origin="derived",
            desc=(
                "三个互相独立维度（动量簇 / 活跃度簇 / 当日涨跌）的秩合成，两个变体："
                "同向 eq3 与反向 rev3。rev3 恒等于 −eq3（已有测试钉死）。"
            ),
            grain="1 行 = 1 只标的 1 个交易日",
            producer="python -m factors.calc --category combo",
            root=FACTOR_DIR / "combo",
            pattern=None,
            date_col="date",
            stats_from_manifest=True,
            sample=SampleSpec(
                caption="按日期倒序；两个变体横向拼成一张宽表（值是 0~1 的百分位秩）",
                sql=(
                    'SELECT a."date", a."symbol", a."value" AS combo_eq3, b."value" AS combo_rev3'
                    " FROM read_parquet('{factor_dir}/combo/combo_eq3.parquet') a"
                    " JOIN read_parquet('{factor_dir}/combo/combo_rev3.parquet') b"
                    ' USING ("date", "symbol")'
                    ' WHERE a."symbol" = \'sh.600519\' ORDER BY a."date" DESC'
                ),
            ),
        ),
        # ---------------- 回测与寻优 ----------------
        Dimension(
            key="backtest",
            name="策略回测产物",
            group="research",
            origin="derived",
            desc=(
                "每个 run 一个目录：equity.parquet（净值）+ metrics.json（指标）"
                "+ config.json（参数与口径）。"
            ),
            grain="1 行 = 1 个交易日的净值",
            producer="python -m strategies.run --strategy momentum",
            root=BACKTEST_DIR,
            pattern=_posix(BACKTEST_DIR / "*" / "*" / "equity.parquet"),
            file_pattern=_posix(BACKTEST_DIR / "*" / "*" / "*"),
            rows_scope="仅净值表（orders / trades / positions 未计入行数）",
            date_col="date",
            sample=SampleSpec(
                caption="最新一次 run 的净值曲线，按日期倒序",
                glob=_posix(BACKTEST_DIR / "*" / "*" / "equity.parquet"),
                latest=True,
                columns=(
                    "date", "nav", "total_asset", "cash", "market_value",
                    "position_count", "drawdown", "benchmark_nav",
                ),
                order_by="date",
            ),
        ),
        Dimension(
            key="sweep",
            name="参数寻优产物",
            group="research",
            origin="derived",
            desc="`grid.json`（网格定义）+ `results.parquet`（每组结果）+ `summary.md`（报告）。",
            grain="1 行 = 1 组参数组合",
            producer="python -m strategies.sweep --config sweeps/hold_tp.yaml --tag <tag>",
            root=SWEEP_DIR,
            pattern=_posix(SWEEP_DIR / "*" / "results.parquet"),
            file_pattern=_posix(SWEEP_DIR / "*" / "*"),
            rows_scope="仅 results.parquet（grid / runs / summary 未计入行数）",
            date_col=None,
            sample=SampleSpec(
                caption="最新一组网格的参数组合，按年化收益倒序",
                glob=_posix(SWEEP_DIR / "*" / "results.parquet"),
                skip_contains=("smoke",),
                latest=True,
                columns=(
                    "hold_days_max", "max_weight_per_stock", "max_positions",
                    "take_profit_gross_main", "take_profit_gross_gem",
                    "run_id", "total_return",
                    "annual_return", "max_drawdown", "sharpe", "annual_cost_ratio",
                    "trade_count", "win_rate", "avg_hold_days", "is_spike",
                ),
                order_by="annual_return",
            ),
        ),
        # ---------------- 模拟盘 ----------------
        Dimension(
            key="paper",
            name="模拟盘净值与成交",
            group="paper",
            origin="derived",
            desc="每个账户一个目录：nav / positions / trades / config。盘后由调度器落盘。",
            grain="1 行 = 1 个交易日的净值（trades 是 1 笔平仓）",
            producer="app/services/paper_service.py（调度任务 paper_daily）",
            root=PAPER_DIR,
            pattern=_posix(PAPER_DIR / "*" / "nav.parquet"),
            file_pattern=_posix(PAPER_DIR / "*" / "*"),
            rows_scope="仅净值表（positions / trades / config 未计入行数）",
            date_col="date",
            sample=SampleSpec(
                caption="默认账户的净值，按日期倒序",
                glob=_posix(PAPER_DIR / "*" / "nav.parquet"),
                latest=True,
                columns=(
                    "date", "nav", "total_asset", "cash", "market_value",
                    "position_count", "drawdown", "benchmark_nav",
                ),
                order_by="date",
            ),
        ),
        # ---------------- 元数据 ----------------
        Dimension(
            key="calendar",
            name="交易日历",
            group="meta",
            origin="downloaded",
            desc="全市场交易日（含未来日期）。一切日期运算的唯一依据。",
            grain="1 行 = 1 个自然日 + 是否交易日",
            producer="tools/sync_trade_calendar.py（baostock 同步）",
            root=TRADE_CALENDAR_PARQUET,
            pattern=_posix(TRADE_CALENDAR_PARQUET),
            date_col="date",
            sample=SampleSpec(
                caption="按日期倒序（因此取到的是日历末端的未来日期）",
                glob=_posix(TRADE_CALENDAR_PARQUET),
                columns=("date", "is_trading_day"),
                order_by="date",
            ),
        ),
        Dimension(
            key="meta_db",
            name="本地状态库（SQLite）",
            group="meta",
            origin="state",
            desc=(
                "状态与日志，**不是行情数据**：数据源调用日志 / 任务执行记录 / 回测登记 / "
                "提醒发送记录。行情与因子的真相一律在文件里，不在这个库里。"
            ),
            grain="1 行 = 1 条日志或登记记录",
            producer="app/repos/meta_repo.py",
            root=META_DB,
            pattern=None,
            date_col=None,
            stats_from_sqlite=True,
            sample=SampleSpec(
                caption="数据源调用日志，按 id 倒序",
                sqlite=True,
            ),
            extras=(("表结构", "见 sql/schema.sql"),),
        ),
        # ==========================================================
        # 同花顺特色数据（fuyao REST，tools/backfill_special.py 落盘）
        #
        # ⚠️ 涨跌停三池**必须各自一个维度**：它们的列集不同（14/9/9），
        #   而 `_scan_rows` 刻意不加 union_by_name（加了 0.9s→9.4s），
        #   同一个 glob 里混 schema 会让 DuckDB 直接报错。
        # ==========================================================
        Dimension(
            key="limit_up_pool",
            name="涨停股票池（同花顺）",
            group="special",
            origin="downloaded",
            desc=(
                "按历史交易日取涨停股池，含封单额、封板时间、连板数、涨停原因。"
                "**东财只能取最近 1~2 日，本数据可回溯到 2020-07 中旬**"
                "（更早日期接口返回空表，不是漏抓）。"
            ),
            grain="1 行 = 1 个交易日 1 只涨停股",
            producer="tools/backfill_special.py --dataset limit_pool",
            root=LIMIT_POOL_DIR,
            pattern=_posix(LIMIT_POOL_DIR / "up.parquet"),
            date_col="date",
            sample=SampleSpec(
                caption="按日期倒序，最新的在最前",
                glob=_posix(LIMIT_POOL_DIR / "up.parquet"),
                columns=(
                    "date", "thscode", "name", "last_price", "price_change_ratio_pct",
                    "limit_up_time", "continue_day_cnt", "seal_money", "limit_up_reason",
                ),
                order_by="date",
            ),
        ),
        Dimension(
            key="limit_down_pool",
            name="跌停股票池（同花顺）",
            group="special",
            origin="downloaded",
            desc="按历史交易日取跌停股池，含首次/最后跌停时间与换手率。",
            grain="1 行 = 1 个交易日 1 只跌停股",
            producer="tools/backfill_special.py --dataset limit_pool",
            root=LIMIT_POOL_DIR,
            pattern=_posix(LIMIT_POOL_DIR / "down.parquet"),
            date_col="date",
            sample=SampleSpec(
                caption="按日期倒序，最新的在最前",
                glob=_posix(LIMIT_POOL_DIR / "down.parquet"),
                columns=(
                    "date", "thscode", "name", "last_price",
                    "first_limit_time", "last_limit_time", "turnover_ratio_pct",
                ),
                order_by="date",
            ),
        ),
        Dimension(
            key="limit_break_pool",
            name="炸板股票池（同花顺）",
            group="special",
            origin="downloaded",
            desc="按历史交易日取涨停炸板股池，含开板次数与换手率。",
            grain="1 行 = 1 个交易日 1 只炸板股",
            producer="tools/backfill_special.py --dataset limit_pool",
            root=LIMIT_POOL_DIR,
            pattern=_posix(LIMIT_POOL_DIR / "break.parquet"),
            date_col="date",
            sample=SampleSpec(
                caption="按日期倒序，最新的在最前",
                glob=_posix(LIMIT_POOL_DIR / "break.parquet"),
                columns=(
                    "date", "thscode", "name", "open_times",
                    "turnover_ratio_pct", "turnover",
                ),
                order_by="date",
            ),
        ),
        Dimension(
            key="board_industry_history",
            name="行业板块指数历史（同花顺）",
            group="special",
            origin="downloaded",
            desc=(
                "同花顺行业板块指数日线，一个板块一个文件。"
                "⚠️ 各板块起始日不同（实测抽样的首个可用起点为 2021-10），"
                "且**接口在起点早于板块首日时会静默返回空表** → 回补按候选起点逐个试。"
            ),
            grain="1 行 = 1 个板块 1 个交易日",
            producer="tools/backfill_special.py --dataset board",
            root=BOARD_DIR / "industry",
            pattern=_posix(BOARD_DIR / "industry" / "*.parquet"),
            date_col="date",
            sample=SampleSpec(
                caption="固定取 881121.TI（半导体），按日期倒序",
                glob=_posix(BOARD_DIR / "industry" / "*.parquet"),
                prefer="881121.TI",
                columns=("date", "thscode", "open", "high", "low", "close", "volume", "amount"),
                order_by="date",
            ),
        ),
        Dimension(
            key="board_concept_history",
            name="概念板块指数历史（同花顺）",
            group="special",
            origin="downloaded",
            desc=(
                "同花顺概念板块指数日线，一个板块一个文件。"
                "⚠️ 概念板块的起始日差异更大（实测有到 2023 才有数据的），"
                "用本页的覆盖区间看单个板块的实际范围。"
            ),
            grain="1 行 = 1 个板块 1 个交易日",
            producer="tools/backfill_special.py --dataset board",
            root=BOARD_DIR / "concept",
            pattern=_posix(BOARD_DIR / "concept" / "*.parquet"),
            date_col="date",
            sample=SampleSpec(
                caption="固定取 886042.TI（新能源车），按日期倒序",
                glob=_posix(BOARD_DIR / "concept" / "*.parquet"),
                prefer="886042.TI",
                columns=("date", "thscode", "open", "high", "low", "close", "volume", "amount"),
                order_by="date",
            ),
        ),
        Dimension(
            key="board_catalog",
            name="同花顺板块清单（快照）",
            group="special",
            origin="downloaded",
            desc=(
                "同花顺行业+概念板块目录快照（tag/thscode/name）。"
                "**以中文名为键**与 akshare 的 `*_ths` 接口对接。"
            ),
            grain="1 行 = 1 个板块",
            producer="tools/backfill_special.py --dataset board",
            root=BOARD_CATALOG_DIR,
            pattern=_posix(BOARD_CATALOG_DIR / "boards.parquet"),
            date_col="snapshot_date",
            date_label="快照日",
            sample=SampleSpec(
                caption="板块清单，按代码升序",
                glob=_posix(BOARD_CATALOG_DIR / "boards.parquet"),
                columns=("tag", "thscode", "name", "snapshot_date"),
                order_by="thscode",
                desc=False,
            ),
        ),
        Dimension(
            key="hot_list_history",
            name="同花顺热榜历史（按自然日）",
            group="special",
            origin="downloaded",
            desc=(
                "按自然日的热股榜 Top30。"
                "⚠️ **接口只支持一年内** → 本数据最多覆盖最近一年，更早的拿不到。"
            ),
            grain="1 行 = 1 个自然日 1 个榜单名次",
            producer="tools/backfill_special.py --dataset hot_list",
            root=HOT_LIST_DIR / "history",
            pattern=_posix(HOT_LIST_DIR / "history" / "*.parquet"),
            date_col="date",
            sample=SampleSpec(
                caption="最新一天的历史榜，按名次升序",
                glob=_posix(HOT_LIST_DIR / "history" / "*.parquet"),
                latest=True,
                columns=("date", "thscode", "name", "rank"),
                order_by="rank",
                desc=False,
            ),
        ),
        Dimension(
            key="hot_list_current",
            name="同花顺热榜快照（每日）",
            group="special",
            origin="downloaded",
            desc=(
                "飙升榜 + 热股榜的每日快照（带热度值与名次变化）。"
                "⚠️ 这两个榜**只有当下、无历史接口** → 只能从接入那天起按日累积。"
            ),
            grain="1 行 = 1 个自然日 1 个榜单 1 个名次",
            producer="tools/backfill_special.py --dataset hot_list",
            root=HOT_LIST_DIR / "current",
            pattern=_posix(HOT_LIST_DIR / "current" / "*.parquet"),
            date_col="date",
            sample=SampleSpec(
                caption="最新一天的实时榜快照，按名次升序",
                glob=_posix(HOT_LIST_DIR / "current" / "*.parquet"),
                latest=True,
                columns=(
                    "date", "list_type", "period", "thscode", "name",
                    "rank", "heat", "rank_change", "rank_trend",
                ),
                order_by="rank",
                desc=False,
            ),
        ),
        Dimension(
            key="hot_rank_trend",
            name="个股热度排名走势（本地派生）",
            group="special",
            origin="derived",
            desc=(
                "由热榜历史**本地透视**得到（省掉数千次按标的的接口调用）。"
                "⚠️ 接口版 `hot_stock_rank_trend` 只支持一年窗口，所以本数据也最多一年。"
            ),
            grain="1 行 = 1 只标的 1 个自然日",
            producer="tools/backfill_special.py（由 hot_list/history 透视）",
            root=HOT_LIST_DIR / "rank_trend",
            pattern=_posix(HOT_LIST_DIR / "rank_trend" / "*.parquet"),
            date_col="date",
            sample=SampleSpec(
                caption="固定取 sh.600519 对应的 600519.SH，按日期倒序",
                glob=_posix(HOT_LIST_DIR / "rank_trend" / "*.parquet"),
                prefer="600519.SH",
                columns=("date", "thscode", "rank"),
                order_by="date",
            ),
        ),
        Dimension(
            key="dragon_tiger",
            name="龙虎榜个股表（同花顺）",
            group="special",
            origin="downloaded",
            desc=(
                "按交易日的龙虎榜个股榜，含同花顺人气排名、所属概念、机构净买、"
                "游资净买。⚠️ 接口只支持一年内。"
            ),
            grain="1 行 = 1 个交易日 1 只上榜个股",
            producer="tools/backfill_special.py --dataset dragon_tiger",
            root=DRAGON_TIGER_DIR / "stock",
            pattern=_posix(DRAGON_TIGER_DIR / "stock" / "*.parquet"),
            date_col="trade_date",
            date_label="覆盖（交易日）",
            sample=SampleSpec(
                caption="最新一个交易日的榜，按净买额倒序",
                glob=_posix(DRAGON_TIGER_DIR / "stock" / "*.parquet"),
                latest=True,
                columns=(
                    "trade_date", "thscode", "name", "hot_rank", "net_value",
                    "org_net_value", "hot_money_net_value", "limit_reason", "concept_names",
                ),
                order_by="net_value",
            ),
        ),
        Dimension(
            key="dragon_tiger_hot_money",
            name="龙虎榜游资明细（同花顺）",
            group="special",
            origin="downloaded",
            desc=(
                "龙虎榜的游资维度，已展平为 行 = 游资 × 个股。"
                "⚠️ 与个股表字段集不同（多 amount / 游资净买占比等），所以**单独一个目录**。"
            ),
            grain="1 行 = 1 个交易日 1 个游资 1 只个股",
            producer="tools/backfill_special.py --dataset dragon_tiger",
            root=DRAGON_TIGER_DIR / "hot_money",
            pattern=_posix(DRAGON_TIGER_DIR / "hot_money" / "*.parquet"),
            date_col="trade_date",
            date_label="覆盖（交易日）",
            sample=SampleSpec(
                caption="最新一个交易日的游资明细，按个股净买额倒序",
                glob=_posix(DRAGON_TIGER_DIR / "hot_money" / "*.parquet"),
                latest=True,
                columns=(
                    "trade_date", "hot_money_name", "buying", "thscode", "name",
                    "hot_money_item_net_value", "limit_reason",
                ),
                order_by="net_value",
            ),
        ),
        Dimension(
            key="anomaly",
            name="个股异动（同花顺，每日快照）",
            group="special",
            origin="downloaded",
            desc=(
                "盘中异动解读 + 关键词，按异动标签（涨停/跌停/大涨/大跌/急拉/急跌）区分。"
                "⚠️ **接口只有当日**（无日期参数）→ 无法回补历史，只能从接入日起按日累积。"
            ),
            grain="1 行 = 1 个自然日 1 条异动",
            producer="tools/backfill_special.py --dataset anomaly",
            root=ANOMALY_DIR,
            pattern=_posix(ANOMALY_DIR / "*.parquet"),
            date_col="date",
            sample=SampleSpec(
                caption="最新一天的异动，按代码排序",
                glob=_posix(ANOMALY_DIR / "*.parquet"),
                latest=True,
                columns=("date", "tag_name", "thscode", "stock_name", "analysis_content", "keywords"),
                order_by="thscode",
                desc=False,
            ),
            extras=(("状态文件", _posix(BACKFILL_SPECIAL_STATE_FILE)),),
        ),
    )


# ======================================================================
# 统计（带缓存）
# ======================================================================
_cache: dict[str, Any] = {"at": 0.0, "data": None}
_cache_lock = threading.Lock()


def clear_cache() -> None:
    """清空统计缓存（测试与"强制刷新"用）。"""
    with _cache_lock:
        _cache["at"] = 0.0
        _cache["data"] = None


def _human_size(n: int) -> str:
    x = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if x < 1024:
            return f"{x:.1f} {unit}"
        x /= 1024
    return f"{x:.1f} PB"


def _human_count(n: int) -> str:
    if n >= 100_000_000:
        return f"{n / 100_000_000:.2f} 亿"
    if n >= 10_000:
        return f"{n / 10_000:.1f} 万"
    return str(n)


def _scan_files(pattern: str) -> tuple[int, int]:
    """返回 (文件数, 字节数) —— 磁盘真相里最便宜的一半。"""
    files = [Path(p) for p in globlib.glob(pattern, recursive=True) if Path(p).is_file()]
    total = 0
    for f in files:
        try:
            total += f.stat().st_size
        except OSError:  # pragma: no cover - 文件刚被删 / 权限异常
            continue
    return len(files), total


def _scan_rows(pattern: str, date_col: str | None) -> dict[str, Any]:
    """用 DuckDB 读 parquet 元信息拿行数与时间覆盖区间（不扫数据列）。

    ⚠️ 这里**不加** `union_by_name`：加了一旦某个文件缺列，DuckDB 会走
    "合并全部 schema" 的慢路径（同一批文件实测 0.9s → 9.4s）。逐只类数据的
    列集由回补工具的白名单保证一致。
    """
    if date_col:
        sql = (
            f'SELECT count(*) AS n, min("{date_col}") AS lo, max("{date_col}") AS hi'
            f" FROM read_parquet('{pattern}')"
        )
    else:
        sql = f"SELECT count(*) AS n FROM read_parquet('{pattern}')"
    row = data_store.query_duck(sql).iloc[0]
    lo = row.get("lo") if date_col else None
    hi = row.get("hi") if date_col else None
    return {
        "rows": int(row["n"]),
        "date_start": None if lo is None or pd.isna(lo) else str(lo)[:10],
        "date_end": None if hi is None or pd.isna(hi) else str(hi)[:10],
    }


def _manifest_factors() -> dict[str, list[dict[str, Any]]]:
    """因子产物清单（按分类分组）。

    因子产物的【状态】按纪律由 `_manifest.json` 记载，**不重复扫磁盘** ——
    扫了就有两个数，两个数就会有对不上的那一天。
    """
    if not FACTOR_MANIFEST_FILE.exists():
        return {}
    try:
        raw = json.loads(FACTOR_MANIFEST_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning(f"因子产物清单不可读，因子维度按无产物处理：{exc}")
        return {}
    out: dict[str, list[dict[str, Any]]] = {}
    for item in (raw.get("factors") or {}).values():
        out.setdefault(str(item.get("category", "daily")), []).append(item)
    return out


def _stats_from_sqlite() -> dict[str, Any]:
    """本地状态库的规模：表行数合计 + 库文件大小。

    `sqlite_sequence` 是 SQLite 自增器内部表，不算业务数据，剔除。
    """
    from app.repos import db as repo_db  # noqa: PLC0415

    counts = {
        k: v for k, v in repo_db.table_row_counts().items() if not k.startswith("sqlite_")
    }
    size = repo_db.db_size_bytes()
    return {
        "rows": sum(counts.values()),
        "files": 1 if META_DB.exists() else 0,
        "bytes": size,
        "tables": counts,
    }


def _stats_from_manifest(category: str) -> dict[str, Any]:
    factors = _manifest_factors().get(category, [])
    starts = [f.get("date_start") for f in factors if f.get("date_start")]
    ends = [f.get("date_end") for f in factors if f.get("date_end")]
    d = FACTOR_DIR / category
    # `_` 开头的文件是测试残留，不是因子产物（见 docs/开发进度.md §5）
    files = [p for p in d.glob("*.parquet") if not p.name.startswith("_")] if d.exists() else []
    return {
        "rows": sum(int(f.get("rows") or 0) for f in factors),
        "date_start": min(starts) if starts else None,
        "date_end": max(ends) if ends else None,
        "files": len(files),
        "bytes": _scan_files(_dir_glob(d))[1],
        "factor_count": len(factors),
        "factor_names": [
            {"name": f.get("name"), "cname": f.get("cname")}
            for f in sorted(factors, key=lambda x: str(x.get("name")))
        ],
    }


def _fund_missing_reason(dataset: str) -> str | None:
    """从回补状态文件里现读"这个数据集为什么没落盘"（自更新，不写死）。"""
    if not BACKFILL_FUND_STATE_FILE.exists():
        return None
    try:
        raw = json.loads(BACKFILL_FUND_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    recs = (raw.get("batch") or {}).get(dataset)
    if not isinstance(recs, dict) or not recs:
        return None
    failed = [r for r in recs.values() if r.get("status") != "ok"]
    if not failed:
        return None
    sample_err = next((str(r.get("error")) for r in failed if r.get("error")), "")
    tail = f"，例如：{sample_err[:120]}" if sample_err else ""
    return f"上次回补 {len(failed)}/{len(recs)} 个报告期失败，本地无落盘文件{tail}"


def _missing_reason_suffix(dataset: str) -> str:
    """维度不可用时，把回补状态里读到的原因接在后面（读不到就返回空串）。"""
    reason = _fund_missing_reason(dataset)
    return f"；{reason}" if reason else ""


def _empty_payload(dim_key: str, name: str, group: str, path: Path, desc: str,
                   producer: str, error: str) -> dict[str, Any]:
    """不可用维度的统一形态（字段齐全，前端不必到处判空）。"""
    return {
        "key": dim_key,
        "name": name,
        "group": group,
        "origin": "downloaded",
        "origin_label": ORIGINS["downloaded"],
        "desc": desc,
        "grain": "—",
        "producer": producer,
        "path": relative_to_project(path),
        "date_label": "覆盖区间",
        "rows_scope": "",
        "extras": [],
        "available": False,
        "files": 0,
        "bytes": 0,
        "human": "0 B",
        "rows": 0,
        "date_start": None,
        "date_end": None,
        "factor_count": 0,
        "factor_names": [],
        "error": error,
    }


def _dimension_payload(dim: Dimension) -> dict[str, Any]:
    """单个维度的清单条目。**任何异常都不向外抛** —— 一个维度坏掉不该让整页空掉。"""
    base = _empty_payload(
        dim.key, dim.name, dim.group, dim.root, dim.desc, dim.producer, ""
    )
    base["origin"] = dim.origin
    base["origin_label"] = ORIGINS.get(dim.origin, dim.origin)
    base["grain"] = dim.grain
    base["date_label"] = dim.date_label
    base["rows_scope"] = dim.rows_scope
    base["extras"] = [{"label": k, "value": v} for k, v in dim.extras]
    base["error"] = None

    try:
        if dim.stats_from_sqlite:
            st = _stats_from_sqlite()
            base.update(
                rows=st["rows"], files=st["files"], bytes=st["bytes"],
                human=_human_size(st["bytes"]), available=st["files"] > 0,
            )
            base["extras"].append(
                {"label": "表", "value": f"{len(st['tables'])} 张（{', '.join(sorted(st['tables']))}）"}
            )
            if not base["available"]:
                base["error"] = "状态库还没建（跑一次 python -m tools.init_db）"
            return base

        if dim.stats_from_manifest:
            st = _stats_from_manifest(dim.root.name)
            if st["files"] == 0:
                base["error"] = "产物目录为空 —— 先跑 python -m factors.calc"
                return base
            base.update(
                rows=st["rows"], date_start=st["date_start"], date_end=st["date_end"],
                files=st["files"], bytes=st["bytes"], human=_human_size(st["bytes"]),
                factor_count=st["factor_count"], factor_names=st["factor_names"],
                available=True,
            )
            return base

        # ---- 磁盘真相：先数文件，再（可选）读元信息拿行数 ----
        if dim.pattern is None or not Path(dim.root).exists():
            base["error"] = "本地没有这个目录 / 文件"
            base["error"] += _missing_reason_suffix(dim.key)
            return base
        files, size = _scan_files(dim.file_pattern or dim.pattern)
        base.update(files=files, bytes=size, human=_human_size(size))
        if files == 0:
            base["error"] = "路径存在，但里面没有任何落盘文件"
            base["error"] += _missing_reason_suffix(dim.key)
            return base
        base.update(_scan_rows(dim.pattern, dim.date_col))
        base["available"] = True
    except AppError as exc:  # 存储层错误：如实报，不吞
        base["error"] = exc.message
    except OSError as exc:
        base["error"] = f"读取失败：{exc}"
    return base


def datasets(refresh: bool = False) -> dict[str, Any]:
    """本地数据清单：分组元信息 + 每个维度的规模 + 汇总。

    首次调用约 10 秒（要读 ~2.5 万个 parquet 的文件尾），之后走缓存。
    """
    with _cache_lock:
        fresh = (time.time() - _cache["at"]) < _TTL_SECONDS
        if _cache["data"] is not None and fresh and not refresh:
            return _cache["data"]

    t0 = time.time()
    items = [_dimension_payload(d) for d in _definitions()]

    present = [i for i in items if i["available"]]
    total_rows = sum(i["rows"] for i in present)
    total_bytes = sum(i["bytes"] for i in present)
    total_files = sum(i["files"] for i in present)

    payload = {
        "groups": [dict(g) for g in GROUPS],
        "origins": ORIGINS,
        "datasets": items,
        "summary": {
            "total": len(items),
            "available": len(present),
            "missing": len(items) - len(present),
            "files": total_files,
            "bytes": total_bytes,
            "human": _human_size(total_bytes),
            "rows": total_rows,
            "rows_human": _human_count(total_rows),
            "data_root": _posix(DATA_DIR),
        },
        # ⚠️ 刻意**不**给"整体覆盖区间"：各维度区间口径不同（行情到最新交易日、
        #    财务到最新报告期、交易日历含未来日期），合并成一个区间只会误导。
        #    区间一律看维度卡片自己的那一行。
        "elapsed_ms": int((time.time() - t0) * 1000),
        "scanned_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "ttl_seconds": _TTL_SECONDS,
    }
    logger.info(
        f"[local_data] 本地数据清单扫描完成：{len(present)}/{len(items)} 个维度就位，"
        f"{total_files} 个文件，{payload['summary']['human']}，"
        f"{payload['summary']['rows_human']} 行，耗时 {payload['elapsed_ms']}ms"
    )

    with _cache_lock:
        _cache["data"] = payload
        _cache["at"] = time.time()
    return payload


# ======================================================================
# 示例数据
# ======================================================================
def _get_dimension(key: str) -> Dimension:
    for d in _definitions():
        if d.key == key:
            return d
    raise NotFoundError(
        f"未知的本地数据维度：{key}",
        detail={"available": [d.key for d in _definitions()]},
    )


def _pick_file(spec: SampleSpec) -> Path | None:
    """按 `prefer` / `latest` 从 glob 里挑一个文件。

    `_` 开头的文件一律跳过 —— `data/factor/daily/_test_lock.parquet` 是测试残留、
    不是数据（见 docs/开发进度.md §5），混进来会让"示例"看起来像脏数据。
    """
    if not spec.glob:
        return None
    skip = spec.skip_contains
    files = [
        Path(p)
        for p in globlib.glob(spec.glob, recursive=True)
        if Path(p).is_file()
        and not Path(p).name.startswith("_")
        and not any(s in _posix(p) for s in skip)
    ]
    if not files:
        return None
    if spec.prefer:
        hit = [f for f in files if f.stem.lower() == spec.prefer.lower()]
        if hit:
            return hit[0]
    files.sort(key=lambda p: p.stem)
    return files[-1] if spec.latest else files[0]


def _render_sql(template: str) -> str:
    """把模板里的路径占位符换成实际路径。

    `{factor_dir}` → `data/factor` 的绝对 posix 路径
    `{data_dir}`   → `data` 的绝对 posix 路径
    """
    return template.replace("{factor_dir}", _posix(FACTOR_DIR)).replace(
        "{data_dir}", _posix(DATA_DIR)
    )


def _cell_text(v: Any) -> str:
    """单元格 → 展示文本。

    `date` / `report_date` / `notice_date` 这些列在 parquet 里是 datetime64，
    直接 str() 会变成 `2026-09-22 00:00:00` —— 日线数据后面那截 `00:00:00`
    纯属噪音，还占掉表格三分之一宽度。零点的时间戳统一按日期显示（不是截断，
    这些列本来就没有日内信息）。
    """
    if pd.isna(v):
        return ""
    # 注意：这里用的是 `from datetime import date, datetime` 进来的**类**（不是模块）
    if isinstance(v, (pd.Timestamp, datetime, date)):
        ts = pd.Timestamp(v)
        if ts == ts.normalize():
            return ts.strftime("%Y-%m-%d")
    return str(v)[:_MAX_CELL_CHARS]


def _frame_payload(
    df: pd.DataFrame, *, note: str = "", total_rows: int | None = None
) -> dict[str, Any]:
    """DataFrame → 前端表格能直接吃的结构。

    与 `catalog_service` 的试跑结果保持同一套约定：`rows` 是**字符串二维数组**，
    NaN → 空串。前端用列下标当 `prop`（见 CatalogView 的试跑表格）。
    """
    columns = [{"name": str(c), "dtype": str(df[c].dtype)} for c in df.columns]
    rows: list[list[str]] = [
        [_cell_text(v) for v in r.tolist()] for _, r in df.iterrows()
    ]
    return {
        "columns": columns,
        "rows": rows,
        "shown_rows": len(rows),
        "shown_columns": len(columns),
        "total_rows": int(total_rows if total_rows is not None else len(df)),
        "note": note,
    }


def _sample_sqlite(spec: SampleSpec, limit: int) -> dict[str, Any]:
    """SQLite 维度的示例。走 `app.repos.db`（SQLite 访问层），不自己开连接。"""
    from app.repos import db as repo_db  # noqa: PLC0415

    table = _SQLITE_SAMPLE_TABLE  # 表名来自白名单，绝不由外部输入拼进 SQL
    raw = repo_db.query(
        f'SELECT "id", "datasource", "func_name", "status", "rows", "duration",'
        f' "created_at" FROM "{table}" ORDER BY "id" DESC LIMIT {limit}'
    )
    payload = _frame_payload(pd.DataFrame(raw), total_rows=len(raw))
    counts = repo_db.table_row_counts()
    payload["source"] = {
        "kind": "sqlite",
        "file": relative_to_project(META_DB),
        "caption": spec.caption,
        "table": table,
        "symbol": "",
    }
    payload["note"] = (
        f"库中共 {len(counts)} 张表；此处取 `{table}`（{counts.get(table, 0):,} 行）"
        f"最新 {len(raw)} 条。表清单见下方。"
    )
    payload["tables"] = [{"name": k, "rows": v} for k, v in sorted(counts.items())]
    return payload


def _sample_with_sql(spec: SampleSpec, limit: int) -> dict[str, Any]:
    """自定义 SQL 的示例（多因子横向拼接等）。"""
    df = data_store.query_duck(_render_sql(spec.sql or "") + f" LIMIT {limit}")
    payload = _frame_payload(df)
    # 标的从**返回的数据**里取，而不是写死 —— SQL 里的过滤条件变了也不会说谎
    if "symbol" in df.columns and len(df):
        payload["symbol"] = str(df["symbol"].iloc[0])
    return payload


def _sample_parquet(dim: Dimension, spec: SampleSpec, limit: int) -> dict[str, Any]:
    """挑一个文件、取最近 N 行的示例。"""
    path = _pick_file(spec)
    if path is None:
        raise AppError(
            f"{dim.name}：本地没有可读的 parquet 文件",
            detail={"path": relative_to_project(dim.root)},
        )

    # 先拿真实列集，再决定展示哪些列 —— 表结构变了也不会炸
    head = data_store.query_duck(f"SELECT * FROM read_parquet('{_posix(path)}') LIMIT 0")
    actual = [str(c) for c in head.columns]
    want = [c for c in spec.columns if c in actual] or actual[:_MAX_SAMPLE_COLUMNS]
    if not want:  # pragma: no cover - 只有空表会走到
        raise AppError(f"{dim.name}：示例文件没有可展示的列", detail={"file": _posix(path)})

    cols = ", ".join(f'"{c}"' for c in want)
    order = (
        f' ORDER BY "{spec.order_by}" {"DESC" if spec.desc else "ASC"}'
        if spec.order_by else ""
    )
    df = data_store.query_duck(
        f"SELECT {cols} FROM read_parquet('{_posix(path)}'){order} LIMIT {limit}"
    )
    note = ""
    if len(want) < len(actual):
        note = f"该表共 {len(actual)} 列，此处展示其中 {len(want)} 列。"
    payload = _frame_payload(df, note=note)
    payload["source"] = {
        "kind": "parquet",
        "file": relative_to_project(path),
        "caption": spec.caption,
        "table": "",
        # 文件名是标的名（`sh.600519.parquet`）时一并带出来 ——
        # 前端要能明确告诉用户"这 20 行是哪只票的"，不能只给个文件名让人自己猜
        "symbol": path.stem if _SYMBOL_NAME_RE.match(path.stem) else "",
    }
    return payload


def sample(key: str, limit: int = 20) -> dict[str, Any]:
    """某个维度的示例数据（真读真表，取最近 N 行）。"""
    dim = _get_dimension(key)
    spec = dim.sample
    if spec is None:  # pragma: no cover - 定义里每个维度都配了
        raise AppError(f"{dim.name} 没有配置示例数据的取法")
    n = max(1, min(int(limit), 100))

    if spec.sqlite:
        payload = _sample_sqlite(spec, n)
    elif spec.sql is not None:
        payload = _sample_with_sql(spec, n)
        payload["source"] = {
            "kind": "parquet",
            "file": relative_to_project(dim.root),
            "caption": spec.caption,
            "table": "",
            "symbol": payload.pop("symbol", ""),
        }
        payload["note"] = "多个因子文件按 (date, symbol) 横向拼接后的一个切片。"
    else:
        payload = _sample_parquet(dim, spec, n)
        if dim.key == "universe":
            day = Path(payload["source"]["file"]).stem
            payload["source"]["caption"] = f"{spec.caption}（快照日 {day}）"

    payload["key"] = dim.key
    payload["dataset"] = dim.name
    payload["grain"] = dim.grain
    return payload


def analysis_products() -> list[dict[str, Any]]:
    """因子有效性分析产物清单（离线算、前端只读）。"""
    if not FACTOR_ANALYSIS_DIR.exists():
        return []
    return [
        {"name": p.stem, "file": relative_to_project(p), "bytes": p.stat().st_size}
        for p in sorted(FACTOR_ANALYSIS_DIR.glob("*.json"))
    ]
