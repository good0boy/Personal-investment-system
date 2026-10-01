"""接口目录自检 —— 让"akshare 升级导致接口失效"从"用户点了才发现"变成"启动就看到"。

检查层次（由浅入深）
--------------------
1. **静态存在性**：akshare 里函数是否还在（`stock_a_indicator_lg` 就是这样
   在 1.18.94 里消失的 —— 两年前的教程代码会静默失效）
2. **签名一致性**：YAML 声明的参数名是否还在函数签名里（参数改名 = broken）
3. **抽样真实调用**：静态检查通过不代表能连上（网络/代理/反爬都可能挂），
   抽 N 个接口真跑一次。受 akshare 1 秒限流约束，N 个 ≈ N~3N 秒。

结果写入 meta.db.catalog_interface（状态唯一来源），API 合并 YAML 定义后返回；
UI 上 broken/missing 标红。

用法
----
    cd backend
    python -m tools.check_catalog              # 全量静态检查
    python -m tools.check_catalog --live 5     # 静态 + 抽样 5 个真实调用
    python -m tools.check_catalog --only quote_history.stock_zh_a_hist
"""
from __future__ import annotations

import argparse
import inspect
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.errors import AppError
from app.core.logging import logger, setup_logging
from app.repos import db, meta_repo
from app.services import catalog_service


def _static_check(item: catalog_service.CatalogItem) -> tuple[str, str]:
    """静态检查。返回 (status, message)，status ∈ ok | broken | missing。"""
    if item.datasource == "baostock":
        # baostock：白名单 + 方法存在性
        if item.func_name not in catalog_service._BAOSTOCK_METHODS:
            return "broken", f"baostock 方法 {item.func_name} 不在目录白名单中"
        if not hasattr(__import__(
            "app.services.data_source.baostock_client", fromlist=["baostock_client"]
        ).baostock_client, item.func_name):
            return "missing", "baostock_client 缺少该方法（代码与目录不同步）"
        return "ok", ""

    if item.datasource == "fuyao":
        # ★ fuyao（同花顺官方 REST，2026-10-01 新增）：走自己的白名单 + 方法存在性。
        #   不能落到下面"在 akshare 里找函数"的分支 —— 那会把 11 个 fuyao 条目
        #   全部误判为 missing（"akshare 中已不存在函数"），页面一片红。
        from app.services.data_source.fuyao_client import (  # noqa: PLC0415
            fuyao_client,
        )

        if item.func_name not in catalog_service._FUYAO_METHODS:
            return "broken", f"fuyao 方法 {item.func_name} 不在可试跑白名单中"
        fn = getattr(fuyao_client, item.func_name, None)
        if fn is None or not callable(fn):
            return "missing", "fuyao_client 缺少该方法（代码与目录不同步）"
        return _check_params_declared(item, fn)

    import akshare as ak  # noqa: PLC0415

    fn = getattr(ak, item.func_name, None)
    if fn is None:
        return "missing", f"akshare 中已不存在函数 {item.func_name}（版本升级被移除/改名）"
    if not callable(fn):
        return "broken", f"{item.func_name} 存在但不可调用"

    return _check_params_declared(item, fn)


def _check_params_declared(
    item: catalog_service.CatalogItem, fn: Any
) -> tuple[str, str]:
    """校验 YAML 声明的参数名确实在函数签名里（签名变更的早期预警）。"""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):  # pragma: no cover —— 极少数无签名
        return "ok", ""

    accepted: set[str] = set()
    has_var_keyword = False
    for p in sig.parameters.values():
        if p.kind is inspect.Parameter.VAR_KEYWORD:
            has_var_keyword = True
        if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY):
            accepted.add(p.name)

    declared = {str(p.get("name")) for p in item.params}
    bad = declared - accepted
    if bad and not has_var_keyword:
        return "broken", f"YAML 声明的参数 {sorted(bad)} 不在函数签名中（签名可能已变更）"
    return "ok", ""


def _pick_live_samples(items: list[catalog_service.CatalogItem], n: int) -> list[catalog_service.CatalogItem]:
    """挑选适合真实调用的接口：优先 无参/参数少 的（快、稳）。

    为什么不随机抽：试跑一次要 1~5 秒（还有 1 秒限流），抽样要花在
    "最可能给出有用结论"的接口上 —— 无参接口（如 spot_em）最能说明
    "网络通不通"，参数复杂的财务接口留给用户按需试。
    """
    ok_items = [it for it in items if it.datasource == "akshare"]
    ok_items.sort(key=lambda it: len(it.sample_params))
    return ok_items[:n]


