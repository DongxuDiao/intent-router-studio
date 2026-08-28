"""改写 API 集成测试（修改方案 §16.2）。

覆盖：
- 四种模式的端到端行为（off / normalize_only / shadow / safe_apply）
- §7.4 不变量：final_route 恒为原文路由；非写→写升级一律拒绝
- §9.4 降级：provider 不可用 / 非法 JSON → 200 + 原文下游，绝不 5xx
- §20 兼容：不带 rewrite 参数的 /predict 行为完全不变
- 配置/术语版本化 + 缓存失效；批量逐条降级；反馈 hash-only 存储

运行时用规则式假路由（注入 RUNTIME），provider 用 MockTransport 包装的 stub。
"""
from __future__ import annotations

import json

import httpx
import pytest

from app import ids
from app.constants import ModelStatus
from app.models import DatasetVersion, ModelVersion, Project, TrainingRun
from app.query_rewrite.client import RewriteClient
from app.query_rewrite.provider import StubProvider
from app.services import inference_service, rewrite_service

QUESTION_MARKERS = ("怎么", "如何", "什么", "吗", "哪", "为什么", "是否", "怎样")
WRITE_VERBS = ("停止", "删除", "创建", "关闭", "修改", "停掉", "下线", "回滚")
READ_VERBS = ("查看", "查询", "看看", "看下", "列出", "读取")


def scripted_route(text: str) -> str:
    """规则式五分类：咨询 → 写 → 读 → unclear。"""
    if any(q in text for q in QUESTION_MARKERS):
        return "information"
    if any(v in text for v in WRITE_VERBS):
        return "write_action"
    if any(v in text for v in READ_VERBS):
        return "read_only"
    return "unclear"


def test_fast_context_rewrite_resolves_unique_typed_reference():
    out = rewrite_service._fast_context_rewrite(
        "帮我看看这个实验为什么没有显著", "用户正在查看实验 123 的结果页"
    )
    assert out is not None
    assert out.standalone_query == "帮我看看实验 123 为什么没有显著"
    assert out.reason_codes == ["RESOLVED_PRONOUN"]


def test_fast_context_rewrite_rejects_ambiguous_entity():
    assert rewrite_service._fast_context_rewrite(
        "看看这个实验", "对比实验 123 和实验 456"
    ) is None


class _ScriptedRuntime:
    """注入共享 RUNTIME 的假模型运行时（跳过真实模型加载）。"""

    model_version_id = "mdl_scripted0001"
    threshold_version_id = "thv_scripted0001"

    def predict(self, text, context=None, threshold_overrides=None):
        route = scripted_route(text)
        return {
            "route": route,
            "decision": "route",
            "margin": 0.42,
            "probabilities": {route: 0.71, "unclear": 0.29},
            "model_version_id": self.model_version_id,
            "latency_ms": 0.05,
        }


def _reply_payload(output: dict, provider: str = "stub", model_id: str = "stub-model") -> dict:
    return {
        "output": output,
        "latency_ms": 12.0,
        "provider": provider,
        "model_id": model_id,
        "prompt_version": "rewrite-prompt-v1",
    }


def _stub_output(standalone: str, **kw) -> dict:
    base = {
        "standalone_query": standalone,
        "rewrite_type": "context_resolution",
        "should_use": True,
        "confidence": kw.pop("confidence", 0.92),
        "preserved_intent": kw.pop("preserved_intent", True),
        "reason_codes": kw.pop("reason_codes", ["RESOLVED_PRONOUN"]),
    }
    base.update(kw)
    return base


