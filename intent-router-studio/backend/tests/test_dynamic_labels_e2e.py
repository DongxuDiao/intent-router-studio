"""动态标签端到端测试（自定义意图标签 Phase 2）。

覆盖：两分类 Schema 下的导入/样本修改/质量校验、自定义标签制品加载、
旧 labels-v1 制品兼容、缺 Schema 制品 fail closed。
"""
from __future__ import annotations

import io

import numpy as np
import pytest

from app.errors import ApiError
from app.router_core.policy import Thresholds
from app.router_core.runtime import ModelRuntime
from app.services import artifact_service, dataset_service
from tests.test_runtime import _StubSetFitModel  # noqa: PLC0415 复用桩模型


class _DimStubModel:
    """按制品标签维度输出的桩模型（复用桩固定 5 列，不能用于多分类冒烟）。"""

    def __init__(self, n: int) -> None:
        self.n = n

    def predict_proba(self, texts: list[str]) -> np.ndarray:
        rows = []
        for _t in texts:
            row = np.full(self.n, 0.05)
            row[0] = 0.75
            rows.append(row)
        return np.array(rows)


def _publish_two_class(client, project_id) -> str:
    new_labels = [
        {"key": "faq", "name": "常见问题", "effect_type": "information", "order": 0},
        {"key": "create_task", "name": "创建任务", "effect_type": "write_action", "order": 10},
    ]
    draft = client.post(f"/api/v1/projects/{project_id}/label-schemas/drafts",
                        json={"change_summary": "两分类试点"}).json()
    client.patch(f"/api/v1/projects/{project_id}/label-schemas/{draft['id']}",
                 json={"expected_hash": draft["hash"], "labels": new_labels})
    current = client.get(f"/api/v1/projects/{project_id}/label-schemas/{draft['id']}").json()
    published = client.post(f"/api/v1/projects/{project_id}/label-schemas/{draft['id']}/publish",
                            json={"expected_hash": current["hash"], "confirm_breaking_changes": True})
    assert published.status_code == 200, published.text
    return published.json()["id"]


def _upload_two_class_csv(client, project_id) -> str:
    csv = "text,label,group_id\n什么是实验？,faq,g1\n怎么建实验？,create_task,g2\n"
    resp = client.post(f"/api/v1/projects/{project_id}/uploads",
                       files={"file": ("two.csv", io.BytesIO(csv.encode()), "text/csv")})
    return resp.json()["id"]


def test_two_class_import_validate_and_patch(db, client, project_id):
    _publish_two_class(client, project_id)
    upload_id = _upload_two_class_csv(client, project_id)
    resp = client.post(f"/api/v1/uploads/{upload_id}/import", json={
        "mode": "prelabeled", "columns": {"text": "text", "label": "label"},
    })
    assert resp.status_code == 200, resp.text
    dataset_id = resp.json()["id"]
    assert resp.json()["label_distribution"] == {"faq": 1, "create_task": 1}

    # 质量校验：按两分类 Schema 判定（不要求五分类齐全）
    report = client.post(f"/api/v1/datasets/{dataset_id}/validate").json()
    codes = [e["code"] for e in report.get("problems", [])] if isinstance(report, dict) else []
    assert "INVALID_LABEL" not in codes

    # 样本改标走草稿链路：自定义标签合法 / Schema 外标签（旧五分类 key）拒绝
    listing = dataset_service.list_samples(db, dataset_id, {})
    samples_items = next(
        v for v in listing.values()
        if isinstance(v, list) and v and isinstance(v[0], dict) and "sample_id" in v[0]
    )
    sid = samples_items[0]["sample_id"]
    dataset_service.create_draft(db, dataset_id, [
        {"action": "update", "sample_id": sid, "label": "create_task"},
    ])
    with pytest.raises(ApiError):
        dataset_service.create_draft(db, dataset_id, [
            {"action": "update", "sample_id": sid, "label": "write_action"},  # 不在两分类 Schema
        ])

    # single_label 导入也以 Schema 为准
    resp = client.post(f"/api/v1/uploads/{_upload_two_class_csv(client, project_id)}/import", json={
        "mode": "single_label", "default_label": "faq", "columns": {"text": "text"},
    })
    assert resp.status_code == 200
    with_new = client.post(f"/api/v1/uploads/{_upload_two_class_csv(client, project_id)}/import", json={
        "mode": "single_label", "default_label": "information", "columns": {"text": "text"},
    })
    assert with_new.status_code == 422


def _artifact_with_labels(tmp_path, monkeypatch, labels_payload: dict) -> None:
    n = len(labels_payload.get("labels", [])) or 5
    monkeypatch.setattr("setfit.SetFitModel.from_pretrained", lambda path: _DimStubModel(n))
    model_dir = tmp_path / "setfit_model"
    model_dir.mkdir(parents=True, exist_ok=True)
    (model_dir / "model.safetensors").write_bytes(b"legit-weights-aaaa")
    (model_dir / "config.json").write_text('{"model_type": "stub"}', encoding="utf-8")
    artifact_service.write_json(tmp_path / "thresholds.json", Thresholds().to_dict())
    artifact_service.write_json(tmp_path / "label_schema.json", labels_payload)
    artifact_service.build_manifest(tmp_path, {"run_id": "run_test"})


