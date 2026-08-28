"""模型连接 CRUD / 测试 / 删除保护集成测试（外部模型 API 接入 V1 阶段 2）。"""
from __future__ import annotations

import base64
import json
import os
import socket

import pytest

from app.models import RewriteConfigVersion, RewriteProviderConnection
from app.query_rewrite import credentials
from app.services import provider_connection_service as svc

KEY = "zhipu-secret-key-abcd1234"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("REWRITE_CREDENTIAL_MASTER_KEY", base64.b64encode(os.urandom(32)).decode())
    monkeypatch.setenv("REWRITE_ALLOW_PRIVATE_PROVIDER_URLS", "false")
    # 让 SSRF 校验对外部域名解析出公网地址（不依赖真实 DNS）
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda host, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("104.18.30.10", 0))],
    )


def _glm_payload(**overrides) -> dict:
    payload = {
        "name": "我的 GLM",
        "provider_type": "glm",
        "model_id": "glm-5.2",
        "api_key": KEY,
        "egress_acknowledged": True,
    }
    payload.update(overrides)
    return payload


def _create(client, **overrides) -> dict:
    resp = client.post("/api/v1/rewrite/provider-connections", json=_glm_payload(**overrides))
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------- 创建与列表

def test_create_glm_and_list(db, client):
    created = _create(client)
    assert created["provider_type"] == "glm"
    assert created["base_url"] == "https://open.bigmodel.cn/api/paas/v4"  # GLM 端点固定
    assert created["api_key_hint"] == "****1234"
    assert created["revision"] == 1
    assert created["last_test_status"] is None

    # 数据库只有密文：明文 Key 不落库
    row = db.get(RewriteProviderConnection, created["id"])
    assert row.api_key_ciphertext and KEY not in row.api_key_ciphertext
    assert credentials.decrypt_api_key(row.api_key_ciphertext, row.api_key_nonce, row.id, 1) == KEY

    items = client.get("/api/v1/rewrite/provider-connections").json()["items"]
    ids = [item["id"] for item in items]
    assert "builtin:local_qwen" in ids and created["id"] in ids
    # 内置连接出现在列表（rewriter 不可达时 available=False 也正常返回）
    builtin = next(i for i in items if i["id"] == "builtin:local_qwen")
    assert builtin["provider_type"] == "local_qwen" and builtin["builtin"] is True


