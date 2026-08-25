"""五分类 Schema 全链路强校验（修改方案 V2 §3.4）。

三层防线：
1. Schema 层：DraftChange/SamplePatch/ImportConfig.default_label/expected_label
   使用唯一 IntentLabel 类型，非法标签入口即 422；
2. 服务层：草稿 update 补上 add 已有的标签校验；validate_dataset 阻断
   非法/空标签/缺类/冲突，并指出样本 ID（冻结被 QUALITY_ERRORS 拦截）；
3. Worker 层：训练前断言标签集与五分类完全一致（缺类/越界在加载模型前失败）。
"""
from __future__ import annotations

import csv
import io

import pandas as pd
import pytest
from pydantic import ValidationError

from app.errors import ApiError
from app.router_core.taxonomy import LABELS, ensure_label_schema
from app.schemas import DraftChange, ImportConfig, PlaygroundCaseRequest, SamplePatch
from app.services import dataset_service

# ---------------------------------------------------------------- Schema 层

def test_intent_label_literal_matches_labels():
    from typing import get_args

    from app.router_core.taxonomy import IntentLabel

    assert set(get_args(IntentLabel)) == set(LABELS)


@pytest.mark.parametrize(
    "model,kw",
    [
        (DraftChange, {"action": "update", "sample_id": "smp_x", "label": "破坏"}),
        (DraftChange, {"action": "add", "text": "看下状态", "label": "nota"}),
        (SamplePatch, {"label": "WRITE_ACTION"}),
        (ImportConfig, {"mode": "single_label", "default_label": "其他"}),
        (PlaygroundCaseRequest, {"text": "q", "expected_label": "任意"}),
    ],
)
def test_illegal_label_rejected_at_schema(model, kw):
    with pytest.raises(ValidationError):
        model(**kw)


def test_legal_labels_accepted_at_schema():
    for lab in LABELS:
        assert SamplePatch(label=lab).label == lab
        assert DraftChange(action="update", sample_id="smp_x", label=lab).label == lab


# ---------------------------------------------------------------- Worker 层

def test_ensure_label_schema_rejects_bad_and_missing():
    with pytest.raises(ApiError) as exc:
        ensure_label_schema(["information", "不存在的标签"])
    assert exc.value.code == "INVALID_LABEL"

    with pytest.raises(ApiError) as exc:
        ensure_label_schema(["information", "read_only", "write_action", "unclear"])  # 缺 oos
    assert exc.value.code == "MISSING_LABEL_CLASS"
    assert exc.value.details["missing"] == ["oos"]

    # None / 空串同样是非法标签
    with pytest.raises(ApiError) as exc:
        ensure_label_schema(["information", None])
    assert exc.value.code == "INVALID_LABEL"

    # 五类齐全通过
    ensure_label_schema(LABELS * 3)


# ---------------------------------------------------------------- 服务层

def _csv_bytes(rows: list[dict]) -> bytes:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=["text", "label", "group_id"])
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8-sig")


@pytest.fixture
def balanced_dataset(db, project_id):
    rows = [
        {"text": f"{lab} 的第 {i} 条样本", "label": lab, "group_id": f"{lab}-g{i % 3}"}
        for lab in LABELS
        for i in range(3)
    ]
    upload = dataset_service.save_upload(db, project_id, "labels.csv", _csv_bytes(rows), "text/csv")
    return dataset_service.import_upload(
        db, upload.id, {"mode": "prelabeled", "columns": {"text": "text", "label": "label", "group_id": "group_id"}}
    )


def _frame(rows: list[dict]) -> pd.DataFrame:
    base = {
        "sample_id": [r["sample_id"] for r in rows],
        "text": [r["text"] for r in rows],
        "label": [r.get("label") for r in rows],
        "normalized_hash": [r["normalized_hash"] for r in rows],
        "context": [None] * len(rows),
        "is_hard_negative": [False] * len(rows),
        "risk_slice": [None] * len(rows),
        "group_id": [None] * len(rows),
        "source": ["test"] * len(rows),
        "metadata_json": [None] * len(rows),
    }
    return pd.DataFrame(base)


