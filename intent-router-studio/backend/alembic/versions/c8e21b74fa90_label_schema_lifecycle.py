"""label schema lifecycle columns + active pointer backfill (自定义意图标签 Phase 1b)

Revision ID: c8e21b74fa90
Revises: a4f92c17d3e6
Create Date: 2026-08-31

- projects.active_label_schema_id：显式当前 Schema 指针（不再按最大 version 推断）
- label_schema_versions：status/parent_id/change_summary/created_by/published_at
  + (project_id, version) 唯一约束
- 回填：旧 schema_json 补 schema_format=intent-schema-v1；每项目最新版 → ACTIVE
  并写入指针（无 Schema 的项目创建兼容五分类 v2 文档）；其余 → SUPERSEDED；
  数据集缺 schema_id 回填项目当前 Schema
"""
from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c8e21b74fa90"
down_revision: str | None = "a4f92c17d3e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 自包含的兼容五分类文档（与 label_schema.default_compat_document 同构；
# 迁移不 import 业务代码，避免未来代码漂移破坏历史迁移）
_FIVE = [
    ("information", "了解信息", "获取概念、规则、方法或能力说明，不读取真实业务状态",
     "Libra 怎么创建实验？", "查实验 123 的状态"),
    ("read_only", "查询状态", "明确要求读取真实对象或执行只读诊断",
     "审批到哪了？", "帮我催一下审批"),
    ("write_action", "修改状态", "明确要求创建、修改、发送、撤回、提交、启动等状态变化",
     "帮我撤回 Review 123", "怎么撤回 Review？"),
    ("unclear", "表达不清", "动作、对象或结果不足，无法安全决定",
     "帮我处理一下这个实验", "分析实验 123 为什么异常"),
    ("oos", "超出范围", "不属于当前 Agent 的业务和能力范围",
     "帮我预订会议室", "Libra 是否支持暂停实验？"),
]


def _default_doc() -> dict:
    return {
        "schema_format": "intent-schema-v2",
        "labels": [
            {
                "key": key,
                "name": name,
                "description": desc,
                "effect_type": key,  # 兼容五分类：恒等映射
                "status": "active",
                "order": i * 10,
                "positive_examples": [pos],
                "negative_examples": [neg],
            }
            for i, (key, name, desc, pos, neg) in enumerate(_FIVE)
        ],
        "reserved_decisions": ["nota"],
    }


def upgrade() -> None:
    with op.batch_alter_table("label_schema_versions", schema=None) as batch:
        batch.add_column(sa.Column("status", sa.String(length=20), nullable=False, server_default="ACTIVE"))
        batch.add_column(sa.Column("parent_id", sa.String(length=40), nullable=True))
        batch.add_column(sa.Column("change_summary", sa.Text(), nullable=False, server_default=""))
        batch.add_column(sa.Column("created_by", sa.String(length=100), nullable=False, server_default="local"))
        batch.add_column(sa.Column("published_at", sa.DateTime(), nullable=True))
        batch.create_foreign_key("fk_label_schema_parent", "label_schema_versions", ["parent_id"], ["id"])
        batch.create_unique_constraint("uq_label_schema_project_version", ["project_id", "version"])

    with op.batch_alter_table("label_schema_versions", schema=None) as batch:
        batch.create_index("ix_label_schema_project", ["project_id"], unique=False)

    with op.batch_alter_table("projects", schema=None) as batch:
        batch.add_column(sa.Column("active_label_schema_id", sa.String(length=40), nullable=True))
        batch.create_foreign_key(
            "fk_projects_active_label_schema",
            "label_schema_versions",
            ["active_label_schema_id"],
            ["id"],
        )

    conn = op.get_bind()

    # 1) 旧 schema_json 补 v1 标记（其余字段保持原样，读取时经适配器）
    rows = conn.execute(sa.text("SELECT id, schema_json FROM label_schema_versions")).fetchall()
    for row_id, schema_json in rows:
        doc = json.loads(schema_json) if isinstance(schema_json, str) else (schema_json or {})
        if isinstance(doc, dict) and not doc.get("schema_format"):
            doc["schema_format"] = "intent-schema-v1"
            conn.execute(
                sa.text("UPDATE label_schema_versions SET schema_json = :sj WHERE id = :id"),
                {"sj": json.dumps(doc, ensure_ascii=False), "id": row_id},
            )

    # 2) 每个项目：最新版 ACTIVE + 写指针；其余 SUPERSEDED；无 Schema 则建默认
    projects = conn.execute(sa.text("SELECT id FROM projects")).fetchall()
    for (project_id,) in projects:
        latest = conn.execute(
            sa.text(
                "SELECT id, created_at FROM label_schema_versions "
                "WHERE project_id = :p ORDER BY version DESC LIMIT 1"
            ),
            {"p": project_id},
        ).fetchone()
        if latest is None:
            doc = _default_doc()
            new_id = f"lsv_{uuid.uuid4().hex[:26]}"
            payload = json.dumps(doc, ensure_ascii=False, sort_keys=True)
            conn.execute(
                sa.text(
                    "INSERT INTO label_schema_versions "
                    "(id, project_id, version, schema_json, hash, status, change_summary, created_by, created_at, published_at) "
                    "VALUES (:id, :p, 1, :sj, :h, 'ACTIVE', '迁移补建默认五分类', 'migration', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {"id": new_id, "p": project_id, "sj": json.dumps(doc, ensure_ascii=False),
                 "h": hashlib.sha256(payload.encode("utf-8")).hexdigest()},
            )
            active_id = new_id
        else:
            conn.execute(
                sa.text(
                    "UPDATE label_schema_versions SET status = 'ACTIVE', "
                    "published_at = COALESCE(published_at, created_at) WHERE id = :id"
                ),
                {"id": latest.id},
            )
            active_id = latest.id
        conn.execute(
            sa.text(
                "UPDATE label_schema_versions SET status = 'SUPERSEDED' "
                "WHERE project_id = :p AND id <> :id"
            ),
            {"p": project_id, "id": active_id},
        )
        conn.execute(
            sa.text("UPDATE projects SET active_label_schema_id = :id WHERE id = :p"),
            {"id": active_id, "p": project_id},
        )

    # 3) 数据集缺 schema_id → 回填项目当前 Schema
    conn.execute(
        sa.text(
            "UPDATE dataset_versions SET schema_id = "
            "(SELECT p.active_label_schema_id FROM projects p WHERE p.id = dataset_versions.project_id) "
            "WHERE schema_id IS NULL AND EXISTS ("
            "  SELECT 1 FROM projects p WHERE p.id = dataset_versions.project_id "
            "  AND p.active_label_schema_id IS NOT NULL)"
        )
    )


def downgrade() -> None:
    # 指针与生命周期列移除；schema_json 中的 v1 标记保留（无害）
    with op.batch_alter_table("projects", schema=None) as batch:
        batch.drop_constraint("fk_projects_active_label_schema", type_="foreignkey")
        batch.drop_column("active_label_schema_id")
    with op.batch_alter_table("label_schema_versions", schema=None) as batch:
        batch.drop_index("ix_label_schema_project")
        batch.drop_constraint("uq_label_schema_project_version", type_="unique")
        batch.drop_constraint("fk_label_schema_parent", type_="foreignkey")
        batch.drop_column("status")
        batch.drop_column("parent_id")
        batch.drop_column("change_summary")
        batch.drop_column("created_by")
        batch.drop_column("published_at")