@pytest.fixture
def runtime(project_id, db):
    rt = _ScriptedRuntime()
    rt.model_version_id = ids.prefixed(ids.MODEL)
    # V2 §3.5：运行时缓存必须与 project.active_model_id 一致，否则会被
    # ensure_project_runtime 判定为陈旧缓存弃用。这里注册一个真实 ACTIVE
    # 模型行（FK 需要完整链 Project → DatasetVersion → TrainingRun → ModelVersion）
    # 并把项目指针指向它，模型制品路径无需真实存在（运行时已注入，不会加载）。
    dataset = DatasetVersion(
        id=ids.prefixed(ids.DATASET),
        project_id=project_id,
        status="FROZEN",
        parquet_path="/nonexistent/scripted.parquet",
    )
    db.add(dataset)
    db.flush()
    run = TrainingRun(
        id=ids.prefixed(ids.RUN),
        project_id=project_id,
        dataset_id=dataset.id,
        config={},
        status="SUCCEEDED",
    )
    db.add(run)
    db.flush()
    model = ModelVersion(
        id=rt.model_version_id,
        project_id=project_id,
        run_id=run.id,
        status=ModelStatus.ACTIVE,
        artifact_path="/nonexistent/scripted-model",
        manifest_hash="0" * 64,
        manifest={},
    )
    db.add(model)
    db.flush()
    project = db.get(Project, project_id)
    project.active_model_id = model.id
    db.commit()
    inference_service.RUNTIME.set(project_id, rt)
    yield rt
    inference_service.RUNTIME.evict(project_id)
    inference_service.RUNTIME.cache.clear()


@pytest.fixture
def install_rewriter():
    """把 rewrite_service 的 HTTP 客户端替换为 MockTransport 驱动的假 rewriter。

    responses: 可调用 (body) -> httpx.Response；缺省用 StubProvider 正常应答。
    """
    rewrite_service.reset_client()

    def _install(responses=None):
        provider = StubProvider()

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/health":
                return httpx.Response(200, json={"ok": True, "provider": "stub", "model_id": "stub-rewriter"})
            body = json.loads(request.content)
            if responses is not None:
                return responses(body)
            try:
                reply = provider.rewrite(
                    body["original_query"], body.get("context"),
                    body.get("terminology"), body.get("timeout_ms", 5000),
                )
            except Exception:  # stub 失败模式转 503
                return httpx.Response(503, json={"error": {"code": "PROVIDER_UNAVAILABLE", "message": "stub"}})
            return httpx.Response(200, json=_reply_payload(reply.output.model_dump()))

        client = RewriteClient(base_url="http://testserver", timeout_ms=2000, failure_threshold=5, open_seconds=0.1)
        client._http = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://testserver")
        rewrite_service._client = client
        return client

    yield _install
    rewrite_service._client = None
    rewrite_service.CACHE.clear()


