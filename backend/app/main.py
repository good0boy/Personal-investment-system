"""FastAPI 应用入口。

启动顺序（lifespan）：
    1. 初始化日志
    2. 创建数据目录
    3. 校验 meta.db 是否已建表（未建则提示，不自动建 —— 避免隐式副作用）
    4. 加载交易日历（缺失则自动同步）
    5. 执行启动补跑扫描（本地关机会漏跑）
    6. 启动调度器

运行：
    cd backend
    uvicorn app.main:app --reload --port 8000
    或 python -m app.main
"""
from __future__ import annotations

import sys
import traceback
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from app.api.v1.router import api_router
from app.core.config import settings
from app.core.errors import AppError
from app.core.logging import logger, setup_logging
from app.core.paths import FRONTEND_DIR, PROJECT_ROOT, ensure_dirs, relative_to_project
from app.repos import db
from app.schemas.common import ApiResponse, ErrorResponse

_BOOT_INFO: dict[str, Any] = {}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期管理。"""
    # ---------- 启动 ----------
    setup_logging()
    logger.info("=" * 62)
    logger.info(f"  {settings.app.name} v{settings.app.version} 启动中")
    logger.info("=" * 62)
    logger.info(f"  环境      : {settings.env.app_env}")
    logger.info(f"  项目根    : {PROJECT_ROOT}")
    logger.info(f"  数据目录  : {relative_to_project(settings.data_dir)}")

    ensure_dirs()

    # 1) 元数据库
    if not db.is_initialized():
        logger.warning(
            "meta.db 尚未初始化。请执行：cd backend && python -m tools.init_db"
        )
        _BOOT_INFO["db_ready"] = False
    else:
        _BOOT_INFO["db_ready"] = True
        logger.info("meta.db 已就绪")

    # 2) 交易日历 + 应用初始化
    try:
        from app.scheduler.jobs import app_bootstrap_job  # noqa: PLC0415

        boot = app_bootstrap_job(__import__("datetime").date.today().strftime("%Y-%m-%d"))
        _BOOT_INFO["bootstrap"] = boot
        cal = boot.get("calendar", {})
        logger.info(
            f"交易日历就绪：{cal.get('first_date')} ~ {cal.get('last_date')}"
            f"（交易日 {cal.get('trading_days')} 天）"
        )
    except AppError as exc:
        logger.error(f"启动初始化失败：{exc.message}")
        _BOOT_INFO["bootstrap_error"] = exc.message
    except Exception as exc:
        logger.exception("启动初始化失败（未预期异常）")
        _BOOT_INFO["bootstrap_error"] = repr(exc)

    # 3) 启动补跑（★ 后台线程，不阻塞端口 —— P1-1，2026-09-22）
    #    实测补跑要 30~90 分钟（增量回补全市场行情）。此前它同步跑在 lifespan 里，
    #    导致 uvicorn 在补跑结束前**根本不监听端口**，浏览器一律"拒绝连接" ——
    #    用户看到的现象与"服务没起来"完全一样，查都无从查起。
    #    改成后台线程后：端口几秒内就绪，补跑照跑，进度可在
    #    `GET /api/v1/system/jobs` 的 `scheduler.catch_up` 里看到。
    try:
        from app.scheduler.scheduler import start_catch_up_background  # noqa: PLC0415

        _BOOT_INFO["catch_up"] = start_catch_up_background()
    except Exception as exc:
        logger.exception("启动补跑（后台）启动失败")
        _BOOT_INFO["catch_up_error"] = repr(exc)

    # 4) 调度器
    try:
        from app.scheduler.scheduler import start_scheduler  # noqa: PLC0415

        _BOOT_INFO["scheduler"] = start_scheduler()
    except Exception as exc:
        logger.exception("启动调度器失败")
        _BOOT_INFO["scheduler_error"] = repr(exc)

    # 5) 接口目录自检（后台线程，不阻塞启动）
    #    让"akshare 升级导致接口失效"在启动时就暴露，而不是用户点了才发现。
    #    用线程而非 asyncio task：check_catalog 是同步阻塞代码（含真实网络调用）。
    import threading  # noqa: PLC0415

    def _bg_catalog_check() -> None:
        try:
            from tools.check_catalog import run_check  # noqa: PLC0415

            boot_check = run_check(live_sample=0)  # 启动时只做静态检查（快，无网络）
            _BOOT_INFO["catalog_check"] = {
                k: boot_check[k] for k in ("static", "source_version")
            }
            s = boot_check["static"]
            level = logger.warning if (s["broken"] or s["missing"]) else logger.info
            level(
                f"接口目录自检（静态）：ok={s['ok']} broken={s['broken']} "
                f"missing={s['missing']}（akshare {boot_check['source_version']}）"
            )
        except Exception as exc:
            logger.warning(f"启动自检失败（不影响使用，可手动执行 POST /api/v1/catalog/check）：{exc}")

    threading.Thread(target=_bg_catalog_check, name="catalog-check", daemon=True).start()

    logger.info(f"启动完成。接口文档：http://{settings.env.host}:{settings.env.port}/docs")

    yield

    # ---------- 关闭 ----------
    logger.info("正在关闭应用...")
    try:
        from app.scheduler.scheduler import stop_scheduler  # noqa: PLC0415

        stop_scheduler()
    except Exception:
        pass
    try:
        from app.services import data_store  # noqa: PLC0415
        from app.services.data_source import shutdown_all  # noqa: PLC0415

        shutdown_all()
        data_store.close_duck()
        db.close_connection()
    except Exception:
        pass
    logger.info("应用已关闭")


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app.name,
        version=settings.app.version,
        description=(
            "A股投资本地服务 · 后端 API\n\n"
            "数据源：baostock（行情主源）+ AKShare（广度补充）\n\n"
            "阶段 0：系统底座（健康检查/调度/存储/数据源）。\n"
            "阶段 1：模块 1 数据分类（接口目录 + 在线试跑 + 数据源能力矩阵）。"
        ),
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    # CORS：本地开发用 Vite proxy 可免，但保留以便直连调试
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            f"http://{settings.env.host}:{settings.env.port}",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ---------------- 异常处理 ----------------
    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        logger.warning(f"业务异常 [{exc.code}] {request.url.path} - {exc.message}")
        body = ErrorResponse(code=exc.code, message=exc.message, detail=exc.detail)
        return JSONResponse(status_code=exc.http_status, content=body.model_dump())

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(x) for x in first.get("loc", []))
        msg = first.get("msg", "参数校验失败")
        body = ErrorResponse(
            code=1400,
            message=f"参数不合法：{loc} — {msg}" if loc else f"参数不合法：{msg}",
            detail=exc.errors()[:5],
        )
        return JSONResponse(status_code=400, content=body.model_dump())

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.error(
            f"未处理异常 {request.method} {request.url.path}\n{traceback.format_exc()}"
        )
        body = ErrorResponse(code=1999, message="服务内部错误，请查看后端日志")
        return JSONResponse(status_code=500, content=body.model_dump())

    # ---------------- 路由 ----------------
    app.include_router(api_router)

    # 根路径健康检查（供负载均衡/探针快速探测）
    @app.get("/api/health", tags=["system"], summary="健康检查（根路径别名）")
    def health_alias(deep: bool = False):
        from app.api.v1.system import _build_health  # noqa: PLC0415

        return ApiResponse.ok(_build_health(deep=deep))

    # ---------------- 前端静态托管（可选，默认关闭）----------------
    # 见 core/config.py::EnvSettings.serve_frontend 的注释：只给本地"服务模式"
    # 用（start-service.bat 会置 SERVE_FRONTEND=1），一个端口跑整站。
    # ⚠️ 注册顺序是关键：以下路由必须排在 include_router() **之后** ——
    #    FastAPI 按注册顺序匹配，/api/* 与 /docs 等显式路由先命中，
    #    SPA 兜底只接剩下的路径。反过来写会把整个 API 吃掉。
    ui_index: Path | None = None
    if settings.env.serve_frontend:
        _dist = FRONTEND_DIR / "dist"
        if (_dist / "index.html").is_file():
            ui_index = _dist / "index.html"
        else:
            logger.warning(
                f"SERVE_FRONTEND 已开启，但找不到 {relative_to_project(_dist)}"
                " —— 请先在 frontend/ 执行 npm run build"
            )

    @app.get("/", include_in_schema=False)
    def index() -> Any:
        # 开了前端托管 → 根路径直接给 UI；否则维持原有的启动信息 JSON
        if ui_index is not None:
            return FileResponse(ui_index)
        return {
            "name": settings.app.name,
            "version": settings.app.version,
            "stage": "阶段 1（模块 1 · 数据分类）",
            "docs": "/docs",
            "health": "/api/health",
            "boot": _BOOT_INFO,
        }

    if ui_index is not None:
        _dist_root = ui_index.parent.resolve()

        @app.get("/{full_path:path}", include_in_schema=False)
        async def serve_spa(full_path: str) -> FileResponse:
            """SPA 兜底：静态文件优先，未命中的路径回 index.html。

            前端用 vue-router 的 **history 模式**，直接刷新 /paper、/factors/xxx
            这类深链必须能落到 index.html，否则页面 404。
            与 deploy/nginx.conf 的 `try_files $uri $uri/ /index.html` 是同一条规则。
            """
            # API/文档路径写错时给 JSON 404，**不要**吞成前端页面 ——
            # 吞了之后前端会把 HTML 当 JSON 解析，报一个与真实原因无关的错。
            if full_path.startswith(("api", "docs", "redoc", "openapi.json")):
                raise HTTPException(status_code=404, detail=f"Not Found: /{full_path}")
            candidate = (_dist_root / full_path).resolve()
            # is_relative_to 防目录穿越（/../../secrets 之类）
            if full_path and candidate.is_file() and candidate.is_relative_to(_dist_root):
                return FileResponse(candidate)
            return FileResponse(ui_index)

        logger.info("前端托管已开启：/ 与未命中路径返回 frontend/dist")

    return app


app = create_app()


def main() -> int:
    """直接运行入口：python -m app.main"""
    import uvicorn  # noqa: PLC0415

    setup_logging()
    host = settings.env.host
    port = settings.env.port
    reload = settings.is_dev

    logger.info(f"以 {'开发' if reload else '服务'} 模式启动：http://{host}:{port}")
    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        reload=reload,
        log_config=None,        # 交给 loguru
        access_log=False,       # 减少噪声
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
