"""模块 3 · 策略回测的接口（阶段 2）。

**全部只读** —— 架构纪律 5「只读展示优先」：前端请求不触发计算。
回测由 `python -m strategies.run` 在 CLI 跑，结果落盘到
`data/backtest/{strategy}/{run_id}/`，这里只把文件读出来。

路由设计说明
------------
路径用 `/runs/{strategy}/{run_id}` 而不是 `/runs/{run_id}`：
`run_id` 里带短哈希（实践中唯一），但把它当全局主键意味着"定位一份结果"
要先查库再拼路径；两段路径直接映射磁盘位置，无歧义，也不依赖 DB 状态。

⚠️ **`/runs/compare` 必须在 `/runs/{strategy}/{run_id}` 之前注册** ——
   否则 FastAPI 会把 `compare` 当成 strategy 名去匹配（段数不同其实不会冲突，
   但顺序放在前面可以彻底杜绝这类问题，也让 OpenAPI 文档读起来更顺）。
"""
from __future__ import annotations

from fastapi import APIRouter, Query

from app.core.errors import InvalidParamError
from app.schemas.common import ApiResponse
from app.services import backtest_service as svc

router = APIRouter(tags=["backtest"])


# ======================================================================
# 策略
# ======================================================================
@router.get("/strategies", summary="已注册策略列表")
def list_strategies() -> ApiResponse[list[dict]]:
    return ApiResponse.ok(svc.list_strategies())


# ======================================================================
# 运行列表
# ======================================================================
@router.get("/runs", summary="回测运行列表（筛选 / 排序）")
def list_runs(
    strategy: str | None = Query(None, description="按策略名过滤"),
    tag: str | None = Query(None, description="按标签模糊过滤"),
    status: str | None = Query(None, description="success | running | failed | incomplete"),
    sort: str = Query("created_at", description="排序字段，见 backtest_service.SORTABLE"),
    desc: bool = Query(True, description="是否降序；无该指标的 run 永远排最后"),
    limit: int = Query(200, ge=1, le=1000),
    exclude_sweep: bool = Query(
        True, description="是否滤掉参数寻优的子运行（默认滤；一次 sweep 会留十几个 run）"
    ),
) -> ApiResponse[dict]:
    """列表数据来自**扫盘**（`config.json` + `metrics.json`），不是数据库。

    数据库里的 `backtest_run` 表只补 `tag` / `cost_seconds` 这类过程信息。
    理由：进程被杀会留下 `status='running'` 的脏记录，而产物目录不会说谎。
    """
    return ApiResponse.ok(
        svc.list_runs(
            strategy=strategy,
            tag=tag,
            status=status,
            sort=sort,
            desc=desc,
            limit=limit,
            exclude_sweep=exclude_sweep,
        )
    )


@router.get("/runs/compare", summary="多 run 净值对比（P1）")
def compare_runs(
    refs: str = Query(..., description="逗号分隔的 `strategy:run_id`，最多 6 个"),
    max_points: int = Query(svc.DEFAULT_MAX_POINTS, ge=50, le=20000),
) -> ApiResponse[dict]:
    """把多份回测的净值对齐到同一时间轴。

    日期取**并集**（各自区间外为 null），不是交集 ——
    否则"全区间基线 vs 半年期新版本"的对比会退化成只有半年。
    """
    parsed: list[tuple[str, str]] = []
    for raw in refs.split(","):
        raw = raw.strip()
        if not raw:
            continue
        if ":" not in raw:
            raise InvalidParamError(
                f"refs 需要 `strategy:run_id` 形式，收到：{raw!r}",
                detail={"refs": refs},
            )
        s, r = raw.split(":", 1)
        parsed.append((s.strip(), r.strip()))
    return ApiResponse.ok(svc.compare_runs(parsed, max_points=max_points))


# ======================================================================
# 运行详情
# ======================================================================
@router.get("/runs/{strategy}/{run_id}", summary="运行详情（配置 + 指标 + 月度收益）")
def get_run(strategy: str, run_id: str) -> ApiResponse[dict]:
    """详情里会带 `notes` —— 其中最重要的是**调试运行标记**。

    一个只跑了 3 只标的的 run，产物结构、指标栏位和全市场 run 完全一样。
    不显式标记，就会有人拿它评估策略。
    """
    return ApiResponse.ok(svc.get_run(strategy, run_id))


@router.get("/runs/{strategy}/{run_id}/equity", summary="净值曲线（LTTB 降采样）")
def get_equity(
    strategy: str,
    run_id: str,
    max_points: int = Query(
        svc.DEFAULT_MAX_POINTS, ge=50, le=20000, description="降采样后的最大点数"
    ),
) -> ApiResponse[dict]:
    """列式返回（`series.date` / `series.nav` / …），比行式省一半字节。

    1115 个交易日 ≈ 1115 点；模拟盘到分钟级会有十万级 —— 必须服务端降采样。
    """
    return ApiResponse.ok(svc.get_equity(strategy, run_id, max_points=max_points))


@router.get("/runs/{strategy}/{run_id}/trades", summary="交易记录（筛选 / 分页）")
def get_trades(
    strategy: str,
    run_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    symbol: str | None = Query(None, description="代码模糊匹配"),
    exit_reasons: str | None = Query(None, description="逗号分隔，如 time_stop,take_profit"),
    win: str | None = Query(None, description="win | lose | all"),
    start: str | None = Query(None, description="买入日 >= YYYY-MM-DD"),
    end: str | None = Query(None, description="买入日 <= YYYY-MM-DD"),
    sort: str = Query("entry_date"),
    desc: bool = Query(True),
) -> ApiResponse[dict]:
    """`summary` 统计的是**筛选后的全量**，不是当前页 —— 否则翻页数字会变。"""
    reasons = [s.strip() for s in (exit_reasons or "").split(",") if s.strip()]
    return ApiResponse.ok(
        svc.get_trades(
            strategy,
            run_id,
            page=page,
            page_size=page_size,
            symbol=symbol,
            exit_reasons=reasons,
            win=None if win in (None, "all") else win,
            start=start,
            end=end,
            sort=sort,
            desc=desc,
        )
    )


@router.get("/runs/{strategy}/{run_id}/orders", summary="委托流水（含被拒/顺延）")
def get_orders(
    strategy: str,
    run_id: str,
    status: str | None = Query(None, description="filled | rejected | pending"),
    limit: int = Query(500, ge=1, le=5000),
) -> ApiResponse[dict]:
    """排查「跌停卖不掉」「停牌顺延」的唯一凭证。

    顺延记录（`status=pending`）在净值曲线上看不出来，必须翻这张表。
    """
    return ApiResponse.ok(svc.get_orders(strategy, run_id, status=status, limit=limit))
