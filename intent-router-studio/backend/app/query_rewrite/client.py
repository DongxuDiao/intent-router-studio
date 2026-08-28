"""API → rewriter HTTP 客户端（修改方案 §9.4；外部模型 V1 §8.2 升级）。

- 默认超时 5s；生成模型故障绝不能转化为 /predict 的 5xx
- 熔断按连接隔离（外部模型 V1 §8.2）：一个失效的 GLM Key 不能熔断本地
  Qwen 或其他连接。键取 connection_id（不含 revision——revision 变化时
  由连接变更回调整体清除该连接状态，新 revision 继承清零后的计数）
- 持久错误（鉴权/欠费）立即标记 unhealthy，直到连接更新或显式测试成功
- 429 速率限制不计故障，只设短暂的 rate_limited_until
- REWRITER_BUSY / INVALID_JSON 均不计入熔断
- 线程安全；health() 供观测接口透出各连接熔断摘要
"""
from __future__ import annotations

import threading
import time
from typing import Any

import httpx

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
from app.query_rewrite.schemas import ProviderOutput, RewriteParseError

DEFAULT_FAILURE_THRESHOLD = 5
DEFAULT_OPEN_SECONDS = 30.0
DEFAULT_RATE_LIMIT_SECONDS = 5.0

BUILTIN_KEY = "builtin:local_qwen"

# 持久错误：连接级 unhealthy，直到配置更新 / 测试成功
_UNHEALTHY_CODES = {
    "PROVIDER_AUTH_FAILED": "PROVIDER_AUTH_FAILED",
    "PROVIDER_FORBIDDEN": "PROVIDER_FORBIDDEN",
    "PROVIDER_QUOTA_EXCEEDED": "PROVIDER_QUOTA_EXCEEDED",
}
_RATE_LIMIT_CODES = ("PROVIDER_RATE_LIMITED", "PROVIDER_OVERLOADED")


class _ConnectionBreaker:
    """单连接熔断状态（进程内）。"""

    def __init__(self) -> None:
        self.consecutive_failures = 0
        self.total_calls = 0
        self.total_failures = 0
        self.opened_at: float | None = None
        self.last_error: str | None = None
        self.unhealthy_code: str | None = None
        self.rate_limited_until: float = 0.0

    def state(self, open_seconds: float) -> str:
        if self.unhealthy_code is not None:
            return "unhealthy"
        if self.rate_limited_until > time.monotonic():
            return "rate_limited"
        if self.opened_at is None:
            return "closed"
        if time.monotonic() - self.opened_at >= open_seconds:
            return "half-open"
        return "open"

    def summary(self, open_seconds: float) -> dict[str, Any]:
        return {
            "state": self.state(open_seconds),
            "consecutive_failures": self.consecutive_failures,
            "total_calls": self.total_calls,
            "total_failures": self.total_failures,
            "last_error": self.last_error,
            "unhealthy_code": self.unhealthy_code,
            "rate_limited": self.rate_limited_until > time.monotonic(),
        }


