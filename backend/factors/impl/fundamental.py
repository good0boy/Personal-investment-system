"""基本面因子实现（分类 `fundamental`）。

契约与 `factors/impl/daily.py` **完全一致**：
    fn(q: pd.DataFrame, **params) -> pd.Series     # 与 q.index 逐行对齐

唯一的差别：数据不是从 `q` 里来的，而是从 `data/raw/fundamental/` 读的。
所以本模块自己承担 **按公告日（notice_date）as-of 对齐** 的全部责任 —— 这是
本项目最危险的静默失效来源：财务数据按【报告期】组织（2024-12-31 的报表
2025-03-29 才公告），若按报告期对齐，回测在 2025-01 就能"看到"尚未公开的业绩，
而**它不会报错，只会让因子 IC 虚高、策略凭空多出一段不存在的收益**。

口径三条（写死在实现里，参数由 registry.yaml 声明）：

1. **as-of 对齐到公告日**：每个交易日的因子值 = 该标的截止当日**已公告**的
   最新一期财务值（`notice_date <= 交易日`）。`notice_date` 缺失时在
   `data_store.read_fundamental` 里已按 `report_date + 90 天` 保守回退。
2. **同一公告日多期的取舍**：按 `report_date` 取较晚的那期（见 `_panel` 的排序），
   规则确定，不依赖文件顺序。
3. **累计值的年度化**：`ROEJQ` / `ZZCJLL` 这类"流量 ÷ 存量"比率是按
   **年内累计**口径披露的（一季报=1 个季度、中报=2 个…），直接横截面比较会把
   "刚出一季报的公司"和"刚出年报的公司"放在一起比，是错的口径。
   故提供 `annualize=True` → 乘 `4 / 已过季度数`。⚠️ 这是**近似**（等比分摊假设），
   反例：茅台 2024Q1 ROE 10.57% × 4 = 42.3%，实际全年 36.02% —— 量级正确、不精确。
   **纯比率（毛利率、净利率、资产负债率）不做年度化** —— 它们分子分母同期，本就可比。

单位：本模块所有字段都是**百分数**（`ROEJQ = 32.53` 表示 32.53%），见各因子的 `unit`。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.core.errors import AppError
from app.services import data_store

#: 默认数据集（财务指标：ROE / 毛利率 / 资产负债率 / 同比增速都在这里）
DEFAULT_DATASET = "financial_indicator"

#: 冒烟自检用的合成标的（与 `factors/loader.smoke_frame` 生成的代码一致）
_SMOKE_SYMBOLS = ("sh.600000", "sh.600001", "sh.600002")

#: 合成面板覆盖的字段（自检时任意因子都能取到值）
_SMOKE_FIELDS = (
    "ROEJQ",
    "ZZCJLL",
    "XSJLL",
    "XSMLL",
    "ZCFZL",
    "TOTALOPERATEREVETZ",
    "PARENTNETPROFITTZ",
)


# ======================================================================
# 面板加载与缓存
# ======================================================================
_PANEL_CACHE: dict[tuple[str, tuple[str, ...]], pd.DataFrame] = {}
_ASOF_CACHE: dict[tuple[int, int, str], tuple[pd.DataFrame, np.ndarray]] = {}

#: 冒烟自检的数据替换（`loader.smoke_test` 会用 `synthetic_data()` 打开它）。
#: 生产路径永远为 None —— 见本模块末尾的说明。
_OVERRIDE: pd.DataFrame | None = None


def _quarters(report_date: pd.Series) -> pd.Series:
    """报告期对应的"已过季度数"（一季报=1 / 中报=2 / 三季报=3 / 年报=4）。"""
    month = pd.to_datetime(report_date, errors="coerce").dt.month
    return pd.Series(month, index=report_date.index).map({3: 1, 6: 2, 9: 3, 12: 4})


def _panel(dataset: str, fields: tuple[str, ...]) -> pd.DataFrame:
    """加载并缓存基本面面板（标识列 + 所需字段 + 年度化系数）。

    排序键 `(symbol, notice_date, report_date)` 是刻意的：`merge_asof`
    在同一 `notice_date` 上取**最后一行**，因此"同日多期"会稳定地选中
    `report_date` 较晚的那期，而不是碰运气看文件里谁在后面。
    """
    if _OVERRIDE is not None:
        return _OVERRIDE

    key = (dataset, tuple(fields))
    hit = _PANEL_CACHE.get(key)
    if hit is not None:
        return hit

    panel = data_store.read_fundamental(dataset, columns=list(fields))
    if panel.empty:
        raise AppError(
            f"基本面数据集 {dataset} 为空（data/raw/fundamental/{dataset}/ 下没有数据）。"
            f"请先执行：python -m tools.backfill_fundamental"
        )
    missing = [f for f in fields if f not in panel.columns]
    if missing:
        available = sorted(
            c for c in panel.columns if c not in data_store.FUNDAMENTAL_ID_COLUMNS
        )
        raise AppError(
            f"基本面数据集 {dataset} 里没有字段 {missing}。"
            f"可用字段示例：{available[:12]}{'…' if len(available) > 12 else ''}"
            f"（完整白名单见 tools/backfill_fundamental.py 的 WHITELISTS）"
        )

    panel = panel.sort_values(
        ["symbol", "notice_date", "report_date"], kind="stable"
    ).reset_index(drop=True)
    # 年度化系数：只对"年内累计"口径的比率有意义（纯比率/同比率用时传 annualize=False）
    q = _quarters(panel["report_date"])
    panel["_annualize_factor"] = 4.0 / pd.to_numeric(q, errors="coerce")
    _PANEL_CACHE[key] = panel
    return panel


def _group_bounds(codes: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """把"已按分组键排好序"的整数编码切成连续区间的起止下标。"""
    if len(codes) == 0:
        return np.array([], dtype=np.int64), np.array([], dtype=np.int64)
    starts = np.flatnonzero(np.r_[True, codes[1:] != codes[:-1]])
    ends = np.r_[starts[1:], len(codes)]
    return starts, ends


def _asof_positions(q: pd.DataFrame, dataset: str, fields: tuple[str, ...]) -> np.ndarray:
    """为 `q` 的每一行定位其"截止该交易日已公告的最新一期"在面板中的行号。

    返回长度为 `len(q)` 的数组，未命中处为 NaN。

    实现说明（为什么不用 `pd.merge_asof`）：
        `merge_asof(by=...)` 要求 on-key **全局**单调，而 panel 是按
        `(symbol, notice_date)` 分组的、notice_date 在每个标的内部单调但跨标的会回退，
        实测直接抛 `ValueError: right keys must be sorted`。
        改成"按标的切连续区间 + `np.searchsorted`"后语义完全等价、更可控，
        且对 700 万行是实现级的速度（面板约 32 万行）。

    ★ 结果按 `(id(q), len(q), dataset)` 缓存 —— 多个基本面因子共用同一次 as-of
      归并（各标的各字段只是同一次定位后的数组取值）。缓存**持有 q 的引用**，
      因此 `id()` 不会被回收后复用。
    """
    key = (id(q), len(q), dataset)
    hit = _ASOF_CACHE.get(key)
    if hit is not None and hit[0] is q:
        return hit[1]

    panel = _panel(dataset, fields)

    q_sym = q["symbol"].astype(str).to_numpy()
    q_dt = pd.to_datetime(q["date"]).to_numpy().astype("datetime64[ns]")
    q_codes, q_uniq = pd.factorize(q_sym, sort=False)

    # 面板已是 (symbol, notice_date, report_date) 排序 —— 这一条由 _panel 保证
    p_sym = panel["symbol"].astype(str).to_numpy()
    p_dt = panel["notice_date"].to_numpy().astype("datetime64[ns]")
    p_codes, p_uniq = pd.factorize(p_sym, sort=False)
    p_starts, p_ends = _group_bounds(p_codes)
    p_index = {u: (int(s), int(e)) for u, s, e in zip(p_uniq, p_starts, p_ends, strict=True)}

    positions = np.full(len(q), np.nan, dtype="float64")
    q_starts, q_ends = _group_bounds(q_codes)
    for u, s, e in zip(q_uniq, q_starts, q_ends, strict=True):
        rng = p_index.get(u)
        if rng is None:
            continue  # 该标的没有财务数据 → 整段保持 NaN
        ps, pe = rng
        # searchsorted 只要求**被搜的数组**有序（面板的 notice_date 在每个标的内单调），
        # 查询数组（q 的交易日）不需要有序 —— 所以这里不需要 q 已排序。
        idx = np.searchsorted(p_dt[ps:pe], q_dt[s:e], side="right") - 1
        ok = idx >= 0
        res = np.full(e - s, np.nan, dtype="float64")
        res[ok] = ps + idx[ok]
        positions[s:e] = res

    _ASOF_CACHE.clear()  # 只保留最近一次（面板可能很大，别囤）
    _ASOF_CACHE[key] = (q, positions)
    return positions


def _extract(q: pd.DataFrame, field: str, *, annualize: bool, dataset: str) -> pd.Series:
    """as-of 抽取一列财务值，可选年度化。返回与 `q.index` 对齐的 Series。"""
    positions = _asof_positions(q, dataset, (field,))
    panel = _panel(dataset, (field,))

    values = pd.to_numeric(panel[field], errors="coerce").to_numpy(dtype="float64")
    out = np.full(len(q), np.nan, dtype="float64")
    valid = ~np.isnan(positions)
    if valid.any():
        idx = positions[valid].astype(np.int64)
        out[valid] = values[idx]
        if annualize:
            factors = panel["_annualize_factor"].to_numpy(dtype="float64")
            out[valid] = out[valid] * factors[idx]
    return pd.Series(out, index=q.index)


# ======================================================================
# 因子实现（registry.yaml 里 impl 指向这里）
# ======================================================================
def fin_value(
    q: pd.DataFrame,
    field: str,
    annualize: bool = False,
    dataset: str = DEFAULT_DATASET,
) -> pd.Series:
    """通用基本面取数因子：把某一行财务字段按公告日 as-of 对齐到每个交易日。

    ★ 为什么做成**一个**通用实现而不是"一个字段一个函数"：
      本项目的因子纪律是「新增因子 = 加一段 YAML + 写一个函数」。财务字段有
      80+ 个，若每个字段写一个函数，registry 就成了函数的复制粘贴。
      真正的口径差异（用哪个字段、要不要年度化）全部由 YAML 的 params 声明。

    Args:
        q: 行情长表（列含 date / symbol，已按 [symbol, date] 排序）
        field: 财务字段名（东财编码的列名，如 `ROEJQ` / `ZCFZL`）
        annualize: 是否把"年内累计"口径的比率做年度化（×4/已过季度数）
        dataset: 数据集名，默认 `financial_indicator`
    """
    return _extract(q, field, annualize=annualize, dataset=dataset)


# ======================================================================
# 冒烟自检的数据替换（仅用于 `loader.smoke_test`，生产路径不经过）
# ======================================================================
def _build_synthetic_panel() -> pd.DataFrame:
    """一份最小但**形态真实**的合成基本面面板。

    覆盖 3 只冒烟标的 × 2 个公告日，且**故意让第二个公告日落在冒烟行情的中间**，
    这样 as-of 归并的"切换点"也被真的走到了一次（都在公告日之前就退化成 NaN）。
    """
    rows = []
    for i, symbol in enumerate(_SMOKE_SYMBOLS):
        for j, notice in enumerate(("2023-10-31", "2024-02-29")):
            rows.append(
                {
                    "symbol": symbol,
                    "report_date": pd.Timestamp("2023-09-30" if j == 0 else "2023-12-31"),
                    "notice_date": pd.Timestamp(notice),
                    "ROEJQ": 8.0 + i + j * 2.0,
                    "ZZCJLL": 3.0 + i + j,
                    "XSJLL": 10.0 + i * 2 + j,
                    "XSMLL": 30.0 + i * 3 + j,
                    "ZCFZL": 45.0 + i * 5 - j,
                    "TOTALOPERATEREVETZ": 5.0 + i - j * 3,
                    "PARENTNETPROFITTZ": 6.0 + i * 2 - j * 4,
                }
            )
    panel = pd.DataFrame(rows).sort_values(
        ["symbol", "notice_date", "report_date"], kind="stable"
    ).reset_index(drop=True)
    panel["_annualize_factor"] = 4.0 / pd.to_numeric(
        _quarters(panel["report_date"]), errors="coerce"
    )
    return panel


class synthetic_data:
    """临时把数据读取替换成合成面板（**只给自检用**）。

    ⚠️ 为什么需要它：`loader.smoke_test()` 是"能不能 import / 签名对不对 /
    调得通不通"的最低成本体检，它跑在一份**合成行情**上（假代码、2024-01 起
    40 个交易日）。基本面因子若走真实读取，这只标的、这段区间取决于**盘上
    恰好有什么数据** —— 体检结论就变成了"看运气"，换台机器可能忽然全红。
    本文件之外没有任何地方使用它。
    """

    def __enter__(self) -> synthetic_data:
        global _OVERRIDE
        self._previous = _OVERRIDE
        _OVERRIDE = _build_synthetic_panel()
        _PANEL_CACHE.clear()
        _ASOF_CACHE.clear()
        return self

    def __exit__(self, *exc: object) -> None:
        global _OVERRIDE
        _OVERRIDE = self._previous
        _PANEL_CACHE.clear()
        _ASOF_CACHE.clear()


def clear_caches() -> None:
    """清空面板/归并缓存（测试与长驻进程换数据后用）。"""
    _PANEL_CACHE.clear()
    _ASOF_CACHE.clear()


__all__ = [
    "DEFAULT_DATASET",
    "clear_caches",
    "fin_value",
    "synthetic_data",
]
