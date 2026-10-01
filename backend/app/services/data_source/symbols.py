"""股票代码格式统一。

★ 这是本项目最容易踩的坑之一。

AKShare 各接口对代码格式要求**完全不统一**，实测共 5 种：

| 格式            | 示例         | 使用场景                                                   |
|-----------------|--------------|------------------------------------------------------------|
| plain           | `600519`     | akshare `stock_zh_a_hist` / `stock_zh_a_spot_em`           |
| prefix          | `SZ000895`   | akshare `stock_zh_valuation_comparison_em`                 |
| suffix          | `301389.SZ`  | akshare `stock_financial_analysis_indicator_em`            |
| baostock        | `sh.600519`  | baostock 全部接口                                          |
| thscode         | `600519.SH`  | 同花顺 fuyao REST（股票/指数）；板块指数另有 `886042.TI`      |
| date / name     | `20230930`   | `stock_zh_a_gdhs`、`stock_fhps_em`（入参是报告期，不是代码）|

**内部统一格式 = baostock 风格 `sh.600519`** —— 无歧义，且能表达沪深两市。

⚠️ 指数与股票代码会冲突：`000001` 既是"平安银行"(sz.000001)，
也是"上证指数"(sh.000001)。因此纯 6 位数字**一律按股票推断**，
指数必须显式带交易所前缀（如 `sh.000001`、`sh.000300`、`sh.000905`）。
"""
from __future__ import annotations

import re
from typing import Literal

from app.core.errors import SymbolFormatError
from app.core.logging import logger

Exchange = Literal["sh", "sz", "bj"]

#: 内部统一分隔符
_SEP = "."

#: 板块枚举
BoardType = Literal[
    "sh_main",      # 沪市主板 600/601/603/605
    "sz_main",      # 深市主板 000/001/002/003（002/003 原中小板）
    "gem",          # 创业板 300/301
    "star",         # 科创板 688/689
    "bse",          # 北交所 8xx/4xx/920
    "b_share",      # B股 900/200
    "index",        # 指数（显式带前缀且属于指数段）
    "fund",         # 基金/ETF
    "unknown",
]

#: 本策略的目标板块（其余一律排除）
TARGET_BOARDS: frozenset[str] = frozenset({"sh_main", "sz_main", "gem"})

_PLAIN_RE = re.compile(r"^\d{6}$")
_BSTOCK_RE = re.compile(r"^(sh|sz|bj)[.\-_]?(\d{6})$", re.IGNORECASE)
_PREFIX_RE = re.compile(r"^(SH|SZ|BJ)(\d{6})$", re.IGNORECASE)
_SUFFIX_RE = re.compile(r"^(\d{6})\.(SH|SZ|BJ)$", re.IGNORECASE)

_CODE_CN_NAME = "股票代码"


def _clean(raw: str) -> str:
    """剥掉常见不可见字符后返回。

    ★ 必须处理 **UTF-8 BOM（`\\ufeff`）**：它不是空白字符，`strip()` 去不掉，
    于是 `sh.000001` 会变成 `\\ufeffsh.000001` 直接报"格式无法识别"。
    来源极其常见 —— Windows 记事本另存的 txt、Excel 导出的 CSV、
    PowerShell `Out-File` 默认带 BOM 的文本，都会在文件头留下它。
    用 `$(cat list.txt)` 传给 CLI 时它就会粘到**第一个**代码上，
    而报错信息里的引号是看不见的，排查会浪费很久。
    """
    return str(raw).lstrip("\ufeff").strip().replace(" ", "")


def exchange_of(code6: str) -> Exchange:
    """按 6 位代码推断交易所。

    注意：本函数**只用于股票**。指数请勿调用（000001 会误判为 sz）。
    """
    if not _PLAIN_RE.match(code6):
        raise SymbolFormatError(f"{_CODE_CN_NAME}必须是 6 位数字，收到：{code6!r}")

    head2, head3 = code6[:2], code6[:3]

    # 上交所
    if head2 in ("60", "68", "69") or head3 in ("900", "500", "510", "511", "512", "513"):
        return "sh"
    if head3 == "605":
        return "sh"

    # 北交所
    if head2 in ("83", "87", "88", "43", "92") or head3 == "920":
        return "bj"

    # 深交所（含主板 000/001/002/003、创业板 300/301、B股 200、基金 15x/16x）
    return "sz"


