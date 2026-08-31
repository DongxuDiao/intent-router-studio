"""GLM / OpenAI-compatible Provider 单元测试（外部模型 API 接入 V1 阶段 1）。

全部使用 httpx.MockTransport，不发起真实网络请求。
覆盖：请求形状、成功映射、错误分类（401/403/429/5xx/超时/非法 JSON）、
重试 ≤1 次且受总预算约束、并发有界、响应体上限、GLM 端点固定。
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.query_rewrite.glm_provider import GLM_BASE_URL, GlmProvider
from app.query_rewrite.prompt import build_messages
from app.query_rewrite.provider import (
    ProviderAuthError,
    ProviderBadRequest,
    ProviderBusy,
    ProviderQuotaExceeded,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
)
from app.query_rewrite.remote_provider import OpenAICompatibleProvider
from app.query_rewrite.schemas import RewriteParseError

_VALID_CONTENT = json.dumps(
    {"standalone_query": "如何停止实验 test-123？", "rewrite_type": "context_resolution",
     "confidence": 0.95, "reason_codes": ["RESOLVED_PRONOUN"]},
    ensure_ascii=False,
)


def _success_body(content: str = _VALID_CONTENT) -> dict:
    return {
        "choices": [{"message": {"content": content}}],
        "request_id": "glmr-abc123",
        "model": "glm-5.2",
        "usage": {"prompt_tokens": 210, "completion_tokens": 24, "total_tokens": 234},
    }


def _glm(transport, **overrides) -> GlmProvider:
    kwargs = dict(
        connection_id="rpc_test",
        revision=1,
        base_url="https://ignored.example.com",
        model_id="glm-5.2",
        api_key="test-key-1234",
        transport=transport,
    )
    kwargs.update(overrides)
    return GlmProvider(**kwargs)


def _openai(transport, **overrides) -> OpenAICompatibleProvider:
    kwargs = dict(
        connection_id="rpc_openai",
        revision=3,
        base_url="https://api.example.com/v1",
        model_id="some-model",
        api_key="sk-test",
        transport=transport,
    )
    kwargs.update(overrides)
    return OpenAICompatibleProvider(**kwargs)


def _handler(responses: list, capture: list | None = None):
    """按顺序返回 responses；元素为 dict（JSON 响应）或 Exception。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture.append(request)
        i = calls["n"]
        calls["n"] += 1
        item = responses[min(i, len(responses) - 1)]
        if isinstance(item, Exception):
            raise item
        status = item.get("_status", 200)
        headers = item.get("_headers", {})
        body = {k: v for k, v in item.items() if not k.startswith("_")}
        return httpx.Response(status, json=body, headers=headers)

    return handler, calls


# ---------------------------------------------------------------- 请求形状

def test_glm_request_shape_and_auth():
    capture: list[httpx.Request] = []
    handler, calls = _handler([_success_body()], capture)
    provider = _glm(httpx.MockTransport(handler))
    reply = provider.rewrite("这个怎么停？", "当前讨论实验 test-123", None, timeout_ms=5000)

    req = capture[0]
    assert str(req.url).startswith(GLM_BASE_URL)
    assert req.url.path.endswith("/chat/completions")
    assert req.headers["authorization"] == "Bearer test-key-1234"
    body = json.loads(req.content)
    assert body["model"] == "glm-5.2"
    assert body["stream"] is False
    assert body["temperature"] == 0.1
    assert body["max_tokens"] == 256
    assert body["thinking"] == {"type": "disabled"}
    assert body["response_format"] == {"type": "json_object"}
    assert body["messages"] == build_messages("这个怎么停？", "当前讨论实验 test-123", None)
    # 成功映射
    assert reply.provider == "glm"
    assert reply.model_id == "glm-5.2"
    assert reply.request_id == "glmr-abc123"
    assert reply.usage and reply.usage.total_tokens == 234
    assert reply.connection_id == "rpc_test" and reply.connection_revision == 1
    assert reply.output.standalone_query == "如何停止实验 test-123？"
    assert reply.latency_ms >= 0


