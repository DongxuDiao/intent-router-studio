"""智谱 GLM Provider（外部模型 API 接入 V1 §4）。

- Base URL 固定为官方通用开放平台端点，不接受用户覆盖（Coding Plan 专用
  端点仅限官方编码工具，不作为本产品接入地址）；
- Bearer 鉴权、`thinking.type=disabled`、`response_format=json_object`；
- 错误映射同时提取 HTTP 状态码与智谱业务错误码，异常消息只含类别与业务码，
  不含 API Key、完整响应体或用户原文。
"""
from __future__ import annotations

from typing import Any

from app.query_rewrite.provider import (
    ProviderAuthError,
    ProviderBadRequest,
    ProviderQuotaExceeded,
    ProviderRateLimited,
)
from app.query_rewrite.remote_provider import OpenAICompatibleProvider, _quota_like

GLM_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"

# 智谱业务错误码 → 错误类别（V1 §4.3 表）
_GLM_INVALID_CODES = {"1210", "1211", "1212", "1213", "1214", "1215", "1261", "1301"}
_GLM_AUTH_CODES = {"1000", "1001", "1002", "1003"}
_GLM_FORBIDDEN_CODES = {"1220"}
_GLM_RATE_LIMIT_CODES = {"1302"}
_GLM_OVERLOADED_CODES = {"1305"}
_GLM_QUOTA_CODES = {"1113"}


class GlmProvider(OpenAICompatibleProvider):
    provider_name = "glm"

    def __init__(self, **kwargs: Any) -> None:
        # GLM 类型连接的 base_url 由后端固定为官方端点，忽略调用方传入值
        kwargs["base_url"] = GLM_BASE_URL
        super().__init__(**kwargs)

    def _build_payload(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        payload = super()._build_payload(messages)
        # GLM-4.5+ 默认可能开启 Thinking；Query 改写是短结构化任务，必须显式关闭。
        # thinking=true 时省略字段，交由模型默认行为。
        if not self.config.thinking:
            payload["thinking"] = {"type": "disabled"}
        return payload

    def _classify_error(self, status: int, body: dict[str, Any], retry_after: float | None) -> None:
        error = body.get("error") if isinstance(body, dict) else None
        code = str(error.get("code", "")) if isinstance(error, dict) else ""
        message = str(error.get("message", "")) if isinstance(error, dict) else ""

        if status == 401 or code in _GLM_AUTH_CODES:
            raise ProviderAuthError(f"GLM 鉴权失败（业务码 {code or '-'}）")
        if status == 403 or code in _GLM_FORBIDDEN_CODES:
            exc = ProviderAuthError(f"GLM 禁止访问（业务码 {code or '-'}）")
            exc.fallback_code = "PROVIDER_FORBIDDEN"
            raise exc
        if status == 429 or code in _GLM_RATE_LIMIT_CODES | _GLM_OVERLOADED_CODES:
            if code in _GLM_QUOTA_CODES or (not code and _quota_like(None, message)) or _quota_like(code, message):
                raise ProviderQuotaExceeded("GLM 额度耗尽")
            exc = ProviderRateLimited(f"GLM 速率限制（业务码 {code or '-'}）")
            if code in _GLM_OVERLOADED_CODES:
                exc.fallback_code = "PROVIDER_OVERLOADED"
            exc.retry_after_s = retry_after
            raise exc
        if status >= 500:
            exc = self._server_error(f"GLM 服务错误（HTTP {status}）")
            exc.retry_after_s = retry_after
            raise exc
        # 400 / 其余业务码（含 Key 类型不匹配等）→ 请求不合法。错误信息保持
        # 白名单化，不回传厂商原始正文，但给出足以修正配置的提示。
        if code == "1210":
            detail = f"参数错误；请检查模型 ID（当前 {self.model_id}）及生成参数"
        elif code == "1211":
            detail = f"模型不存在或当前账号不可用（当前 {self.model_id}）"
        elif code == "1212":
            detail = f"模型不支持 Chat Completions（当前 {self.model_id}）"
        elif code in {"1213", "1214", "1215"}:
            detail = "请求字段缺失、非法或互斥；请检查生成参数"
        else:
            detail = "请检查模型 ID、API Key 类型及生成参数"
        raise ProviderBadRequest(f"GLM 请求不合法（业务码 {code or '-'}）：{detail}")

    def _server_error(self, message: str):
        from app.query_rewrite.provider import ProviderUnavailable

        exc = ProviderUnavailable(message)
        exc.retryable = True
        return exc