def run_check(*, live_sample: int = 0, only: str | None = None) -> dict[str, Any]:
    """执行自检并回写状态。CLI 与 API 共用。"""
    if not db.is_initialized():
        raise AppError("meta.db 尚未初始化，请先执行 python -m tools.init_db")

    try:
        import akshare as ak  # noqa: PLC0415

        source_version = str(getattr(ak, "__version__", "unknown"))
    except ImportError:
        source_version = "unknown"

    items = catalog_service.load_items()
    if only:
        items = [it for it in items if it.id == only]
        if not items:
            raise AppError(f"接口不存在：{only}")

    results: list[dict[str, Any]] = []
    counts = {"ok": 0, "broken": 0, "missing": 0, "unchecked": 0}

    # ---- 1~2) 静态检查 ----
    for it in items:
        status, message = _static_check(it)
        counts[status] += 1
        results.append(
            {"id": it.id, "category": it.category, "func_name": it.func_name,
             "datasource": it.datasource, "status": status, "message": message,
             "kind": "static"}
        )

    # ---- 3) 抽样真实调用（只对静态检查通过的 akshare 接口）----
    live_results: list[dict[str, Any]] = []
    if live_sample > 0:
        static_ok = {r["id"] for r in results if r["status"] == "ok"}
        pool = [it for it in items if it.id in static_ok]
        for it in _pick_live_samples(pool, live_sample):
            t0 = time.perf_counter()
            try:
                # skip_rate_limit: 自检抽样的受众是后端自己，不该撞面向前端的
                # "同 IP 10 次/分钟"防滥用闸门（抽样上限 20 > 限流阈值 10，
                # 不跳过的话第 11 个起必然报"试跑太频繁"，看着像接口坏了）。
                payload = catalog_service.try_run(
                    it.id, dict(it.sample_params), force=True, skip_rate_limit=True
                )
                ms = payload.get("elapsed_ms", round((time.perf_counter() - t0) * 1000, 1))
                live_results.append(
                    {"id": it.id, "ok": True, "elapsed_ms": ms,
                     "shape": payload.get("shape")}
                )
            except AppError as exc:
                live_results.append(
                    {"id": it.id, "ok": False, "error": exc.message[:200]}
                )
            except Exception as exc:
                live_results.append(
                    {"id": it.id, "ok": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
                )
            # akshare 全局限流 1s/次，_invoke 已保证间隔，这里无需 sleep

    # ---- 4) 回写状态（静态结论为准；live 失败不降级 status —— 网络抖动≠接口失效）----
    live_fail_map = {r["id"]: r["error"] for r in live_results if not r["ok"]}
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    for r in results:
        message = r["message"]
        if r["status"] == "ok" and r["id"] in live_fail_map:
            message = f"静态检查通过，但抽样试跑失败：{live_fail_map[r['id']]}"
        meta_repo.catalog_status_upsert(
            r["id"], r["category"], r["func_name"], r["datasource"],
            r["status"],
            status_message=message,
            source_version=source_version,
        )

    # 清理 YAML 中已删除接口的残留状态
    all_ids = {it.id for it in catalog_service.load_items()}
    removed = meta_repo.catalog_status_delete_removed(all_ids)

    live_ok = sum(1 for r in live_results if r["ok"])
    summary = {
        "checked_at": now,
        "source_version": source_version,
        "static": counts,
        "live": {"requested": live_sample, "ran": len(live_results),
                 "ok": live_ok, "fail": len(live_results) - live_ok,
                 "results": live_results},
        "removed_stale_rows": removed,
        "note": "抽样试跑失败不会把接口标为 broken —— 网络抖动与接口失效是两回事；"
                "失败详情见 live.results，供人工判断。",
    }
    logger.info(
        f"[catalog_check] 静态: {counts} ｜ 抽样: {live_ok}/{len(live_results)} 成功"
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="接口目录自检")
    parser.add_argument("--live", type=int, default=0, help="抽样真实调用的接口数")
    parser.add_argument("--only", default=None, help="只检查指定 id")
    args = parser.parse_args()

    setup_logging()
    try:
        summary = run_check(live_sample=args.live, only=args.only)
    except AppError as exc:
        logger.error(f"自检失败：{exc.message}")
        return 2

    print()
    print("=" * 62)
    print("  接口目录自检完成")
    print("=" * 62)
    print(f"  akshare 版本 : {summary['source_version']}")
    print(f"  静态检查     : ok={summary['static']['ok']}  broken={summary['static']['broken']}  "
          f"missing={summary['static']['missing']}")
    if summary["live"]["ran"]:
        print(f"  抽样试跑     : {summary['live']['ok']}/{summary['live']['ran']} 成功")
        for r in summary["live"]["results"]:
            if not r["ok"]:
                print(f"    ✗ {r['id']}: {r['error']}")
    if summary["removed_stale_rows"]:
        print(f"  清理残留状态 : {summary['removed_stale_rows']} 行（YAML 中已删除的接口）")
    print("=" * 62)
    return 0 if summary["static"]["broken"] == 0 and summary["static"]["missing"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