def board_of(code6: str) -> BoardType:
    """判断 6 位代码所属板块。

    ⚠️ **本函数不看交易所**，因此**无法**区分 `sz.000001`（平安银行）与
       `sh.000001`（上证综合指数）—— 两者都会返回 `sz_main`。
       只要手上已经有完整代码（`sh.600519`），就必须用 `board_of_symbol()`。
       本函数只适用于"已知它确实是股票、且只有 6 位码"的场景
       （典型：akshare 的纯数字接口）。
    """
    if not _PLAIN_RE.match(code6):
        return "unknown"

    h3 = code6[:3]

    if h3 in ("600", "601", "603", "605"):
        return "sh_main"
    if h3 in ("688", "689"):
        return "star"
    if h3 == "900" or code6[:2] == "90":
        return "b_share"
    if h3 in ("000", "001", "002", "003"):
        return "sz_main"
    if h3 in ("300", "301"):
        return "gem"
    if h3 == "200" or code6[:2] == "20":
        return "b_share"
    if code6[0] in ("4", "8") or h3 == "920":
        return "bse"
    if h3 in ("510", "511", "512", "513", "515", "159", "150", "160", "161", "162", "163"):
        return "fund"

    return "unknown"


def board_of_symbol(symbol: str) -> BoardType:
    """按**内部格式**（`sh.600519`）判断板块 —— 与 `board_of()` 的关键区别是**看交易所**。

    ★ 为什么必须看交易所
    ------------------
    `000001` 是个双关代码：`sz.000001` 是平安银行（深主板股票），
    `sh.000001` 是**上证综合指数**。`board_of("000001")` 只能按"6 位数字默认
    是股票"推断成 `sz_main`，于是 `sh.000001` 会被判成深主板股票混进股票池。

    实测（2026-09-18 全市场回补）：按 `board_of` 取出的 5014 只清单里
    混进了 **223 个 `sh.000xxx` 指数**（占 4.4%）。它们会进入回测候选池，
    而且**不报任何错** —— 典型的静默污染。

    各交易所的段位（本函数据此判定）
    -------------------------------
    | 交易所 | 段位 | 板块 |
    |---|---|---|
    | sh | 600/601/603/605 | 沪主板 |
    | sh | 688/689 | 科创板（排除） |
    | sh | **000 / 930 / 931 / 950** | **指数（排除）** |
    | sh | 900 / 90x | B 股（排除） |
    | sh | 51x/52x/56x/58x | 基金 ETF（排除） |
    | sz | 000/001/002/003 | 深主板 |
    | sz | 300/301 | 创业板 |
    | sz | **399** | **指数（排除）** |
    | sz | 200 / 20x | B 股（排除） |
    | sz | 159/150/160/161/162/163 | 基金（排除） |
    | bj | 全部 | 北交所（排除） |
    """
    ex, code6 = split_exchange(symbol)
    if not _PLAIN_RE.match(code6):
        return "unknown"

    h2, h3 = code6[:2], code6[:3]

    if ex == "bj":
        return "bse"

    if ex == "sh":
        if h3 == "000" or h3 in ("930", "931", "950"):
            return "index"                 # 上证系列 000xxx / 中证系列 930xxx~950xxx
        if h3 in ("600", "601", "603", "605"):
            return "sh_main"
        if h3 in ("688", "689"):
            return "star"
        if h3 == "900" or h2 == "90":
            return "b_share"
        if h2 in ("51", "52", "56", "58"):
            return "fund"
        return "unknown"                   # 沪市债券/回购等

    # 深交所
    if h3 == "399":
        return "index"                     # 深证成指 399001 / 创业板指 399006 …
    if h3 in ("000", "001", "002", "003"):
        return "sz_main"
    if h3 in ("300", "301"):
        return "gem"
    if h3 == "200" or h2 == "20":
        return "b_share"
    if h3 in ("159", "150", "160", "161", "162", "163"):
        return "fund"
    return "unknown"


def is_target_board(code6: str) -> bool:
    """裸 6 位码是否属于本策略股票池板块。

    ⚠️ 与 `board_of()` 一样**不看交易所**；有完整代码时请用
       `is_target_board_symbol()`。
    """
    return board_of(code6) in TARGET_BOARDS


