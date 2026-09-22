"""合成因子实现（分类 `combo`）—— 把若干**已算好的**因子按逐日截面秩合成。

为什么要合成（背景，别当"多加几个因子"看）
------------------------------------------
研究文档 §3.2 用逐日截面 Spearman 秩相关矩阵实测：11 个日度因子其实只坍缩成
**约 3 个独立信息维度** ——

    · 动量簇（ma_bias_20 × ret_10 ρ=0.886、×rsi_14 0.863、×momentum_20 0.800 …）
    · 活跃度簇（amplitude_5 × volatility_20 0.780、turnover_20 × volatility_20 0.712 …）
    · today_pct（与波动/换手/振幅近零相关，**独立**）

所以"再加一个动量类因子"只是给同一个信号加权，信息增量为零。真正可能带来增量的是
**跨簇组合** —— 本模块就干这一件事：每簇选一个代表，秩标准化后合成。

方案要点（三条，都是刻意的）
---------------------------
1. **先逐日截面秩标准化，再加权求和**。
   组件的量纲完全不同（`today_pct` 是小数、`amplitude_5` 是小数、未来若混入
   基本面因子则是百分数），不标准化直接相加等于让量纲大的那一个独占权重。
   秩变换还顺带把极值压掉（`profit_yoy` 那种 ±数千 % 的字段尤其需要）。
2. **截面按 `q` 的全集算秩，不按"当日可投资池"**。
   后者会让排名依赖逐日股票池的变动，同一只股票在不同日子的秩不可比，
   且不利于复现。（`analyze` 之后会自己套可投资池掩码，两件事不该混在一起。）
3. **权重与符号由 YAML 声明**（`factors` / `signs` / `weights`），实现里不写死。
   为什么符号要显式给：本项目实测全部动量/活跃度类因子 IC 为**负**
   （"追强势股赚不到钱"），所以"取负"是一个**待检验的假设**，必须由 YAML 明说，
   而不是让实现偷偷翻转 —— 那样注册表里就看不出口径了。

⚠️ 数据依赖：本模块读的是**其他因子的产物**（`data/factor/{category}/{name}.parquet`），
   不是行情。所以它在注册表里的 `data_deps` 是 `[factor]`，且组件因子必须**先算过**。
   对齐靠"校验后快路径 + 显式失败"，绝不假设两侧行序一致
   （DuckDB 不带 ORDER BY 读 Parquet 时不保证行序）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.core.errors import AppError
from app.services import data_store

#: 组件因子对齐结果缓存：`(id(q), len(q), 组件元组)` -> 形状 (n_components, len(q)) 的秩矩阵
_RANK_CACHE: dict[tuple[int, int, tuple[str, ...]], tuple[pd.DataFrame, np.ndarray]] = {}

#: 冒烟自检用：为真时 `_rank_matrix` 直接给出合成秩矩阵，**不读组件产物**。
#: 组件因子必须先算过才能合成，而自检跑在一份合成行情上 —— 若走真实读取，
#: "体检结论"就变成了"看盘上恰好算了哪些因子"，换台机器可能忽然全红。
#: 生产路径永远为 False，见本模块末尾的 `synthetic_data`。
_SYNTHETIC = False


def _align_to_q(q: pd.DataFrame, f: pd.DataFrame, name: str) -> np.ndarray:
    """把某个因子产物的 `value` 对齐到 `q` 的行序。

    ★ 为什么不直接假设两侧同序：`read_factor` 走的是 DuckDB
      `SELECT * FROM read_parquet(...)`，**没有 ORDER BY 时不保证行序**。
      同序只是"通常成立"，靠它就会在某次升级后静默把 A 股票的值贴到 B 股票上。

    先试同序（最快），再试"各自按 (symbol, date) 排序后同序"，两者都不成立就
    **显式报错** —— 这类错必须大声，不能返回一个看起来正常的错配结果。
    """
    if len(f) != len(q):
        raise AppError(
            f"因子 {name} 的产物有 {len(f):,} 行，当前行情有 {len(q):,} 行 —— 对不上。"
            f"通常是用不同区间算出来的（如因子只算到某天、行情到另一天）。"
            f"请用同一区间重算该因子：python -m factors.calc --name {name} --force"
        )
    q_sym = q["symbol"].astype(str).to_numpy()
    q_dt = pd.to_datetime(q["date"]).to_numpy().astype("datetime64[ns]")
    f_sym = f["symbol"].astype(str).to_numpy()
    f_dt = pd.to_datetime(f["date"]).to_numpy().astype("datetime64[ns]")
    values = pd.to_numeric(f["value"], errors="coerce").to_numpy(dtype="float64")

    if np.array_equal(q_sym, f_sym) and np.array_equal(q_dt, f_dt):
        return values

    oq = np.lexsort((q_dt.view("int64"), q_sym))
    of = np.lexsort((f_dt.view("int64"), f_sym))
    if np.array_equal(q_sym[oq], f_sym[of]) and np.array_equal(q_dt[oq], f_dt[of]):
        out = np.full(len(q), np.nan, dtype="float64")
        out[oq] = values[of]
        return out

    raise AppError(
        f"因子 {name} 的产物行数与当前行情相同，但标的/日期组合对不上 —— "
        f"两份数据不是同一个宇宙。请用同一区间重算该因子："
        f"python -m factors.calc --name {name} --force"
    )


def _component_category(name: str) -> str:
    """组件因子属于哪个分类 —— 从注册表读（**定义的唯一来源**），不猜。"""
    from factors import loader  # noqa: PLC0415 - 延迟导入避免与 loader 的循环依赖

    return loader.get_factor(name).category


def _rank_matrix(q: pd.DataFrame, factors: tuple[str, ...]) -> np.ndarray:
    """各组件在 `q` 上的**逐日截面百分位秩**矩阵，形状 (len(factors), len(q))。"""
    if _SYNTHETIC:
        n = max(len(q), 1)
        idx = np.arange(len(q), dtype="float64")
        return np.vstack(
            [((idx * (i + 1)) % n) / max(n - 1, 1) for i in range(len(factors))]
        )

    key = (id(q), len(q), factors)
    hit = _RANK_CACHE.get(key)
    if hit is not None and hit[0] is q:
        return hit[1]

    date_codes, _ = pd.factorize(pd.to_datetime(q["date"]).to_numpy(), sort=False)
    rows = []
    for name in factors:
        f = data_store.read_factor(name, category=_component_category(name))
        raw = _align_to_q(q, f, name)
        # 逐日截面秩（pct=True → 0~1）。NaN 保留为 NaN，不参与该日排序。
        ranked = (
            pd.Series(raw)
            .groupby(date_codes, sort=False)
            .rank(pct=True, na_option="keep")
            .to_numpy(dtype="float64")
        )
        rows.append(ranked)

    matrix = np.vstack(rows)
    _RANK_CACHE.clear()  # 只留最近一次（每行 8 字节 × 700 万 × 组件数是真金白银）
    _RANK_CACHE[key] = (q, matrix)
    return matrix


def rank_combo(
    q: pd.DataFrame,
    factors: list[str],
    signs: list[float] | None = None,
    weights: list[float] | None = None,
) -> pd.Series:
    """逐日截面秩合成因子。

    合成值 = Σ(wᵢ · signᵢ · rankᵢ) / Σ|wᵢ|，其中 rankᵢ 是该组件当日的百分位秩（0~1）。

    Args:
        q: 行情长表（列含 date / symbol）
        factors: 组件因子名（按名从注册表查分类，产物必须已算过）
        signs: 每个组件的符号，默认全 +1。**取负是待检验的假设，由 YAML 显式声明**
        weights: 每个组件的权重，默认等权。归一化用 Σ|w|，故权重和不必为 1

    Returns:
        与 `q.index` 对齐的 Series。任一组件缺失处为 NaN，**不会**用 0 顶替
        （0 在秩空间里是"最低分"，顶替会凭空制造一个看空信号）。
    """
    names = [str(x).strip() for x in (factors or []) if str(x).strip()]
    if not names:
        raise AppError("合成因子必须声明至少一个组件（params.factors）")
    if len(set(names)) != len(names):
        raise AppError(f"合成因子的组件重复：{names}")

    sg = [1.0] * len(names) if signs is None else [float(x) for x in signs]
    wt = [1.0] * len(names) if weights is None else [float(x) for x in weights]
    if len(sg) != len(names) or len(wt) != len(names):
        raise AppError(
            f"signs({len(sg)}) / weights({len(wt)}) 的长度必须与 factors({len(names)}) 一致"
        )
    denom = float(np.sum(np.abs(wt)))
    if denom == 0:
        raise AppError(f"weights 全为 0，合成没有意义：{wt}")

    matrix = _rank_matrix(q, tuple(names))
    total = np.zeros(len(q), dtype="float64")
    covered = np.zeros(len(q), dtype="float64")
    for i, (s, w) in enumerate(zip(sg, wt)):
        vals = matrix[i] * float(s)
        # 只有所有组件都有值的行才算"覆盖"；缺一个就整行 NaN（见 docstring 的理由）
        ok = ~np.isnan(matrix[i])
        total[ok] += vals[ok] * float(w)
        covered[ok] += 1.0

    out = np.where(covered == len(names), total / denom, np.nan)
    return pd.Series(out, index=q.index)


def clear_caches() -> None:
    """清空秩矩阵缓存（测试与长驻进程换数据后用）。"""
    _RANK_CACHE.clear()


class synthetic_data:  # noqa: N801 - 按约定名暴露给 loader，与 fundamental.py 同一约定
    """临时让合成走一份合成秩矩阵（**只给自检用**）。

    与 `factors/impl/fundamental.py` 的同名钩子同一约定：任何"依赖外部落盘产物"
    的因子都可以提供它，`loader.smoke_test` 会自动发现并使用，因此 loader 里
    **没有**按分类硬编码的分支。
    """

    def __enter__(self) -> "synthetic_data":
        global _SYNTHETIC
        self._previous = _SYNTHETIC
        _SYNTHETIC = True
        _RANK_CACHE.clear()
        return self

    def __exit__(self, *exc: object) -> None:
        global _SYNTHETIC
        _SYNTHETIC = self._previous
        _RANK_CACHE.clear()


__all__ = ["clear_caches", "rank_combo", "synthetic_data"]
