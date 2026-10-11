"""全市场基本面/财务数据回补（核心套装，2026-09-20 拍板）。

为什么必须有它
--------------
因子开发要的财务/估值数据**只能来自本地**（业务层不联网的纪律不变）。
本轮拉「核心套装」：**逐只 4 个**（财务指标 + 三大报表）+ **全市场批量 5 个**
（业绩报表/业绩预告/股东户数/分红送配/基金持仓）。

耗时实测（2026-09-20 试点）：三大报表接口是**分页拉取**（20~25 页/次），
单次调用 ~5-6s，不是"1s 限流"能覆盖的节奏。全市场 4792 只：
    单通道串行 ≈ 28h ❌ → **按数据集分 4 通道并行**（2026-09-20 主人拍板）：
    每通道独立客户端、各自 1s 单次间隔；由于单次传输就要 5s+，
    聚合请求发起率 ≈ 0.7 次/秒，**不高于**原单通道计划的 1 次/秒 ——
    对东财的总压力不变，只是把空闲等待利用了起来。总耗时 ≈ 最慢通道 ≈ 9h。
    财务指标通道 ~3.5h 先完成退出。

设计要点（与 tools/backfill_quotes.py 同一先例）
----------------
1. **断点续传**：状态写 `data/meta/backfill_fundamental_state.json`，
   粒度 = {symbol: {dataset: rec}}（逐只）/ {dataset: {报告期: rec}}（批量），
   每 25 只落一次盘（全局锁内原子替换）。中断重跑即续上。
2. **单只失败不中断整批**：失败只记 error 继续；限频熔断时
   `wait_if_limited` 等恢复再试一次，仍失败才记 failed。
3. **落盘只走 data_store**：`save_fundamental()`（逐只按 [report_date, notice_date]
   去重，批量按 [symbol, report_date] 去重），重复跑安全。
4. **公告日是第一公民**：NOTICE_DATE 缺失时保留 NaT（读取侧回退
   report_date+90 天的保守口径）。
5. **列白名单**：东财报表 141~319 列，下游因子只用得上一小半；
   白名单在本文件 `WHITELISTS`（唯一维护点），未识别列丢弃并计数留痕。

用法
----
    cd backend
    python -m tools.backfill_fundamental --status                  # 只看进度（不联网）
    python -m tools.backfill_fundamental --symbols sh.600519,sz.000001   # 试点（不写状态）
    python -m tools.backfill_fundamental --batch                   # 只跑批量（分钟级）
    python -m tools.backfill_fundamental                           # 全量续传（批量+4 通道并行）
    python -m tools.backfill_fundamental --retry-failed            # 只重试失败的
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import zlib
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings
from app.core.errors import AppError, DataSourceRateLimited
from app.core.logging import logger, setup_logging
from app.core.paths import (
    BACKFILL_FUND_STATE_FILE,
    FUNDAMENTAL_DIR,
    QUOTE_DAILY_DIR,
    ensure_dirs,
)
from app.services import data_store
from app.services.data_source.akshare_client import AkshareClient
from app.services.data_source.symbols import is_target_board_symbol, normalize, to_akshare

#: 每通道每处理多少只落一次盘（状态写入在全局锁内原子替换，代价很低）
SAVE_EVERY = 25
#: 每通道每处理多少只打一行进度
PROGRESS_EVERY = 25

#: 报告期扫描起点（行情自 2020-01 起，2018 起的报告期足够做 as-of 对齐）
REPORT_PERIOD_START_YEAR = 2018


# ======================================================================
# 数据集定义（列白名单的唯一维护点）
# ======================================================================
#: 逐只数据集：dataset 名 -> (akshare 函数, 代码格式, 固定参数)
#: 代码格式与 config/catalog/fundamental.yaml 的 symbol_format 一致
STOCK_DATASETS: dict[str, dict[str, Any]] = {
    "financial_indicator": {
        "func": "stock_financial_analysis_indicator_em",
        "fmt": "suffix",       # 600519.SH
        "params": {"indicator": "按报告期"},
    },
    "balance_sheet": {"func": "stock_balance_sheet_by_report_em", "fmt": "prefix"},   # SH600519
    "profit_sheet": {"func": "stock_profit_sheet_by_report_em", "fmt": "prefix"},
    "cashflow_sheet": {"func": "stock_cash_flow_sheet_by_report_em", "fmt": "prefix"},
}

#: 全市场批量数据集：dataset 名 -> (akshare 函数, 报告期入参名, 固定参数)
BATCH_DATASETS: dict[str, dict[str, Any]] = {
    "yjbb": {"func": "stock_yjbb_em", "period_arg": "date", "params": {}},
    "yjyg": {"func": "stock_yjyg_em", "period_arg": "date", "params": {}},
    "gdhs": {"func": "stock_zh_a_gdhs", "period_arg": "symbol", "params": {}},
    "fhps": {"func": "stock_fhps_em", "period_arg": "date", "params": {}},
    "fund_hold": {
        "func": "stock_report_fund_hold",
        "period_arg": "date",
        "params": {"symbol": "基金持仓"},
        # ⚠️ 2026-09-20 实测禁用：akshare 对东财改版后的页面解析错位 ——
        #    "股票代码"列装的是持股变动数值（float64），真代码跑到"股票简称"列，
        #    其余列同样对不上号。数据不可用，跳过并留痕，待上游修复后移除本标记。
        "disabled": "上游 akshare 列名错位（代码列装的是变动数值），2026-09-20 实测",
    },
}

#: 批量数据集的代码列 / 公告日列（各接口列名不统一，逐一实测核对）
_BATCH_CODE_COLS = {
    "yjbb": "股票代码", "yjyg": "股票代码", "gdhs": "代码", "fhps": "代码", "fund_hold": "股票代码",
}
_BATCH_NOTICE_COLS = {
    "yjbb": "最新公告日期", "yjyg": "公告日期", "gdhs": "公告日期", "fhps": "预案公告日", "fund_hold": None,
}

#: 逐只数据集的通用标识列（三家报表/财务指标共用；列名以 2026-09-20 试点实测为准，
#: 白名单多写不存在的列会被 `_apply_whitelist` 静默跳过，安全）。
_BASE_ID_COLS = [
    "SECUCODE", "SECURITY_CODE", "SECURITY_NAME_ABBR", "ORG_CODE", "ORG_TYPE",
    "REPORT_TYPE", "REPORT_DATE_NAME", "SECURITY_TYPE_CODE", "UPDATE_DATE",
    "CURRENCY", "OPINION_TYPE", "OSOPINION_TYPE", "LISTING_STATE",
]

#: 资产负债表核心科目（一般企业会计准则 + 少数普适科目；银行/保险专有架构列丢弃）
_BALANCE_COLS = [*_BASE_ID_COLS,
    # 资产
    "MONETARYFUNDS", "TRADE_FINASSET", "NOTE_ACCOUNTS_RECE", "NOTE_RECE",
    "ACCOUNTS_RECE", "FINANCE_RECE", "ADVANCE_RECEIVABLES", "OTHER_RECE",
    "INVENTORY", "CONTRACT_ASSET", "PREPAYMENT", "OTHER_CURRENT_ASSET",
    "TOTAL_CURRENT_ASSETS", "LONG_EQUITY_INVEST", "OTHER_EQUITY_INVEST",
    "INVEST_REALESTATE", "FIXED_ASSET", "FIXED_ASSET_DISPOSAL", "CIP",
    "PROJECT_MATERIAL", "CONSUMPTIVE_BIOLOGICAL_ASSET", "PRODUCTIVE_BIOLOGY_ASSET",
    "OIL_GAS_ASSET", "USERIGHT_ASSET", "INTANGIBLE_ASSET", "DEVELOP_EXPENSE",
    "GOODWILL", "LONG_PREPAID_EXPENSE", "DEFER_TAX_ASSET", "HOLDSALE_ASSET",
    "OTHER_NONCURRENT_ASSET", "TOTAL_NONCURRENT_ASSETS", "ASSET_BALANCE", "TOTAL_ASSETS",
    # 负债
    "SHORT_LOAN", "TRADE_FINLIAB", "NOTE_ACCOUNTS_PAYABLE", "NOTE_PAYABLE",
    "ACCOUNTS_PAYABLE", "CONTRACT_LIAB", "STAFF_SALARY_PAYABLE", "ACCRUED_EXPENSE",
    "DIVIDEND_PAYABLE", "INTEREST_PAYABLE", "TAX_PAYABLE", "OTHER_PAYABLE",
    "PREDICT_LIAB", "HOLDSALE_LIAB", "OTHER_CURRENT_LIAB", "TOTAL_CURRENT_LIAB",
    "LONG_LOAN", "BOND_PAYABLE", "SHORT_BOND_PAYABLE", "LEASE_LIAB", "LONG_PAYABLE",
    "SPECIAL_PAYABLE", "DEFER_INCOME", "DEFER_INCOME_1YEAR", "DEFER_TAX_LIAB",
    "NONCURRENT_LIAB_1YEAR", "OTHER_NONCURRENT_LIAB", "TOTAL_NONCURRENT_LIAB",
    "LIAB_BALANCE", "TOTAL_LIABILITIES",
    # 权益
    "SHARE_CAPITAL", "OTHER_EQUITY_TOOL", "PREFERRED_SHARES", "PERPETUAL_BOND",
    "CAPITAL_RESERVE", "TREASURY_SHARES", "OTHER_COMPRE_INCOME", "CONVERT_DIFF",
    "SPECIAL_RESERVE", "SURPLUS_RESERVE", "GENERAL_RISK_RESERVE", "UNASSIGN_RPOFIT",
    "PARENT_EQUITY_BALANCE", "MINORITY_EQUITY", "EQUITY_BALANCE",
    "TOTAL_PARENT_EQUITY", "TOTAL_EQUITY", "TOTAL_LIAB_EQUITY",
]

#: 利润表核心科目（金融/保险收入成本专用列、OCI 长尾丢弃；扣非归母/少数股东损益必留）
_PROFIT_COLS = [*_BASE_ID_COLS,
    "TOTAL_OPERATE_INCOME", "OPERATE_INCOME", "OTHER_BUSINESS_INCOME",
    "TOTAL_OPERATE_COST", "OPERATE_COST", "RESEARCH_EXPENSE", "OTHER_BUSINESS_COST",
    "OPERATE_TAX_ADD", "SALE_EXPENSE", "MANAGE_EXPENSE", "ME_RESEARCH_EXPENSE",
    "FINANCE_EXPENSE", "FE_INTEREST_EXPENSE", "FE_INTEREST_INCOME",
    "ASSET_IMPAIRMENT_LOSS", "CREDIT_IMPAIRMENT_LOSS",
    "FAIRVALUE_CHANGE_INCOME", "INVEST_INCOME", "INVEST_JOINT_INCOME",
    "EXCHANGE_INCOME", "ASSET_DISPOSAL_INCOME", "OTHER_INCOME",
    "OPERATE_PROFIT", "NONBUSINESS_INCOME", "NONCURRENT_DISPOSAL_INCOME",
    "NONBUSINESS_EXPENSE", "NONCURRENT_DISPOSAL_LOSS", "TOTAL_PROFIT",
    "INCOME_TAX", "NETPROFIT", "PARENT_NETPROFIT", "MINORITY_INTEREST",
    "DEDUCT_PARENT_NETPROFIT", "BASIC_EPS", "DILUTED_EPS", "UNCONFIRM_INVEST_LOSS",
]

#: 现金流量表核心科目（间接法附注保留 —— 折旧摊销是 EBITDA/盈利质量因子的原料）
_CASHFLOW_COLS = [*_BASE_ID_COLS,
    # 经营活动
    "SALES_SERVICES", "RECEIVE_TAX_REFUND", "RECEIVE_OTHER_OPERATE",
    "OPERATE_INFLOW_OTHER", "OPERATE_INFLOW_BALANCE", "TOTAL_OPERATE_INFLOW",
    "BUY_SERVICES", "PAY_STAFF_CASH", "PAY_ALL_TAX", "PAY_OTHER_OPERATE",
    "OPERATE_OUTFLOW_OTHER", "OPERATE_OUTFLOW_BALANCE", "TOTAL_OPERATE_OUTFLOW",
    "OPERATE_NETCASH_OTHER", "OPERATE_NETCASH_BALANCE", "NETCASH_OPERATE",
    # 投资活动（CONSTRUCT_LONG_ASSET = 购建固定资产无形资产付现，CAPEX 因子核心）
    "WITHDRAW_INVEST", "RECEIVE_INVEST_INCOME", "DISPOSAL_LONG_ASSET",
    "DISPOSAL_SUBSIDIARY_OTHER", "RECEIVE_OTHER_INVEST", "INVEST_INFLOW_OTHER",
    "INVEST_INFLOW_BALANCE", "TOTAL_INVEST_INFLOW", "CONSTRUCT_LONG_ASSET",
    "INVEST_PAY_CASH", "PAY_OTHER_INVEST", "INVEST_OUTFLOW_OTHER",
    "INVEST_OUTFLOW_BALANCE", "TOTAL_INVEST_OUTFLOW", "INVEST_NETCASH_OTHER",
    "INVEST_NETCASH_BALANCE", "NETCASH_INVEST",
    # 筹资活动
    "ACCEPT_INVEST_CASH", "SUBSIDIARY_ACCEPT_INVEST", "RECEIVE_LOAN_CASH",
    "ISSUE_BOND", "RECEIVE_OTHER_FINANCE", "FINANCE_INFLOW_OTHER",
    "FINANCE_INFLOW_BALANCE", "TOTAL_FINANCE_INFLOW", "PAY_DEBT_CASH",
    "ASSIGN_DIVIDEND_PORFIT", "SUBSIDIARY_PAY_DIVIDEND", "PAY_OTHER_FINANCE",
    "FINANCE_OUTFLOW_OTHER", "FINANCE_OUTFLOW_BALANCE", "TOTAL_FINANCE_OUTFLOW",
    "FINANCE_NETCASH_OTHER", "FINANCE_NETCASH_BALANCE", "NETCASH_FINANCE",
    # 汇率与现金净变动
    "RATE_CHANGE_EFFECT", "CCE_ADD_OTHER", "CCE_ADD_BALANCE", "CCE_ADD",
    "BEGIN_CCE", "END_CCE_OTHER", "END_CCE_BALANCE", "END_CCE",
    # 间接法附注
    "NETPROFIT", "ASSET_IMPAIRMENT", "FA_IR_DEPR", "IR_DEPR", "IA_AMORTIZE",
    "LPE_AMORTIZE", "DEFER_INCOME_AMORTIZE", "PREPAID_EXPENSE_REDUCE",
    "ACCRUED_EXPENSE_ADD", "DISPOSAL_LONGASSET_LOSS", "FA_SCRAP_LOSS",
    "FAIRVALUE_CHANGE_LOSS", "FINANCE_EXPENSE", "INVEST_LOSS", "DEFER_TAX",
    "INVENTORY_REDUCE", "OPERATE_RECE_REDUCE", "OPERATE_PAYABLE_ADD", "OTHER",
    "USERIGHT_ASSET_AMORTIZE", "MINORITY_INTEREST",
]

#: 财务指标（东财编码列名）。银行/保险/券商专用列与无法确认语义的编码列
#: （PER_*/AVG_*/SS_*/NCO_OP/NCO_FIXED/FC_LIABILITIES 等）丢弃。
_FINANCIAL_INDICATOR_COLS = [*_BASE_ID_COLS,
    # 每股
    "EPSJB", "EPSKCJB", "EPSXS", "BPS", "MGZBGJ", "MGWFPLR", "MGJYXJJE",
    # 规模
    "TOTALOPERATEREVE", "MLR", "PARENTNETPROFIT", "KCFJCXSYJLR",
    # 增速（累计同比 / 单季同比环比 / 毛利率同比变动）
    "TOTALOPERATEREVETZ", "PARENTNETPROFITTZ", "KCFJCXSYJLRTZ",
    "EPSJBTZ", "BPSTZ", "ROEJQTZ", "ZZCJLLTZ", "ZCFZLTZ", "XSMLL_TB",
    "DJD_TOI_YOY", "DJD_DPNP_YOY", "DJD_DEDUCTDPNP_YOY",
    "DJD_TOI_QOQ", "DJD_DPNP_QOQ", "DJD_DEDUCTDPNP_QOQ", "REPORT_YEAR",
    # 盈利
    "ROEJQ", "ROEKCJQ", "ZZCJLL", "XSJLL", "XSMLL", "ROIC", "ROICTZ",
    # 现金流质量
    "JYXJLYYSR", "XSJXLYYSR", "YSZKYYSR", "XJLLB",
    # 偿债 / 资本结构
    "LD", "SD", "ZCFZL", "QYCS", "CQBL", "CASH_RATIO",
    "INTEREST_COVERAGE_RATIO", "GUARD_SPEED_RATIO", "LIQUIDATION_RATIO",
    "INTEREST_DEBT_RATIO", "CA_TA", "NCA_TA", "TAXRATE",
    # 营运效率
    "TOAZZL", "CHZZL", "YSZKZZL", "ZZCZZTS", "CHZZTS", "YSZKZZTS",
    "OPERATE_CYCLE", "CURRENT_ASSET_TR", "FIXED_ASSET_TR", "ACCOUNTS_PAYABLE_TR",
    "PREPAID_ACCOUNTS_RATIO", "PREPAID_ACCOUNTS_TDAYS", "PAYABLE_TDAYS",
    # 其他
    "STAFF_NUM", "FCFF_FORWARD", "FCFF_BACK", "NCO_NETPROFIT",
]

#: ★ 列白名单（唯一维护点）。None = 保留全部列。
#: 批量 5 个接口列数少（9~19 列）且信息密度高，全保留：
#: yjbb/yjyg 已按全列落盘（schema 已定），gdhs/fhps/fund_hold 未逐列实测，None 最稳。
#: 通用三列 symbol/report_date/notice_date 由标准化步骤生成，`_apply_whitelist`
#: 会无条件保留它们，不用写在这里。
WHITELISTS: dict[str, list[str] | None] = {
    "financial_indicator": list(_FINANCIAL_INDICATOR_COLS),
    "balance_sheet": list(_BALANCE_COLS),
    "profit_sheet": list(_PROFIT_COLS),
    "cashflow_sheet": list(_CASHFLOW_COLS),
    "yjbb": None,
    "yjyg": None,
    "gdhs": None,
    "fhps": None,
    "fund_hold": None,
}

#: 三大报表东财同时返回科目值和 *_YOY 同比双份，程序化展开（不存在的列自动跳过）
for _ds in ("balance_sheet", "profit_sheet", "cashflow_sheet"):
    _wl = WHITELISTS[_ds]
    WHITELISTS[_ds] = _wl + [c + "_YOY" for c in _wl]

#: 公告日的常见列名（逐只接口用，按出现顺序取第一个命中的）
_NOTICE_DATE_CANDIDATES = ("NOTICE_DATE", "NOTICE_DATEFIRST")


# ======================================================================
# 专用客户端：窗口放宽到 60/min（默认 30/min 会拖慢已耗费 5s+ 的调用）
#   单次间隔仍 1s。注意：AkshareClient.__init__ 会用实例属性遮蔽类属性，
#   所以必须在 super().__init__() **之前**设置（与原类的写法一致）。
#   ★ 每个数据集通道一个实例 —— 限流器/熔断器/缓存互不干扰。
# ======================================================================
class _BatchAkshareClient(AkshareClient):
    """基本面批量回补专用：60 次/分钟窗口。"""

    def __init__(self) -> None:
        ak_cfg = settings.datasource.akshare
        self._min_interval = 1.0
        self._max_calls_per_minute = 60
        self._circuit_break_seconds = ak_cfg.circuit_break_seconds
        self._cache_ttl = float(ak_cfg.cache_ttl)
        super().__init__()
        self._spot_cache_ttl = float(ak_cfg.spot_cache_ttl)


# ======================================================================
# 状态文件（断点续传的载体）
# ======================================================================
def load_state() -> dict[str, Any]:
    if not BACKFILL_FUND_STATE_FILE.exists():
        return {"version": 1, "stocks": {}, "batch": {}, "started_at": None}
    try:
        data = json.loads(BACKFILL_FUND_STATE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logger.warning(f"状态文件损坏，将重建：{BACKFILL_FUND_STATE_FILE}")
        return {"version": 1, "stocks": {}, "batch": {}, "started_at": None}
    data.setdefault("stocks", {})
    data.setdefault("batch", {})
    return data


def save_state(state: dict[str, Any], lock: threading.Lock | None = None) -> None:
    """原子写：先写临时文件再 replace，避免断电留下半截 JSON。

    多通道并行时必须传共享锁 —— 两个线程同时 replace 同名 tmp 会互踩。
    """

    def _write() -> None:
        BACKFILL_FUND_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = BACKFILL_FUND_STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(BACKFILL_FUND_STATE_FILE)

    if lock is not None:
        with lock:
            _write()
    else:
        _write()


# ======================================================================
# 标的清单与报告期
# ======================================================================
def discover_symbols() -> list[str]:
    """标的清单 = 本地日线已回补的全部个股（quote_daily 文件名）。

    与行情池严格对齐：行情里有的才拉财务；退市股只要行情拉过就覆盖。
    """
    symbols = sorted(p.stem for p in QUOTE_DAILY_DIR.glob("*.parquet"))
    targets = [s for s in symbols if is_target_board_symbol(s)]
    dropped = len(symbols) - len(targets)
    if dropped:
        logger.warning(f"quote_daily 里有 {dropped} 个非目标板块文件已跳过（指数/基金等）")
    if not targets:
        raise AppError(
            "行情为空：data/raw/quote_daily/ 下没有任何日线文件。"
            "请先执行：python -m tools.backfill_quotes"
        )
    return targets


#: 批量数据集的**法定披露截止日**（月-日）；0331/1231 落在次年。
#: ★ 2026-10-11 修复：原 `report_periods` 用「报告期结束 ≤ 今天」判定，
#:   会把**刚结束、披露远未完成**的报告期纳入（如 9/30 报告期，法定披露
#:   截止 10/31，此刻全市场只有百来家公司披露）。接口只返回已披露的那一小部分，
#:   被记为 ok；而批量通道 `--force` 无效 + "ok 即跳过"没有自愈路径 ——
#:   残缺截面被**永久锁死**，且随时间变成"历史"、无任何告警。
_DISCLOSURE_DUE: dict[str, tuple[int, int]] = {
    "0331": (4, 30),   # 一季报：次年 4 月底
    "0630": (8, 31),   # 半年报：同年 8 月底
    "0930": (10, 31),  # 三季报：同年 10 月底
    "1231": (4, 30),   # 年报：次年 4 月底
}
#: 截止后再等几天，避免"最后一天披露"的边界（预留缓冲，非精确要求）
_DISCLOSURE_BUFFER_DAYS = 7


def report_periods() -> list[str]:
    """批量数据集要扫的报告期（YYYYMMDD，季度端点）。

    只返回**披露已完毕**的报告期（含缓冲）—— 刚结束的季度要等法定截止日
    过后才扫，否则拿到的是残缺截面（见 `_DISCLOSURE_DUE` 的事故注释）。
    """
    today = date.today()
    out: list[str] = []
    for year in range(REPORT_PERIOD_START_YEAR, today.year + 1):
        for md, (due_month, due_day) in _DISCLOSURE_DUE.items():
            due_year = year + 1 if md in ("0331", "1231") else year
            due = date(due_year, due_month, due_day) + timedelta(
                days=_DISCLOSURE_BUFFER_DAYS
            )
            if due <= today:
                out.append(f"{year}{md}")
    return sorted(out)


# ======================================================================
# 标准化：原始 DataFrame -> [symbol, report_date, notice_date, 白名单列]
# ======================================================================
def _pick_notice_date(df: pd.DataFrame) -> pd.Series:
    for col in _NOTICE_DATE_CANDIDATES:
        if col in df.columns:
            return pd.to_datetime(df[col], errors="coerce")
    return pd.Series(pd.NaT, index=df.index, dtype="datetime64[ns]")


#: 标准化生成的通用三列，白名单裁剪时无条件保留（否则下游去重/落盘直接崩）
_BASE_KEEP_COLS = ("symbol", "report_date", "notice_date")


def _apply_whitelist(df: pd.DataFrame, dataset: str) -> tuple[pd.DataFrame, int]:
    """按白名单裁列。返回 (裁剪后的表, 丢弃列数)。

    通用三列 symbol/report_date/notice_date 始终保留；白名单里写了但数据里
    没有的列静默跳过（列名以试点实测为准，多写安全）。
    """
    wl = WHITELISTS.get(dataset)
    if wl is None:
        return df, 0
    keep = [c for c in _BASE_KEEP_COLS if c in df.columns]
    keep += [c for c in wl if c in df.columns and c not in _BASE_KEEP_COLS]
    dropped = len([c for c in df.columns if c not in keep])
    return df[keep], dropped


def standardize_stock_df(raw: pd.DataFrame, symbol: str, dataset: str) -> tuple[pd.DataFrame, int]:
    """逐只接口的返回标准化。"""
    if raw is None or raw.empty:
        return pd.DataFrame(), 0
    df = raw.copy()
    notice = _pick_notice_date(df)
    report = pd.to_datetime(df.get("REPORT_DATE"), errors="coerce")
    df["symbol"] = symbol
    df["report_date"] = report
    df["notice_date"] = notice
    df = df.dropna(subset=["report_date"])
    df, dropped = _apply_whitelist(df, dataset)
    return df.reset_index(drop=True), dropped


def standardize_batch_df(raw: pd.DataFrame, dataset: str, period: str) -> tuple[pd.DataFrame, int]:
    """批量接口的返回标准化（一次 = 一个报告期的全市场）。"""
    if raw is None or raw.empty:
        return pd.DataFrame(), 0
    df = raw.copy()
    code_col = _BATCH_CODE_COLS[dataset]
    if code_col not in df.columns:
        raise AppError(f"{dataset} 返回里找不到代码列 {code_col!r}（实际列：{list(df.columns)[:12]}…）")
    df["symbol"] = df[code_col].map(lambda c: normalize(str(c)))
    # ★ 与逐只通道统一股票池口径（2026-10-11）：批量接口含北交所，实测 yjbb
    #   20260630 的 11450 行里 bj.* 有 6135 个（54%），而逐只通道用
    #   `discover_symbols()` + is_target_board_symbol 早已把北交所滤掉 ——
    #   两套口径并存会让人误以为"目标池有 1.15 万只"，将来因子接 yjbb 会大面积取不到值。
    before = len(df)
    df = df[df["symbol"].map(is_target_board_symbol)]
    dropped_n = before - len(df)
    if dropped_n:
        logger.info(
            f"[{dataset} {period}] 剔除非目标板块 {dropped_n} 行"
            f"（批量接口含北交所等，逐只通道不采）"
        )
    df["report_date"] = pd.to_datetime(period, format="%Y%m%d", errors="coerce")
    notice_col = _BATCH_NOTICE_COLS[dataset]
    df["notice_date"] = (
        pd.to_datetime(df[notice_col], errors="coerce")
        if notice_col and notice_col in df.columns
        else pd.Series(pd.NaT, index=df.index, dtype="datetime64[ns]")
    )
    df, dropped = _apply_whitelist(df, dataset)
    return df.reset_index(drop=True), dropped


# ======================================================================
# 逐只回补（每数据集一个通道线程）
# ======================================================================
def _fetch_stock_dataset(client: AkshareClient, dataset: str, symbol: str) -> pd.DataFrame:
    spec = STOCK_DATASETS[dataset]
    code = to_akshare(symbol, spec["fmt"])
    params = {**spec.get("params", {}), "symbol": code}
    return client.call(spec["func"], params)


def _run_dataset_pass(
    client: AkshareClient,
    dataset: str,
    symbols: list[str],
    state: dict[str, Any],
    lock: threading.Lock,
    stop: threading.Event,
    *,
    pilot: bool,
) -> None:
    """一个数据集通道：顺序拉完 symbols 里属于自己的待办。"""
    t_start = time.perf_counter()
    for i, symbol in enumerate(symbols, 1):
        if stop.is_set():
            return
        t0 = time.perf_counter()
        try:
            try:
                raw = _fetch_stock_dataset(client, dataset, symbol)
            except DataSourceRateLimited:
                # 熔断/限频：等恢复后再试一次（批量任务的正确动作是等，不是逃）
                client.wait_if_limited(max_wait=600.0)
                raw = _fetch_stock_dataset(client, dataset, symbol)

            df, dropped = standardize_stock_df(raw, symbol, dataset)
            status: dict[str, Any]
            if df.empty:
                status = {"status": "empty", "rows": 0, "error": ""}
            else:
                if dropped:
                    logger.debug(f"[{dataset}] {symbol} 白名单丢弃 {dropped} 列")
                data_store.save_fundamental(df, dataset, symbol=symbol)
                status = {
                    "status": "ok",
                    "rows": len(df),
                    "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
                    "error": "",
                }
        except Exception as exc:
            logger.error(f"[{dataset}] {symbol} 回补失败：{type(exc).__name__}: {str(exc)[:300]}")
            status = {"status": "failed", "error": f"{type(exc).__name__}: {str(exc)[:200]}"}

        if not pilot:
            with lock:
                rec = state["stocks"].setdefault(symbol, {})
                rec[dataset] = {**status, "updated_at": datetime.now().isoformat(timespec="seconds")}
                if i % SAVE_EVERY == 0:
                    save_state(state, lock=None)  # 已在锁内
        if i % PROGRESS_EVERY == 0 or i == len(symbols):
            spent = time.perf_counter() - t_start
            speed = i / spent if spent > 0 else 0
            eta = (len(symbols) - i) / speed / 60 if speed > 0 else 0
            logger.info(
                f"[{dataset}] {i}/{len(symbols)}（{i / len(symbols) * 100:.1f}%）｜ "
                f"{speed:.2f} 只/秒 用时 {spent / 60:.1f}min 预计还需 {eta:.0f}min"
            )


# ======================================================================
# 批量回补（单线程，分钟级）
# ======================================================================
def _do_batch(client: AkshareClient, state: dict[str, Any], *, retry_failed: bool) -> None:
    periods = report_periods()
    for dataset, spec in BATCH_DATASETS.items():
        if spec.get("disabled"):
            logger.warning(f"批量数据集 {dataset} 跳过：{spec['disabled']}")
            # ★ 把残留的 failed 记录改判 disabled（2026-10-11）：否则它们会被
            #   --retry-failed 反复重试、并计入失败总数 → 退出码恒为 1，
            #   每周例行告警全是"已知不会修的数据集失败"的噪音。
            bucket_d = state["batch"].setdefault(dataset, {})
            n_marked = 0
            for _period, prev in bucket_d.items():
                if prev.get("status") == "failed":
                    prev["status"] = "disabled"
                    prev["error"] = str(spec["disabled"])[:200]
                    n_marked += 1
            if n_marked:
                logger.info(f"[{dataset}] {n_marked} 期 failed 记录改判为 disabled")
                save_state(state)
            continue
        bucket = state["batch"].setdefault(dataset, {})
        for period in periods:
            prev = bucket.get(period) or {}
            st = prev.get("status")
            if retry_failed:
                if st != "failed":
                    continue
            elif st in ("ok", "empty"):
                continue
            try:
                try:
                    raw = client.call(spec["func"], {**spec["params"], spec["period_arg"]: period})
                except DataSourceRateLimited:
                    client.wait_if_limited(max_wait=600.0)
                    raw = client.call(spec["func"], {**spec["params"], spec["period_arg"]: period})
                df, _ = standardize_batch_df(raw, dataset, period)
                if df.empty:
                    bucket[period] = {"status": "empty", "rows": 0, "error": ""}
                else:
                    data_store.save_fundamental(df, dataset, symbol=None)
                    bucket[period] = {"status": "ok", "rows": len(df), "error": ""}
            except Exception as exc:
                logger.error(f"[{dataset}] {period} 批量回补失败：{type(exc).__name__}: {str(exc)[:200]}")
                bucket[period] = {
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                }
            save_state(state)


# ======================================================================
# 进度展示
# ======================================================================
def _stable_hash(s: str) -> int:
    """跨进程稳定的字符串哈希（分片用）。

    ★ **不能用内置 `hash()`** —— Python 对 str 的哈希默认带随机种子
      （PYTHONHASHSEED），同一天两次运行会得到不同分片，续传直接失效。
    """
    return zlib.crc32(s.encode("utf-8")) & 0xFFFFFFFF


def _print_status(state: dict[str, Any]) -> None:
    stocks = state.get("stocks") or {}
    per_dataset: dict[str, dict[str, int]] = {}
    for rec in stocks.values():
        for ds, info in rec.items():
            if ds.startswith("_") or not isinstance(info, dict):
                continue
            st = info.get("status", "pending")
            per_dataset.setdefault(ds, {}).setdefault(st, 0)
            per_dataset[ds][st] = per_dataset[ds].get(st, 0) + 1

    print()
    print("=" * 62)
    print("  基本面数据回补 · 进度")
    print("=" * 62)
    print(f"  已处理      : {len(stocks)} 只（状态文件条目数，非清单总量）")
    for ds in STOCK_DATASETS:
        b = per_dataset.get(ds, {})
        line = "  ".join(f"{k}={v}" for k, v in sorted(b.items())) or "（未开始）"
        print(f"  {ds:20s}: {line}")
    print("  —— 批量 ——")
    for ds, spec in BATCH_DATASETS.items():
        if spec.get("disabled"):
            print(f"  {ds:20s}: ⛔ 已禁用（{spec['disabled']}）")
            continue
        b = (state.get("batch") or {}).get(ds) or {}
        agg: dict[str, int] = {}
        for info in b.values():
            agg[info.get("status", "pending")] = agg.get(info.get("status", "pending"), 0) + 1
        line = "  ".join(f"{k}={v}" for k, v in sorted(agg.items())) or "（未开始）"
        print(f"  {ds:20s}: {line}")
    print(f"  数据目录    : {FUNDAMENTAL_DIR}")
    print(f"  状态文件    : {BACKFILL_FUND_STATE_FILE}")
    print("=" * 62)


# ======================================================================
# 主流程
# ======================================================================
def main() -> int:
    parser = argparse.ArgumentParser(
        description="全市场基本面/财务数据回补（核心套装，断点续传 + 4 通道并行）"
    )
    parser.add_argument("--status", action="store_true", help="只看进度，不联网")
    parser.add_argument("--symbols", default=None, help="试点：只处理指定代码（逗号分隔，不写状态文件）")
    parser.add_argument("--limit", type=int, default=None, help="只处理前 N 只（冒烟用）")
    parser.add_argument("--batch", action="store_true", help="只跑全市场批量数据集")
    parser.add_argument("--stocks-only", action="store_true", help="只跑逐只数据集（跳过批量）")
    parser.add_argument("--retry-failed", action="store_true", help="只重试失败的")
    parser.add_argument("--force", action="store_true", help="忽略进度全量重拉（慎用）")
    parser.add_argument(
        "--stale-days",
        type=int,
        default=None,
        metavar="N",
        help="增量语义：除待办外，同时重拉「上次更新距今超过 N 天」的逐只数据集。"
        "用于周频轮转（如 N=30 → 稳态每周重拉 ~1/4 市场，季报披露后最多滞后 4 周被刷到）。"
        "★ 调度器用的是 N=6（每周一次全量刷新，但**断点续传有效** —— 本轮已成功的"
        "标的 updated_at=今天，下次不再入待办）；`--force` 会让续传失效，别用。"
        "批量数据集不受影响 —— 它本来就按报告期记录，新季度自动补。",
    )
    parser.add_argument(
        "--shard-count",
        type=int,
        default=1,
        metavar="K",
        help="按标的稳定哈希分成 K 片，只跑其中一片（--shard-index 指定第几片，0 起）。"
        "★ 为什么要分片：全量跑批会把所有标的的 updated_at 刷成同一时刻，"
        "`--stale-days N` 于是退化成「全动或全不动」的齐步振荡；"
        "分片与 updated_at 无关，天然均匀且跨进程稳定（内置 hash 对 str 有随机化，不可用）。",
    )
    parser.add_argument("--shard-index", type=int, default=0, metavar="I", help="跑第 I 片（默认 0）。")
    args = parser.parse_args()

    setup_logging()
    ensure_dirs()

    if args.status:
        _print_status(load_state())
        return 0

    state = load_state()
    lock = threading.Lock()
    stop = threading.Event()

    # ---- 1) 批量数据集（分钟级，先跑）。试点模式（--symbols）跳过：试点只验证逐只链路 ----
    if not args.stocks_only and not args.symbols:
        logger.info(f"批量数据集开始：{list(BATCH_DATASETS)}，报告期 {REPORT_PERIOD_START_YEAR}Q1 ~ 最新")
        _do_batch(_BatchAkshareClient(), state, retry_failed=args.retry_failed)
        save_state(state)
        logger.info("批量数据集完成")
    if args.batch:
        _print_status(state)
        return 0

    # ---- 2) 逐只数据集：4 通道并行（每数据集一个线程）----
    if args.symbols:
        all_symbols = [normalize(s) for s in args.symbols.split(",") if s.strip()]
        pilot = True
        logger.info(f"试点模式：{len(all_symbols)} 只 × {len(STOCK_DATASETS)} 数据集（不写状态文件）")
    else:
        all_symbols = discover_symbols()
        pilot = False
        if not state.get("started_at"):
            state["started_at"] = datetime.now().isoformat(timespec="seconds")
            save_state(state)
        logger.info(f"沿用行情清单（{len(all_symbols)} 只）")

    if args.limit:
        all_symbols = all_symbols[: args.limit]

    # 每个数据集各自筛待办（互不干扰；同一条纪律：状态判定只在主线程做）
    passes: dict[str, list[str]] = {}
    for dataset in STOCK_DATASETS:
        todo: list[str] = []
        for s in all_symbols:
            rec_ds = (state["stocks"].get(s) or {}).get(dataset) or {}
            st = rec_ds.get("status")
            # --shard-count/--shard-index：按稳定哈希分片（与 updated_at 无关，
            #   破齐步）；分片是"这一轮跑哪些"，在其它判据之前筛掉即可。
            if args.shard_count > 1 and _stable_hash(s) % args.shard_count != args.shard_index:
                continue
            if args.force:
                todo.append(s)
            elif args.retry_failed:
                if st == "failed":
                    todo.append(s)
            elif st not in ("ok", "empty"):
                todo.append(s)
            elif args.stale_days is not None:
                # --stale-days：状态"成功"但太久没刷新的也重拉（周频轮转的增量语义）
                updated = rec_ds.get("updated_at") or ""
                age_days = (
                    (datetime.now() - datetime.fromisoformat(updated)).days
                    if updated
                    else 10**6  # 无时间戳 = 更要刷
                )
                if age_days > args.stale_days:
                    todo.append(s)
        passes[dataset] = todo
        logger.info(f"[{dataset}] 待办 {len(todo)} 只")

    total_todo = sum(len(v) for v in passes.values())
    if total_todo == 0:
        logger.info("没有待办标的 —— 基本面回补已全部完成（要重拉加 --force / --stale-days）")
        _print_status(state)
        return 0

    threads: list[threading.Thread] = []
    for dataset, todo in passes.items():
        if not todo:
            continue
        th = threading.Thread(
            target=_run_dataset_pass,
            args=(_BatchAkshareClient(), dataset, todo, state, lock, stop),
            kwargs={"pilot": pilot},
            name=f"fund-{dataset}",
            daemon=True,
        )
        threads.append(th)

    t_start = time.perf_counter()
    for th in threads:
        th.start()
    try:
        while any(th.is_alive() for th in threads):
            time.sleep(2.0)
    except KeyboardInterrupt:
        logger.warning("收到中断 —— 通知各通道停止，保存断点后退出（重跑即续传）")
        stop.set()
        for th in threads:
            th.join(timeout=10.0)
    finally:
        if not pilot:
            save_state(state)

    failed_total = 0
    for rec in (state["stocks"] if not pilot else {}).values():
        for info in rec.values():
            if isinstance(info, dict) and info.get("status") == "failed":
                failed_total += 1
    spent = time.perf_counter() - t_start
    logger.info(
        f"回补结束：处理 {total_todo} 项（{len(threads)} 通道），"
        f"失败 {failed_total} 项，用时 {spent / 60:.1f} 分钟"
    )
    if not pilot:
        _print_status(state)
    if failed_total:
        print("  重试：python -m tools.backfill_fundamental --retry-failed")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