def test_glm_base_url_is_fixed():
    provider = _glm(httpx.MockTransport(lambda req: httpx.Response(200, json=_success_body())),
                    base_url="http://attacker.example/")
    assert provider.base_url == GLM_BASE_URL
    assert provider.base_url.endswith("/api/paas/v4")


def test_glm_thinking_enabled_omits_flag():
    capture: list[httpx.Request] = []
    handler, _ = _handler([_success_body()], capture)
    provider = _glm(httpx.MockTransport(handler), generation_config={"thinking": True})
    provider.rewrite("q", None, None, 3000)
    assert "thinking" not in json.loads(capture[0].content)


def test_openai_compatible_json_mode_off():
    capture: list[httpx.Request] = []
    handler, _ = _handler([_success_body()], capture)
    provider = _openai(httpx.MockTransport(handler), generation_config={"json_mode": False})
    provider.rewrite("q", None, None, 3000)
    assert "response_format" not in json.loads(capture[0].content)


# ---------------------------------------------------------------- 错误分类

@pytest.mark.parametrize(
    "body,status,exc_type,code",
    [
        ({"error": {"code": "1001", "message": "invalid api key"}}, 401, ProviderAuthError, "PROVIDER_AUTH_FAILED"),
        ({"error": {"code": "1220", "message": "no permission"}}, 403, ProviderAuthError, "PROVIDER_FORBIDDEN"),
        ({"error": {"code": "1113", "message": "欠费"}}, 429, ProviderQuotaExceeded, "PROVIDER_QUOTA_EXCEEDED"),
    ],
)
def test_glm_persistent_errors_no_retry(body, status, exc_type, code):
    handler, calls = _handler([{**body, "_status": status}])
    provider = _glm(httpx.MockTransport(handler))
    with pytest.raises(exc_type) as exc:
        provider.rewrite("q", None, None, 3000)
    assert exc.value.fallback_code == code
    assert exc.value.persistent is True
    assert calls["n"] == 1  # 持久错误不重试


def test_glm_429_1302_retries_once_then_succeeds():
    handler, calls = _handler([
        {"error": {"code": "1302", "message": "rate limit"}, "_status": 429, "_headers": {"retry-after": "0"}},
        _success_body(),
    ])
    provider = _glm(httpx.MockTransport(handler))
    reply = provider.rewrite("这个怎么停？", None, None, timeout_ms=8000)
    assert reply.output.standalone_query
    assert calls["n"] == 2


def test_glm_429_1305_overloaded_code():
    handler, calls = _handler([{"error": {"code": "1305"}, "_status": 429}])
    provider = _glm(httpx.MockTransport(handler))
    with pytest.raises(ProviderRateLimited) as exc:
        provider.rewrite("q", None, None, 2000)
    assert exc.value.fallback_code == "PROVIDER_OVERLOADED"
    assert calls["n"] == 2  # 429 重试一次后放弃


def test_glm_500_retries_once_then_succeeds():
    handler, calls = _handler([{"_status": 503}, _success_body()])
    provider = _glm(httpx.MockTransport(handler))
    reply = provider.rewrite("q", None, None, timeout_ms=8000)
    assert reply.request_id == "glmr-abc123"
    assert calls["n"] == 2


def test_glm_500_exhausts_after_single_retry():
    handler, calls = _handler([{"_status": 500}])
    provider = _glm(httpx.MockTransport(handler))
    with pytest.raises(ProviderUnavailable):
        provider.rewrite("q", None, None, timeout_ms=8000)
    assert calls["n"] == 2  # 1 次原始 + 1 次重试，封顶


def test_retry_skipped_when_budget_exhausted():
    # Retry-After 超过总预算：第一次 429 后直接放弃，不等待
    handler, calls = _handler([{"error": {"code": "1302"}, "_status": 429, "_headers": {"retry-after": "60"}}])
    provider = _glm(httpx.MockTransport(handler))
    with pytest.raises(ProviderRateLimited):
        provider.rewrite("q", None, None, timeout_ms=300)
    assert calls["n"] == 1


