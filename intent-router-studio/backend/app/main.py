"""FastAPI 应用入口：路由装配、错误结构、request_id、SSE、静态前端托管。"""
from __future__ import annotations

import contextvars
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import ids
from app.api import datasets, inference, models, projects, provider_connections, rewrite, runs, system
from app.config import REPO_ROOT, get_settings
from app.db import SessionLocal, init_db
from app.errors import ApiError

logger = logging.getLogger("app")
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

MAX_BODY_INJECT_BYTES = 5 * 1024 * 1024


def _error_body(code: str, message: str, details: dict | None = None) -> dict:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details or {},
            "request_id": request_id_var.get(),
        }
    }


def _startup_load_active_models() -> None:
    """API 启动时加载各项目 ACTIVE 模型；失败不阻塞服务（设计文档 13.1）。"""
    from app.models import Project
    from app.services import inference_service

    db = SessionLocal()
    try:
        for project in db.query(Project).all():
            if not project.active_model_id:
                continue
            try:
                inference_service.ensure_project_runtime(db, project.id)
                logger.info("已加载 ACTIVE 模型 project=%s", project.id)
            except Exception as exc:
                logger.warning("加载 ACTIVE 模型失败 project=%s: %s", project.id, exc)
    finally:
        db.close()


def create_app() -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        settings.ensure_dirs()
        os.environ["HF_HOME"] = str(settings.hf_home_path)
        os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
        init_db()
        from app.services import project_service

        db = SessionLocal()
        try:
            recovery = project_service.recover_staged_project_deletions(db)
            if any(recovery.values()):
                logger.warning("项目删除恢复完成 result=%s", recovery)
        finally:
            db.close()
        _startup_load_active_models()
        logger.info("API 启动完成 artifact_root=%s", settings.artifact_root_path.name)
        yield

    app = FastAPI(
        title="Intent Router Studio",
        version="0.1.0",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )

    allowed_origins = {settings.web_origin, "http://localhost:5173", "http://127.0.0.1:5173"}
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o for o in allowed_origins if o],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def _rebuild(response, body: bytes, content: dict | None = None):
        """用已消费的 body 重建响应；content 提供时注入字段后重新序列化。"""
        from starlette.responses import Response

        headers = {
            k: v
            for k, v in response.headers.items()
            if k.lower() not in ("content-length", "transfer-encoding", "content-type")
        }
        payload = body if content is None else json.dumps(content, ensure_ascii=False).encode("utf-8")
        new_response = Response(
            content=payload,
            status_code=response.status_code,
            media_type=response.headers.get("content-type", "application/json"),
            headers=headers,
        )
        new_response.headers["X-Request-ID"] = response.headers.get("X-Request-ID", "")
        return new_response

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        rid = ids.new_request_id()
        request_id_var.set(rid)
        request.state.request_id = rid
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        # 所有 JSON 响应包含 request_id（设计文档 9）；流式响应（SSE）与错误结构除外。
        # BaseHTTPMiddleware 返回的是流式响应，无 .body 属性：对 JSON 消费迭代器后
        # 必须重建响应（含不注入的分支），否则迭代器已耗尽、客户端拿到空 body。
        content_type = response.headers.get("content-type", "")
        if "application/json" not in content_type:
            return response
        body = None
        consumed = False
        try:
            body = getattr(response, "body", None)
            if body is None and hasattr(response, "body_iterator"):
                chunks = [chunk async for chunk in response.body_iterator]
                body = b"".join(c if isinstance(c, bytes) else c.encode("utf-8") for c in chunks)
                consumed = True
            if body is None:
                return response
            data = json.loads(body) if len(body) <= MAX_BODY_INJECT_BYTES else None
            # 错误结构形如 {"error": {...}}（request_id 在 error 内）；业务对象可能自带
            # 顶层 "error" 字段（如 run.error=None），仅当其为对象时才视为错误结构。
            is_error_envelope = isinstance(data, dict) and isinstance(data.get("error"), dict)
            if isinstance(data, dict) and "request_id" not in data and not is_error_envelope:
                return _rebuild(response, body, content={**data, "request_id": rid})
        except Exception:  # 注入失败不影响原响应
            pass
        if consumed and body is not None:
            return _rebuild(response, body)
        return response

    # V2 §4.4：应用层请求体上限。带 Content-Length 的超大请求在读 body 前
    # 即刻拒绝（413）；分块流式上传兜住无 Content-Length / 谎报的请求。
    @app.middleware("http")
    async def body_limit_middleware(request: Request, call_next):
        if request.method in ("POST", "PUT", "PATCH"):
            declared = request.headers.get("content-length")
            if declared is not None:
                try:
                    size = int(declared)
                except ValueError:
                    size = -1
                limit = get_settings().max_upload_mb * 1024 * 1024 + 8 * 1024 * 1024  # multipart 开销余量
                if size > limit:
                    return JSONResponse(
                        status_code=413,
                        content=_error_body(
                            "REQUEST_BODY_TOO_LARGE",
                            f"请求体超过 {limit // (1024 * 1024)}MB 上限",
                        ),
                    )
        return await call_next(request)

    # ---- 错误结构（设计文档 9）----
    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError):
        return JSONResponse(status_code=exc.status_code, content=_error_body(exc.code, exc.message, exc.details))

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        # 本版 FastAPI 的 RequestValidationError 无 .json()；errors() 的 ctx
        # 可能含异常对象，default=str 兜底保证可序列化
        errors = json.loads(json.dumps(exc.errors(), default=str))[:20]
        return JSONResponse(
            status_code=422,
            content=_error_body("VALIDATION_ERROR", "请求参数校验失败", {"errors": errors}),
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body("HTTP_ERROR", str(exc.detail)),
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception):
        logger.exception("未处理异常 path=%s: %s", request.url.path, exc)
        return JSONResponse(
            status_code=500,
            content=_error_body("INTERNAL_ERROR", "服务内部错误", {"exception_type": type(exc).__name__}),
        )

    # ---- 路由 ----
    prefix = "/api/v1"
    app.include_router(system.router, prefix=prefix)
    app.include_router(projects.router, prefix=prefix)
    app.include_router(datasets.router, prefix=prefix)
    app.include_router(runs.router, prefix=prefix)
    app.include_router(models.router, prefix=prefix)
    app.include_router(inference.router, prefix=prefix)
    app.include_router(rewrite.router, prefix=prefix)
    app.include_router(provider_connections.router, prefix=prefix)

    # ---- 静态前端（生产模式由 FastAPI 托管构建产物，设计文档 17.3）----
    static_candidates = [
        Path(os.environ.get("FRONTEND_DIST")) if os.environ.get("FRONTEND_DIST") else None,
        REPO_ROOT / "frontend" / "dist",
        Path(__file__).resolve().parents[1] / "static",
    ]
    static_dir = next((p for p in static_candidates if p and (p / "index.html").is_file()), None)

    if static_dir is not None:
        assets_dir = static_dir / "assets"
        if assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa_fallback(full_path: str):
            if full_path.startswith(("api/", "uploads/")):
                return JSONResponse(status_code=404, content=_error_body("NOT_FOUND", f"路径不存在: /{full_path}"))
            candidate = (static_dir / full_path).resolve()
            if str(candidate).startswith(str(static_dir.resolve())) and candidate.is_file():
                return FileResponse(str(candidate))
            return FileResponse(str(static_dir / "index.html"))

        logger.info("静态前端目录: %s", static_dir)

    return app


app = create_app()
