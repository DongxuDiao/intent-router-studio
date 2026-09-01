"""标签 Schema 服务（自定义意图标签方案 §3/§5）。

- 读取经 projects.active_label_schema_id 显式指针（不再按最大 version 推断）
- 草稿 → 影响分析 → 发布 事务化；发布后不可变（修改产生新草稿版本）
- 审计事件：LABEL_SCHEMA_DRAFT_CREATED/UPDATED/PUBLISHED/EFFECT_TYPE_CHANGED
  详情只含 key、旧/新 effect type、Schema ID 与操作者，不含训练文本
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app import ids
from app.errors import ApiError, ConflictError, NotFoundError
from app.models import (
    AuditEvent,
    DatasetVersion,
    LabelSchemaVersion,
    ModelVersion,
    Project,
    TrainingRun,
)
from app.router_core.label_schema import (
    LabelDefinition,
    LabelSchemaDocument,
    ResolvedLabelSchema,
    default_compat_document,
    document_from_json,
    normalize,
    schema_hash,
    validate_schema,
)

logger = logging.getLogger("app.label_schema")

SCHEMA_STATUSES = ("DRAFT", "ACTIVE", "SUPERSEDED")


def _audit(db: Session, project_id: str, event: str, details: dict[str, Any] | None = None) -> None:
    db.add(AuditEvent(id=ids.prefixed("aud"), project_id=project_id, event=event, details=details or {}))


# ---------------------------------------------------------------- 读取

def get_active_row(db: Session, project_id: str) -> LabelSchemaVersion | None:
    """当前生效 Schema：显式指针优先；指针缺失时回退最新 ACTIVE 行。"""
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError("Project", project_id)
    if project.active_label_schema_id:
        row = db.get(LabelSchemaVersion, project.active_label_schema_id)
        if row is not None and row.project_id == project_id:
            return row
    return (
        db.query(LabelSchemaVersion)
        .filter(LabelSchemaVersion.project_id == project_id, LabelSchemaVersion.status == "ACTIVE")
        .order_by(LabelSchemaVersion.version.desc())
        .first()
    )


def resolve_document(row: LabelSchemaVersion) -> LabelSchemaDocument:
    return document_from_json(row.schema_json)


def active_document(db: Session, project_id: str) -> tuple[LabelSchemaVersion | None, LabelSchemaDocument]:
    row = get_active_row(db, project_id)
    if row is None:
        return None, default_compat_document()
    return row, resolve_document(row)


# ---------------------------------------------------------------- 统一运行时上下文（Review 修复 §2）

def resolve_schema_context(row: LabelSchemaVersion) -> ResolvedLabelSchema:
    return ResolvedLabelSchema.from_document(resolve_document(row), row.id, row.hash)


def _compat_context() -> ResolvedLabelSchema:
    """历史 v1 数据的恒等五分类适配（仅读取路径；新 Run 由 create_run 拦截）。"""
    doc = default_compat_document()
    return ResolvedLabelSchema.from_document(doc, None, schema_hash(doc))


def resolve_project_active_schema(db: Session, project_id: str) -> ResolvedLabelSchema:
    """项目入口：仅用于创建新数据集或新迁移任务（当前 Active Schema）。"""
    row = get_active_row(db, project_id)
    if row is None:
        raise NotFoundError("LabelSchemaVersion", f"project:{project_id}:active")
    return resolve_schema_context(row)


def resolve_dataset_schema(db: Session, dataset: DatasetVersion) -> ResolvedLabelSchema:
    """已存在数据集的查看/编辑/校验/切分/训练入口：只认 dataset.schema_id。

    - 绑定版本不可变：项目发布新 Schema 不影响旧数据集；
    - schema_id 缺失视为历史 v1 数据，仅读取路径兼容适配恒等五分类；
      启动新 Run 在 run_service.create_run 以 DATASET_SCHEMA_MISMATCH 拦截。
    """
    if dataset.schema_id:
        row = db.get(LabelSchemaVersion, dataset.schema_id)
        if row is None or row.project_id != dataset.project_id:
            raise ApiError(
                "DATASET_SCHEMA_MISMATCH",
                f"数据集绑定的标签 Schema 不存在: {dataset.schema_id}",
                409,
                {"schema_id": dataset.schema_id, "dataset_id": dataset.id},
            )
        return resolve_schema_context(row)
    return _compat_context()


def _get_row(db: Session, project_id: str, schema_id: str) -> LabelSchemaVersion:
    row = db.get(LabelSchemaVersion, schema_id)
    if row is None or row.project_id != project_id:
        raise NotFoundError("LabelSchemaVersion", schema_id)
    return row


def _reference_counts(db: Session, schema_ids: list[str]) -> dict[str, dict[str, int]]:
    """各 Schema 版本被数据集 / Run / 模型引用的计数（Run/模型经数据集聚合）。"""
    counts = {sid: {"datasets": 0, "runs": 0, "models": 0} for sid in schema_ids}
    if not schema_ids:
        return counts
    datasets = db.query(DatasetVersion).filter(DatasetVersion.schema_id.in_(schema_ids)).all()
    dataset_by_schema: dict[str, list[str]] = {}
    for d in datasets:
        dataset_by_schema.setdefault(d.schema_id, []).append(d.id)
        if d.schema_id:
            counts[d.schema_id]["datasets"] += 1
    all_dataset_ids = [d.id for d in datasets]
    runs: dict[str, str] = {}
    models: dict[str, str] = {}
    if all_dataset_ids:
        for run in db.query(TrainingRun).filter(TrainingRun.dataset_id.in_(all_dataset_ids)).all():
            runs[run.id] = run.dataset_id
        run_ids = list(runs)
        if run_ids:
            for model in db.query(ModelVersion).filter(ModelVersion.run_id.in_(run_ids)).all():
                models[model.id] = runs.get(model.run_id, "")
    dataset_to_schema = {d.id: d.schema_id for d in datasets}
    for run_ds in runs.values():
        sid = dataset_to_schema.get(run_ds)
        if sid:
            counts[sid]["runs"] += 1
    for model_ds in models.values():
        sid = dataset_to_schema.get(model_ds)
        if sid:
            counts[sid]["models"] += 1
    return counts


def _row_to_dict(row: LabelSchemaVersion, refs: dict[str, int]) -> dict[str, Any]:
    doc = resolve_document(row)
    return {
        "id": row.id,
        "project_id": row.project_id,
        "version": row.version,
        "status": row.status,
        "parent_id": row.parent_id,
        "change_summary": row.change_summary,
        "created_by": row.created_by,
        "hash": row.hash,
        "schema_format": doc.schema_format,
        "active_label_count": len(doc.active_labels()),
        "deprecated_label_count": len(doc.labels) - len(doc.active_labels()),
        "label_keys": doc.label_keys_in_order(include_deprecated=True),
        "published_at": row.published_at.isoformat() if row.published_at else None,
        "created_at": row.created_at.isoformat(),
        "references": refs,
    }


def list_schemas(db: Session, project_id: str) -> list[dict[str, Any]]:
    rows = (
        db.query(LabelSchemaVersion)
        .filter(LabelSchemaVersion.project_id == project_id)
        .order_by(LabelSchemaVersion.version.desc())
        .all()
    )
    refs = _reference_counts(db, [r.id for r in rows])
    return [_row_to_dict(r, refs.get(r.id, {})) for r in rows]


def schema_detail(db: Session, project_id: str, schema_id: str) -> dict[str, Any]:
    row = _get_row(db, project_id, schema_id)
    refs = _reference_counts(db, [row.id]).get(row.id, {})
    out = _row_to_dict(row, refs)
    doc = resolve_document(row)
    out["schema_json"] = doc.to_dict()
    out["document"] = doc.to_dict()  # 同内容别名，前端读 document
    return out


# ---------------------------------------------------------------- 草稿

def create_draft(
    db: Session, project_id: str, base_schema_id: str | None = None, change_summary: str = ""
) -> LabelSchemaVersion:
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError("Project", project_id)
    existing = (
        db.query(LabelSchemaVersion)
        .filter(LabelSchemaVersion.project_id == project_id, LabelSchemaVersion.status == "DRAFT")
        .first()
    )
    if existing is not None:
        raise ConflictError(
            "LABEL_SCHEMA_DRAFT_EXISTS",
            f"项目已有编辑中草稿（v{existing.version}），先发布或删除该草稿",
            {"draft_id": existing.id},
        )
    if base_schema_id:
        base = _get_row(db, project_id, base_schema_id)
    else:
        base = get_active_row(db, project_id)
    base_doc = resolve_document(base) if base is not None else default_compat_document()
    last = (
        db.query(LabelSchemaVersion)
        .filter(LabelSchemaVersion.project_id == project_id)
        .order_by(LabelSchemaVersion.version.desc())
        .first()
    )
    row = LabelSchemaVersion(
        id=ids.prefixed(ids.LABEL_SCHEMA),
        project_id=project_id,
        version=(last.version + 1) if last else 1,
        schema_json=base_doc.to_dict(),
        hash=schema_hash(base_doc),
        status="DRAFT",
        parent_id=base.id if base is not None else None,
        change_summary=change_summary or "",
        created_by="local",
    )
    db.add(row)
    _audit(db, project_id, "LABEL_SCHEMA_DRAFT_CREATED",
           {"schema_id": row.id, "base_schema_id": base.id if base is not None else None,
            "version": row.version})
    db.commit()
    db.refresh(row)
    return row


def _labels_from_payload(raw_labels: list[dict[str, Any]]) -> list[LabelDefinition]:
    labels: list[LabelDefinition] = []
    for i, item in enumerate(raw_labels):
        if not isinstance(item, dict):
            raise ApiError("VALIDATION_ERROR", f"labels[{i}] 必须是对象", 422)
        labels.append(
            LabelDefinition(
                key=str(item.get("key") or ""),
                name=str(item.get("name") or ""),
                description=str(item.get("description") or ""),
                effect_type=str(item.get("effect_type") or ""),
                status=str(item.get("status") or "active"),
                order=item.get("order") if isinstance(item.get("order"), int) else i * 10,
                positive_examples=[str(x) for x in (item.get("positive_examples") or []) if x],
                negative_examples=[str(x) for x in (item.get("negative_examples") or []) if x],
            )
        )
    return labels


def update_draft(
    db: Session,
    project_id: str,
    schema_id: str,
    expected_hash: str,
    labels: list[dict[str, Any]],
    change_summary: str | None = None,
) -> LabelSchemaDocument:
    row = _get_row(db, project_id, schema_id)
    if row.status != "DRAFT":
        raise ConflictError(
            "LABEL_SCHEMA_IMMUTABLE",
            f"Schema v{row.version} 已发布（{row.status}），修改需创建新草稿",
            {"status": row.status},
        )
    if expected_hash != row.hash:
        raise ConflictError(
            "LABEL_SCHEMA_CONFLICT",
            "expected_hash 与当前草稿不一致（他人已修改），请刷新后重试",
            {"expected": expected_hash, "actual": row.hash},
        )
    doc = normalize(LabelSchemaDocument(labels=_labels_from_payload(labels)))
    problems = validate_schema(doc)
    if problems:
        # 细分稳定错误码（§9）：key 与 effect 各自归类，其余归 VALIDATION_ERROR
        for problem in problems:
            if "key" in problem:
                raise ApiError("INVALID_LABEL_KEY", problem, 422)
            if "effect_type" in problem:
                raise ApiError("INVALID_EFFECT_TYPE", problem, 422)
        raise ApiError("VALIDATION_ERROR", "标签 Schema 不合法", 422, {"problems": problems[:10]})

    effect_changes = _effect_type_diff(resolve_document(row), doc)
    row.schema_json = doc.to_dict()
    row.hash = schema_hash(doc)
    if change_summary is not None:
        row.change_summary = change_summary
    _audit(db, project_id, "LABEL_SCHEMA_UPDATED", {"schema_id": row.id, "hash": row.hash})
    if effect_changes:
        _audit(db, project_id, "LABEL_EFFECT_TYPE_CHANGED",
               {"schema_id": row.id, "changes": effect_changes})
    db.commit()
    return doc


def _effect_type_diff(old: LabelSchemaDocument, new: LabelSchemaDocument) -> list[dict[str, str]]:
    old_map = {d.key: d.effect_type for d in old.labels}
    changes = []
    for d in new.labels:
        if d.key in old_map and old_map[d.key] != d.effect_type:
            changes.append({"key": d.key, "from": old_map[d.key], "to": d.effect_type})
    return changes


def delete_draft(db: Session, project_id: str, schema_id: str) -> None:
    row = _get_row(db, project_id, schema_id)
    if row.status != "DRAFT":
        raise ConflictError(
            "LABEL_SCHEMA_IMMUTABLE",
            f"Schema v{row.version} 状态为 {row.status}，不可删除（已发布标签只能停用于新草稿）",
            {"status": row.status},
        )
    db.delete(row)
    db.commit()


# ---------------------------------------------------------------- 影响分析与发布

def impact_analysis(db: Session, project_id: str, schema_id: str) -> dict[str, Any]:
    row = _get_row(db, project_id, schema_id)
    draft_doc = resolve_document(row)
    active_row = get_active_row(db, project_id)
    active_doc = resolve_document(active_row) if active_row is not None else default_compat_document()

    active_keys = {d.key: d for d in active_doc.labels}
    draft_keys = {d.key: d for d in draft_doc.labels}
    added = sorted(set(draft_keys) - set(active_keys))
    removed = sorted(set(active_keys) - set(draft_keys))
    deprecated = sorted(k for k, d in draft_keys.items() if d.status == "deprecated" and k in active_keys
                        and active_keys[k].status == "active")
    effect_changed = _effect_type_diff(active_doc, draft_doc)
    # 破坏性：移除非停用标签，或修改任何标签的 effect type（安全语义变化）
    removed_active = [k for k in removed if active_keys[k].status == "active"]
    breaking = bool(removed_active or effect_changed)
    refs = _reference_counts(db, [active_row.id]).get(active_row.id, {}) if active_row is not None else {}
    return {
        "schema_id": row.id,
        "base_schema_id": active_row.id if active_row is not None else None,
        "breaking": breaking,
        "added": added,
        "removed": removed,
        "deprecated": deprecated,
        "effect_type_changed": effect_changed,
        "affected_datasets": refs.get("datasets", 0),
        "affected_runs": refs.get("runs", 0),
        "affected_models": refs.get("models", 0),
        "requires_retraining": bool(added or removed or deprecated or effect_changed),
    }


def publish(
    db: Session,
    project_id: str,
    schema_id: str,
    expected_hash: str,
    confirm_breaking_changes: bool = False,
) -> LabelSchemaVersion:
    row = _get_row(db, project_id, schema_id)
    if row.status != "DRAFT":
        raise ConflictError("LABEL_SCHEMA_IMMUTABLE", f"仅 DRAFT 可发布（当前 {row.status}）", {"status": row.status})
    if expected_hash != row.hash:
        raise ConflictError("LABEL_SCHEMA_CONFLICT", "expected_hash 与草稿不一致，请刷新后重试",
                            {"expected": expected_hash, "actual": row.hash})
    doc = resolve_document(row)
    problems = validate_schema(doc)
    if problems:
        raise ApiError("VALIDATION_ERROR", "Schema 未通过发布校验", 422, {"problems": problems[:10]})

    active_row = get_active_row(db, project_id)
    if active_row is not None and active_row.hash == row.hash and active_row.id != row.id:
        raise ConflictError("LABEL_SCHEMA_CONFLICT", "与当前生效版本内容完全相同，禁止发布重复版本", {})

    report = impact_analysis(db, project_id, schema_id)
    if report["breaking"] and not confirm_breaking_changes:
        raise ApiError(
            "LABEL_SCHEMA_CONFLICT",
            "存在影响安全语义或引用的变更，需 confirm_breaking_changes=true 二次确认",
            422,
            {"impact": report},
        )

    project = db.get(Project, project_id)
    try:
        if active_row is not None and active_row.id != row.id:
            active_row.status = "SUPERSEDED"
        row.status = "ACTIVE"
        row.published_at = datetime.now(UTC)
        project.active_label_schema_id = row.id
        _audit(db, project_id, "LABEL_SCHEMA_PUBLISHED", {
            "schema_id": row.id, "version": row.version,
            "from_schema_id": active_row.id if active_row is not None else None,
        })
        for change in report["effect_type_changed"]:
            _audit(db, project_id, "LABEL_EFFECT_TYPE_CHANGED",
                   {"schema_id": row.id, **change, "confirmed": True})
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(row)
    logger.info(
        "label_schema_published project=%s schema=%s version=%s labels=%s",
        project_id, row.id, row.version, len(doc.labels),
    )
    return row
