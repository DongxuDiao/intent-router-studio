"""标签 Schema 生命周期 API 测试（自定义意图标签方案 Phase 1 验收）。

覆盖：默认五分类与指针、草稿状态机（409 并行草稿/乐观锁/不可变）、
发布校验（内容去重/破坏性确认）、影响分析、审计事件、跨项目隔离。
"""
from __future__ import annotations

import json

import pytest

from app.models import AuditEvent, LabelSchemaVersion, Project

FIVE = ["information", "read_only", "write_action", "unclear", "oos"]


def _two_class_labels() -> list[dict]:
    return [
        {"key": "faq", "name": "常见问题", "effect_type": "information", "order": 0},
        {"key": "create_task", "name": "创建任务", "effect_type": "write_action", "order": 10},
    ]


def _active(db, project_id):
    project = db.get(Project, project_id)
    return db.get(LabelSchemaVersion, project.active_label_schema_id)


# ---------------------------------------------------------------- 默认与兼容

def test_new_project_gets_compat_five_class_and_pointer(db, client, project_id):
    row = _active(db, project_id)
    assert row is not None and row.status == "ACTIVE" and row.version == 1
    assert row.published_at is not None
    detail = client.get(f"/api/v1/projects/{project_id}/label-schemas/active").json()
    assert detail["label_keys"] == FIVE
    assert all(lb["effect_type"] == lb["key"] for lb in detail["document"]["labels"])  # 恒等映射
    # 兼容旧接口：等价 active，响应补元信息
    legacy = client.get(f"/api/v1/projects/{project_id}/label-schema").json()
    assert legacy["id"] == row.id and legacy["version"] == 1
    assert legacy["status"] == "ACTIVE" and legacy["schema_format"] == "intent-schema-v2"
    assert [lb["key"] for lb in legacy["labels"]] == FIVE


# ---------------------------------------------------------------- 草稿生命周期

def _draft(client, project_id) -> dict:
    resp = client.post(f"/api/v1/projects/{project_id}/label-schemas/drafts",
                       json={"change_summary": "两分类试点"})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_draft_conflict_and_delete(db, client, project_id):
    first = _draft(client, project_id)
    resp = client.post(f"/api/v1/projects/{project_id}/label-schemas/drafts", json={})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "LABEL_SCHEMA_DRAFT_EXISTS"
    # 删除草稿后可再建
    assert client.delete(f"/api/v1/projects/{project_id}/label-schemas/{first['id']}").status_code == 200
    assert _draft(client, project_id)["status"] == "DRAFT"


