"""项目服务：创建项目时初始化默认五分类 Label Schema。"""
from __future__ import annotations

import json
import logging
import os
import shutil
import uuid
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from app import ids
from app.config import get_settings
from app.constants import TERMINAL_RUN_STATUSES
from app.errors import ApiError, ConflictError, NotFoundError
from app.models import (
    AuditEvent,
    DatasetQualityReport,
    DatasetSplit,
    DatasetVersion,
    LabelSchemaVersion,
    ModelVersion,
    PlaygroundCase,
    Project,
    RewriteConfigVersion,
    RewriteFeedback,
    RunEvent,
    RunMetric,
    TerminologyVersion,
    ThresholdVersion,
    TrainingRun,
    Upload,
)
from app.router_core.taxonomy import default_label_schema

logger = logging.getLogger("app.project")


def create_project(db: Session, name: str, description: str = "") -> Project:
    if not name or not name.strip():
        raise ApiError("VALIDATION_ERROR", "项目名称不能为空", 422)
    project = Project(id=ids.prefixed(ids.PROJECT), name=name.strip(), description=description or "")
    db.add(project)
    db.flush()  # 先落 Project 行，保证 schema 外键可满足

    # 自定义意图标签 §6.2：新项目仍默认兼容五分类，但以 v2 文档存储并显式写指针
    from datetime import UTC, datetime

    from app.router_core.label_schema import default_compat_document, schema_hash

    doc = default_compat_document()
    schema_row = LabelSchemaVersion(
        id=ids.prefixed(ids.LABEL_SCHEMA),
        project_id=project.id,
        version=1,
        schema_json=doc.to_dict(),
        hash=schema_hash(doc),
        status="ACTIVE",
        change_summary="项目创建默认五分类",
        created_by="local",
        published_at=datetime.now(UTC),
    )
    db.add(schema_row)
    db.flush()
    project.active_label_schema_id = schema_row.id
    db.commit()
    db.refresh(project)
    return project


def get_project(db: Session, project_id: str) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError("Project", project_id)
    return project


def get_label_schema(db: Session, project_id: str) -> dict:
    """兼容期旧接口（§5.1）：等价于读取 active Schema，响应补 id/version/status/schema_format。"""
    from app.router_core.label_schema import SCHEMA_FORMAT_V2

    get_project(db, project_id)
    from app.services import label_schema_service

    row = label_schema_service.get_active_row(db, project_id)
    if row is None:
        doc = default_label_schema()
        return {**doc, "id": None, "version": None, "status": None, "schema_format": SCHEMA_FORMAT_V2}
    doc = label_schema_service.resolve_document(row)
    out = doc.to_dict()
    return {**out, "id": row.id, "version": row.version, "status": row.status,
            "schema_format": doc.schema_format}


def _deletion_rows(db: Session, project_id: str) -> dict[str, list]:
    datasets = db.query(DatasetVersion).filter(DatasetVersion.project_id == project_id).all()
    dataset_ids = [row.id for row in datasets]
    runs = db.query(TrainingRun).filter(TrainingRun.project_id == project_id).all()
    run_ids = [row.id for row in runs]
    return {
        "uploads": db.query(Upload).filter(Upload.project_id == project_id).all(),
        "datasets": datasets,
        "dataset_quality_reports": (
            db.query(DatasetQualityReport).filter(DatasetQualityReport.dataset_id.in_(dataset_ids)).all()
            if dataset_ids else []
        ),
        "dataset_splits": (
            db.query(DatasetSplit).filter(DatasetSplit.dataset_id.in_(dataset_ids)).all()
            if dataset_ids else []
        ),
        "runs": runs,
        "run_events": db.query(RunEvent).filter(RunEvent.run_id.in_(run_ids)).all() if run_ids else [],
        "run_metrics": db.query(RunMetric).filter(RunMetric.run_id.in_(run_ids)).all() if run_ids else [],
        "threshold_versions": (
            db.query(ThresholdVersion).filter(ThresholdVersion.run_id.in_(run_ids)).all()
            if run_ids else []
        ),
        "models": db.query(ModelVersion).filter(ModelVersion.project_id == project_id).all(),
        "playground_cases": db.query(PlaygroundCase).filter(PlaygroundCase.project_id == project_id).all(),
        "rewrite_configs": db.query(RewriteConfigVersion).filter(RewriteConfigVersion.project_id == project_id).all(),
        "terminology_versions": db.query(TerminologyVersion).filter(TerminologyVersion.project_id == project_id).all(),
        "rewrite_feedback": db.query(RewriteFeedback).filter(RewriteFeedback.project_id == project_id).all(),
        "audit_events": db.query(AuditEvent).filter(AuditEvent.project_id == project_id).all(),
        "label_schemas": db.query(LabelSchemaVersion).filter(LabelSchemaVersion.project_id == project_id).all(),
    }