def _put_config(client, project_id, **overrides):
    # V2 §4.3 方案A：provider/model 等由部署管理，项目配置只含策略字段
    config = {
        "mode": "shadow", "timeout_ms": 5000,
        "min_rewrite_confidence": 0.8, "require_route_consistency": True,
        "fallback": "original", "store_raw_text": False,
    }
    config.update(overrides)
    resp = client.put(f"/api/v1/projects/{project_id}/rewrite-config", json={"config": config})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _put_terminology(client, project_id, terms: list[dict]):
    resp = client.put(
        f"/api/v1/projects/{project_id}/terminology",
        json={"terms": {"terms": terms}},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_busy_falls_back_to_original(client, project_id, runtime, install_rewriter):
    # V2 §3.3：队列满 429 → 回退原文，绝不 5xx，路由仍由原文决定
    install_rewriter(responses=lambda body: httpx.Response(
        429, json={"error": {"code": "REWRITER_BUSY", "message": "queue full"}}))
    _put_config(client, project_id, mode="shadow")
    data = client.post(
        "/api/v1/inference/rewrite",
        json={"project_id": project_id, "text": "这个怎么停？", "context": "当前讨论实验 123"},
    ).json()
    assert data["safety_decision"] == "fallback_original"
    assert data["fallback_reason"] == "REWRITER_BUSY"
    assert data["downstream_query"] == "这个怎么停？"
    assert data["downstream_query_source"] == "original"
    assert data["final_route"] == data["original_route"]["route"]


# ---------------------------------------------------------------- 模式行为

def test_off_mode_short_circuits(client, project_id, runtime, install_rewriter):
    install_rewriter(responses=lambda body: httpx.Response(503, json={}))  # off 不应触达 provider
    _put_config(client, project_id, mode="off")
    resp = client.post(
        "/api/v1/inference/rewrite",
        json={"project_id": project_id, "text": "这个怎么停？", "context": "当前讨论实验 123"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["safety_decision"] == "mode_off"
    assert data["downstream_query"] == "这个怎么停？"
    assert data["rewrite"]["should_use"] is False
    assert data["final_route"] == "information"


def test_normalize_only_applies_terminology_without_provider(client, project_id, runtime, install_rewriter):
    install_rewriter(responses=lambda body: httpx.Response(503, json={}))
    _put_config(client, project_id, mode="normalize_only")
    _put_terminology(client, project_id, [{"canonical": "Libra 实验", "aliases": ["libra exp"]}])
    resp = client.post(
        "/api/v1/inference/rewrite",
        json={"project_id": project_id, "text": "libra exp 看下状态"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "Libra 实验" in data["rewrite"]["standalone_query"]
    assert data["rewrite"]["rewrite_type"] == "term_normalization"
    assert "NORMALIZED_TERM" in data["rewrite"]["reason_codes"]
    assert data["rewrite"]["model"]["provider"] == "terminology"  # 未调用生成模型
    # normalize_only 与 shadow 同样不替换下游（保守）
    assert data["downstream_query"] == "libra exp 看下状态"
    assert data["downstream_query_source"] == "original"


def test_shadow_mode_runs_dual_path_without_applying(client, project_id, runtime, install_rewriter):
    install_rewriter()
    _put_config(client, project_id, mode="shadow")
    resp = client.post(
        "/api/v1/inference/rewrite",
        json={"project_id": project_id, "text": "这个怎么停？", "context": "当前讨论实验 123"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["mode"] == "shadow"
    assert data["rewrite"]["standalone_query"] != data["rewrite"]["original_query"]
    assert "实验 123" in data["rewrite"]["standalone_query"]  # 指代解析进改写文本
    assert data["route_consistent"] is True  # 咨询→咨询
    assert data["safety_decision"] == "allow_rewrite_shadow"
    assert data["downstream_query"] == "这个怎么停？"  # shadow 不替换
    assert data["downstream_query_source"] == "original"
    assert data["final_route"] == data["original_route"]["route"] == "information"


def test_safe_apply_uses_rewrite_when_safe(client, project_id, runtime, install_rewriter):
    install_rewriter()
    _put_config(client, project_id, mode="safe_apply")
    resp = client.post(
        "/api/v1/inference/rewrite",
        json={"project_id": project_id, "text": "这个怎么停？", "context": "当前讨论实验 123"},
    )
    data = resp.json()
    assert data["safety_decision"] == "allow_rewrite"
    assert data["downstream_query"] == data["rewrite"]["standalone_query"]
    assert data["downstream_query_source"] == "rewrite"
    # §7.4：即便采用改写，final_route 仍是原文路由
    assert data["final_route"] == "information"
    assert data["rewrite_route"]["route"] == "information"


def test_safe_apply_blocks_write_escalation(client, project_id, runtime, install_rewriter):
    # 篡改改写结果：咨询（原文）→ 命令（改写），模拟 false write escalation
    install_rewriter(
        responses=lambda body: httpx.Response(200, json=_reply_payload(_stub_output("帮我停止实验 123")))
    )
    _put_config(client, project_id, mode="safe_apply")
    resp = client.post(
        "/api/v1/inference/rewrite",
        json={"project_id": project_id, "text": "这个怎么停？", "context": "当前讨论实验 123"},
    )
    data = resp.json()
    assert data["original_route"]["route"] == "information"
    assert data["rewrite_route"]["route"] == "write_action"
    assert data["safety"]["escalation"] is True
    assert "ROUTE_CONFLICT" in data["safety"]["reason_codes"]
    assert data["safety"]["allow"] is False
    assert data["downstream_query"] == "这个怎么停？"  # 拒绝应用
    assert data["final_route"] == "information"  # 路由不变


# ---------------------------------------------------------------- 降级（§9.4）

def test_provider_unavailable_degrades_to_original(client, project_id, runtime, install_rewriter):
    install_rewriter(responses=lambda body: httpx.Response(503, json={"error": {"code": "X", "message": "down"}}))
    _put_config(client, project_id, mode="safe_apply")
    resp = client.post(
        "/api/v1/inference/rewrite",
        json={"project_id": project_id, "text": "这个怎么停？", "context": "当前讨论实验 123"},
    )
    assert resp.status_code == 200  # 绝不 5xx
    data = resp.json()
    assert data["safety_decision"] == "fallback_original"
    assert data["fallback_reason"] == "PROVIDER_UNAVAILABLE"
    assert data["downstream_query"] == "这个怎么停？"
    assert "PROVIDER_UNAVAILABLE" in data["rewrite"]["reason_codes"]


def test_invalid_json_degrades_without_tripping_breaker(client, project_id, runtime, install_rewriter):
    calls = {"n": 0}

    def responses(body):
        calls["n"] += 1
        return httpx.Response(422, json={"error": {"code": "INVALID_JSON", "message": "坏 JSON"}})

    client_http = install_rewriter(responses=responses)
    _put_config(client, project_id, mode="shadow")
    for _ in range(3):
        resp = client.post(
            "/api/v1/inference/rewrite",
            json={"project_id": project_id, "text": "这个怎么停？", "context": "当前讨论实验 123"},
        )
        assert resp.status_code == 200
        assert resp.json()["fallback_reason"] == "INVALID_JSON"
    assert calls["n"] == 3  # 422 不计熔断，每次都真实请求
    assert client_http.breaker_summary()["builtin:local_qwen"]["state"] == "closed"


# ---------------------------------------------------------------- /predict 兼容与集成（§20）

def test_predict_without_rewrite_unchanged(client, project_id, runtime, install_rewriter):
    install_rewriter()
    resp = client.post(
        "/api/v1/inference/predict",
        json={"project_id": project_id, "text": "这个怎么停？", "context": "当前讨论实验 123"},
    )
    assert resp.status_code == 200
    assert "query_understanding" not in resp.json()  # 不带参数 = 现有行为完全一致


def test_predict_with_rewrite_shadow_attaches_understanding(client, project_id, runtime, install_rewriter):
    install_rewriter()
    _put_config(client, project_id, mode="shadow")
    plain = client.post(
        "/api/v1/inference/predict",
        json={"project_id": project_id, "text": "这个怎么停？", "context": "当前讨论实验 123"},
    ).json()
    resp = client.post(
        "/api/v1/inference/predict",
        json={
            "project_id": project_id,
            "text": "这个怎么停？",
            "context": "当前讨论实验 123",
            "rewrite": {"enabled": True, "mode": "shadow", "include_trace": True},
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    qu = data["query_understanding"]
    assert qu["available"] is True
    assert qu["mode"] == "shadow"
    assert qu["downstream_query"] == "这个怎么停？"
    assert "实验 123" in qu["rewrite"]["standalone_query"]  # 指代解析进改写文本
    assert qu["safety"]["allow"] is True
    # 正式路由字段与不带改写时完全一致
    for key in ("route", "decision", "probabilities"):
        assert data[key] == plain[key]


def test_predict_rewrite_provider_down_never_5xx(client, project_id, runtime, install_rewriter):
    install_rewriter(responses=lambda body: httpx.Response(503, json={}))
    resp = client.post(
        "/api/v1/inference/predict",
        json={
            "project_id": project_id,
            "text": "这个怎么停？",
            "context": "当前讨论实验 123",
            "rewrite": {"enabled": True},
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["route"] == "information"  # 主响应完好
    assert data["query_understanding"]["fallback_reason"] == "PROVIDER_UNAVAILABLE"
    assert data["query_understanding"]["downstream_query"] == "这个怎么停？"


# ---------------------------------------------------------------- 缓存与配置版本

def test_cache_hit_then_config_bump_invalidates(client, project_id, runtime, install_rewriter):
    calls = {"n": 0}

    def responses(body):
        calls["n"] += 1
        reply = StubProvider().rewrite(body["original_query"], body.get("context"))
        return httpx.Response(200, json=_reply_payload(reply.output.model_dump()))

    install_rewriter(responses=responses)
    _put_config(client, project_id, mode="shadow")
    payload = {"project_id": project_id, "text": "这个怎么停？", "context": "当前讨论实验 123"}
    first = client.post("/api/v1/inference/rewrite", json=payload).json()
    second = client.post("/api/v1/inference/rewrite", json=payload).json()
    assert first["cache_hit"] is False
    assert second["cache_hit"] is True
    assert calls["n"] == 1
    # 配置新版本 → 缓存失效
    _put_config(client, project_id, mode="shadow", min_rewrite_confidence=0.85)
    third = client.post("/api/v1/inference/rewrite", json=payload).json()
    assert third["cache_hit"] is False
    assert calls["n"] == 2


def test_terminology_versioning(client, project_id, runtime, install_rewriter):
    install_rewriter()
    v1 = _put_terminology(client, project_id, [{"canonical": "Libra 实验", "aliases": ["libra exp"]}])
    assert v1["version"] == 1
    v2 = _put_terminology(client, project_id, [{"canonical": "Libra 平台", "aliases": ["libra exp"]}])
    assert v2["version"] == 2
    got = client.get(f"/api/v1/projects/{project_id}/terminology").json()
    assert got["active"]["terms"][0]["canonical"] == "Libra 平台"
    assert len(got["versions"]) == 2


def test_config_validation_rejects_bad_values(client, project_id):
    resp = client.put(
        f"/api/v1/projects/{project_id}/rewrite-config",
        json={"config": {"mode": "yolo", "min_rewrite_confidence": 5, "timeout_ms": 10}},
    )
    assert resp.status_code == 422
    problems = resp.json()["error"]["details"]["problems"]
    assert any("mode" in p for p in problems)
    assert any("min_rewrite_confidence" in p for p in problems)
    # validate 端点不落库
    resp = client.post(
        f"/api/v1/projects/{project_id}/rewrite-config/validate",
        json={"config": {"mode": "shadow"}},
    )
    assert resp.json()["valid"] is True


# ---------------------------------------------------------------- V2 §4.3 方案A

def test_config_rejects_deployment_owned_keys(client, project_id):
    """provider/model 等由部署管理：项目级保存必须被拒绝，避免假配置。"""
    resp = client.put(
        f"/api/v1/projects/{project_id}/rewrite-config",
        json={"config": {"mode": "shadow", "provider": "local_qwen", "model_id": "Qwen/Qwen3-0.6B"}},
    )
    assert resp.status_code == 422
    problems = resp.json()["error"]["details"]["problems"]
    assert any("部署配置管理" in p and "model_id" in p for p in problems)

    resp = client.post(
        f"/api/v1/projects/{project_id}/rewrite-config/validate",
        json={"config": {"mode": "shadow", "max_new_tokens": 999, "device": "cuda"}},
    )
    assert resp.json()["valid"] is False
    assert any("max_new_tokens" in p for p in resp.json()["problems"])


def test_config_get_strips_legacy_deployment_keys(client, project_id, db):
    """历史版本里残留的部署字段在读取时剥离，只暴露策略字段。"""
    from app.models import Project, RewriteConfigVersion
    from app.services import rewrite_service as svc
    from app.utils import ids as ids_mod

    legacy = RewriteConfigVersion(
        id=ids_mod.prefixed(ids_mod.REWRITE_CONFIG),
        project_id=project_id,
        version=1,
        config={"mode": "safe_apply", "provider": "local_qwen", "model_id": "Qwen/Qwen3-0.6B",
                "max_new_tokens": 256, "timeout_ms": 8000},
        hash="0" * 64,
        status="ACTIVE",
    )
    db.add(legacy)
    db.flush()
    project = db.get(Project, project_id)
    project.active_rewrite_config_id = legacy.id
    db.commit()

    spec = svc.get_project_rewrite_config(db, project_id)
    assert spec["config"]["mode"] == "safe_apply"
    assert spec["config"]["timeout_ms"] == 8000
    for key in svc.DEPLOYMENT_OWNED_KEYS:
        assert key not in spec["config"]

    resp = client.get(f"/api/v1/projects/{project_id}/rewrite-config")
    assert resp.status_code == 200
    body = resp.json()
    assert "provider" not in body["active"]["config"]
    deployment = body["deployment"]
    assert deployment["note"]
    assert "部署环境管理" in deployment["note"]
    assert isinstance(deployment["available"], bool)


# ---------------------------------------------------------------- 批量与反馈

def test_batch_rewrite_partial_degradation(client, project_id, runtime, install_rewriter):
    calls = {"n": 0}

    def responses(body):
        calls["n"] += 1
        if body["original_query"].startswith("坏的"):
            return httpx.Response(503, json={"error": {"code": "X", "message": "down"}})
        reply = StubProvider().rewrite(body["original_query"], body.get("context"))
        return httpx.Response(200, json=_reply_payload(reply.output.model_dump()))

    install_rewriter(responses=responses)
    _put_config(client, project_id, mode="shadow")
    resp = client.post(
        "/api/v1/inference/rewrite/batch",
        json={
            "project_id": project_id,
            "items": [
                {"text": "这个怎么停？", "context": "当前讨论实验 123"},
                {"text": "坏的请求"},
                {"text": "查看今天的日程"},
            ],
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 3
    assert data["rewrite_failed_count"] == 1
    decisions = [r["safety_decision"] for r in data["results"]]
    assert "fallback_original" in decisions
    assert any(d == "allow_rewrite_shadow" for d in decisions)


def test_feedback_hash_only_by_default(client, project_id, runtime, install_rewriter):
    install_rewriter()
    resp = client.post(
        f"/api/v1/projects/{project_id}/rewrite-feedback",
        json={
            "text": "这个怎么停？",
            "context": "当前讨论实验 123",
            "proposed_rewrite": "如何停止实验 123",
            "verdict": "reject",
            "reason_codes": ["RESOLVED_PRONOUN"],
            "original_route": "information",
            "rewrite_route": "information",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["stored_raw_text"] is False
    listed = client.get(f"/api/v1/projects/{project_id}/rewrite-feedback").json()["items"]
    assert listed[0]["input_hash"]
    assert listed[0]["has_raw_text"] is False

    # 显式允许才存原文
    client.post(
        f"/api/v1/projects/{project_id}/rewrite-feedback",
        json={"text": "这个怎么停？", "verdict": "edit", "edited_rewrite": "如何停止实验 123？", "store_raw_text": True},
    )
    items = client.get(f"/api/v1/projects/{project_id}/rewrite-feedback").json()["items"]
    assert items[0]["has_raw_text"] is True


def test_rewrite_health_endpoint(client, project_id, runtime, install_rewriter):
    install_rewriter()
    resp = client.get("/api/v1/inference/rewrite/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["connections"]["builtin:local_qwen"]["state"] in ("closed", "open", "half-open")
    assert "metrics" in data
    assert "requests_total" in data["metrics"]


def test_mode_override_per_request(client, project_id, runtime, install_rewriter):
    install_rewriter()
    _put_config(client, project_id, mode="safe_apply")
    # 项目默认 safe_apply，但单条请求强制 off：不应触达 provider
    resp = client.post(
        "/api/v1/inference/rewrite",
        json={"project_id": project_id, "text": "这个怎么停？", "context": "当前讨论实验 123", "mode": "off"},
    )
    data = resp.json()
    assert data["mode"] == "off"
    assert data["safety_decision"] == "mode_off"
    assert data["downstream_query"] == "这个怎么停？"
