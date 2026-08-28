"""凭据加密模块测试（外部模型 API 接入 V1 阶段 2）。"""
from __future__ import annotations

import base64
import os

import pytest

from app.query_rewrite import credentials


@pytest.fixture(autouse=True)
def _master_key(monkeypatch):
    monkeypatch.setenv("REWRITE_CREDENTIAL_MASTER_KEY", base64.b64encode(os.urandom(32)).decode())
    # 测试环境可能残留 NEXT 钥（轮换用例自行设置）
    monkeypatch.delenv("REWRITE_CREDENTIAL_MASTER_KEY_NEXT", raising=False)


def test_roundtrip_and_nonce_uniqueness():
    c1, n1 = credentials.encrypt_api_key("zhipu-key-abcdef", "rpc_a", 1)
    c2, n2 = credentials.encrypt_api_key("zhipu-key-abcdef", "rpc_a", 1)
    assert c1 != c2 and n1 != n2  # 随机 nonce：同 Key 两次密文不同
    assert credentials.decrypt_api_key(c1, n1, "rpc_a", 1) == "zhipu-key-abcdef"


def test_aad_blocks_cross_connection_copy():
    c, n = credentials.encrypt_api_key("zhipu-key-abcdef", "rpc_a", 1)
    with pytest.raises(credentials.CredentialError) as exc:
        credentials.decrypt_api_key(c, n, "rpc_b", 1)  # 复制到别的连接
    assert exc.value.code == "CREDENTIAL_DECRYPT_FAILED"


def test_aad_blocks_revision_mismatch():
    c, n = credentials.encrypt_api_key("zhipu-key-abcdef", "rpc_a", 1)
    with pytest.raises(credentials.CredentialError):
        credentials.decrypt_api_key(c, n, "rpc_a", 2)  # revision 变化后旧密文不可用


def test_wrong_master_key_fails(monkeypatch):
    c, n = credentials.encrypt_api_key("zhipu-key-abcdef", "rpc_a", 1)
    monkeypatch.setenv("REWRITE_CREDENTIAL_MASTER_KEY", base64.b64encode(os.urandom(32)).decode())
    with pytest.raises(credentials.CredentialError):
        credentials.decrypt_api_key(c, n, "rpc_a", 1)


def test_not_configured_detected(monkeypatch):
    monkeypatch.delenv("REWRITE_CREDENTIAL_MASTER_KEY", raising=False)
    assert credentials.master_key_configured() is False
    with pytest.raises(credentials.CredentialError) as exc:
        credentials.encrypt_api_key("key", "rpc_a", 1)
    assert exc.value.code == "CREDENTIAL_ENCRYPTION_NOT_CONFIGURED"


@pytest.mark.parametrize("bad", ["", "not-base64!!", "c2hvcnQ="])  # 空 / 非法 / 长度不足
def test_malformed_master_key_rejected(monkeypatch, bad):
    monkeypatch.setenv("REWRITE_CREDENTIAL_MASTER_KEY", bad)
    assert credentials.master_key_configured() is False


def test_key_hint_masks_all_but_last_four():
    assert credentials.key_hint("abcdefghijklmnopqrstuvwxyz1234") == "****1234"
    assert credentials.key_hint("short") == "****"
