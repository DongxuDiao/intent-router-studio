"""OpenAI-compatible 远程 Provider 骨架（外部模型 API 接入 V1 §5.2）。

职责：连接池、通用 Chat Completions 请求、总超时预算内的有限重试、
错误分类基类与响应提取。GLM 等具体厂商在 glm_provider.py 覆写错误映射
与请求体扩展，不在此处堆业务分支。

硬约束：
- `timeout_ms` 是整个 Provider 调用预算（含重试与退避），不是单次请求预算；
- 只重试幂等的非流式生成请求，最多 1 次；
- 异常消息不得包含 API Key、完整响应体或用户原文。
"""
from __future__ import annotations

import json
import random
import threading
import time
from typing import Any

import httpx
from pydantic import BaseModel, Field

from app.query_rewrite.prompt import PROMPT_VERSION, build_messages
from app.query_rewrite.provider import (
    ProviderAuthError,
    ProviderBadRequest,
    ProviderBusy,
    ProviderQuotaExceeded,
    ProviderRateLimited,
    ProviderReply,
    ProviderTimeout,
    ProviderUnavailable,
    ProviderUsage,
)
from app.query_rewrite.schemas import RewriteParseError, parse_provider_output

# 响应体上限（V1 §10 SSRF/安全）：超过视为异常响应，不继续解析
MAX_RESPONSE_BYTES = 1024 * 1024
# 重试退避：基线 200ms + 0~100ms jitter；Retry-After 存在时尊重该值
RETRY_BACKOFF_BASE_S = 0.2
RETRY_BACKOFF_JITTER_S = 0.1


