"""数据集服务集成测试：上传 → 导入 → 校验 → 样本 → 草稿 → 提交 → 切分。"""
from __future__ import annotations

import csv
import io

import pytest

from app.errors import ApiError
from app.services import dataset_service


def _csv_bytes(rows: list[dict], header: list[str] | None = None) -> bytes:
    header = header or ["text", "label", "group_id", "risk_slice", "is_hard_negative"]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=header)
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8-sig")


def _balanced_rows(per_label: int = 8) -> list[dict]:
    rows = []
    for lab in ("information", "read_only", "write_action", "unclear", "oos"):
        for i in range(per_label):
            rows.append(
                {
                    "text": f"{lab} 的第 {i} 条样本问句",
                    "label": lab,
                    "group_id": f"{lab}-g{i % 4}",
                    "risk_slice": "qa_vs_write" if i % 5 == 0 else "",
                    "is_hard_negative": "true" if i % 5 == 0 else "false",
                }
            )
    return rows


@pytest.fixture
def uploaded(db, project_id):
    def _upload(rows: list[dict] | None = None, name: str = "test.csv"):
        upload = dataset_service.save_upload(db, project_id, name, _csv_bytes(rows or _balanced_rows()), "text/csv")
        return upload

    return _upload


def test_upload_rejects_bad_extension(db, project_id):
    with pytest.raises(ApiError) as exc:
        dataset_service.save_upload(db, project_id, "evil.zip", b"PK", "application/zip")
    assert exc.value.code == "UNSUPPORTED_FILE_TYPE"


def test_preview_and_import_flow(db, uploaded):
    upload = uploaded()
    preview = dataset_service.preview_upload(db, upload.id)
    assert "text" in preview["columns"]
    assert preview["suggested_columns"]["text"] == "text"
    assert preview["row_count"] == 40

    dataset = dataset_service.import_upload(
        db,
        upload.id,
        {"mode": "prelabeled", "columns": {"text": "text", "label": "label", "group_id": "group_id", "risk_slice": "risk_slice", "is_hard_negative": "is_hard_negative"}, "name": "测试集"},
    )
    assert dataset.status == "FROZEN"
    assert dataset.sample_count == 40
    assert dataset.labeled_count == 40
    report = dataset_service.latest_report(db, dataset.id)
    assert report["errors"] == []
    assert report["stats"]["label_distribution"]["write_action"] == 8

    samples = dataset_service.list_samples(db, dataset.id, {"label": "write_action"})
    assert samples["total"] == 8
    assert all(s["label"] == "write_action" for s in samples["samples"])


def test_import_label_mapping_and_unknown_label(db, uploaded):
    upload = uploaded(
        [
            {"text": "怎么创建实验", "label": "查信息"},
            {"text": "帮我创建实验", "label": "write_action"},
        ]
        + [
            {"text": f"填充样本 {lab} {i}", "label": lab, "group_id": f"g{i}", "risk_slice": "", "is_hard_negative": "false"}
            for lab in ("information", "read_only", "write_action", "unclear", "oos")
            for i in range(2)
        ]
    )
    # 未知标签未映射 → 记录 error
    dataset = dataset_service.import_upload(
        db, upload.id, {"mode": "prelabeled", "columns": {"text": "text", "label": "label"}},
    )
    report = dataset_service.latest_report(db, dataset.id)
    codes = [e["code"] for e in report["errors"]]
    assert "INVALID_LABEL" in codes
    # 非法标签样本整行跳过（12 行 → 11 个样本，全部有标签）
    assert dataset.sample_count == 11
    assert dataset.labeled_count == 11

    # 映射后重新导入另一个上传
    upload2 = dataset_service.save_upload(db, upload.project_id, "mapped.csv", _csv_bytes([
        {"text": "怎么创建实验", "label": "查信息", "group_id": "g0", "risk_slice": "", "is_hard_negative": "false"},
        {"text": "帮我创建实验", "label": "write_action", "group_id": "g1", "risk_slice": "", "is_hard_negative": "false"},
    ] + [
        {"text": f"填充 {lab} {i}", "label": lab, "group_id": f"g{i}", "risk_slice": "", "is_hard_negative": "false"}
        for lab in ("information", "read_only", "unclear", "oos") for i in range(2)
    ]), "text/csv")
    dataset2 = dataset_service.import_upload(
        db,
        upload2.id,
        {"mode": "prelabeled", "columns": {"text": "text", "label": "label"}, "label_mapping": {"查信息": "information"}},
    )
    report2 = dataset_service.latest_report(db, dataset2.id)
    assert report2["errors"] == []
    assert dataset2.label_distribution.get("information") == 3


