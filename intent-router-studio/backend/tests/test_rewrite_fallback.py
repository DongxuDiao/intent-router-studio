"""超时 / 熔断 / 降级测试（修改方案 §9.4 / §16.1）。

stub rewriter（真实 FastAPI 应用）驱动端点行为；
RewriteClient 熔断状态机用 httpx.MockTransport 按脚本回放状态码。
"""
from __future__ import annotations

import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app.query_rewrite.client import RewriteClient
from app.query_rewrite.provider import (
    ProviderBusy,
    ProviderReply,
    ProviderTimeout,
    ProviderUnavailable,
    StubProvider,
)
from app.query_rewrite.schemas import RewriteParseError
from app.rewriter.main import build_rewriter_app


def _stub_reply(query: str) -> dict:
    provider = StubProvider()
    reply: ProviderReply = provider.rewrite(query, "当前讨论实验 123")
    return {
        "output": reply.output.model_dump(),
        "latency_ms": reply.latency_ms,
        "provider": reply.provider,
        "model_id": reply.model_id,
        "prompt_version": reply.prompt_version,
    }


def test_stub_provider_context_resolution():
    p = StubProvider()
    r = p.rewrite("这个怎么停？", "当前讨论实验 123")
    assert "实验 123" in r.output.standalone_query
    assert r.output.rewrite_type == "context_resolution"
    assert r.output.reason_codes == ["RESOLVED_PRONOUN"]
    assert "停" in r.output.standalone_query  # 疑问/动作语义保留


def test_stub_provider_no_context_no_change():
    p = StubProvider()
    r = p.rewrite("查看今天的日程", None)
    assert r.output.standalone_query == "查看今天的日程"
    assert r.output.rewrite_type == "none"
    assert r.output.reason_codes == ["NO_REWRITE_NEEDED"]


def test_stub_provider_failure_modes():
    with pytest.raises(ProviderUnavailable):
        StubProvider("unavailable").rewrite("q", None)
    with pytest.raises(ProviderTimeout):
        StubProvider("timeout").rewrite("q", None)


def test_rewriter_app_endpoints():
    app = build_rewriter_app(StubProvider())
    with TestClient(app) as client:
        h = client.get("/health").json()
        assert h["ok"] is True and h["stub"] is True
        r = client.post("/rewrite", json={"original_query": "这个怎么停？", "context": "当前讨论实验 123"})
        assert r.status_code == 200
        assert r.json()["output"]["rewrite_type"] == "context_resolution"
        bad = client.post("/rewrite", json={"context": "x"})  # 缺 original_query
        assert bad.status_code == 422
        assert bad.json()["error"]["code"]


def test_rewriter_app_maps_invalid_json():

    class _BadJson:
        provider_name = "stub"
        model_id = "stub"

        def health(self):
            return {"ok": True}

        def rewrite(self, q, ctx, terminology=None, timeout_ms=5000):
            raise RewriteParseError("非法 JSON")

    with TestClient(build_rewriter_app(_BadJson(), warmup=False)) as client:
        r = client.post("/rewrite", json={"original_query": "q"})
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "INVALID_JSON"


# ---------------- 熔断状态机（MockTransport 按脚本回放） ----------------

class _ScriptedServer:
    """按状态码脚本响应 /rewrite 的假 rewriter。"""

    def __init__(self, script: list[int]):
        self.script = script
        self.calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            self.calls += 1
            code = self.script[min(self.calls - 1, len(self.script) - 1)]
            if code == 200:
                return httpx.Response(200, json=_stub_reply("q"))
            return httpx.Response(code, json={"error": {"code": "X", "message": "err"}})

        self.http = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://testserver")

    @property
    def status_codes(self):
        return self.script


def _client_for(server: _ScriptedServer, **kw) -> RewriteClient:
    client = RewriteClient(base_url="http://testserver", timeout_ms=1000, failure_threshold=5, open_seconds=0.2, **kw)
    client._http = server.http
    return client


def _state(client) -> str:
    """外部模型 V1：熔断按连接隔离，内置连接的键为 builtin:local_qwen。"""
    return client.breaker_summary()["builtin:local_qwen"]["state"]

def test_breaker_opens_after_consecutive_failures():
    server = _ScriptedServer([503] * 10)
    client = _client_for(server)
    for _ in range(5):
        with pytest.raises(ProviderUnavailable):
            client.rewrite("q", None)
    assert _state(client) == "open"
    before = server.calls
    with pytest.raises(ProviderUnavailable, match="熔断"):
        client.rewrite("q", None)
    assert server.calls == before  # 打开期间不再发起请求


def test_breaker_half_open_lets_request_through():
    server = _ScriptedServer([503] * 10)
    client = _client_for(server)
    for _ in range(5):
        with pytest.raises(ProviderUnavailable):
            client.rewrite("q", None)
    time.sleep(0.25)  # 超过 open_seconds
    assert _state(client) == "half-open"
    with pytest.raises(ProviderUnavailable):
        client.rewrite("q", None)  # 放行但仍失败
    assert server.calls == 6


def test_invalid_json_does_not_trip_breaker():
    server = _ScriptedServer([422] * 10)
    client = _client_for(server)
    for _ in range(6):
        with pytest.raises(RewriteParseError):
            client.rewrite("q", None)
    assert _state(client) == "closed"  # 内容问题不计入熔断


def test_success_resets_failure_streak():
    server = _ScriptedServer([503, 503, 200, 503, 503, 503, 503])
    client = _client_for(server)
    with pytest.raises(ProviderUnavailable):
        client.rewrite("q", None)
    with pytest.raises(ProviderUnavailable):
        client.rewrite("q", None)
    assert client.breaker_summary()["builtin:local_qwen"]["consecutive_failures"] == 2
    assert client.rewrite("q", None).output.standalone_query
    assert client.breaker_summary()["builtin:local_qwen"]["consecutive_failures"] == 0
    for _ in range(4):  # streak 被重置，4 次 < 阈值 5
        with pytest.raises(ProviderUnavailable):
            client.rewrite("q", None)
    assert _state(client) == "closed"


def test_busy_429_does_not_trip_breaker():
    # V2 §3.3：队列满是限流信号——不计入熔断、不累计失败
    server = _ScriptedServer([429] * 10)
    client = _client_for(server)
    for _ in range(6):
        with pytest.raises(ProviderBusy):
            client.rewrite("q", None)
    assert _state(client) == "closed"
    assert client.breaker_summary()["builtin:local_qwen"]["total_failures"] == 0


def test_client_health_never_raises():
    server = _ScriptedServer([503])
    client = _client_for(server)
    info = client.health()
    assert info["connections"]["builtin:local_qwen"]["state"] in ("closed", "open", "half-open")
    assert "rewriter" in info
