# sweeps/ · 参数寻优

## 目录内容

| 文件 | 说明 |
|---|---|
| `hold_tp.yaml` | **首组网格**：「最大持有期 × 止盈阈值」= 4 × 3 = 12 组 |
| `smoke_subset.txt` | **通线验证用的 165 只抽样标的**（逗号分隔）。**不是正式股票池** |

## 用法

```bash
cd backend

# 正式跑（全市场，样本内 2020-06-01 ~ 2024-12-31）
python -m strategies.sweep --config sweeps/hold_tp.yaml --tag v1

# ⚠️ 通线验证（只跑 165 只，约 2 分钟）
#    截面选股被严重削弱，**结论不可用于评估策略**，只证明流水线通
python -m strategies.sweep --config sweeps/hold_tp.yaml --tag smoke \
    --sweep-id smoke-165 --symbols "$(cat sweeps/smoke_subset.txt)"

# 列出历史寻优
python -m strategies.sweep --list
```

`--config` 传 `sweeps/hold_tp.yaml` 或 `hold_tp.yaml` **都可以**（内部按多层候选目录解析，
报错时会列出可用文件）。

## 三个必须知道的约束

1. **只能跑样本内**。`sweep.py` 硬编码拒绝样本外区间 —— 样本外数据在规则冻结前
   绝不允许被看到（策略规格说明书 §5.1 纪律 1）。要跑样本外得先冻结规则，
   再用 `python -m strategies.run --out-sample` 跑一次。
2. **网格规模上限** `MAX_GRID_COMBOS`（见 `strategies/runner.py`）。12 组 × 1115 交易日
   已是分钟级；再往上加维度要重新评估耗时，别把 CLI 变成一跑一晚上的任务。
3. **`--symbols` 是调试开关，不是"小规模正式回测"**。产物结构与全市场完全一致，
   所以运行会显式打上调试标记（`config.json` 的 `debug_symbols`），详情页也会红字警示。

## 产物

落盘到 `data/sweep/{sweep_id}/`：

| 文件 | 内容 |
|---|---|
| `grid.json` | 网格配置摘要 + 区间 + 目标指标 + 各组合的参数与 run_id |
| `results.parquet` | 每组一行：参数 + 指标 + `neighbor_mean` + `is_spike` |
| `runs.json` | 组合序号 → `run_id` 映射（便于跳转到单次回测详情） |
| `summary.md` | **给人看的报告**：结论 + 全网格表 + 敏感性分析 + 读法说明 |

**注意**：寻优子运行**不写明细表**（trades/positions/orders）—— 12 组 × 数百笔体积过大
且无人逐笔看。子运行的 `config.json::_run` 里有 `detail_written: false` 与 `sweep_id` /
`combo_index`，据此①从回测列表默认隐藏、②详情页说明"未落明细表"而不是伪装成"0 笔交易"。
要看逐笔明细就跑单次：`python -m strategies.run --params ...`。