def test_validate_dataset_clean_passes(balanced_dataset, db):
    report = dataset_service.validate_dataset(db, balanced_dataset.id)
    assert [e["code"] for e in report["errors"]] == []


def test_validate_dataset_blocks_bad_empty_missing_with_sample_ids(balanced_dataset, db, monkeypatch):
    rows = [
        {"sample_id": "smp_a", "text": "a", "label": "information", "normalized_hash": "h1"},
        {"sample_id": "smp_b", "text": "b", "label": "恶意标签", "normalized_hash": "h2"},
        {"sample_id": "smp_c", "text": "c", "label": "", "normalized_hash": "h3"},
        {"sample_id": "smp_d", "text": "d", "label": "read_only", "normalized_hash": "h4"},
    ]
    monkeypatch.setattr(dataset_service, "load_dataset_frame", lambda d: _frame(rows))
    report = dataset_service.validate_dataset(db, balanced_dataset.id)
    codes = [e["code"] for e in report["errors"]]
    assert "INVALID_LABEL" in codes and "EMPTY_LABEL" in codes and "MISSING_LABEL_CLASS" in codes
    invalid = next(e for e in report["errors"] if e["code"] == "INVALID_LABEL")
    assert invalid["details"]["sample_ids"] == ["smp_b"]
    empty = next(e for e in report["errors"] if e["code"] == "EMPTY_LABEL")
    assert empty["details"]["sample_ids"] == ["smp_c"]


def test_validate_dataset_conflict_lists_sample_ids(balanced_dataset, db, monkeypatch):
    rows = [
        {"sample_id": f"smp_{i}", "text": f"t{i}", "label": lab, "normalized_hash": "h_same"}
        for i, lab in enumerate(LABELS)
    ]
    rows.append({"sample_id": "smp_conflict", "text": "冲突样本", "label": "information", "normalized_hash": "h_same"})
    # h_same 上 write_action(第2条) 与 information(冲突条) 不一致
    monkeypatch.setattr(dataset_service, "load_dataset_frame", lambda d: _frame(rows))
    report = dataset_service.validate_dataset(db, balanced_dataset.id)
    conflict = next(e for e in report["errors"] if e["code"] == "DATASET_LABEL_CONFLICT")
    assert "smp_conflict" in conflict["details"]["sample_ids"]
    assert set(conflict["details"]["labels"]) >= {"information", "write_action"}


def test_commit_draft_blocked_by_missing_class(balanced_dataset, db, monkeypatch):
    rows = [
        {"sample_id": f"smp_{i}", "text": f"t{i}", "label": lab, "normalized_hash": f"h{i}"}
        for i, lab in enumerate(LABELS[:4])  # 缺 oos
    ]
    frame = _frame(rows)

    draft = dataset_service.create_draft(
        db,
        balanced_dataset.id,
        [{"action": "remove", "sample_id": "whatever"}],  # 结构性变更占位，实际标签由 monkeypatch 决定
        "缺类草稿",
    )
    monkeypatch.setattr(dataset_service, "load_dataset_frame", lambda d: frame)
    with pytest.raises(ApiError) as exc:
        dataset_service.commit_draft(db, draft.id)
    assert exc.value.code == "QUALITY_ERRORS"
    assert any(p["code"] == "MISSING_LABEL_CLASS" for p in exc.value.details["report"])


def test_create_draft_update_validates_label(balanced_dataset, db):
    sample = dataset_service.list_samples(db, balanced_dataset.id, {})["samples"][0]
    # 服务层兜底：即使绕过 Schema 直接调服务，update 非法标签也被拒绝
    with pytest.raises(ApiError) as exc:
        dataset_service.create_draft(
            db,
            balanced_dataset.id,
            [{"action": "update", "sample_id": sample["sample_id"], "label": "nota"}],
        )
    assert exc.value.code == "INVALID_LABEL"