def project_deletion_impact(db: Session, project_id: str) -> dict:
    project = get_project(db, project_id)
    rows = _deletion_rows(db, project_id)
    counts = {name: len(items) for name, items in rows.items() if name != "label_schemas"}
    active_runs = [
        {"id": run.id, "name": run.name, "status": run.status}
        for run in rows["runs"]
        if run.status not in TERMINAL_RUN_STATUSES and run.status != "DRAFT"
    ]
    return {
        "project_id": project.id,
        "project_name": project.name,
        "is_empty": not any(counts.values()),
        "can_delete": not active_runs,
        "counts": counts,
        "active_runs": active_runs,
    }


def _validated_artifact_paths(project_id: str, rows: dict[str, list]) -> list[Path]:
    settings = get_settings()
    root = settings.artifact_root_path.resolve()
    candidates: list[tuple[Path, Path, str]] = [
        (settings.projects_dir / project_id, settings.projects_dir / project_id, "project"),
    ]
    upload_paths: set[Path] = set()
    for row in rows["uploads"]:
        if not row.safe_path:
            continue
        candidate = Path(row.safe_path)
        resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
        expected_parent = settings.uploads_dir.resolve()
        if resolved.parent != expected_parent or not resolved.name.startswith(f"{row.id}."):
            raise ApiError(
                "INVALID_ARTIFACT_PATH",
                "上传制品路径与资源归属不一致，已拒绝删除",
                409,
                {"project_id": project_id, "resource_id": row.id, "path": str(candidate)},
            )
        upload_paths.add(resolved)
        candidates.append((candidate, resolved, f"upload:{row.id}"))
    for row in rows["datasets"]:
        dataset_root = (settings.projects_dir / project_id / "datasets" / row.id).resolve()
        if row.parquet_path:
            candidates.append((Path(row.parquet_path), dataset_root, f"dataset:{row.id}"))
        if row.raw_path:
            raw_candidate = Path(row.raw_path)
            raw_resolved = (raw_candidate if raw_candidate.is_absolute() else root / raw_candidate).resolve()
            if raw_resolved not in upload_paths:
                raise ApiError(
                    "INVALID_ARTIFACT_PATH",
                    "数据集原始文件不属于当前项目上传，已拒绝删除",
                    409,
                    {"project_id": project_id, "resource_id": row.id, "path": str(raw_candidate)},
                )
            candidates.append((raw_candidate, raw_resolved, f"dataset-raw:{row.id}"))
    dataset_by_id = {row.id: row for row in rows["datasets"]}
    for row in rows["dataset_splits"]:
        dataset = dataset_by_id.get(row.dataset_id)
        if row.parquet_path and dataset is not None:
            dataset_root = (settings.projects_dir / project_id / "datasets" / dataset.id).resolve()
            candidates.append((Path(row.parquet_path), dataset_root, f"split:{row.id}"))
    for row in rows["runs"]:
        expected = (settings.runs_dir / row.id).resolve()
        if row.artifacts_dir:
            candidates.append((Path(row.artifacts_dir), expected, f"run:{row.id}"))
        candidates.extend(
            (
                (settings.runs_dir / row.id, expected, f"run:{row.id}"),
                (settings.runs_dir / f"{row.id}.tmp", (settings.runs_dir / f"{row.id}.tmp").resolve(), f"run-tmp:{row.id}"),
            )
        )
    for row in rows["models"]:
        expected = (settings.models_dir / row.id).resolve()
        if row.artifact_path:
            candidates.append((Path(row.artifact_path), expected, f"model:{row.id}"))
        candidates.append((settings.models_dir / row.id, expected, f"model:{row.id}"))

    validated: set[Path] = set()
    for candidate, expected_root, resource in candidates:
        path = candidate if candidate.is_absolute() else root / candidate
        resolved = path.resolve()
        if resolved == root or not resolved.is_relative_to(root):
            raise ApiError(
                "INVALID_ARTIFACT_PATH",
                "项目制品路径越界，已拒绝删除",
                409,
                {"project_id": project_id, "path": str(candidate)},
            )
        expected_resolved = expected_root.resolve()
        if resource.startswith(("dataset:", "split:")):
            belongs = resolved.is_relative_to(expected_resolved)
        else:
            belongs = resolved == expected_resolved
        if not belongs:
            raise ApiError(
                "INVALID_ARTIFACT_PATH",
                "项目制品路径与资源归属不一致，已拒绝删除",
                409,
                {"project_id": project_id, "resource": resource, "path": str(candidate)},
            )
        if resolved.exists():
            validated.add(resolved)

    # 父目录已纳入时不重复移动子项（例如 projects/<id> 已覆盖 dataset 文件）。
    selected: list[Path] = []
    for path in sorted(validated, key=lambda item: len(item.parts)):
        if not any(path == parent or path.is_relative_to(parent) for parent in selected):
            selected.append(path)
    return selected