class RewriteClient:
    def __init__(
        self,
        base_url: str,
        timeout_ms: int = 5000,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        open_seconds: float = DEFAULT_OPEN_SECONDS,
        rate_limit_seconds: float = DEFAULT_RATE_LIMIT_SECONDS,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_ms = timeout_ms
        self.failure_threshold = failure_threshold
        self.open_seconds = open_seconds
        self.rate_limit_seconds = rate_limit_seconds
        self._http = httpx.Client(base_url=self.base_url, timeout=timeout_ms / 1000.0 + 1.0)
        # RLock：_record_* 在持锁状态下会再经 _breaker() 取状态
        self._lock = threading.RLock()
        self._breakers: dict[str, _ConnectionBreaker] = {}

    # ---- 连接级熔断状态 ----
    def _breaker(self, key: str) -> _ConnectionBreaker:
        with self._lock:
            breaker = self._breakers.get(key)
            if breaker is None:
                breaker = _ConnectionBreaker()
                self._breakers[key] = breaker
            return breaker

    def _check_breaker(self, key: str) -> None:
        breaker = self._breaker(key)
        state = breaker.state(self.open_seconds)
        if state == "unhealthy":
            raise ProviderUnavailable(f"连接 {key} 已标记异常（{breaker.unhealthy_code}），请更新配置或重新测试连接")
        if state == "rate_limited":
            exc = ProviderRateLimited(f"连接 {key} 处于限流窗口内")
            exc.retryable = False  # 客户端直接回退，不重试
            raise exc
        if state == "open":
            raise ProviderUnavailable(f"连接 {key} 熔断打开中（连续失败 ≥{self.failure_threshold}），{breaker.last_error}")

    def _record_failure(self, key: str, error: str) -> None:
        with self._lock:
            breaker = self._breaker(key)
            breaker.consecutive_failures += 1
            breaker.total_failures += 1
            breaker.total_calls += 1
            breaker.last_error = error
            if breaker.consecutive_failures >= self.failure_threshold and breaker.opened_at is None:
                breaker.opened_at = time.monotonic()

    def _record_success(self, key: str) -> None:
        with self._lock:
            breaker = self._breaker(key)
            breaker.consecutive_failures = 0
            breaker.opened_at = None
            breaker.last_error = None
            breaker.unhealthy_code = None  # 成功即恢复（含显式测试成功后的首请求）
            breaker.total_calls += 1

    def _mark_unhealthy(self, key: str, code: str) -> None:
        with self._lock:
            breaker = self._breaker(key)
            breaker.unhealthy_code = code
            breaker.last_error = code
            breaker.total_calls += 1
            breaker.total_failures += 1

    def _mark_rate_limited(self, key: str, seconds: float | None) -> None:
        with self._lock:
            breaker = self._breaker(key)
            breaker.rate_limited_until = time.monotonic() + min(seconds or self.rate_limit_seconds, 30.0)
            breaker.total_calls += 1

    def clear_connection_state(self, connection_id: str | None) -> None:
        """连接更新 / 删除 / 测试成功后清除该连接的熔断与限流状态。"""
        key = connection_id or BUILTIN_KEY
        with self._lock:
            self._breakers.pop(key, None)

    def breaker_summary(self) -> dict[str, Any]:
        with self._lock:
            summary = {key: breaker.summary(self.open_seconds) for key, breaker in self._breakers.items()}
            # 内置连接始终出现在摘要中（未调用过 = closed 全零）
            summary.setdefault(BUILTIN_KEY, _ConnectionBreaker().summary(self.open_seconds))
            return summary

    # ---- 调用 ----
    def rewrite(
        self,
        original_query: str,
        context: str | None,
        terminology: dict[str, str] | None = None,
        timeout_ms: int | None = None,
        provider_connection_id: str | None = None,
        provider_connection_revision: int | None = None,
    ) -> ProviderReply:
        key = provider_connection_id or BUILTIN_KEY
        self._check_breaker(key)
        effective_timeout = timeout_ms or self.timeout_ms
        try:
            resp = self._http.post(
                "/rewrite",
                json={
                    "original_query": original_query,
                    "context": context,
                    "terminology": terminology,
                    "timeout_ms": effective_timeout,
                    "provider_connection_id": provider_connection_id,
                    "provider_connection_revision": provider_connection_revision,
                },
                timeout=effective_timeout / 1000.0 + 1.0,
            )
        except httpx.TimeoutException as exc:
            self._record_failure(key, f"timeout: {exc.__class__.__name__}")
            raise ProviderTimeout(f"rewriter 超时（{effective_timeout}ms）") from exc
        except httpx.HTTPError as exc:
            self._record_failure(key, f"unavailable: {exc.__class__.__name__}")
            raise ProviderUnavailable(f"rewriter 不可达: {exc.__class__.__name__}") from exc

        code, details = self._error_envelope(resp)

        if resp.status_code == 504 or code == "TIMEOUT":
            self._record_failure(key, "timeout")
            raise ProviderTimeout("rewriter 生成超时")
        if code in _UNHEALTHY_CODES:
            self._mark_unhealthy(key, code)
            self._raise_persistent(code, details)
        if code in _RATE_LIMIT_CODES:
            retry_after = details.get("retry_after_s") if isinstance(details, dict) else None
            self._mark_rate_limited(key, float(retry_after) if retry_after else None)
            exc = ProviderRateLimited(f"远程模型限流（{code}）")
            exc.fallback_code = code
            exc.retryable = False
            raise exc
        if code == "PROVIDER_INVALID_REQUEST":
            # 请求参数问题（含模型 ID 不存在等）：不重试不熔断，提示改配置。
            # 注意必须先于通用 503 分支判断（rewriter 对该错误也返回 503）
            with self._lock:
                self._breaker(key).total_calls += 1
            raise ProviderBadRequest(details.get("message", "PROVIDER_INVALID_REQUEST") if isinstance(details, dict) else code)
        if resp.status_code == 503 or code == "PROVIDER_UNAVAILABLE":
            self._record_failure(key, f"unavailable: {details.get('code') if isinstance(details, dict) else resp.status_code}")
            raise ProviderUnavailable("rewriter / 远程模型不可用")
        if resp.status_code == 429 or code == "REWRITER_BUSY":
            # V2 §3.3：队列满是限流信号而非服务故障——不计入熔断，调用方立即回退原文
            message = details.get("message", "") if isinstance(details, dict) else ""
            with self._lock:
                self._breaker(key).total_calls += 1
            raise ProviderBusy(message or "生成队列已满")
        if resp.status_code == 422 or code == "INVALID_JSON":
            # INVALID_JSON 是内容问题而非服务问题：不计入熔断
            message = details.get("message", "") if isinstance(details, dict) else ""
            with self._lock:
                self._breaker(key).total_calls += 1
            raise RewriteParseError(message or "INVALID_JSON")
        if resp.status_code != 200:
            self._record_failure(key, f"http {resp.status_code}")
            raise ProviderUnavailable(f"rewriter 返回 {resp.status_code}")

        data = resp.json()
        try:
            output = ProviderOutput.model_validate(data["output"])
        except Exception as exc:
            self._record_failure(key, f"bad payload: {exc}")
            raise RewriteParseError(f"rewriter 响应校验失败: {exc}") from exc
        self._record_success(key)
        usage_raw = data.get("usage")
        from app.query_rewrite.provider import ProviderUsage

        usage = None
        if isinstance(usage_raw, dict):
            usage = ProviderUsage(**{k: usage_raw.get(k) for k in ("prompt_tokens", "completion_tokens", "total_tokens")})
        return ProviderReply(
            output=output,
            latency_ms=float(data.get("latency_ms", 0.0)),
            provider=data.get("provider", "unknown"),
            model_id=data.get("model_id", "unknown"),
            prompt_version=data.get("prompt_version", "unknown"),
            request_id=data.get("request_id"),
            usage=usage,
            connection_id=data.get("connection_id") or provider_connection_id,
            connection_revision=data.get("connection_revision") or provider_connection_revision,
        )

    def _raise_persistent(self, code: str, details: dict | None) -> None:
        message = details.get("message", "") if isinstance(details, dict) else ""
        if code == "PROVIDER_AUTH_FAILED":
            raise ProviderAuthError(message or "远程模型鉴权失败")
        if code == "PROVIDER_FORBIDDEN":
            exc = ProviderAuthError(message or "远程模型禁止访问")
            exc.fallback_code = "PROVIDER_FORBIDDEN"
            raise exc
        raise ProviderQuotaExceeded(message or "远程模型额度耗尽")

    @staticmethod
    def _error_envelope(resp: httpx.Response) -> tuple[str | None, dict | None]:
        """提取统一错误结构 {error:{code,message,details}}；非 JSON 体返回 (None, None)。"""
        try:
            data = resp.json()
        except Exception:
            return None, None
        error = data.get("error") if isinstance(data, dict) else None
        if not isinstance(error, dict):
            return None, None
        return error.get("code"), {**error, "message": error.get("message", "")}

    def health(self) -> dict[str, Any]:
        """透传 rewriter /health；不可达时返回自身熔断状态而非抛错。"""
        info: dict[str, Any] = {
            "base_url": self.base_url,
            "connections": self.breaker_summary(),
        }
        try:
            resp = self._http.get("/health", timeout=2.0)
            info["rewriter"] = resp.json() if resp.status_code == 200 else {"ok": False, "status": resp.status_code}
        except Exception as exc:  # 健康检查自身绝不抛错（含响应体解析失败）
            info["rewriter"] = {"ok": False, "error": exc.__class__.__name__}
        return info

    def reset_breaker(self) -> None:
        with self._lock:
            self._breakers.clear()
