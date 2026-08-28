"""凭据主密钥轮换（外部模型 API 接入 V1 §6.3）。

用法：
    python -m app.cli.rotate_rewrite_master_key \
        --old-key-env REWRITE_CREDENTIAL_MASTER_KEY \
        --new-key-env REWRITE_CREDENTIAL_MASTER_KEY_NEXT

流程：校验两把密钥 → 单个数据库事务内逐条「旧钥解密 → 新钥重加密 →
新钥解密校验」→ 全部成功才提交；任一失败整体回滚，避免部分连接使用
新旧不一致的密钥。提交成功后，再切换部署环境变量并重启服务。
"""
from __future__ import annotations

import argparse
import os
import sys

from app.db import SessionLocal
from app.models import RewriteProviderConnection
from app.query_rewrite import credentials


def rotate(old_key_env: str, new_key_env: str) -> int:
    old_raw = os.environ.get(old_key_env, "").strip()
    new_raw = os.environ.get(new_key_env, "").strip()
    if not old_raw or not new_raw:
        print(f"缺少环境变量：{old_key_env if not old_raw else new_key_env}")
        return 1
    if old_raw == new_raw:
        print("新旧主密钥相同，无需轮换")
        return 0

    # 在独立环境名下校验两把密钥的格式
    os.environ[credentials._ENV] = old_raw
    try:
        credentials._load_master_key()
    except credentials.CredentialError as exc:
        print(f"旧主密钥不合法（{old_key_env}）: {exc.message}")
        return 1
    os.environ[credentials._ENV] = new_raw
    try:
        credentials._load_master_key()
    except credentials.CredentialError as exc:
        print(f"新主密钥不合法（{new_key_env}）: {exc.message}")
        return 1

    db = SessionLocal()
    rotated = 0
    try:
        rows = db.query(RewriteProviderConnection).order_by(RewriteProviderConnection.id).all()
        # 1) 旧钥解密全部密文（先全部解出，避免中途换钥状态混乱）
        plains: dict[str, str] = {}
        os.environ[credentials._ENV] = old_raw
        for row in rows:
            if not row.api_key_ciphertext:
                continue
            try:
                plains[row.id] = credentials.decrypt_api_key(
                    row.api_key_ciphertext, row.api_key_nonce or "", row.id, row.revision
                )
            except Exception as exc:
                print(f"回滚：连接 {row.id} 用旧主密钥解密失败（{exc}）")
                db.rollback()
                return 1
        # 2) 新钥重加密并当场解密校验
        os.environ[credentials._ENV] = new_raw
        for row in rows:
            plain = plains.get(row.id)
            if plain is None:
                continue
            ciphertext, nonce = credentials.encrypt_api_key(plain, row.id, row.revision)
            credentials.decrypt_api_key(ciphertext, nonce, row.id, row.revision)  # 校验
            row.api_key_ciphertext = ciphertext
            row.api_key_nonce = nonce
            rotated += 1
        db.commit()
        print(f"轮换完成：{rotated} 个连接的密文已用新主密钥重加密；请更新部署环境变量 {old_key_env} 后重启服务")
        return 0
    except Exception as exc:
        db.rollback()
        print(f"回滚：轮换失败（{type(exc).__name__}: {exc}），数据库未变更")
        return 1
    finally:
        os.environ[credentials._ENV] = old_raw  # 部署环境变量仍指向旧钥，直到人工切换
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="轮换改写凭据主密钥")
    parser.add_argument("--old-key-env", default="REWRITE_CREDENTIAL_MASTER_KEY")
    parser.add_argument("--new-key-env", default="REWRITE_CREDENTIAL_MASTER_KEY_NEXT")
    args = parser.parse_args()
    sys.exit(rotate(args.old_key_env, args.new_key_env))


if __name__ == "__main__":
    main()
