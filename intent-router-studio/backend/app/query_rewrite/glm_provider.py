"""智谱 GLM Provider（外部模型 API 接入 V1 §4）。

- Base URL 固定为官方通用开放平台端点，不接受用户覆盖（Coding Plan 专用
  端点仅限官方编码工具，不作为本产品接入地址）；
- 使用官方 ``zai-sdk`` / ``ZhipuAiClient`` 调用 Chat Completions；
- Bearer 鉴权、按模型能力设置 Thinking、`response_format=json_object`；
- 错误映射同时提取 HTTP 状态码与智谱业务错误码，异常消息只含类别与业务码，
  不含 API Key、完整响应体或用户原文。
"""
from __future__ import annotations

import json
import time
from typing import Any

import httpx
from zai import ZhipuAiClient
from zai.core import APIResponseError, APIStatusError, APITimeoutError

from app.query_rewrite.prompt import build_messages
from app.query_rewrite.provider import (
    ProviderAuthError,
    ProviderBadRequest,
    ProviderBusy,
    ProviderQuotaExceeded,
    ProviderRateLimited,
    ProviderReply,
    ProviderTimeout,
    ProviderUnavailable,
)
from app.query_rewrite.remote_provider import MAX_RESPONSE_BYTES, OpenAICompatibleProvider, _quota_like

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
        # 传入现有受控 httpx.Client，保留连接池、禁止重定向和测试用
        # MockTransport；SDK 内部重试关闭，由 Provider 的总预算重试统一管理。
        self._zhipu = ZhipuAiClient(
            api_key=self._api_key,
            base_url=GLM_BASE_URL,
            max_retries=0,
            http_client=self._http,
            source_channel="intent-router-studio",
        )

    def _rewrite_once(
        self,
        original_query: str,
        context: str | None,
        terminology: dict[str, str] | None,
        deadline: float,
    ) -> ProviderReply:
        """通过智谱官方 SDK 完成一次非流式生成。"""
        if not self._sem.acquire(blocking=False):
            raise ProviderBusy(f"连接 {self.connection_id} 并发已满（{self.config.max_concurrency}）")
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProviderTimeout(f"总预算已耗尽（连接 {self.connection_id}）")
            payload = self._build_payload(build_messages(original_query, context, terminology))
            try:
                response = self._zhipu.chat.completions.create(
                    **payload,
                    timeout=httpx.Timeout(
                        min(self.config.connect_timeout_ms / 1000.0, remaining),
                        read=min(self.config.read_timeout_ms / 1000.0, remaining),
                        write=min(10.0, remaining),
                        pool=min(5.0, remaining),
                    ),
                )
            except APITimeoutError as exc:
                raise ProviderTimeout("智谱 SDK 请求超时") from exc
            except APIStatusError as exc:
                self._raise_sdk_status(exc)  # 恒定抛出
                raise AssertionError("unreachable") from exc
            except APIResponseError as exc:
                raise ProviderUnavailable(f"智谱 SDK 连接失败: {exc.__class__.__name__}") from exc

            body = response.to_dict()
            if len(json.dumps(body, ensure_ascii=False).encode("utf-8")) > MAX_RESPONSE_BYTES:
                raise ProviderBadRequest("响应体超过 1MiB 上限")
            return self._parse_success(body)
        finally:
            self._sem.release()

    def _raise_sdk_status(self, exc: APIStatusError) -> None:
        """把 SDK 异常还原为产品稳定错误码，不泄露厂商原始响应。"""
        try:
            body = exc.response.json()
        except (ValueError, TypeError):
            body = {}
        retry_after = self._retry_after_seconds(exc.response)
        self._classify_error(exc.status_code, body, retry_after)

    def _build_payload(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        payload = super()._build_payload(messages)
        # GLM-5.3-Flash 是始终思考模型，传 disabled 会返回 1210：
        # “该模型始终思考，不支持关闭思考”。即使旧连接保存了 thinking=false，
        # Provider 也必须按模型能力纠正请求。
        if self.model_id.lower() == "glm-5.3-flash":
            payload["thinking"] = {"type": "enabled"}
            return payload
        # SDK 会把未传 thinking 序列化为 null；这里始终显式表达配置，避免
        # 不同模型默认值或 SDK 版本变化改变连接行为。
        payload["thinking"] = {"type": "enabled" if self.config.thinking else "disabled"}
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
        exc = ProviderUnavailable(message)
        exc.retryable = True
        return exc
