"""外部模型编排集成测试（外部模型 API 接入 V1 阶段 3）。

- 项目配置切换 provider_connection_id：保存校验、版本化、缓存隔离
- 远程 Provider 失败（401/超时）→ /inference/rewrite 仍 200 回退原文，
  final_route 恒为原文分类
- RewriteClient 熔断按连接隔离：一个连接 unhealthy 不影响 builtin
"""
from __future__ import annotations

import base64
import json
import os

import httpx
import pytest

from app.query_rewrite.client import RewriteClient
from app.query_rewrite.provider import (
    ProviderAuthError,
    ProviderRateLimited,
    ProviderReply,
    ProviderTimeout,
    ProviderUnavailable,
)
from app.query_rewrite.schemas import ProviderOutput
from app.services import provider_connection_service as svc
from app.services import rewrite_service


@pytest.fixture(autouse=True)
def _master_key(monkeypatch):
    monkeypatch.setenv("REWRITE_CREDENTIAL_MASTER_KEY", base64.b64encode(os.urandom(32)).decode())


def _create_glm(db) -> object:
    return svc.create_connection(db, {
        "name": "GLM 集成", "provider_type": "glm", "model_id": "glm-5.2",
        "api_key": "zhipu-secret-key-abcd1234", "egress_acknowledged": True,
    })


def _reply(connection_id: str, revision: int = 1) -> ProviderReply:
    return ProviderReply(
        output=ProviderOutput(
            standalone_query="如何停止实验 test-123？", confidence=0.95,
            rewrite_type="context_resolution", reason_codes=["RESOLVED_PRONOUN"],
        ),
        latency_ms=42.0, provider="glm", model_id="glm-5.2", prompt_version="p",
        request_id="req_glm_1", usage=None,
        connection_id=connection_id, connection_revision=revision,
    )


class _FakeClient:
    """替身 RewriteClient：可编程的响应队列与调用统计。"""

    def __init__(self, outcomes: dict[str, list]) -> None:
        self.outcomes = outcomes
        self.calls: list[dict] = []

    def rewrite(self, query, context, terminology=None, timeout_ms=None,
                provider_connection_id=None, provider_connection_revision=None):
        self.calls.append({
            "query": query, "connection_id": provider_connection_id,
            "revision": provider_connection_revision,
        })
        queue = self.outcomes[provider_connection_id or "builtin:local_qwen"]
        if not queue:
            raise AssertionError("意外的额外调用（缓存未命中？）")
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


# ---------------------------------------------------------------- 配置保存