def _write_deletion_manifest(trash: Path, project_id: str, moves: list[tuple[Path, Path]]) -> None:
    payload = {
        "version": 1,
        "project_id": project_id,
        "moves": [{"source": str(source), "target": str(target)} for source, target in moves],
    }
    temporary = trash / ".manifest.tmp"
    with temporary.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, trash / "manifest.json")


def _stage_artifacts(project_id: str, paths: list[Path]) -> tuple[Path, list[tuple[Path, Path]]]:
    root = get_settings().artifact_root_path.resolve()
    trash = root / ".trash" / f"project-{project_id}-{uuid.uuid4().hex}"
    planned = [(source, trash / f"{index:04d}-{source.name}") for index, source in enumerate(paths)]
    moved: list[tuple[Path, Path]] = []
    try:
        trash.mkdir(parents=True, exist_ok=False)
        _write_deletion_manifest(trash, project_id, planned)
        for source, target in planned:
            os.replace(source, target)
            moved.append((source, target))
        return trash, moved
    except Exception:
        restore_conflict = False
        for source, target in reversed(moved):
            if source.exists():
                restore_conflict = True
                logger.error("回滚制品暂存时目标已存在 source=%s backup=%s", source, target)
                continue
            source.parent.mkdir(parents=True, exist_ok=True)
            os.replace(target, source)
        if not restore_conflict:
            shutil.rmtree(trash, ignore_errors=True)
        raise


def _restore_artifacts(trash: Path, moved: list[tuple[Path, Path]]) -> None:
    restore_conflict = False
    for source, target in reversed(moved):
        if target.exists():
            if source.exists():
                restore_conflict = True
                logger.error("恢复删除制品时目标已存在 source=%s backup=%s", source, target)
                continue
            source.parent.mkdir(parents=True, exist_ok=True)
            os.replace(target, source)
    if not restore_conflict:
        shutil.rmtree(trash, ignore_errors=True)


def recover_staged_project_deletions(db: Session) -> dict[str, int]:
    """启动时恢复崩溃中断的项目删除：项目仍在则还原文件，否则清理回收区。"""
    root = get_settings().artifact_root_path.resolve()
    trash_root = root / ".trash"
    recovered = cleaned = conflicts = 0
    if not trash_root.exists():
        return {"recovered": 0, "cleaned": 0, "conflicts": 0}
    for trash in trash_root.iterdir():
        if not trash.is_dir() or not trash.name.startswith("project-"):
            continue
        manifest_path = trash / "manifest.json"
        if not manifest_path.is_file():
            if not any(trash.iterdir()):
                trash.rmdir()
            else:
                conflicts += 1
                logger.error("删除回收区缺少 manifest，保留等待人工检查 path=%s", trash)
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            project_id = str(manifest["project_id"])
            moves: list[tuple[Path, Path]] = []
            for item in manifest.get("moves", []):
                source = Path(item["source"]).resolve()
                target = Path(item["target"]).resolve()
                if source == root or not source.is_relative_to(root) or not target.is_relative_to(trash.resolve()):
                    raise ValueError("manifest path out of bounds")
                moves.append((source, target))
        except Exception as exc:
            conflicts += 1
            logger.error("删除回收区 manifest 无效，保留等待人工检查 path=%s err=%s", trash, exc)
            continue
        if db.get(Project, project_id) is None:
            shutil.rmtree(trash)
            cleaned += 1
            continue
        had_conflict = False
        for source, target in reversed(moves):
            if not target.exists():
                continue
            if source.exists():
                had_conflict = True
                logger.error("恢复删除制品时目标已存在 source=%s backup=%s", source, target)
                continue
            source.parent.mkdir(parents=True, exist_ok=True)
            os.replace(target, source)
        if had_conflict:
            conflicts += 1
        else:
            shutil.rmtree(trash)
            recovered += 1
    return {"recovered": recovered, "cleaned": cleaned, "conflicts": conflicts}