def is_target_board_symbol(symbol: str) -> bool:
    """内部格式代码是否属于本策略股票池板块（沪主板 / 深主板 / 创业板）。

    ★ 这是**应该被业务层调用**的那一个 —— 它会正确排除 `sh.000001`
      这类"长得像深主板股票"的指数。

    ★ 非法代码返回 False 而不抛异常：本函数是**逐行过滤谓词**，
      上游数据里混进脏代码时应当"不入选"，而不是让整轮回测崩掉。
      但会打 WARNING 留痕 —— 静默丢标的比崩掉更难查。
    """
    try:
        return board_of_symbol(symbol) in TARGET_BOARDS
    except SymbolFormatError:
        logger.warning(f"板块判定遇到非法代码，按「非目标板块」处理：{symbol!r}")
        return False


def normalize(raw: str, *, is_index: bool = False) -> str:
    """把任意格式的输入统一成内部格式 `sh.600519`。

    支持的输入：
        "600519"        -> "sh.600519"
        "sh.600519"     -> "sh.600519"
        "SH600519"      -> "sh.600519"
        "600519.SH"     -> "sh.600519"
        "sz.000001"     -> "sz.000001"
        "000300"        -> "sz.000300"  ⚠️ 若非指数，会按股票推断
        "sh.000300"     -> "sh.000300"  ✅ 指数必须这样传

    Args:
        raw: 任意格式代码
        is_index: 为 True 时，纯 6 位数字按上交所指数处理（指数绝大多数在沪市）
    """
    s = _clean(raw)
    if not s:
        raise SymbolFormatError(f"{_CODE_CN_NAME}不能为空")

    # 1) baostock 风格：sh.600519 / sh-600519
    m = _BSTOCK_RE.match(s)
    if m:
        return f"{m.group(1).lower()}{_SEP}{m.group(2)}"

    # 2) 前缀风格：SH600519
    m = _PREFIX_RE.match(s)
    if m:
        return f"{m.group(1).lower()}{_SEP}{m.group(2)}"

    # 3) 后缀风格：600519.SH
    m = _SUFFIX_RE.match(s)
    if m:
        return f"{m.group(2).lower()}{_SEP}{m.group(1)}"

    # 4) 纯 6 位数字
    if _PLAIN_RE.match(s):
        if is_index:
            # 指数：沪市 000xxx（上证系列），深市 399xxx（深证系列）
            # ⚠️ 不能复用 exchange_of —— 它按股票规则推断，会把 399001 判成 sh
            ex = "sz" if s.startswith("399") else "sh"
            return f"{ex}{_SEP}{s}"
        return f"{exchange_of(s)}{_SEP}{s}"

    raise SymbolFormatError(
        f"无法识别的{_CODE_CN_NAME}格式：{raw!r}。"
        f"支持 600519 / sh.600519 / SH600519 / 600519.SH"
    )


def _split(canonical: str) -> tuple[str, str]:
    s = _clean(canonical)
    if _SEP not in s:
        s = normalize(s)
    ex, code6 = s.split(_SEP, 1)
    if ex not in ("sh", "sz", "bj") or not _PLAIN_RE.match(code6):
        raise SymbolFormatError(f"内部代码格式非法：{canonical!r}，应为 sh.600519 形式")
    return ex, code6


def to_baostock(canonical: str) -> str:
    """`sh.600519` —— baostock 全部接口使用。"""
    ex, code6 = _split(canonical)
    return f"{ex}{_SEP}{code6}"


def to_plain(canonical: str) -> str:
    """`600519` —— akshare 日线/快照类接口使用。"""
    return _split(canonical)[1]


def to_prefixed(canonical: str) -> str:
    """`SH600519` —— akshare `stock_zh_valuation_comparison_em` 使用。"""
    ex, code6 = _split(canonical)
    return f"{ex.upper()}{code6}"


def to_suffixed(canonical: str) -> str:
    """`600519.SH` —— akshare `stock_financial_analysis_indicator_em` 使用。"""
    ex, code6 = _split(canonical)
    return f"{code6}{_SEP}{ex.upper()}"


# ======================================================================
# 同花顺 thscode（fuyao REST 专用）
#
# ⚠️ 与 baostock 的关键区别：**thscode 不是内部格式**（内部是 `sh.600519`）。
#    所以这一组是"边界转换"，只应在 `fuyao_client` 与接它的调用点使用 ——
#    不要把它当成又一个 `to_xxx` 到处用，否则内部会出现两种真相。
#
# ⚠️ 板块指数（同花顺行业/概念）不是股票也不是交易所指数，后缀是 `.TI`
#    （TongHuaShun Index），且**只能是 6 位数字 + .TI**，没有 sh/sz 之分。
# ======================================================================