def test_config_accepts_valid_connection(db, client, project_id):
    conn = _create_glm(db)
    resp = client.put(f"/api/v1/projects/{project_id}/rewrite-config", json={
        "config": {"mode": "shadow", "provider_connection_id": conn.id},
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["config"]["provider_connection_id"] == conn.id

    detail = client.get(f"/api/v1/projects/{project_id}/rewrite-config").json()
    assert detail["selected_provider"]["id"] == conn.id
    assert detail["selected_provider"]["provider_type"] == "glm"
    assert detail["selected_provider"]["revision"] == 1
    assert detail["selected_provider"]["available"] is True


def test_config_rejects_missing_or_disabled_connection(db, client, project_id):
    resp = client.put(f"/api/v1/projects/{project_id}/rewrite-config", json={
        "config": {"mode": "shadow", "provider_connection_id": "rpc_missing0000000000"},
    })
    assert resp.status_code == 422

    conn = _create_glm(db)
    conn.enabled = False
    db.commit()
    resp = client.put(f"/api/v1/projects/{project_id}/rewrite-config", json={
        "config": {"mode": "shadow", "provider_connection_id": conn.id},
    })
    assert resp.status_code == 422


def test_config_rejects_api_key_in_project_config(db, client, project_id):
    resp = client.put(f"/api/v1/projects/{project_id}/rewrite-config", json={
        "config": {"mode": "shadow", "api_key": "leak"},
    })
    assert resp.status_code == 422  # api_key 属于连接表，禁止进项目配置


def test_legacy_config_defaults_to_builtin(db, client, project_id):
    from app import ids as ids_mod
    from app.models import Project, RewriteConfigVersion

    version = RewriteConfigVersion(
        id=ids_mod.prefixed(ids_mod.REWRITE_CONFIG), project_id=project_id,
        version=1, config={"mode": "shadow"}, hash="0" * 64, status="ACTIVE",
    )
    db.add(version)
    project = db.get(Project, project_id)
    project.active_rewrite_config_id = version.id
    db.commit()
    spec = rewrite_service.get_project_rewrite_config(db, project_id)
    assert spec["config"]["provider_connection_id"] == "builtin:local_qwen"
    assert spec["provider"]["builtin"] is True


# ---------------------------------------------------------------- 编排链路

@pytest.fixture
def runtime(project_id, db):
    """复用 test_rewrite_api 的 ACTIVE 模型链构造。"""
    from app import ids as ids_mod
    from app.constants import ModelStatus
    from app.models import DatasetVersion, ModelVersion, Project, TrainingRun
    from tests.test_rewrite_api import _ScriptedRuntime  # noqa: PLC0415

    rt = _ScriptedRuntime()
    rt.model_version_id = ids_mod.prefixed(ids_mod.MODEL)
    dataset = DatasetVersion(id=ids_mod.prefixed(ids_mod.DATASET), project_id=project_id,
                             status="FROZEN", parquet_path="/nonexistent/x.parquet")
    db.add(dataset)
    db.flush()
    run = TrainingRun(id=ids_mod.prefixed(ids_mod.RUN), project_id=project_id,
                      dataset_id=dataset.id, config={}, status="SUCCEEDED")
    db.add(run)
    db.flush()
    model = ModelVersion(id=rt.model_version_id, project_id=project_id, run_id=run.id,
                         status=ModelStatus.ACTIVE, artifact_path="/nonexistent/m",
                         manifest_hash="0" * 64, manifest={})
    db.add(model)
    db.flush()
    project = db.get(Project, project_id)
    project.active_model_id = model.id
    db.commit()
    from app.services import inference_service

    inference_service.RUNTIME.set(project_id, rt)
    yield rt
    inference_service.RUNTIME.evict(project_id)


def _patch_client(monkeypatch, fake):
    monkeypatch.setattr(rewrite_service, "get_client", lambda: fake)


def test_remote_success_trace_and_cache_isolation(db, project_id, runtime, monkeypatch):
    conn = _create_glm(db)
    rewrite_service.put_rewrite_config(db, project_id, {"mode": "shadow", "provider_connection_id": conn.id})
    fake = _FakeClient({conn.id: [_reply(conn.id)]})
    _patch_client(monkeypatch, fake)

    payload = rewrite_service.understand_query(db, project_id, "这个怎么停？", "当前讨论实验 test-123", runtime)
    trace = payload["provider_trace"]
    assert trace["connection_id"] == conn.id
    assert trace["provider"] == "glm" and trace["provider_request_id"] == "req_glm_1"
    assert payload["final_route"] == payload["original_route"]["route"]
    assert payload["downstream_query_source"] == "original"  # shadow 不替换下游

    # 缓存隔离：换一个 revision（模拟连接更新）后同 Query 需重新生成
    conn.revision += 1
    db.commit()
    fake.outcomes[conn.id] = [_reply(conn.id, revision=2)]
    rewrite_service.put_rewrite_config(db, project_id, {
        "mode": "shadow", "provider_connection_id": conn.id,
    })
    payload2 = rewrite_service.understand_query(db, project_id, "这个怎么停？", "当前讨论实验 test-123", runtime)
    assert payload2["provider_trace"]["connection_revision"] == 2
    assert len(fake.calls) == 2  # 不同 revision → 不同缓存键 → 未复用


def test_remote_auth_failure_falls_back_with_reason(db, project_id, runtime, monkeypatch):
    conn = _create_glm(db)
    rewrite_service.put_rewrite_config(db, project_id, {"mode": "shadow", "provider_connection_id": conn.id})
    fake = _FakeClient({conn.id: [ProviderAuthError("401")]})
    _patch_client(monkeypatch, fake)

    payload = rewrite_service.understand_query(db, project_id, "帮我删掉实验 A", None, runtime)
    assert payload["fallback_reason"] == "PROVIDER_AUTH_FAILED"
    assert payload["downstream_query"] == "帮我删掉实验 A"
    assert payload["final_route"] == payload["original_route"]["route"]
    assert rewrite_service.METRICS["fallback_total"].get("PROVIDER_AUTH_FAILED") == 1


def test_remote_timeout_falls_back(db, project_id, runtime, monkeypatch):
    conn = _create_glm(db)
    rewrite_service.put_rewrite_config(db, project_id, {"mode": "shadow", "provider_connection_id": conn.id})
    fake = _FakeClient({conn.id: [ProviderTimeout("t")]})
    _patch_client(monkeypatch, fake)
    payload = rewrite_service.understand_query(db, project_id, "查一下进度", None, runtime)
    assert payload["fallback_reason"] == "TIMEOUT"


# ---------------------------------------------------------------- RewriteClient 按连接熔断

def _client_with(handler) -> RewriteClient:
    client = RewriteClient(base_url="http://rewriter.test")
    client._http = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://rewriter.test")
    return client


def _error(code: str, status: int, details: dict | None = None) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": "x", "details": details or {}}})


def test_breaker_isolated_per_connection():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        body = json.loads(request.content) if request.content else {}
        if body.get("provider_connection_id") == "rpc_bad":
            return _error("PROVIDER_AUTH_FAILED", 503, {"provider_connection_id": "rpc_bad"})
        return httpx.Response(200, json={
            "output": {"standalone_query": "q", "confidence": 0.9},
            "latency_ms": 5.0, "provider": "local_qwen", "model_id": "q",
            "prompt_version": "p", "usage": None, "connection_id": None, "connection_revision": None,
        })

    client = _client_with(handler)

    # 坏连接：401 → unhealthy
    with pytest.raises(ProviderAuthError):
        client.rewrite("q", None, provider_connection_id="rpc_bad", provider_connection_revision=1)
    # 第二次直接被 unhealthy 拦截（不发起 HTTP）
    with pytest.raises(ProviderUnavailable):
        client.rewrite("q", None, provider_connection_id="rpc_bad", provider_connection_revision=1)
    assert calls["n"] == 1

    # 内置连接不受影响（隔离验证）
    reply = client.rewrite("q", None)
    assert reply.output.standalone_query == "q"
    assert calls["n"] == 2

    # 恢复：清除该连接状态后可再次发起请求
    client.clear_connection_state("rpc_bad")
    with pytest.raises(ProviderAuthError):
        client.rewrite("q", None, provider_connection_id="rpc_bad", provider_connection_revision=1)
    assert calls["n"] == 3

    summary = client.breaker_summary()
    assert summary["rpc_bad"]["unhealthy_code"] == "PROVIDER_AUTH_FAILED"


def test_rate_limited_window_without_failure_count():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return _error("PROVIDER_RATE_LIMITED", 429, {"retry_after_s": 30})

    client = _client_with(handler)
    with pytest.raises(ProviderRateLimited) as exc:
        client.rewrite("q", None, provider_connection_id="rpc_rl", provider_connection_revision=1)
    assert exc.value.fallback_code == "PROVIDER_RATE_LIMITED"
    # 窗口内直接拒绝，不发起 HTTP、不计失败熔断
    with pytest.raises(ProviderRateLimited):
        client.rewrite("q", None, provider_connection_id="rpc_rl", provider_connection_revision=1)
    assert calls["n"] == 1
    assert client.breaker_summary()["rpc_rl"]["state"] == "rate_limited"
    assert client.breaker_summary()["rpc_rl"]["consecutive_failures"] == 0


def test_timeout_failures_open_breaker_after_threshold():
    def handler(request: httpx.Request) -> httpx.Response:
        return _error("TIMEOUT", 504)

    client = _client_with(handler)
    for _ in range(5):
        with pytest.raises(ProviderTimeout):
            client.rewrite("q", None, provider_connection_id="rpc_slow", provider_connection_revision=1)
    with pytest.raises(ProviderUnavailable):  # 第 6 次熔断打开，不发请求
        client.rewrite("q", None, provider_connection_id="rpc_slow", provider_connection_revision=1)


def test_invalid_request_does_not_count_toward_breaker():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return _error("PROVIDER_INVALID_REQUEST", 503)

    from app.query_rewrite.provider import ProviderBadRequest

    client = _client_with(handler)
    for _ in range(8):
        with pytest.raises(ProviderBadRequest):
            client.rewrite("q", None, provider_connection_id="rpc_badreq", provider_connection_revision=1)
    assert calls["n"] == 8  # 从不熔断打开
    assert client.breaker_summary()["rpc_badreq"]["consecutive_failures"] == 0
