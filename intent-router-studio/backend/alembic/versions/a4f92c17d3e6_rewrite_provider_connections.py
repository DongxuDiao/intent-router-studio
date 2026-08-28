"""rewrite provider connections (外部模型 API 接入 V1 §6.1)

Revision ID: a4f92c17d3e6
Revises: 7c1d9f40a2b8
Create Date: 2026-08-28

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a4f92c17d3e6"
down_revision: str | None = "7c1d9f40a2b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "rewrite_provider_connections",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("provider_type", sa.String(length=30), nullable=False),
        sa.Column("base_url", sa.String(length=500), nullable=False),
        sa.Column("model_id", sa.String(length=200), nullable=False),
        # 密文与 nonce 可空：DELETE /credential 清除后连接保留但不可用
        sa.Column("api_key_ciphertext", sa.Text(), nullable=True),
        sa.Column("api_key_nonce", sa.String(length=100), nullable=True),
        sa.Column("api_key_hint", sa.String(length=16), nullable=False),
        sa.Column("generation_config", sa.JSON(), nullable=False),
        sa.Column("capabilities", sa.JSON(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("egress_acknowledged_at", sa.DateTime(), nullable=True),
        sa.Column("last_test_status", sa.String(length=20), nullable=True),
        sa.Column("last_test_error_code", sa.String(length=50), nullable=True),
        sa.Column("last_test_latency_ms", sa.Float(), nullable=True),
        sa.Column("last_tested_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("rewrite_provider_connections", schema=None) as batch_op:
        batch_op.create_index("ix_provider_connections_type", ["provider_type"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("rewrite_provider_connections", schema=None) as batch_op:
        batch_op.drop_index("ix_provider_connections_type")
    op.drop_table("rewrite_provider_connections")
