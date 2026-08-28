"""API Key 凭据加密（外部模型 API 接入 V1 §6.3）。

- AES-256-GCM，主密钥来自环境变量 REWRITE_CREDENTIAL_MASTER_KEY（32 字节 base64）
- 每次写入生成随机 12 字节 nonce；AAD = connection_id:revision，
  密文复制到其他连接 / 其他 revision 无法解密
- 主密钥只在此模块与轮换 CLI 中使用；API 输出永远只有 has_api_key + hint
"""
from __future__ import annotations

import base64
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.errors import ApiError

_NONCE_BYTES = 12
_KEY_BYTES = 32
_ENV = "REWRITE_CREDENTIAL_MASTER_KEY"


class CredentialError(ApiError):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(code, message, status_code)


def _load_master_key(env_name: str = _ENV) -> bytes:
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        raise CredentialError(
            "CREDENTIAL_ENCRYPTION_NOT_CONFIGURED",
            f"未配置凭据主密钥：请设置 {env_name}（32 字节 base64）后再创建远程连接",
            503,
        )
    try:
        key = base64.b64decode(raw, validate=True)
    except Exception as exc:
        raise CredentialError(
            "CREDENTIAL_ENCRYPTION_NOT_CONFIGURED",
            f"{env_name} 不是合法 base64",
            503,
        ) from exc
    if len(key) != _KEY_BYTES:
        raise CredentialError(
            "CREDENTIAL_ENCRYPTION_NOT_CONFIGURED",
            f"{env_name} 必须是 32 字节（256 位）密钥的 base64 编码，当前 {len(key)} 字节",
            503,
        )
    return key


def master_key_configured(env_name: str = _ENV) -> bool:
    try:
        _load_master_key(env_name)
        return True
    except CredentialError:
        return False


def generate_master_key() -> str:
    """生成新主密钥的 base64 文本（供部署初始化；不写库不入 Git）。"""
    return base64.b64encode(os.urandom(_KEY_BYTES)).decode("ascii")


def _aad(connection_id: str, revision: int) -> bytes:
    return f"{connection_id}:{revision}".encode()


def encrypt_api_key(api_key: str, connection_id: str, revision: int) -> tuple[str, str]:
    """返回 (ciphertext_base64, nonce_base64)。"""
    key = _load_master_key()
    nonce = os.urandom(_NONCE_BYTES)
    ciphertext = AESGCM(key).encrypt(nonce, api_key.encode("utf-8"), _aad(connection_id, revision))
    return base64.b64encode(ciphertext).decode("ascii"), base64.b64encode(nonce).decode("ascii")


def decrypt_api_key(ciphertext_b64: str, nonce_b64: str, connection_id: str, revision: int) -> str:
    """解密；密文被移动 / 主密钥不匹配 / revision 变化都会抛 CredentialError。"""
    key = _load_master_key()
    try:
        ciphertext = base64.b64decode(ciphertext_b64, validate=True)
        nonce = base64.b64decode(nonce_b64, validate=True)
    except Exception as exc:
        raise CredentialError("CREDENTIAL_DECRYPT_FAILED", "密文编码不合法", 500) from exc
    try:
        plain = AESGCM(key).decrypt(nonce, ciphertext, _aad(connection_id, revision))
    except InvalidTag as exc:
        raise CredentialError(
            "CREDENTIAL_DECRYPT_FAILED",
            "API Key 解密失败：主密钥不匹配或密文不属于该连接/revision",
            500,
        ) from exc
    return plain.decode("utf-8")


def key_hint(api_key: str) -> str:
    """遮罩展示：仅末 4 位（****a1b2）；短 Key 全遮蔽。"""
    tail = api_key[-4:] if len(api_key) >= 8 else ""
    return f"****{tail}" if tail else "****"