def test_create_openai_compatible_validates_url(db, client):
    resp = client.post(
        "/api/v1/rewrite/provider-connections",
        json=_glm_payload(
            provider_type="openai_compatible",
            name="自建兼容端点",
            base_url="http://127.0.0.1:9000/v1",
        ),
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "PROVIDER_URL_FORBIDDEN"


def test_create_rejects_egress_not_acknowledged(db, client):
    resp = client.post("/api/v1/rewrite/provider-connections", json=_glm_payload(egress_acknowledged=False))
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "EGRESS_NOT_ACKNOWLEDGED"


def test_create_rejects_unknown_generation_config(db, client):
    resp = client.post(
        "/api/v1/rewrite/provider-connections",
        json=_glm_payload(generation_config={"temperature": 0.3, "custom_headers": {"x-evil": "1"}}),
    )
    assert resp.status_code == 422
    assert "generation_config" in resp.text or "custom_headers" in resp.text


def test_create_without_master_key(db, client, monkeypatch):
    monkeypatch.delenv("REWRITE_CREDENTIAL_MASTER_KEY", raising=False)
    resp = client.post("/api/v1/rewrite/provider-connections", json=_glm_payload())
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "CREDENTIAL_ENCRYPTION_NOT_CONFIGURED"
    # 未配置主密钥不影响内置本地连接的列表
    items = client.get("/api/v1/rewrite/provider-connections").json()["items"]
    assert any(i["id"] == "builtin:local_qwen" for i in items)


# ---------------------------------------------------------------- 更新

def test_patch_bumps_revision_only_for_output_fields(db, client):
    created = _create(client)
    # 仅改名：不动 revision
    resp = client.patch(f"/api/v1/rewrite/provider-connections/{created['id']}", json={"name": "改名"})
    assert resp.status_code == 200 and resp.json()["revision"] == 1
    # 换模型：revision +1
    resp = client.patch(
        f"/api/v1/rewrite/provider-connections/{created['id']}",
        json={"model_id": "glm-5-air"},
    )
    assert resp.json()["revision"] == 2


def test_patch_reencrypts_kept_key_with_new_revision(db, client):
    created = _create(client)
    row_before = db.get(RewriteProviderConnection, created["id"])
    cipher_before = row_before.api_key_ciphertext

    client.patch(
        f"/api/v1/rewrite/provider-connections/{created['id']}",
        json={"generation_config": {"temperature": 0.2}},
    )
    db.expire_all()  # API 用独立会话提交，测试会话需失效缓存后重读
    row_after = db.get(RewriteProviderConnection, created["id"])
    assert row_after.revision == 2
    assert row_after.api_key_ciphertext != cipher_before  # AAD 绑定 revision → 必须重加密
    # 旧 Key 在新 revision 下仍可解出
    assert credentials.decrypt_api_key(
        row_after.api_key_ciphertext, row_after.api_key_nonce, row_after.id, 2
    ) == KEY


def test_patch_glm_base_url_forbidden(db, client):
    created = _create(client)
    resp = client.patch(
        f"/api/v1/rewrite/provider-connections/{created['id']}",
        json={"base_url": "https://evil.example.com/v1"},
    )
    assert resp.status_code == 422


def test_patch_empty_api_key_keeps_old(db, client):
    created = _create(client)
    resp = client.patch(
        f"/api/v1/rewrite/provider-connections/{created['id']}",
        json={"api_key": ""},
    )
    assert resp.status_code == 200
    assert resp.json()["revision"] == 1  # 空 Key = 保留旧值，不触发重加密
    assert resp.json()["api_key_hint"] == "****1234"


def test_patch_resets_test_status(db, client, monkeypatch):
    created = _create(client)
    _set_test_status(client, created["id"], monkeypatch, ok=True)
    assert client.get(f"/api/v1/rewrite/provider-connections/{created['id']}").json()["last_test_status"] == "SUCCESS"
    client.patch(f"/api/v1/rewrite/provider-connections/{created['id']}", json={"model_id": "glm-5"})
    assert client.get(f"/api/v1/rewrite/provider-connections/{created['id']}").json()["last_test_status"] is None


# ---------------------------------------------------------------- 删除保护

def test_delete_builtin_rejected(client):
    resp = client.delete("/api/v1/rewrite/provider-connections/builtin:local_qwen")
    assert resp.status_code == 422


def test_delete_referenced_returns_409_with_count(db, client, project_id):
    created = _create(client)
    db.add(RewriteConfigVersion(
        id="rwcfg_ref0000000000000000000", project_id=project_id, version=1,
        config={"mode": "shadow", "provider_connection_id": created["id"]},
        hash="0" * 64, status="ACTIVE",
    ))
    db.commit()
    resp = client.delete(f"/api/v1/rewrite/provider-connections/{created['id']}")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "PROVIDER_CONNECTION_IN_USE"
    assert resp.json()["error"]["details"]["affected_projects"] == 1
    # 切走引用后可删
    db.query(RewriteConfigVersion).filter(RewriteConfigVersion.id == "rwcfg_ref0000000000000000000").delete()
    db.commit()
    resp = client.delete(f"/api/v1/rewrite/provider-connections/{created['id']}")
    assert resp.status_code == 200
    assert client.get(f"/api/v1/rewrite/provider-connections/{created['id']}").status_code == 404


def test_clear_credential_requires_confirm(db, client):
    created = _create(client)
    resp = client.request("DELETE", f"/api/v1/rewrite/provider-connections/{created['id']}/credential", json={"confirm": False})
    assert resp.status_code == 422
    resp = client.request("DELETE", f"/api/v1/rewrite/provider-connections/{created['id']}/credential", json={"confirm": True})
    assert resp.status_code == 200
    body = resp.json()
    assert body["has_api_key"] is False and body["enabled"] is False
    db.expire_all()
    row = db.get(RewriteProviderConnection, created["id"])
    assert row.api_key_ciphertext is None


# ---------------------------------------------------------------- 显式测试

def _set_test_status(client, connection_id, monkeypatch, ok: bool):
    class _Fake:
        provider_name = "glm"
        model_id = "glm-5.2"

        def rewrite(self, q, c, t=None, timeout_ms=5000):
            from app.query_rewrite.provider import ProviderAuthError, ProviderReply
            from app.query_rewrite.schemas import ProviderOutput
            if not ok:
                raise ProviderAuthError("401")
            return ProviderReply(
                output=ProviderOutput(standalone_query="如何停止实验 test-123？", confidence=0.95,
                                      rewrite_type="context_resolution", reason_codes=["RESOLVED_PRONOUN"]),
                latency_ms=12.0, provider="glm", model_id="glm-5.2", prompt_version="p",
                request_id="req_fake", usage=None, connection_id=connection_id, connection_revision=1,
            )

    monkeypatch.setattr(svc, "_build_remote_provider", lambda row: _Fake())
    resp = client.post(f"/api/v1/rewrite/provider-connections/{connection_id}/test")
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_test_connection_success(db, client, monkeypatch):
    created = _create(client)
    result = _set_test_status(client, created["id"], monkeypatch, ok=True)
    assert result["status"] == "SUCCESS"
    assert result["provider_request_id"] == "req_fake"
    body = client.get(f"/api/v1/rewrite/provider-connections/{created['id']}").json()
    assert body["last_test_status"] == "SUCCESS" and body["last_test_latency_ms"] >= 0


def test_test_connection_failure_records_code(db, client, monkeypatch):
    created = _create(client)
    result = _set_test_status(client, created["id"], monkeypatch, ok=False)
    assert result["status"] == "FAILED"
    assert result["error_code"] == "PROVIDER_AUTH_FAILED"
    body = client.get(f"/api/v1/rewrite/provider-connections/{created['id']}").json()
    assert body["last_test_error_code"] == "PROVIDER_AUTH_FAILED"


def test_test_builtin_rejected(client):
    resp = client.post("/api/v1/rewrite/provider-connections/builtin:local_qwen/test")
    assert resp.status_code == 422


def test_test_disabled_connection_rejected(db, client):
    created = _create(client)
    client.patch(f"/api/v1/rewrite/provider-connections/{created['id']}", json={"enabled": False})
    resp = client.post(f"/api/v1/rewrite/provider-connections/{created['id']}/test")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "PROVIDER_CONNECTION_DISABLED"


# ---------------------------------------------------------------- 密钥泄漏扫描

def test_no_secret_leakage_in_any_response(db, client, monkeypatch):
    created = _create(client)
    _set_test_status(client, created["id"], monkeypatch, ok=True)
    row = db.get(RewriteProviderConnection, created["id"])
    sweep = [
        json.dumps(created),
        json.dumps(client.get("/api/v1/rewrite/provider-connections").json()),
        json.dumps(client.get(f"/api/v1/rewrite/provider-connections/{created['id']}").json()),
        json.dumps(client.patch(
            f"/api/v1/rewrite/provider-connections/{created['id']}", json={"name": "再改名"},
        ).json()),
        json.dumps(client.post(f"/api/v1/rewrite/provider-connections/{created['id']}/test").json()),
    ]
    for text in sweep:
        assert KEY not in text
        assert (row.api_key_ciphertext or "") not in text
        assert "authorization" not in text.lower()