def test_runtime_loads_custom_label_artifact(tmp_path, monkeypatch):
    """七分类制品（多意图共享 effect）按制品顺序加载。"""
    labels = ["metric_qa", "status_query", "log_query", "task_create", "task_update", "vague", "outside"]
    payload = {
        "schema_format": "intent-schema-v2", "schema_id": "lsv_x", "schema_hash": "abc",
        "labels": labels,
        "label_definitions": [
            {"key": "metric_qa", "effect_type": "information"},
            {"key": "status_query", "effect_type": "read_only"},
            {"key": "log_query", "effect_type": "read_only"},
            {"key": "task_create", "effect_type": "write_action"},
            {"key": "task_update", "effect_type": "write_action"},
            {"key": "vague", "effect_type": "unclear"},
            {"key": "outside", "effect_type": "oos"},
        ],
    }
    _artifact_with_labels(tmp_path, monkeypatch, payload)
    runtime = ModelRuntime.load(tmp_path, "mdl_x", verify=True)
    assert runtime.labels == labels  # 顺序即制品数组顺序（分类头契约）
    result = runtime.predict("查一下状态")
    # 两层契约：intent 是业务标签（桩模型 top1 恒为第 0 列），route=effect_type
    assert result["intent"] == {"key": labels[0], "name": labels[0]}  # 无 name 时回退 key
    assert result["effect_type"] == "information" and result["route"] == "information"
    # Review 修复 §7.3：预测结果携带 Schema 溯源
    assert result["schema_id"] == "lsv_x" and result["schema_hash"] == "abc"


def test_runtime_intent_object_carries_definition_name(tmp_path, monkeypatch):
    """§9.1：intent.name 来自制品 label_definitions 的展示名。"""
    payload = {
        "schema_format": "intent-schema-v2", "schema_id": "lsv_y", "schema_hash": "def",
        "labels": ["faq", "create_task"],
        "label_definitions": [
            {"key": "faq", "name": "常见问题", "effect_type": "information"},
            {"key": "create_task", "name": "创建任务", "effect_type": "write_action"},
        ],
    }
    _artifact_with_labels(tmp_path, monkeypatch, payload)
    runtime = ModelRuntime.load(tmp_path, "mdl_y", verify=True)
    result = runtime.predict("什么是实验？")
    assert result["intent"] == {"key": "faq", "name": "常见问题"}


def test_runtime_rejects_v2_artifact_missing_effect_mapping(tmp_path, monkeypatch):
    """§10.1-4：v2 制品缺 effect mapping（definitions 不完整）加载即失败。"""
    payload = {
        "schema_format": "intent-schema-v2", "schema_id": "lsv_z", "schema_hash": "xyz",
        "labels": ["faq", "create_task"],
        "label_definitions": [{"key": "faq", "effect_type": "information"}],  # 缺 create_task
    }
    _artifact_with_labels(tmp_path, monkeypatch, payload)
    with pytest.raises(RuntimeError, match="MODEL_SCHEMA_MISMATCH.*create_task"):
        ModelRuntime.load(tmp_path, "mdl_z", verify=True)


def test_runtime_rejects_invalid_effect_type_in_definitions(tmp_path, monkeypatch):
    """definitions 里出现平台外 effect type：加载失败（不静默接受）。"""
    payload = {
        "schema_format": "intent-schema-v2", "schema_id": "lsv_w", "schema_hash": "www",
        "labels": ["faq", "create_task"],
        "label_definitions": [
            {"key": "faq", "effect_type": "information"},
            {"key": "create_task", "effect_type": "super_admin"},  # 平台外
        ],
    }
    _artifact_with_labels(tmp_path, monkeypatch, payload)
    with pytest.raises(RuntimeError, match="MODEL_SCHEMA_MISMATCH"):
        ModelRuntime.load(tmp_path, "mdl_w", verify=True)


def test_runtime_rejects_unmapped_custom_label_without_definitions(tmp_path, monkeypatch):
    """无 definitions 的自定义标签（v1 形状）：加载失败，不回退恒等映射。"""
    payload = {
        "schema_version": "labels-v1",
        "labels": ["read_only", "create_task"],  # read_only 恒等合法；create_task 无映射
    }
    _artifact_with_labels(tmp_path, monkeypatch, payload)
    with pytest.raises(RuntimeError, match="MODEL_SCHEMA_MISMATCH.*create_task"):
        ModelRuntime.load(tmp_path, "mdl_v", verify=True)


def test_runtime_accepts_legacy_v1_artifact(tmp_path, monkeypatch):
    """旧 labels-v1 制品（仅 labels 列表）继续工作，顺序不变。"""
    from app.router_core.taxonomy import LABELS

    _artifact_with_labels(tmp_path, monkeypatch,
                          {"schema_version": "labels-v1", "labels": list(LABELS)})
    runtime = ModelRuntime.load(tmp_path, "mdl_old", verify=True)
    assert runtime.labels == list(LABELS)


def test_runtime_fail_closed_without_schema_artifact(tmp_path, monkeypatch):
    """制品缺 label_schema.json：fail closed（禁止回退固定五分类导致概率错位）。"""
    monkeypatch.setattr("setfit.SetFitModel.from_pretrained", lambda path: _StubSetFitModel())
    model_dir = tmp_path / "setfit_model"
    model_dir.mkdir(parents=True)
    (model_dir / "model.safetensors").write_bytes(b"legit-weights-aaaa")
    (model_dir / "config.json").write_text('{"model_type": "stub"}', encoding="utf-8")
    artifact_service.write_json(tmp_path / "thresholds.json", Thresholds().to_dict())
    artifact_service.build_manifest(tmp_path, {"run_id": "run_test"})
    with pytest.raises(RuntimeError, match="MODEL_SCHEMA_MISMATCH"):
        ModelRuntime.load(tmp_path, "mdl_x", verify=True)


