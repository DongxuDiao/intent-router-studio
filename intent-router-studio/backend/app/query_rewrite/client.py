"""API → rewriter HTTP 客户端（修改方案 §9.4）。

- 默认超时 5s；生成模型故障绝不能转化为 /predict 的 5xx
- 熔断：连续 5 次失败后打开 30s，期间直接抛 ProviderUnavailable（不发起请求）
- 线程安全；health() 供观测接口透出熔断状态
"""
from __future__ import annotations

import threading
import time
from typing import Any

import httpx

from app.query_rewrite.provider import ProviderBusy, ProviderReply, ProviderTimeout, ProviderUnavailable
from app.query_rewrite.schemas import ProviderOutput, RewriteParseError

DEFAULT_FAILURE_THRESHOLD = 5
DEFAULT_OPEN_SECONDS = 30.0


class RewriteClient:
    def __init__(
        self,
        base_url: str,
        timeout_ms: int = 5000,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        open_seconds: float = DEFAULT_OPEN_SECONDS,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_ms = timeout_ms
        self.failure_threshold = failure_threshold
        self.open_seconds = open_seconds
        self._http = httpx.Client(base_url=self.base_url, timeout=timeout_ms / 1000.0 + 1.0)
        self._lock = threading.Lock()
        self._consecutive_failures = 0
        self._opened_at: float | None = None
        self.last_error: str | None = None
        self.total_calls = 0
        self.total_failures = 0

    # ---- 熔断状态 ----
    def _breaker_state(self) -> str:
        with self._lock:
            if self._opened_at is None:
                return "closed"
            if time.monotonic() - self._opened_at >= self.open_seconds:
                return "half-open"
            return "open"

    def _record_failure(self, error: str) -> None:
        with self._lock:
            self._consecutive_failures += 1
            self.total_failures += 1
            self.last_error = error
            if self._consecutive_failures >= self.failure_threshold and self._opened_at is None:
                self._opened_at = time.monotonic()

    def _record_success(self) -> None:
        with self._lock:
            self._consecutive_failures = 0
            self._opened_at = None
            self.last_error = None

    def _check_breaker(self) -> None:
        state = self._breaker_state()
        if state == "open":
            raise ProviderUnavailable(f"熔断打开中（连续失败 ≥{self.failure_threshold}），{self.last_error}")

    # ---- 调用 ----
    def rewrite(
        self,
        original_query: str,
        context: str | None,
        terminology: dict[str, str] | None = None,
        timeout_ms: int | None = None,
    ) -> ProviderReply:
        self._check_breaker()
        self.total_calls += 1
        effective_timeout = timeout_ms or self.timeout_ms
        try:
            resp = self._http.post(
                "/rewrite",
                json={
                    "original_query": original_query,
                    "context": context,
                    "terminology": terminology,
                    "timeout_ms": effective_timeout,
                },
                timeout=effective_timeout / 1000.0 + 1.0,
            )
        except httpx.TimeoutException as exc:
            self._record_failure(f"timeout: {exc.__class__.__name__}")
            raise ProviderTimeout(f"rewriter 超时（{effective_timeout}ms）") from exc
        except httpx.HTTPError as exc:
            self._record_failure(f"unavailable: {exc.__class__.__name__}")
            raise ProviderUnavailable(f"rewriter 不可达: {exc.__class__.__name__}") from exc

        if resp.status_code == 504:
            self._record_failure("timeout: 504")
            raise ProviderTimeout("rewriter 生成超时")
        if resp.status_code in (502, 503):
            self._record_failure(f"unavailable: {resp.status_code}")
            raise ProviderUnavailable("rewriter 服务不可用")
        if resp.status_code == 429:
            # V2 §3.3：队列满是限流信号而非服务故障——不计入熔断，调用方立即回退原文
            try:
                message = resp.json().get("error", {}).get("message", "")
            except Exception:  # 代理可能返回非 JSON 体
                message = ""
            raise ProviderBusy(message or "生成队列已满")
        if resp.status_code == 422:
            # INVALID_JSON 是内容问题而非服务问题：不计入熔断
            data = resp.json().get("error", {})
            raise RewriteParseError(data.get("message", "INVALID_JSON"))
        if resp.status_code != 200:
            self._record_failure(f"http {resp.status_code}")
            raise ProviderUnavailable(f"rewriter 返回 {resp.status_code}")

        data = resp.json()
        try:
            output = ProviderOutput.model_validate(data["output"])
        except Exception as exc:
            self._record_failure(f"bad payload: {exc}")
            raise RewriteParseError(f"rewriter 响应校验失败: {exc}") from exc
        self._record_success()
        return ProviderReply(
            output=output,
            latency_ms=float(data.get("latency_ms", 0.0)),
            provider=data.get("provider", "unknown"),
            model_id=data.get("model_id", "unknown"),
            prompt_version=data.get("prompt_version", "unknown"),
        )

    def health(self) -> dict[str, Any]:
        """透传 rewriter /health；不可达时返回自身熔断状态而非抛错。"""
        state = self._breaker_state()
        info: dict[str, Any] = {
            "base_url": self.base_url,
            "breaker_state": state,
            "consecutive_failures": self._consecutive_failures,
            "total_calls": self.total_calls,
            "total_failures": self.total_failures,
            "last_error": self.last_error,
        }
        try:
            resp = self._http.get("/health", timeout=2.0)
            info["rewriter"] = resp.json() if resp.status_code == 200 else {"ok": False, "status": resp.status_code}
        except Exception as exc:  # 健康检查自身绝不抛错（含响应体解析失败）
            info["rewriter"] = {"ok": False, "error": exc.__class__.__name__}
        return info

    def reset_breaker(self) -> None:
        with self._lock:
            self._consecutive_failures = 0
            self._opened_at = None