def delete_project(db: Session, project_id: str, confirm_name: str | None = None) -> dict:
    """级联删除项目数据库记录和制品；非空项目必须精确确认项目名。"""
    from app.services import inference_service, rewrite_service, run_service

    with run_service.project_lifecycle_lock(project_id):
        # SQLite 写锁覆盖“检查运行态 → 删除”，避免 Worker 在检查后领取 QUEUED Run。
        db.execute(text("BEGIN IMMEDIATE"))
        project = get_project(db, project_id)
        rows = _deletion_rows(db, project_id)
        counts = {name: len(items) for name, items in rows.items() if name != "label_schemas"}
        active_runs = [
            run for run in rows["runs"]
            if run.status not in TERMINAL_RUN_STATUSES and run.status != "DRAFT"
        ]
        if active_runs:
            db.rollback()
            raise ConflictError(
                "PROJECT_HAS_ACTIVE_RUNS",
                "项目仍有排队中或运行中的训练，取消并等待结束后再删除",
                {"runs": [{"id": run.id, "name": run.name, "status": run.status} for run in active_runs]},
            )
        is_empty = not any(counts.values())
        if not is_empty and confirm_name != project.name:
            db.rollback()
            raise ConflictError(
                "PROJECT_DELETE_CONFIRMATION_REQUIRED",
                "非空项目必须输入完整项目名确认删除",
                {"project_id": project_id, "project_name": project.name, "counts": counts},
            )

        artifact_paths = _validated_artifact_paths(project_id, rows)
        trash, moved = _stage_artifacts(project_id, artifact_paths)
        model_ids = [row.id for row in rows["models"]]
        try:
            project.active_model_id = None
            project.active_rewrite_config_id = None
            # 自定义意图标签 §4.1：先解除 Schema 指针，避免删除 Schema 行时触发 FK
            project.active_label_schema_id = None
            db.flush()
            for key, model in (
                ("models", ModelVersion),
                ("threshold_versions", ThresholdVersion),
                ("run_metrics", RunMetric),
                ("run_events", RunEvent),
                ("runs", TrainingRun),
                ("dataset_quality_reports", DatasetQualityReport),
                ("dataset_splits", DatasetSplit),
                ("datasets", DatasetVersion),
                ("uploads", Upload),
                ("playground_cases", PlaygroundCase),
                ("rewrite_feedback", RewriteFeedback),
                ("terminology_versions", TerminologyVersion),
                ("rewrite_configs", RewriteConfigVersion),
                ("audit_events", AuditEvent),
                ("label_schemas", LabelSchemaVersion),
            ):
                ids_to_delete = [row.id for row in rows[key]]
                if ids_to_delete:
                    id_column = model.id
                    db.query(model).filter(id_column.in_(ids_to_delete)).delete(synchronize_session=False)
            db.delete(project)
            db.commit()
        except Exception:
            db.rollback()
            _restore_artifacts(trash, moved)
            raise

        inference_service.RUNTIME.evict_project(project_id, model_ids)
        rewrite_service.CACHE.clear_project(project_id)
        try:
            shutil.rmtree(trash, ignore_errors=False)
        except OSError as exc:
            logger.warning("项目删除后清理回收站失败 project=%s err=%s", project_id, exc)
        return {
            "deleted": True,
            "project_id": project_id,
            "counts": counts,
            "artifact_paths_deleted": len(artifact_paths),
        }
