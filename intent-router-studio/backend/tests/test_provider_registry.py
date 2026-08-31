"""ProviderRegistry 测试（外部模型 API 接入 V1 阶段 3）。"""
from __future__ import annotations

import base64
import os

import pytest

from app.models import RewriteProviderConnection
from app.query_rewrite.provider import ProviderUnavailable, StubProvider
from app.query_rewrite.provider_registry import ProviderRegistry, set_registry
from app.services import provider_connection_service as svc


@pytest.fixture(autouse=True)
def _master_key(monkeypatch):
    monkeypatch.setenv("REWRITE_CREDENTIAL_MASTER_KEY", base64.b64encode(os.urandom(32)).decode())


class _FakeRemote:
    """假远程 Provider：记录构造与 close。"""

    built: list[_FakeRemote] = []

    def __init__(self, row) -> None:
        self.connection_id = row.id
        self.connection_revision = row.revision
        self.closed = False
        self.provider_name = "glm"
        self.model_id = row.model_id
        _FakeRemote.built.append(self)

    def health(self):
        return {"ok": True}

    def rewrite(self, q, c, t=None, timeout_ms=5000):  # pragma: no cover - 不在注册表测试中调用
        raise NotImplementedError

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _fake_build(monkeypatch):
    _FakeRemote.built = []
    monkeypatch.setattr(svc, "_build_remote_provider", lambda row: _FakeRemote(row))


@pytest.fixture
def connection(db):
    row = svc.create_connection(db, {
        "name": "GLM", "provider_type": "glm", "model_id": "glm-5.2",
        "api_key": "zhipu-secret-key-abcd1234", "egress_acknowledged": True,
    })
    return row


# ---------------------------------------------------------------- 解析与缓存

def test_builtin_returns_injected_singleton():
    stub = StubProvider()
    registry = ProviderRegistry(stub)
    assert registry.resolve(None) is stub
    assert registry.resolve("builtin:local_qwen") is stub


def test_remote_resolve_caches_by_connection(db, connection):
    registry = ProviderRegistry(StubProvider())
    first = registry.resolve(connection.id)
    second = registry.resolve(connection.id, expected_revision=1)
    assert first is second
    assert len(_FakeRemote.built) == 1
    assert registry.snapshot()["cached_remote_providers"] == [
        {"connection_id": connection.id, "revision": 1}
    ]


def test_revision_change_creates_new_instance_and_closes_old(db, connection):
    registry = ProviderRegistry(StubProvider())
    old = registry.resolve(connection.id)
    # 模拟连接更新：revision +1 并通知（update_connection 走 listener 通知）
    db.query(RewriteProviderConnection).filter(
        RewriteProviderConnection.id == connection.id
    ).update({"revision": 2, "generation_config": {"temperature": 0.5}})
    db.commit()
    set_registry(registry)  # 挂接 listener
    svc._notify_connection_changed(
        type("Row", (), {"id": connection.id})()
    )
    fresh = registry.resolve(connection.id)
    assert fresh is not old and fresh.connection_revision == 2
    assert old.closed  # 旧实例连接池已关闭淘汰


def test_disabled_or_missing_connection_raises(db, connection):
    registry = ProviderRegistry(StubProvider())
    with pytest.raises(ProviderUnavailable):
        registry.resolve("rpc_doesnotexist0000000000000")
    connection.enabled = False
    db.commit()
    registry.invalidate(connection.id)
    with pytest.raises(ProviderUnavailable):
        registry.resolve(connection.id)


def test_cache_cap_evicts_oldest(db):
    registry = ProviderRegistry(StubProvider())
    rows = []
    for i in range(22):
        rows.append(svc.create_connection(db, {
            "name": f"连接{i}", "provider_type": "glm", "model_id": "glm-5.2",
            "api_key": "zhipu-secret-key-abcd1234", "egress_acknowledged": True,
        }))
    for row in rows:
        registry.resolve(row.id)
    built = _FakeRemote.built
    assert len(registry.snapshot()["cached_remote_providers"]) == 20
    assert built[0].closed and not built[-1].closed  # 最旧的被淘汰并关闭


def test_update_connection_notifies_registry(db, connection):
    registry = ProviderRegistry(StubProvider())
    old = registry.resolve(connection.id)
    set_registry(registry)
    svc.update_connection(db, connection.id, {"model_id": "glm-5-air"})
    assert old.closed
    fresh = registry.resolve(connection.id)
    assert fresh.connection_revision == 2 and fresh.model_id == "glm-5-air"


def test_delete_connection_invalidates_registry(db, connection):
    registry = ProviderRegistry(StubProvider())
    old = registry.resolve(connection.id)
    set_registry(registry)
    svc.delete_connection(db, connection.id)
    assert old.closed


# ---------------------------------------------------------------- 跨进程 revision 感知

def test_registry_detects_revision_change_without_listener(db, connection):
    """模拟另一进程更新连接（无 listener 通知）：registry 读库发现 revision
    变化后必须重建实例，淘汰旧实例。"""
    registry = ProviderRegistry(StubProvider())
    old = registry.resolve(connection.id)
    # 直接改库 +2（绕过 update_connection 的 listener，模拟 rewriter 进程视角）
    db.query(RewriteProviderConnection).filter(
        RewriteProviderConnection.id == connection.id
    ).update({"revision": old.connection_revision + 1, "model_id": "glm-5-air"})
    db.commit()

    fresh = registry.resolve(connection.id)
    assert fresh is not old
    assert fresh.connection_revision == old.connection_revision + 1
    assert fresh.model_id == "glm-5-air"
    assert old.closed


def test_registry_detects_disable_without_listener(db, connection):
    registry = ProviderRegistry(StubProvider())
    registry.resolve(connection.id)  # 建立缓存
    db.query(RewriteProviderConnection).filter(
        RewriteProviderConnection.id == connection.id
    ).update({"enabled": False})
    db.commit()
    with pytest.raises(ProviderUnavailable):
        registry.resolve(connection.id)


def test_registry_detects_delete_without_listener(db, connection):
    registry = ProviderRegistry(StubProvider())
    cached = registry.resolve(connection.id)
    db.delete(connection)
    db.commit()
    with pytest.raises(ProviderUnavailable):
        registry.resolve(connection.id)
    assert cached.closed