def test_update_draft_with_optimistic_lock(db, client, project_id):
    draft = _draft(client, project_id)
    resp = client.patch(
        f"/api/v1/projects/{project_id}/label-schemas/{draft['id']}",
        json={"expected_hash": "0" * 64, "labels": _two_class_labels()},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "LABEL_SCHEMA_CONFLICT"
    # 正确 hash → 两分类生效
    resp = client.patch(
        f"/api/v1/projects/{project_id}/label-schemas/{draft['id']}",
        json={"expected_hash": draft["hash"], "labels": _two_class_labels(), "change_summary": "改两分类"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["label_keys"] == ["faq", "create_task"]
    assert body["status"] == "DRAFT"
    new_hash = body["hash"]
    assert new_hash != draft["hash"]


@pytest.mark.parametrize(
    "labels, code",
    [
        ([{"key": "Bad Key", "name": "x", "effect_type": "information"},
          {"key": "ok_label", "name": "y", "effect_type": "information"}], "INVALID_LABEL_KEY"),
        ([{"key": "dup", "name": "x", "effect_type": "information"},
          {"key": "dup", "name": "y", "effect_type": "read_only"}], "INVALID_LABEL_KEY"),
        ([{"key": "a_label", "name": "x", "effect_type": "deploy_everything"},
          {"key": "b_label", "name": "y", "effect_type": "information"}], "INVALID_EFFECT_TYPE"),
        ([{"key": "only", "name": "单标签", "effect_type": "information"}], "VALIDATION_ERROR"),
    ],
)
def test_update_draft_validation_codes(db, client, project_id, labels, code):
    draft = _draft(client, project_id)
    resp = client.patch(
        f"/api/v1/projects/{project_id}/label-schemas/{draft['id']}",
        json={"expected_hash": draft["hash"], "labels": labels},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == code


def test_published_schema_is_immutable(db, client, project_id):
    draft = _draft(client, project_id)
    client.patch(f"/api/v1/projects/{project_id}/label-schemas/{draft['id']}",
                 json={"expected_hash": draft["hash"], "labels": _two_class_labels()})
    published = client.post(
        f"/api/v1/projects/{project_id}/label-schemas/{draft['id']}/publish",
        json={"expected_hash": client.get(
            f"/api/v1/projects/{project_id}/label-schemas/{draft['id']}").json()["hash"],
            "confirm_breaking_changes": True},  # 整体替换=移除五个 active 标签，属破坏性
    )
    assert published.status_code == 200
    schema_id = published.json()["id"]
    # 已发布：PATCH 与 DELETE 都不可
    resp = client.patch(f"/api/v1/projects/{project_id}/label-schemas/{schema_id}",
                        json={"expected_hash": published.json()["hash"], "labels": _two_class_labels()})
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "LABEL_SCHEMA_IMMUTABLE"
    resp = client.delete(f"/api/v1/projects/{project_id}/label-schemas/{schema_id}")
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "LABEL_SCHEMA_IMMUTABLE"


def test_publish_switches_pointer_and_supersedes(db, client, project_id):
    old_active = _active(db, project_id)
    draft = _draft(client, project_id)
    client.patch(f"/api/v1/projects/{project_id}/label-schemas/{draft['id']}",
                 json={"expected_hash": draft["hash"], "labels": _two_class_labels()})
    current = client.get(f"/api/v1/projects/{project_id}/label-schemas/{draft['id']}").json()
    resp = client.post(f"/api/v1/projects/{project_id}/label-schemas/{draft['id']}/publish",
                       json={"expected_hash": current["hash"], "confirm_breaking_changes": True})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ACTIVE" and body["label_keys"] == ["faq", "create_task"]
    db.expire_all()
    assert _active(db, project_id).id == draft["id"]
    assert db.get(LabelSchemaVersion, old_active.id).status == "SUPERSEDED"
    # 兼容接口与 active 接口都指向新 Schema
    legacy = client.get(f"/api/v1/projects/{project_id}/label-schema").json()
    assert [lb["key"] for lb in legacy["labels"]] == ["faq", "create_task"]


def test_publish_rejects_identical_content(db, client, project_id):
    draft = _draft(client, project_id)  # 基于 active 同内容创建
    resp = client.post(f"/api/v1/projects/{project_id}/label-schemas/{draft['id']}/publish",
                       json={"expected_hash": draft["hash"]})
    assert resp.status_code == 409
    assert "相同" in resp.json()["error"]["message"]


# ---------------------------------------------------------------- 影响分析与破坏性确认

def test_effect_type_change_requires_confirmation(db, client, project_id):
    """仅修改既有标签的 effect type（不动标签集合）：破坏性 + 需二次确认 + 审计。"""
    detail = client.get(f"/api/v1/projects/{project_id}/label-schemas/active").json()
    labels = [dict(lb) for lb in detail["document"]["labels"]]
    for lb in labels:
        if lb["key"] == "write_action":
            lb["effect_type"] = "read_only"  # 写 → 只读：安全降级同样是语义变化
    draft = _draft(client, project_id)
    client.patch(f"/api/v1/projects/{project_id}/label-schemas/{draft['id']}",
                 json={"expected_hash": draft["hash"], "labels": labels})
    impact = client.post(f"/api/v1/projects/{project_id}/label-schemas/{draft['id']}/impact").json()
    assert impact["breaking"] is True
    assert impact["removed"] == [] and impact["added"] == []
    assert impact["effect_type_changed"] == [{"key": "write_action", "from": "write_action", "to": "read_only"}]
    current = client.get(f"/api/v1/projects/{project_id}/label-schemas/{draft['id']}").json()
    resp = client.post(f"/api/v1/projects/{project_id}/label-schemas/{draft['id']}/publish",
                       json={"expected_hash": current["hash"]})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "LABEL_SCHEMA_CONFLICT"
    resp = client.post(f"/api/v1/projects/{project_id}/label-schemas/{draft['id']}/publish",
                       json={"expected_hash": current["hash"], "confirm_breaking_changes": True})
    assert resp.status_code == 200
    # effect type 变更写审计
    db.expire_all()
    events = db.query(AuditEvent).filter(
        AuditEvent.project_id == project_id, AuditEvent.event == "LABEL_EFFECT_TYPE_CHANGED"
    ).all()
    assert any(e.details.get("key") == "write_action" for e in events)


def test_adding_labels_is_non_breaking(db, client, project_id):
    detail = client.get(f"/api/v1/projects/{project_id}/label-schemas/active").json()
    labels = [dict(lb) for lb in detail["document"]["labels"]]
    labels.append({"key": "status_query", "name": "状态查询", "effect_type": "read_only", "order": 50})
    draft = _draft(client, project_id)
    client.patch(f"/api/v1/projects/{project_id}/label-schemas/{draft['id']}",
                 json={"expected_hash": draft["hash"], "labels": labels})
    impact = client.post(f"/api/v1/projects/{project_id}/label-schemas/{draft['id']}/impact").json()
    assert impact["breaking"] is False
    assert impact["added"] == ["status_query"] and impact["removed"] == []
    assert impact["requires_retraining"] is True


# ---------------------------------------------------------------- 审计与隔离

def test_lifecycle_audit_events(db, client, project_id):
    draft = _draft(client, project_id)
    client.patch(f"/api/v1/projects/{project_id}/label-schemas/{draft['id']}",
                 json={"expected_hash": draft["hash"], "labels": _two_class_labels()})
    current = client.get(f"/api/v1/projects/{project_id}/label-schemas/{draft['id']}").json()
    client.post(f"/api/v1/projects/{project_id}/label-schemas/{draft['id']}/publish",
                json={"expected_hash": current["hash"], "confirm_breaking_changes": True})
    db.expire_all()
    events = {e.event for e in db.query(AuditEvent).filter(AuditEvent.project_id == project_id).all()}
    assert {"LABEL_SCHEMA_DRAFT_CREATED", "LABEL_SCHEMA_UPDATED", "LABEL_SCHEMA_PUBLISHED"} <= events
    # 审计详情不含训练文本（只有 id/key 级字段）
    for e in db.query(AuditEvent).filter(AuditEvent.project_id == project_id).all():
        assert set(json.dumps(e.details or {})) and "positive_example" not in json.dumps(e.details or {})


def test_cross_project_schema_isolated(db, client, project_id):
    other = client.post("/api/v1/projects", json={"name": "另一项目", "description": ""}).json()["id"]
    active_other = _active(db, other)
    resp = client.get(f"/api/v1/projects/{project_id}/label-schemas/{active_other.id}")
    assert resp.status_code == 404
    resp = client.post(f"/api/v1/projects/{project_id}/label-schemas/{active_other.id}/publish",
                       json={"expected_hash": active_other.hash})
    assert resp.status_code == 404