class GenerationConfig(BaseModel):
    """连接级生成参数（V1 §6.1 白名单）；未知键在服务层拒绝，不透传。"""

    model_config = {"extra": "forbid"}

    temperature: float = Field(default=0.1, ge=0.0, le=1.0)
    max_tokens: int = Field(default=256, ge=64, le=1024)
    thinking: bool = False
    json_mode: bool = True
    connect_timeout_ms: int = Field(default=3000, ge=100, le=30_000)
    read_timeout_ms: int = Field(default=15_000, ge=200, le=120_000)
    max_retries: int = Field(default=1, ge=0, le=1)  # 计划 §4.3：最多重试 1 次
    max_concurrency: int = Field(default=2, ge=1, le=10)

    def fingerprint(self) -> str:
        """进入缓存键的稳定指纹（V1 §8.1）：只含影响生成结果的字段。"""
        import hashlib

        payload = json.dumps(
            {
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
                "thinking": self.thinking,
                "json_mode": self.json_mode,
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


_QUOTA_MARKERS = ("arrears", "欠费", "quota", "额度", "balance", "余额", "insufficient", "耗尽")


def _quota_like(code: str | None, message: str) -> bool:
    text = (message or "").lower()
    return any(marker in text for marker in _QUOTA_MARKERS)


class OpenAICompatibleProvider:
    """通用 Chat Completions Provider；每个实例绑定一个 (connection, revision)。"""

    provider_name = "openai_compatible"

    def __init__(
        self,
        *,
        connection_id: str,
        revision: int,
        base_url: str,
        model_id: str,
        api_key: str,
        generation_config: dict[str, Any] | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.connection_id = connection_id
        self.connection_revision = revision
        self.base_url = base_url.rstrip("/")
        self.model_id = model_id
        self._api_key = api_key
        self.config = GenerationConfig(**(generation_config or {}))
        limits = httpx.Limits(max_connections=max(4, self.config.max_concurrency * 2))
        self._http = httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(
                self.config.connect_timeout_ms / 1000.0,
                read=self.config.read_timeout_ms / 1000.0,
                write=10.0,
                pool=5.0,
            ),
            limits=limits,
            follow_redirects=False,  # V1 §10：禁止跟随重定向（避免绕过地址校验）
            transport=transport,
        )
        self._sem = threading.BoundedSemaphore(self.config.max_concurrency)

    # ---------------------------------------------------------------- 协议
    def health(self) -> dict[str, Any]:
        """远程连接的健康检查不发请求：正确性由显式「测试连接」验证（V1 §5.3）。"""
        return {
            "ok": True,
            "provider": self.provider_name,
            "model_id": self.model_id,
            "connection_id": self.connection_id,
            "connection_revision": self.connection_revision,
            "note": "远程连接健康状态需显式测试（POST /rewrite/provider-connections/{id}/test）",
        }

    def close(self) -> None:
        """registry 淘汰旧 revision 时关闭连接池。"""
        self._http.close()

    def rewrite(
        self,
        original_query: str,
        context: str | None,
        terminology: dict[str, str] | None = None,
        timeout_ms: int = 5000,
    ) -> ProviderReply:
        started = time.monotonic()
        budget_s = max(0.2, timeout_ms / 1000.0)
        deadline = started + budget_s
        attempts_allowed = 1 + (1 if self.config.max_retries >= 1 else 0)
        last_exc: Exception | None = None
        reply: ProviderReply | None = None
        for attempt in range(attempts_allowed):
            if attempt > 0:
                backoff = self._backoff_seconds(last_exc, deadline)
                if backoff is None:
                    raise last_exc  # 预算耗尽：不再重试
                if backoff > 0:
                    time.sleep(backoff)
            try:
                reply = self._rewrite_once(original_query, context, terminology, deadline)
                break
            except (ProviderRateLimited, ProviderUnavailable) as exc:
                if not getattr(exc, "retryable", False):
                    raise
                last_exc = exc
        if reply is None:
            raise last_exc if last_exc else ProviderUnavailable("未知错误")
        reply.latency_ms = round((time.monotonic() - started) * 1000, 2)
        return reply

    # ---------------------------------------------------------------- 单次调用
    def _rewrite_once(
        self,
        original_query: str,
        context: str | None,
        terminology: dict[str, str] | None,
        deadline: float,
    ) -> ProviderReply:
        if not self._sem.acquire(blocking=False):
            raise ProviderBusy(f"连接 {self.connection_id} 并发已满（{self.config.max_concurrency}）")
        try:
            messages = build_messages(original_query, context, terminology)
            payload = self._build_payload(messages)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProviderTimeout(f"总预算已耗尽（连接 {self.connection_id}）")
            per_call_timeout = httpx.Timeout(
                min(self.config.connect_timeout_ms / 1000.0, remaining),
                read=min(self.config.read_timeout_ms / 1000.0, remaining),
                write=min(10.0, remaining),
                pool=min(5.0, remaining),
            )
            request = self._http.build_request(
                "POST",
                "/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=per_call_timeout,
            )
            try:
                response = self._http.send(request, stream=True)
            except httpx.TimeoutException as exc:
                raise ProviderTimeout(f"远程模型超时（{exc.__class__.__name__}）") from exc
            except httpx.HTTPError as exc:
                raise ProviderUnavailable(f"远程模型不可达: {exc.__class__.__name__}") from exc
            try:
                if response.status_code != 200:
                    self._raise_for_status(response)
                body = self._read_capped(response)
            finally:
                response.close()  # 流式与缓冲响应都可安全关闭
            return self._parse_success(body)
        finally:
            self._sem.release()

    def _build_payload(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model_id,
            "messages": messages,
            "stream": False,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
        }
        if self.config.json_mode:
            payload["response_format"] = {"type": "json_object"}
        return payload

    def _read_capped(self, response: httpx.Response) -> dict[str, Any]:
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise ProviderBadRequest("响应体超过 1MiB 上限")
            chunks.append(chunk)
        try:
            return json.loads(b"".join(chunks) or b"{}")
        except json.JSONDecodeError as exc:
            raise ProviderUnavailable("远程模型返回非 JSON 响应") from exc

    def _raise_for_status(self, response: httpx.Response) -> None:
        try:
            body = json.loads(response.read() or b"{}")
        except json.JSONDecodeError:
            body = {}
        retry_after = self._retry_after_seconds(response)
        self._classify_error(response.status_code, body, retry_after)

    def _classify_error(self, status: int, body: dict[str, Any], retry_after: float | None) -> None:
        """HTTP 状态分类基类；厂商子类先按业务码映射，未命中回退到这里。"""
        error = body.get("error") if isinstance(body, dict) else None
        message = str(error.get("message", "")) if isinstance(error, dict) else ""
        if status == 401:
            raise ProviderAuthError("鉴权失败（401）")
        if status == 403:
            exc = ProviderAuthError("禁止访问（403）")
            exc.fallback_code = "PROVIDER_FORBIDDEN"
            raise exc
        if status == 429:
            if _quota_like(None, message):
                raise ProviderQuotaExceeded("额度耗尽（429）")
            raise self._rate_limited(retry_after)
        if status >= 500:
            exc = ProviderUnavailable(f"远程模型服务错误（{status}）")
            exc.retryable = True
            exc.retry_after_s = retry_after
            raise exc
        raise ProviderBadRequest(f"请求不合法（HTTP {status}）")

    def _rate_limited(self, retry_after: float | None) -> ProviderRateLimited:
        exc = ProviderRateLimited("速率限制（429）")
        exc.retry_after_s = retry_after
        return exc

    @staticmethod
    def _retry_after_seconds(response: httpx.Response) -> float | None:
        value = response.headers.get("retry-after")
        if not value:
            return None
        try:
            seconds = float(value)
        except ValueError:
            return None
        return seconds if seconds >= 0 else None

    # ---------------------------------------------------------------- 成功解析
    def _parse_success(self, body: dict[str, Any]) -> ProviderReply:
        choices = body.get("choices") if isinstance(body, dict) else None
        if not isinstance(choices, list) or not choices:
            raise RewriteParseError("choices 为空")
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            raise RewriteParseError("message.content 为空")
        output = parse_provider_output(content)
        usage_raw = body.get("usage") if isinstance(body, dict) else None
        usage = None
        if isinstance(usage_raw, dict):
            usage = ProviderUsage(
                prompt_tokens=usage_raw.get("prompt_tokens"),
                completion_tokens=usage_raw.get("completion_tokens"),
                total_tokens=usage_raw.get("total_tokens"),
            )
        return ProviderReply(
            output=output,
            latency_ms=0.0,  # 由调用方按整体预算计时（见 rewrite() 的 started）
            provider=self.provider_name,
            model_id=self.model_id,
            prompt_version=PROMPT_VERSION,
            request_id=str(body.get("request_id")) if body.get("request_id") is not None else None,
            usage=usage,
            connection_id=self.connection_id,
            connection_revision=self.connection_revision,
        )

    # ---------------------------------------------------------------- 重试
    def _backoff_seconds(self, exc: Exception | None, deadline: float) -> float | None:
        """返回重试前等待秒数；预算不足返回 None（放弃重试）。"""
        if exc is None:
            return RETRY_BACKOFF_BASE_S
        waited = exc.retry_after_s if getattr(exc, "retry_after_s", None) is not None else RETRY_BACKOFF_BASE_S
        backoff = min(max(waited, 0.0), 30.0) + random.uniform(0.0, RETRY_BACKOFF_JITTER_S)
        if time.monotonic() + backoff >= deadline - 0.05:
            return None
        return backoff
