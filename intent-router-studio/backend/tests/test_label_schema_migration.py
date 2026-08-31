"""Schema 生命周期迁移测试（自定义意图标签方案 §11.3）。

在真实 SQLite 上演练：旧基线建库 → 手工插入旧格式数据 → 升级到 head →
断言回填结果；再演练 downgrade 可逆。
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from sqlalchemy import create_engine, text

REPO = Path(__file__).resolve().parents[1]


def _run_alembic(url: str, *args: str) -> None:
    env = {
        **dict(__import__("os").environ),
        "DATABASE_URL": url,
        "ARTIFACT_ROOT": tempfile.mkdtemp(prefix="irs-mig-"),
        "HF_HOME": tempfile.mkdtemp(prefix="irs-hf-"),
    }
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(REPO), env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _old_project_with_schema(engine) -> tuple[str, str, str]:
    """造一个旧项目：2 个 Schema 版本（旧 JSON 无 schema_format）+ 1 个数据集。"""
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO projects (id, name, description, created_at, updated_at)"
                          " VALUES ('prj_old', '旧项目', '', '2026-01-01 00:00:00', '2026-01-01 00:00:00')"))
        for ver, key_extra in ((1, "v1"), (2, "v2")):
            doc = {
                "schema_version": "labels-v1",
                "labels": [
                    {"key": "information", "name": f"了解信息{key_extra}", "definition": "d",
                     "positive_example": "p", "negative_example": "n"},
                    {"key": "read_only", "name": "查询状态", "definition": "d",
                     "positive_example": "p", "negative_example": "n"},
                    {"key": "write_action", "name": "修改状态", "definition": "d",
                     "positive_example": "p", "negative_example": "n"},
                    {"key": "unclear", "name": "表达不清", "definition": "d",
                     "positive_example": "p", "negative_example": "n"},
                    {"key": "oos", "name": "超出范围", "definition": "d",
                     "positive_example": "p", "negative_example": "n"},
                ],
                "reserved_routes": ["nota"],
            }
            conn.execute(text(
                "INSERT INTO label_schema_versions (id, project_id, version, schema_json, hash, created_at)"
                " VALUES (:id, 'prj_old', :v, :sj, :h, '2026-01-0%s 00:00:00')" % ver
            ), {"id": f"lsv_old{ver}", "v": ver, "sj": json.dumps(doc, ensure_ascii=False), "h": "0" * 64})
        conn.execute(text(
            "INSERT INTO dataset_versions (id, project_id, version, name, origin, status, parquet_path,"
            " sample_count, labeled_count, change_summary, created_by, created_at)"
            " VALUES ('dsv_old', 'prj_old', 1, '旧数据集', 'import', 'FROZEN', '/tmp/x.parquet', 0, 0, '', 'local',"
            " '2026-01-01 00:00:00')"
        ))


def test_upgrade_backfills_active_pointer_and_statuses():
    with tempfile.TemporaryDirectory() as tmp:
        url = f"sqlite:///{tmp}/mig.db"
        engine = create_engine(url)
        _run_alembic(url, "upgrade", "a4f92c17d3e6")  # Schema 生命周期之前的基线
        _old_project_with_schema(engine)
        _run_alembic(url, "upgrade", "head")

        with engine.begin() as conn:
            # 指针指向最新版；最新 ACTIVE、旧版 SUPERSEDED
            pointer = conn.execute(text("SELECT active_label_schema_id FROM projects WHERE id='prj_old'")).scalar()
            assert pointer == "lsv_old2"
            statuses = dict(conn.execute(
                text("SELECT id, status FROM label_schema_versions WHERE project_id='prj_old'")
            ).fetchall())
            assert statuses == {"lsv_old1": "SUPERSEDED", "lsv_old2": "ACTIVE"}
            published = conn.execute(text("SELECT published_at FROM label_schema_versions WHERE id='lsv_old2'")).scalar()
            assert published is not None
            # 旧 JSON 补了 v1 标记且其余字段保持
            doc = json.loads(conn.execute(text("SELECT schema_json FROM label_schema_versions WHERE id='lsv_old1'")).scalar())
            assert doc["schema_format"] == "intent-schema-v1"
            assert doc["labels"][0]["key"] == "information" and doc["labels"][0]["definition"] == "d"
            # 数据集回填 schema_id
            assert conn.execute(text("SELECT schema_id FROM dataset_versions WHERE id='dsv_old'")).scalar() == "lsv_old2"
            # 唯一约束生效
            try:
                conn.execute(text("INSERT INTO label_schema_versions (id, project_id, version, schema_json, hash,"
                                  " status, created_at) VALUES ('lsv_dup', 'prj_old', 2, '{}', 'x', 'ACTIVE',"
                                  " '2026-01-01 00:00:00')"))
                raised = False
            except Exception:
                raised = True
            assert raised

        # 可逆（保留 JSON 标记无害）
        _run_alembic(url, "downgrade", "a4f92c17d3e6")
        with engine.begin() as conn:
            cols = {row[1] for row in conn.execute(text("PRAGMA table_info('label_schema_versions')")).fetchall()}
            assert "status" not in cols and "parent_id" not in cols
            pcols = {row[1] for row in conn.execute(text("PRAGMA table_info('projects')")).fetchall()}
            assert "active_label_schema_id" not in pcols


def test_upgrade_creates_default_schema_for_project_without_any():
    with tempfile.TemporaryDirectory() as tmp:
        url = f"sqlite:///{tmp}/mig2.db"
        engine = create_engine(url)
        _run_alembic(url, "upgrade", "a4f92c17d3e6")
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO projects (id, name, description, created_at, updated_at)"
                              " VALUES ('prj_bare', '裸项目', '', '2026-01-01 00:00:00', '2026-01-01 00:00:00')"))
        _run_alembic(url, "upgrade", "head")
        with engine.begin() as conn:
            row = conn.execute(text(
                "SELECT s.id, s.status, s.schema_json FROM label_schema_versions s"
                " JOIN projects p ON p.active_label_schema_id = s.id WHERE p.id = 'prj_bare'"
            )).fetchone()
            assert row is not None and row[1] == "ACTIVE"
            doc = json.loads(row[2])
            assert doc["schema_format"] == "intent-schema-v2"
            assert [l["key"] for l in doc["labels"]] == [
                "information", "read_only", "write_action", "unclear", "oos",
            ]
            assert all(l["effect_type"] == l["key"] for l in doc["labels"])
