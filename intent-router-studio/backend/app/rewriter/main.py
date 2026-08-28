"""rewriter 服务入口（修改方案 §5.3）：python -m app.rewriter.main。

- 默认端口 8010，仅 Docker 内网暴露
- /health：模型状态（加载进度 / 设备 / 最近错误 / 熔断无关，熔断在 API 侧）
- /rewrite：单条改写；失败返回结构化错误（422 INVALID_JSON / 503 UNAVAILABLE / 504 TIMEOUT）
- 启动即后台预热加载模型，避免首请求承担冷启动
"""
from __future__ import annotations

import logging
import multiprocessing as mp
import os
import threading

from fastapi import FastAPI
from pydantic import BaseModel, Field

from app.errors import ApiError
from app.query_rewrite.provider import (
    ProviderBusy,
    ProviderRateLimited,
    ProviderReply,
    ProviderTimeout,
    ProviderUnavailable,
    RewriteProvider,
    StubProvider,
)
from app.query_rewrite.qwen_provider import QwenProvider
from app.query_rewrite.schemas import RewriteParseError

logger = logging.getLogger("rewriter")
logger.setLevel(logging.INFO)

REWRITE_PORT = int(os.environ.get("REWRITE_PORT", "8010"))
REWRITE_PROVIDER = os.environ.get("REWRITE_PROVIDER", "local_qwen")  # local_qwen | stub
REWRITE_MODEL_ID = os.environ.get("REWRITE_MODEL_ID", "Qwen/Qwen3-0.6B")
REWRITE_DEVICE = os.environ.get("REWRITE_DEVICE", "auto")
REWRITE_MAX_NEW_TOKENS = int(os.environ.get("REWRITE_MAX_NEW_TOKENS", "96"))


class RewriteBody(BaseModel):
    original_query: str = Field(min_length=1, max_length=4000)
    context: str | None = Field(default=None, max_length=4000)
    terminology: dict[str, str] | None = None
    timeout_ms: int = Field(default=5000, ge=200, le=120_000)  # 上限 120s：兼容纯 CPU 部署
    # 外部模型 V1 §7.3：主 API 只传连接引用，绝不传解密后的 Key；
    # 缺省（旧版本主 API）等价 builtin:local_qwen
    provider_connection_id: str | None = None
    provider_connection_revision: int | None = None


def _resolve_provider(body: RewriteBody, builtin_provider: RewriteProvider) -> RewriteProvider:
    if not body.provider_connection_id or body.provider_connection_id == "builtin:local_qwen":
        return builtin_provider
    from app.query_rewrite.provider_registry import get_registry

    return get_registry().resolve(body.provider_connection_id, body.provider_connection_revision)


def build_rewriter_app(provider: RewriteProvider, warmup: bool = True) -> FastAPI:
    """构造 rewriter FastAPI 应用；provider 可注入（测试用 stub）。"""
    app = FastAPI(title="Intent Router Rewriter", version="0.1.0", docs_url=None, openapi_url=None)
    state = {"warmup_error": None}

    def _warmup() -> None:
        try:
            warm = getattr(provider, "warmup", None)
            if warm is not None:
                warm()
            logger.info("rewriter 预热完成 provider=%s", getattr(provider, "provider_name", "?"))
        except Exception as exc:  # 预热失败不退出进程，交由 /health 呈现
            state["warmup_error"] = f"{type(exc).__name__}: {exc}"
            logger.warning("rewriter 预热失败（惰性重试）: %s", state["warmup_error"])

    @app.post("/rewrite")
    def rewrite(body: RewriteBody) -> dict:
        target = _resolve_provider(body, provider)
        conn_details = {
            "provider_connection_id": body.provider_connection_id,
            "provider_connection_revision": body.provider_connection_revision,
        }
        try:
            reply: ProviderReply = target.rewrite(
                body.original_query, body.context, body.terminology, body.timeout_ms
            )
        except RewriteParseError as exc:
            raise ApiError("INVALID_JSON", f"改写输出无法解析: {exc}", 422) from exc
        except ProviderBusy as exc:
            # V2 §3.3：有界队列满 → 429，调用方回退原文（限流信号，不是故障）
            raise ApiError("REWRITER_BUSY", str(exc), 429) from exc
        except ProviderTimeout as exc:
            raise ApiError("TIMEOUT", str(exc), 504) from exc
        except ProviderRateLimited as exc:
            # 限流是短时信号：429 + 明细错误码，不计入故障熔断
            raise ApiError(
                exc.fallback_code, str(exc), 429, {**conn_details, "retry_after_s": exc.retry_after_s}
            ) from exc
        except ProviderUnavailable as exc:
            # 远程 Provider 错误子类携带各自 fallback_code（AUTH/QUOTA/INVALID_REQUEST...）
            raise ApiError(exc.fallback_code, str(exc), 503, dict(conn_details)) from exc
        except FileNotFoundError as exc:
            raise ApiError("PROVIDER_UNAVAILABLE", f"模型文件缺失: {exc}", 503) from exc
        return {
            "output": reply.output.model_dump(),
            "latency_ms": reply.latency_ms,
            "provider": reply.provider,
            "model_id": reply.model_id,
            "prompt_version": reply.prompt_version,
            "request_id": reply.request_id,
            "usage": reply.usage.model_dump() if reply.usage else None,
            "connection_id": reply.connection_id or body.provider_connection_id,
            "connection_revision": reply.connection_revision or body.provider_connection_revision,
        }

    @app.get("/health")
    def health():
        from fastapi.responses import JSONResponse

        info = dict(provider.health())
        info["warmup_error"] = state["warmup_error"]
        info["stub"] = isinstance(provider, StubProvider)
        # V2 §3.3 观测指标（有界队列 / 超时终止 / 拒绝计数）
        metrics_fn = getattr(provider, "metrics", None)
        if callable(metrics_fn):
            info["metrics"] = metrics_fn()
        # 外部模型 V1 §11：registry 缓存摘要（远程连接数 / revision）
        from app.query_rewrite.provider_registry import get_registry

        info["registry"] = get_registry().snapshot()
        return JSONResponse(status_code=200 if info.get("ok") else 503, content=info)

    # ApiError 结构与主 API 同构（rewriter 独立进程，需自带 handler）
    @app.exception_handler(ApiError)
    async def _api_error_handler(request, exc: ApiError):
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message, "details": exc.details or {}, "request_id": "-"}},
        )

    from fastapi.exceptions import RequestValidationError

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request, exc: RequestValidationError):
        from fastapi.responses import JSONResponse as _JR

        return _JR(
            status_code=422,
            content={"error": {"code": "VALIDATION_ERROR", "message": "请求参数校验失败", "details": {"errors": exc.errors()[:10]}, "request_id": "-"}},
        )

    if warmup and getattr(provider, "warmup", None) is not None and mp.current_process().name == "MainProcess":
        # spawn 子进程会重导入本模块；进程名判定在 bootstrap 导入阶段也可靠，
        # 避免 daemon 生成进程再次启动孙进程。
        threading.Thread(target=_warmup, daemon=True).start()
    return app


def _build_provider_from_env() -> RewriteProvider:
    if REWRITE_PROVIDER == "stub":
        return StubProvider(failure_mode=os.environ.get("REWRITE_STUB_FAILURE"))
    return QwenProvider(
        model_id=REWRITE_MODEL_ID,
        device=REWRITE_DEVICE,
        max_new_tokens=REWRITE_MAX_NEW_TOKENS,
    )


app = build_rewriter_app(_build_provider_from_env())


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=REWRITE_PORT, log_level="info")