def test_timeout_not_retried():
    handler, calls = _handler([httpx.ReadTimeout("read timed out")])
    provider = _glm(httpx.MockTransport(handler))
    with pytest.raises(ProviderTimeout):
        provider.rewrite("q", None, None, timeout_ms=2000)
    assert calls["n"] == 1


def test_openai_generic_error_mapping():
    handler, calls = _handler([{"error": {"message": "bad"}, "_status": 400}])
    provider = _openai(httpx.MockTransport(handler))
    with pytest.raises(ProviderBadRequest):
        provider.rewrite("q", None, None, 3000)
    assert calls["n"] == 1


@pytest.mark.parametrize(
    "code,expected",
    [
        ("1210", "请检查模型 ID"),
        ("1211", "模型不存在"),
        ("1212", "不支持 Chat Completions"),
        ("1214", "生成参数"),
    ],
)
def test_glm_invalid_request_has_actionable_message(code, expected):
    handler, calls = _handler([{"error": {"code": code}, "_status": 400}])
    provider = _glm(httpx.MockTransport(handler))
    with pytest.raises(ProviderBadRequest) as exc:
        provider.rewrite("q", None, None, 3000)
    assert expected in str(exc.value)
    assert "glm-5.2" in str(exc.value) or code == "1214"
    assert calls["n"] == 1


def test_openai_429_quota_message_maps_to_quota():
    handler, _ = _handler([{"error": {"message": "insufficient quota"}, "_status": 429}])
    provider = _openai(httpx.MockTransport(handler))
    with pytest.raises(ProviderQuotaExceeded):
        provider.rewrite("q", None, None, 3000)


# ---------------------------------------------------------------- 输出校验

def test_invalid_json_content_raises_parse_error():
    handler, _ = _handler([_success_body(content="这不是 JSON {")])
    provider = _glm(httpx.MockTransport(handler))
    with pytest.raises(RewriteParseError):
        provider.rewrite("q", None, None, 3000)


def test_empty_choices_raises_parse_error():
    handler, _ = _handler([{"choices": []}])
    provider = _glm(httpx.MockTransport(handler))
    with pytest.raises(RewriteParseError):
        provider.rewrite("q", None, None, 3000)


def test_response_body_over_1mib_rejected():
    huge = _success_body(content="x" * (1024 * 1024 + 10))
    handler, _ = _handler([huge])
    provider = _glm(httpx.MockTransport(handler))
    with pytest.raises(ProviderBadRequest):
        provider.rewrite("q", None, None, 3000)


def test_exception_message_has_no_api_key_or_body():
    handler, _ = _handler([{"error": {"code": "1211", "message": "SECRET-DETAIL 不能透露"}, "_status": 400}])
    provider = _glm(httpx.MockTransport(handler), api_key="super-secret-key")
    with pytest.raises(ProviderBadRequest) as exc:
        provider.rewrite("q", None, None, 3000)
    assert "super-secret-key" not in str(exc.value)
    assert "SECRET-DETAIL" not in str(exc.value)
    assert "1211" in str(exc.value)  # 业务码保留，便于提示更换通用 Key


# ---------------------------------------------------------------- 并发与生命周期

def test_concurrency_limit_returns_busy():
    handler, _ = _handler([_success_body()])
    provider = _glm(httpx.MockTransport(handler), generation_config={"max_concurrency": 1})
    provider._sem.acquire()  # 占满并发
    try:
        with pytest.raises(ProviderBusy):
            provider.rewrite("q", None, None, 3000)
    finally:
        provider._sem.release()


def test_health_does_not_call_remote():
    provider = _glm(httpx.MockTransport(lambda req: pytest.fail("health 不应发起远程请求")))
    info = provider.health()
    assert info["ok"] is True
    assert info["connection_id"] == "rpc_test"
    assert "测试" in info["note"]


def test_close_closes_pool():
    provider = _glm(httpx.MockTransport(lambda req: httpx.Response(200, json=_success_body())))
    provider.close()
    assert provider._http.is_closed