#: 股票/交易所指数的 thscode：`600519.SH` / `000001.SH` / `399001.SZ`
_THSCODE_RE = re.compile(r"^(\d{6})\.(SH|SZ|BJ)$", re.IGNORECASE)

#: 同花顺板块（行业/概念）指数的 thscode：`886042.TI` / `881121.TI`
_TS_BOARD_RE = re.compile(r"^(\d{6})\.TI$", re.IGNORECASE)


def to_thscode(canonical: str) -> str:
    """内部格式 `sh.600519` → fuyao 的 thscode `600519.SH`。

    指数同样适用：`sh.000001` → `000001.SH`、`sz.399001` → `399001.SZ`。
    板块指数请用 `to_board_thscode()`（后缀是 `.TI`，与交易所无关）。
    """
    ex, code6 = _split(canonical)
    return f"{code6}{_SEP}{ex.upper()}"


def from_thscode(value: str) -> str:
    """fuyao 的 thscode `600519.SH` → 内部格式 `sh.600519`。

    ⚠️ 只接受股票/交易所指数形态（`.SH`/`.SZ`/`.BJ`）。
       板块指数的 `.TI` 会被拒绝 —— 它没有交易所语义，硬转成
       `xx.886042` 会造出一个根本不存在的内部代码，属静默污染的温床。
       要处理板块请用 `is_board_thscode()` 判类型后单独走板块逻辑。
    """
    s = _clean(value)
    m = _THSCODE_RE.match(s)
    if not m:
        raise SymbolFormatError(
            f"不是合法的 thscode：{value!r}。"
            f"股票/指数应为 600519.SH / 000001.SH / 399001.SZ"
            f"（板块指数 886042.TI 不在此列，请用 is_board_thscode 判断）"
        )
    return f"{m.group(2).lower()}{_SEP}{m.group(1)}"


def to_board_thscode(code: str) -> str:
    """同花顺板块代码 `881121` → fuyao 的板块 thscode `881121.TI`。

    入参是同花顺自己的板块码（akshare `stock_board_*_name_ths` 返回的 `code` 列），
    **不是**内部股票格式，也不是交易所指数代码。
    """
    c = _clean(code)
    if not _PLAIN_RE.match(c):
        raise SymbolFormatError(
            f"同花顺板块代码应为 6 位数字，收到：{code!r}"
        )
    return f"{c}.TI"


def is_board_thscode(value: str) -> bool:
    """是否为同花顺板块指数 thscode（`886042.TI` 形态）。"""
    return bool(_TS_BOARD_RE.match(_clean(value)))


def to_akshare(canonical: str, fmt: str = "plain") -> str:
    """按 akshare 接口声明的格式转换。

    Args:
        fmt: "plain" | "prefix" | "suffix" | "date" | "name"
             后两种表示该接口的入参不是代码（如报告期 / 中文名），原样透传。
    """
    if fmt == "plain":
        return to_plain(canonical)
    if fmt == "prefix":
        return to_prefixed(canonical)
    if fmt == "suffix":
        return to_suffixed(canonical)
    if fmt in ("date", "name"):
        return canonical
    raise SymbolFormatError(f"未知的 akshare 代码格式声明：{fmt!r}")


def split_exchange(canonical: str) -> tuple[str, str]:
    """返回 (交易所, 6位代码)，如 ("sh", "600519")。"""
    return _split(canonical)


def csi_index_code(name_or_code: str) -> str:
    """常用指数代码便捷转换（指数统一在沪市）。"""
    aliases = {
        "上证指数": "sh.000001",
        "上证综指": "sh.000001",
        "sh.000001": "sh.000001",
        "000001": "sh.000001",
        "沪深300": "sh.000300",
        "000300": "sh.000300",
        "中证500": "sh.000905",
        "000905": "sh.000905",
        "中证1000": "sh.000852",
        "000852": "sh.000852",
        "深证成指": "sz.399001",
        "399001": "sz.399001",
        "创业板指": "sz.399006",
        "399006": "sz.399006",
    }
    key = _clean(name_or_code)
    if key in aliases:
        return aliases[key]
    return normalize(key, is_index=True)
