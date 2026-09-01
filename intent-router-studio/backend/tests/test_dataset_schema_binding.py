"""数据集 Schema 版本绑定测试（Review 修复 §3/§4/§10.2）。

不变量：
- 导入时一次性锁定项目 Active Schema：schema_id + manifest 元信息一致；
- 项目发布新 Schema 不影响旧数据集的校验、编辑与派生；
- 派生草稿继承源 Schema；只有新导入的数据集绑定新 Schema；
- 无 Schema 数据集禁止启动新 Run（DATASET_SCHEMA_MISMATCH），
  但历史 v1 数据的读取路径兼容恒等五分类。
"""
from __future__ import annotations

import io

import pytest

from app.errors import ApiError
from app.models import DatasetVersion, LabelSchemaVersion, Project
from app.services import dataset_service, run_service

SCHEMA_A = [
    {"key": "faq", "name": "常见问题", "effect_type": "information", "order": 0},
    {"key": "create_task", "name": "创建任务", "effect_type": "write_action", "order": 10},
]
SCHEMA_B = [
    {"key": "metric_qa", "name": "指标问答", "effect_type": "information", "order": 0},
    {"key": "status_query", "name": "状态查询", "effect_type": "read_only", "order": 10},
    {"key": "deploy_cmd", "name": "部署命令", "effect_type": "write_action", "order": 20},
]


def _publish(client, project_id, labels) -> str:
    draft = client.post(
        f"/api/v1/projects/{project_id}/label-schemas/drafts", json={"change_summary": "测试"}
    ).json()
    assert client.patch(
        f"/api/v1/projects/{project_id}/label-schemas/{draft['id']}",
        json={"expected_hash": draft["hash"], "labels": labels},
    ).status_code == 200
    current = client.get(f"/api/v1/projects/{project_id}/label-schemas/{draft['id']}").json()
    published = client.post(
        f"/api/v1/projects/{project_id}/label-schemas/{draft['id']}/publish",
        json={"expected_hash": current["hash"], "confirm_breaking_changes": True},
    )
    assert published.status_code == 200, published.text
    return published.json()["id"]