def test_label_conflict_blocks(db, project_id):
    rows = [
        {"text": "同一句话", "label": "read_only", "group_id": "g0", "risk_slice": "", "is_hard_negative": "false"},
        {"text": "同一句话", "label": "write_action", "group_id": "g0", "risk_slice": "", "is_hard_negative": "false"},
    ] + [
        {"text": f"填充 {lab} {i}", "label": lab, "group_id": f"g{i}", "risk_slice": "", "is_hard_negative": "false"}
        for lab in ("information", "read_only", "write_action", "unclear", "oos")
        for i in range(2)
    ]
    upload = dataset_service.save_upload(db, project_id, "conflict.csv", _csv_bytes(rows), "text/csv")
    dataset = dataset_service.import_upload(db, upload.id, {"mode": "prelabeled", "columns": {"text": "text", "label": "label"}})
    report = dataset_service.latest_report(db, dataset.id)
    codes = [e["code"] for e in report["errors"]]
    assert "DATASET_LABEL_CONFLICT" in codes
    # 创建 Run 时应被 QUALITY_ERRORS 拦截
    from app.services import run_service

    with pytest.raises(ApiError) as exc:
        run_service.create_run(db, project_id, dataset.id, "", None)
    assert exc.value.code == "QUALITY_ERRORS"


def test_frozen_immutable_and_draft_flow(db, uploaded):
    upload = uploaded()
    dataset = dataset_service.import_upload(db, upload.id, {"mode": "prelabeled", "columns": {"text": "text", "label": "label", "group_id": "group_id"}})
    sample = dataset_service.list_samples(db, dataset.id, {})["samples"][0]

    with pytest.raises(ApiError) as exc:
        dataset_service.update_sample(db, dataset.id, sample["sample_id"], {"label": "oos"})
    assert exc.value.code == "DATASET_IMMUTABLE"

    # 错误样本回流：创建草稿 → 修改 → 提交
    draft = dataset_service.create_draft(
        db,
        dataset.id,
        [
            {"action": "update", "sample_id": sample["sample_id"], "label": "oos", "note": "修正标注"},
            {"action": "add", "text": "帮我预订会议室", "label": "oos", "group_id": "oos-extra", "is_hard_negative": True},
        ],
        "错误回流草稿",
    )
    assert draft.status == "DRAFT"
    assert draft.parent_id == dataset.id
    assert draft.sample_count == dataset.sample_count + 1

    updated = dataset_service.update_sample(db, draft.id, sample["sample_id"], {"label": "unclear"})
    assert updated["label"] == "unclear"

    frozen = dataset_service.commit_draft(db, draft.id)
    assert frozen.status == "FROZEN"


def test_split_and_unlabeled_guard(db, uploaded):
    # 每类 3 组 × 2 行（满足切分最小分组数），标注后可切分
    rows = [
        {"text": f"未标注 {lab} {g} {i}", "label": "", "group_id": f"{lab}-g{g}"}
        for lab in ("information", "read_only", "write_action", "unclear", "oos")
        for g in range(3)
        for i in range(2)
    ]
    upload = dataset_service.save_upload(db, uploaded().project_id, "unlabeled.csv", _csv_bytes(rows), "text/csv")
    dataset = dataset_service.import_upload(db, upload.id, {"mode": "unlabeled", "columns": {"text": "text", "group_id": "group_id"}})
    assert dataset.status == "DRAFT"

    with pytest.raises(ApiError) as exc:
        dataset_service.create_split(db, dataset.id)
    assert exc.value.code == "UNLABELED_SAMPLES"

    # 标注全部样本（group_id 前缀即类别名）
    page = dataset_service.list_samples(db, dataset.id, {"unlabeled_only": True})
    assert page["total"] == len(rows)
    for sample in page["samples"]:
        dataset_service.update_sample(db, dataset.id, sample["sample_id"], {"label": sample["group_id"].split("-")[0]})

    from app.services import run_service

    with pytest.raises(ApiError) as exc:
        run_service.create_run(db, dataset.project_id, dataset.id, "", None)
    assert exc.value.code in ("DATASET_NOT_FROZEN", "MISSING_CLASS", "QUALITY_ERRORS")

    frozen = dataset_service.commit_draft(db, dataset.id)
    split = dataset_service.create_split(db, frozen.id, seed=7)
    stats = split.stats_json
    assert stats["rows"]["train"] + stats["rows"]["validation"] + stats["rows"]["test"] == len(rows)
    for part in ("train", "validation", "test"):
        assert stats["rows"][part] > 0


def test_create_split_blocks_insufficient_groups(db, uploaded):
    """每类只有 2 个语义组：切分被结构化阻断，而非静默全进 train。"""
    rows = [
        {"text": f"样本 {lab} {g} {i}", "label": lab, "group_id": f"{lab}-g{g}", "risk_slice": "", "is_hard_negative": "false"}
        for lab in ("information", "read_only", "write_action", "unclear", "oos")
        for g in range(2)
        for i in range(4)
    ]
    upload = dataset_service.save_upload(db, uploaded().project_id, "few-groups.csv", _csv_bytes(rows), "text/csv")
    dataset = dataset_service.import_upload(db, upload.id, {"mode": "prelabeled", "columns": {"text": "text", "label": "label", "group_id": "group_id"}})

    with pytest.raises(ApiError) as exc:
        dataset_service.create_split(db, dataset.id)
    assert exc.value.code == "INSUFFICIENT_SPLIT_GROUPS"
    assert exc.value.details["label"] in ("information", "read_only", "write_action", "unclear", "oos")
    assert exc.value.details["group_count"] == 2
