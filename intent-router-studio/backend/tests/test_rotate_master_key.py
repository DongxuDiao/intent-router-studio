"""主密钥轮换 CLI 测试（外部模型 API 接入 V1 §6.3）。"""
from __future__ import annotations

import base64
import os

import pytest

from app.cli import rotate_rewrite_master_key as rotator
from app.models import RewriteProviderConnection
from app.query_rewrite import credentials
from app.services import provider_connection_service as svc

KEY = "zhipu-secret-key-abcd1234"


def _payload(**overrides) -> dict:
    payload = {
        "name": "轮换 GLM",
        "provider_type": "glm",
        "model_id": "glm-5.2",
        "api_key": KEY,
        "egress_acknowledged": True,
    }
    payload.update(overrides)
    return payload


@pytest.fixture(autouse=True)
def _key_pair(monkeypatch):
    monkeypatch.setenv("REWRITE_CREDENTIAL_MASTER_KEY", base64.b64encode(os.urandom(32)).decode())
    monkeypatch.setenv("REWRITE_CREDENTIAL_MASTER_KEY_NEXT", base64.b64encode(os.urandom(32)).decode())


def test_rotate_reencrypts_all_connections(db):
    # 测试库跨文件共享：清掉其他用例在不同主密钥下创建的连接，
    # 轮换必须遍历全部连接，任何一条解不开都会整体回滚
    db.query(RewriteProviderConnection).delete()
    db.commit()
    row = svc.create_connection(db, _payload())
    old_cipher = row.api_key_ciphertext

    assert rotator.rotate("REWRITE_CREDENTIAL_MASTER_KEY", "REWRITE_CREDENTIAL_MASTER_KEY_NEXT") == 0

    db.expire_all()
    refreshed = db.get(RewriteProviderConnection, row.id)
    assert refreshed.api_key_ciphertext != old_cipher
    # 新钥可解密，密文内容不变
    os.environ[credentials._ENV] = os.environ["REWRITE_CREDENTIAL_MASTER_KEY_NEXT"]
    assert credentials.decrypt_api_key(
        refreshed.api_key_ciphertext, refreshed.api_key_nonce, refreshed.id, refreshed.revision
    ) == KEY


def test_rotate_rolls_back_on_mismatch(db, monkeypatch):
    """旧钥解不开（例如已提前切换部署）→ 整体回滚，密文保持原样。"""
    db.query(RewriteProviderConnection).delete()
    db.commit()
    row = svc.create_connection(db, _payload())
    original = row.api_key_ciphertext
    # 把“旧钥”换成另一把：解密必然失败
    monkeypatch.setenv("REWRITE_CREDENTIAL_MASTER_KEY", base64.b64encode(os.urandom(32)).decode())

    assert rotator.rotate("REWRITE_CREDENTIAL_MASTER_KEY", "REWRITE_CREDENTIAL_MASTER_KEY_NEXT") == 1

    db.expire_all()
    refreshed = db.get(RewriteProviderConnection, row.id)
    assert refreshed.api_key_ciphertext == original  # 未变更
    os.environ[credentials._ENV] = os.environ.get("REWRITE_CREDENTIAL_MASTER_KEY_NEXT", "")
    # 注意：rollback 后 deployment 旧钥也已不可用——生产中这正是“必须先轮换再切环境变量”的原因


def test_rotate_same_keys_is_noop():
    assert rotator.rotate("REWRITE_CREDENTIAL_MASTER_KEY", "REWRITE_CREDENTIAL_MASTER_KEY") == 0
