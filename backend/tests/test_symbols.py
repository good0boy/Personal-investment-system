"""股票代码格式转换测试。

★ 背景：akshare 各接口的代码格式**完全不统一**，实测共 5 种。
   内部统一格式是 baostock 风格的 `sh.600519`。
   这里锁定转换规则，避免换接口时静默传错格式。
"""
from __future__ import annotations

import pytest

from app.core.errors import SymbolFormatError
from app.services.data_source.symbols import (
    TARGET_BOARDS,
    board_of,
    csi_index_code,
    exchange_of,
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


# ======================================================================
# normalize：多种输入 → 内部格式
# ======================================================================
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("600519", "sh.600519"),
        ("sh.600519", "sh.600519"),
        ("sh-600519", "sh.600519"),
        ("SH600519", "sh.600519"),
        ("sh600519", "sh.600519"),
        ("600519.SH", "sh.600519"),
        ("600519.sh", "sh.600519"),
        (" 600519 ", "sh.600519"),
        ("300750", "sz.300750"),
        ("SZ300750", "sz.300750"),
        ("300750.SZ", "sz.300750"),
        ("000001", "sz.000001"),
        ("002415", "sz.002415"),
        ("688981", "sh.688981"),
    ],
)
def test_normalize_variants(raw, expected):
    assert normalize(raw) == expected


def test_normalize_index_keeps_sh001_series():
    """上证指数 000001 与平安银行冲突，指数必须显式带前缀或声明 is_index。"""
    assert normalize("000001") == "sz.000001"                 # 默认按股票
    assert normalize("000001", is_index=True) == "sh.000001"  # 显式声明
    assert normalize("sh.000001") == "sh.000001"              # 显式前缀


def test_normalize_sz_index_399():
    assert normalize("399001", is_index=True) == "sz.399001"


@pytest.mark.parametrize("bad", ["", "   ", "60051", "6005199", "abc", "sh.abc", "600519.XX"])
def test_normalize_rejects_invalid(bad):
    with pytest.raises(SymbolFormatError):
        normalize(bad)


def test_normalize_strips_utf8_bom():
    """★ UTF-8 BOM 必须被吃掉。

    BOM (`\\ufeff`) **不是空白字符**，`strip()` 去不掉。Windows 记事本另存的 txt、
    Excel 导出的 CSV、PowerShell `Out-File` 默认带 BOM 的文本都会留它，
    而 `--symbols "$(cat list.txt)"` 正是最自然的用法 —— BOM 会粘到**第一个**代码上，
    报错信息里的引号看不见，排查极费时间。
    """
    assert normalize("\ufeffsh.000001") == "sh.000001"
    assert normalize("\ufeff600519") == "sh.600519"
    assert normalize("\ufeff 600519 ") == "sh.600519"


# ======================================================================
# 5 种 akshare 格式互转
# ======================================================================
def test_to_plain():
    assert to_plain("sh.600519") == "600519"
    assert to_plain("sz.000001") == "000001"
    assert to_plain("300750") == "300750"


def test_to_prefixed():
    assert to_prefixed("sh.600519") == "SH600519"
    assert to_prefixed("sz.000895") == "SZ000895"


def test_to_suffixed():
    assert to_suffixed("sz.301389") == "301389.SZ"
    assert to_suffixed("sh.600519") == "600519.SH"


def test_to_baostock_roundtrip():
    assert to_baostock("sh.600519") == "sh.600519"
    assert to_baostock("600519") == "sh.600519"


def test_to_akshare_dispatch():
    assert to_akshare("sh.600519", "plain") == "600519"
    assert to_akshare("sh.600519", "prefix") == "SH600519"
    assert to_akshare("sh.600519", "suffix") == "600519.SH"
    # date / name：该接口入参不是代码（如报告期），原样透传
    assert to_akshare("sh.600519", "date") == "sh.600519"
    assert to_akshare("sh.600519", "name") == "sh.600519"


def test_to_akshare_rejects_unknown_format():
    with pytest.raises(SymbolFormatError):
        to_akshare("sh.600519", "banana")


def test_split_exchange():
    assert split_exchange("sh.600519") == ("sh", "600519")
    assert split_exchange("600519") == ("sh", "600519")
    assert split_exchange("sz.000001") == ("sz", "000001")


@pytest.mark.parametrize("bad", ["sh.60051", "xx.600519", "600519"])
def test_split_exchange_rejects_bad_internal(bad):
    if bad == "600519":
        # 能自动 normalize，不算非法
        assert split_exchange(bad) == ("sh", "600519")
    else:
        with pytest.raises(SymbolFormatError):
            split_exchange(bad)