def _import_csv(client, project_id, rows: list[tuple[str, str]]) -> str:
    body = "".join(f"{text},{label},g{i % 3}\n" for i, (text, label) in enumerate(rows))
    upload = client.post(
        f"/api/v1/projects/{project_id}/uploads",
        files={"file": ("x.csv", io.BytesIO(f"text,label,group_id\n{body}".encode()), "text/csv")},
    ).json()
    resp = client.post(
        f"/api/v1/uploads/{upload['id']}/import",
        json={"mode": "prelabeled", "columns": {"text": "text", "label": "label", "group_id": "group_id"}},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


A_ROWS = [(f"faq 第 {i} 条问题", "faq") for i in range(8)] + [
    (f"创建任务 第 {i} 条", "create_task") for i in range(8)
]


def test_import_binds_active_schema_and_manifest(db, client, project_id):
    schema_a = _publish(client, project_id, SCHEMA_A)
    dataset_id = _import_csv(client, project_id, A_ROWS)

    dataset = db.get(DatasetVersion, dataset_id)
    assert dataset.schema_id == schema_a
    manifest = dataset.manifest
    assert manifest["label_schema_id"] == schema_a
    assert manifest["label_schema_format"] == "intent-schema-v2"
    assert len(manifest["label_schema_hash"]) == 64
    assert manifest["label_order"] == ["faq", "create_task"]
    schema_row = db.get(LabelSchemaVersion, schema_a)
    assert manifest["label_schema_hash"] == schema_row.hash


def test_publishing_new_schema_does_not_affect_old_dataset(db, client, project_id):
    schema_a = _publish(client, project_id, SCHEMA_A)
    dataset_id = _import_csv(client, project_id, A_ROWS)
    _publish(client, project_id, SCHEMA_B)  # 发布 Schema B

    # 校验仍按 A：A 内标签全部合法，不要求 B 的类别
    report = dataset_service.validate_dataset(db, dataset_id)
    assert not [e for e in report["errors"] if e["code"] == "INVALID_LABEL"]
    assert not [e for e in report["errors"] if e["code"] == "MISSING_LABEL_CLASS"]

    # 派生草稿继承 A；B 专属标签不可写入
    draft = dataset_service.create_draft(db, dataset_id, [])
    assert draft.schema_id == schema_a
    listing = dataset_service.list_samples(db, draft.id, {})
    samples = listing["samples"]
    with pytest.raises(ApiError) as exc:
        dataset_service.create_draft(db, dataset_id, [
            {"action": "update", "sample_id": samples[0]["sample_id"], "label": "status_query"},
        ])
    assert exc.value.code == "INVALID_LABEL"
    with pytest.raises(ApiError) as exc:
        dataset_service.update_sample(db, draft.id, samples[0]["sample_id"], {"label": "deploy_cmd"})
    assert exc.value.code == "INVALID_LABEL"
    # A 内标签可用
    changed = dataset_service.update_sample(db, draft.id, samples[0]["sample_id"], {"label": "create_task"})
    assert changed["label"] == "create_task"

    # 发布 B 后的新导入绑定 B
    new_id = _import_csv(client, project_id, [
        (f"指标 {i}", "metric_qa") for i in range(8)
    ] + [(f"状态 {i}", "status_query") for i in range(8)])
    assert db.get(DatasetVersion, new_id).schema_id != schema_a


def test_import_requires_project_schema(db, client, project_id):
    _publish(client, project_id, SCHEMA_A)
    upload = client.post(
        f"/api/v1/projects/{project_id}/uploads",
        files={"file": ("x.csv", io.BytesIO(b"text,label\nt,faq\n"), "text/csv")},
    ).json()
    # 清空项目 Schema（模拟异常状态）：先释放指针再删行，避免外键约束
    db.get(Project, project_id).active_label_schema_id = None
    db.flush()
    db.query(LabelSchemaVersion).filter(LabelSchemaVersion.project_id == project_id).delete()
    db.commit()
    with pytest.raises(ApiError) as exc:
        dataset_service.import_upload(db, upload["id"], {
            "mode": "prelabeled", "columns": {"text": "text", "label": "label"},
        })
    assert exc.value.code == "LABEL_SCHEMA_NOT_FOUND"


FIVE = ("information", "read_only", "write_action", "unclear", "oos")


def _schemaless_dataset(db, project_id, tmp_path) -> DatasetVersion:
    """直接落库的历史 v1 数据集：无 schema_id（迁移遗漏视为损坏）。"""
    import pandas as pd

    frame = pd.DataFrame(
        {
            "sample_id": [f"smp_legacy_{i}" for i in range(10)],
            "text": [f"{lab} 的第 {i} 条" for i, lab in enumerate(FIVE * 2)],
            "label": list(FIVE * 2),
            "normalized_hash": [f"hash_legacy_{i}" for i in range(10)],
        }
    )
    parquet = tmp_path / "legacy.parquet"
    frame.to_parquet(parquet, index=False)
    row = DatasetVersion(
        id="dsv_legacy0000000000000000000001",
        project_id=project_id,
        version=99,
        name="历史数据集",
        origin="import",
        status="FROZEN",
        parquet_path=str(parquet),
        sample_count=10,
        labeled_count=10,
        label_distribution={lab: 2 for lab in FIVE},
    )
    db.add(row)
    db.commit()
    return row


def test_schemaless_dataset_blocks_run_but_reads_compat(db, client, project_id, tmp_path):
    legacy = _schemaless_dataset(db, project_id, tmp_path)

    # 读取路径兼容：校验按恒等五分类，无 INVALID/MISSING 错误
    report = dataset_service.validate_dataset(db, legacy.id)
    assert not [e for e in report["errors"] if e["code"] in ("INVALID_LABEL", "MISSING_LABEL_CLASS")]

    # 新 Run 拦截：DATASET_SCHEMA_MISMATCH
    with pytest.raises(ApiError) as exc:
        run_service.create_run(db, project_id, legacy.id, "", None)
    assert exc.value.code == "DATASET_SCHEMA_MISMATCH"