# ======================================================================
# 交易所与板块推断
# ======================================================================
@pytest.mark.parametrize(
    ("code6", "ex"),
    [
        ("600519", "sh"),
        ("601398", "sh"),
        ("603259", "sh"),
        ("605499", "sh"),
        ("688981", "sh"),
        ("000001", "sz"),
        ("001979", "sz"),
        ("002415", "sz"),
        ("003816", "sz"),
        ("300750", "sz"),
        ("301389", "sz"),
        ("830799", "bj"),
        ("920819", "bj"),
    ],
)
def test_exchange_of(code6, ex):
    assert exchange_of(code6) == ex


@pytest.mark.parametrize(
    ("code6", "board"),
    [
        ("600519", "sh_main"),
        ("601398", "sh_main"),
        ("603259", "sh_main"),
        ("605499", "sh_main"),
        ("000001", "sz_main"),
        ("001979", "sz_main"),
        ("002415", "sz_main"),
        ("003816", "sz_main"),
        ("300750", "gem"),
        ("301389", "gem"),
        ("688981", "star"),
        ("900901", "b_share"),
        ("200011", "b_share"),
        ("830799", "bse"),
        ("920819", "bse"),
        ("510300", "fund"),
    ],
)
def test_board_of(code6, board):
    assert board_of(code6) == board


def test_board_of_invalid():
    assert board_of("abc") == "unknown"
    assert board_of("60051") == "unknown"


def test_target_boards_are_exactly_three():
    assert {"sh_main", "sz_main", "gem"} == TARGET_BOARDS


@pytest.mark.parametrize("code6", ["600519", "000001", "002415", "300750", "301389"])
def test_is_target_board_true(code6):
    assert is_target_board(code6) is True


@pytest.mark.parametrize("code6", ["688981", "689009", "830799", "900901", "200011", "510300"])
def test_is_target_board_false(code6):
    assert is_target_board(code6) is False


# ======================================================================
# 指数代码
# ======================================================================
@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("上证指数", "sh.000001"),
        ("sh.000001", "sh.000001"),
        ("000001", "sh.000001"),
        ("沪深300", "sh.000300"),
        ("000300", "sh.000300"),
        ("中证500", "sh.000905"),
        ("中证1000", "sh.000852"),
        ("深证成指", "sz.399001"),
        ("创业板指", "sz.399006"),
    ],
)
def test_csi_index_code(name, expected):
    assert csi_index_code(name) == expected


# ======================================================================
# thscode 互转（同花顺 fuyao REST）—— 2026-10-01 新增
# ======================================================================
def test_to_thscode_stocks_and_indices():
    assert to_thscode("sh.600519") == "600519.SH"
    assert to_thscode("sz.000001") == "000001.SZ"
    assert to_thscode("bj.830799") == "830799.BJ"
    # 指数同样适用（注意 sh.000001 是上证指数，不是平安银行）
    assert to_thscode("sh.000001") == "000001.SH"
    assert to_thscode("sz.399001") == "399001.SZ"


def test_from_thscode_roundtrip():
    for internal in ("sh.600519", "sz.000001", "bj.830799", "sh.000001", "sz.399001"):
        assert from_thscode(to_thscode(internal)) == internal


def test_from_thscode_rejects_bad_input():
    """★ 板块指数（.TI）必须被拒绝 —— 硬转成内部格式会造出不存在的代码。"""
    for bad in ("600519", "600519.XX", "886042.TI", "sh.600519", ""):
        with pytest.raises(SymbolFormatError):
            from_thscode(bad)


def test_board_thscode_helpers():
    assert to_board_thscode("881121") == "881121.TI"
    assert is_board_thscode("886042.TI") is True
    assert is_board_thscode("886042.ti") is True          # 大小写不敏感
    assert is_board_thscode("600519.SH") is False
    with pytest.raises(SymbolFormatError):
        to_board_thscode("886042.TI")                      # 不接受已带后缀的
    with pytest.raises(SymbolFormatError):
        to_board_thscode("abcdef")


def test_thscode_normalize_accepts_suffix_form():
    """`normalize()` 早就能吃 600519.SH —— 与 fuyao 的 thscode 天然兼容。"""
    assert normalize("600519.SH") == "sh.600519"
    assert normalize("000001.SZ") == "sz.000001"
