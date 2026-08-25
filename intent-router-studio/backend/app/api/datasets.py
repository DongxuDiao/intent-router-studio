"""数据集接口（设计文档 9.3）：上传、预览、导入、样本、校验、切分、草稿。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, UploadFile
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import DatasetSplit, DatasetVersion, Upload
from app.schemas import DraftCreate, ImportConfig, SamplePatch, SplitConfig
from app.services import dataset_service, project_service

router = APIRouter(tags=["datasets"])


def _upload_to_dict(u: Upload) -> dict:
    return {
        "id": u.id,
        "project_id": u.project_id,
        "original_name": u.original_name,
        "sha256": u.sha256,
        "size_bytes": u.size_bytes,
        "status": u.status,
        "created_at": u.created_at.isoformat(),
    }


def _dataset_to_dict(d: DatasetVersion, db: Session) -> dict:
    report = dataset_service.latest_report(db, d.id)
    latest_split = (
        db.query(DatasetSplit)
        .filter(DatasetSplit.dataset_id == d.id)
        .order_by(DatasetSplit.created_at.desc())
        .first()
    )
    return {
        "id": d.id,
        "project_id": d.project_id,
        "parent_id": d.parent_id,
        "version": d.version,
        "name": d.name,
        "origin": d.origin,
        "status": d.status,
        "sample_count": d.sample_count,
        "labeled_count": d.labeled_count,
        "unlabeled_count": d.sample_count - d.labeled_count,
        "label_distribution": d.label_distribution or {},
        "change_summary": d.change_summary,
        "manifest": d.manifest,
        "quality_report": report,
        "latest_split_id": latest_split.id if latest_split else None,
        "created_at": d.created_at.isoformat(),
    }


def _split_to_dict(s: DatasetSplit) -> dict:
    return {
        "id": s.id,
        "dataset_id": s.dataset_id,
        "seed": s.seed,
        "algorithm": s.algorithm,
        "ratios": s.ratios,
        "stats": s.stats_json,
        "created_at": s.created_at.isoformat(),
    }


@router.post("/projects/{project_id}/uploads")
async def create_upload(project_id: str, file: UploadFile = File(...), db: Session = Depends(get_db)) -> dict:
    project_service.get_project(db, project_id)
    # V2 §4.4：分块流式落盘，边读边限流——不再一次性读入内存
    writer = dataset_service.StreamingUploadWriter(db, project_id, file.filename or "upload.csv", file.content_type)
    try:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            writer.write(chunk)
        upload = writer.finish(db)
    except Exception:
        writer.abort()
        raise
    return _upload_to_dict(upload)


@router.get("/uploads/{upload_id}/preview")
def preview_upload(upload_id: str, encoding: str | None = None, db: Session = Depends(get_db)) -> dict:
    return dataset_service.preview_upload(db, upload_id, encoding)


@router.post("/uploads/{upload_id}/import")
def import_upload(upload_id: str, payload: ImportConfig, db: Session = Depends(get_db)) -> dict:
    dataset = dataset_service.import_upload(db, upload_id, payload.model_dump())
    return _dataset_to_dict(dataset, db)


@router.get("/projects/{project_id}/datasets")
def list_datasets(project_id: str, db: Session = Depends(get_db)) -> dict:
    project_service.get_project(db, project_id)
    rows = (
        db.query(DatasetVersion)
        .filter(DatasetVersion.project_id == project_id)
        .order_by(DatasetVersion.created_at.desc())
        .all()
    )
    return {"items": [_dataset_to_dict(d, db) for d in rows]}


@router.get("/datasets/{dataset_id}")
def get_dataset(dataset_id: str, db: Session = Depends(get_db)) -> dict:
    dataset = db.get(DatasetVersion, dataset_id)
    if dataset is None:
        from app.errors import NotFoundError

        raise NotFoundError("DatasetVersion", dataset_id)
    return _dataset_to_dict(dataset, db)


@router.get("/datasets/{dataset_id}/samples")
def list_samples(
    dataset_id: str,
    q: str | None = None,
    label: str | None = None,
    unlabeled_only: bool = False,
    page: int = 1,
    page_size: int = 50,
    db: Session = Depends(get_db),
) -> dict:
    return dataset_service.list_samples(
        db,
        dataset_id,
        {"q": q, "label": label, "unlabeled_only": unlabeled_only},
        page=min(page, 10_000),
        page_size=min(max(page_size, 1), 200),
    )


@router.patch("/datasets/{dataset_id}/samples/{sample_id}")
def patch_sample(dataset_id: str, sample_id: str, payload: SamplePatch, db: Session = Depends(get_db)) -> dict:
    return dataset_service.update_sample(db, dataset_id, sample_id, payload.model_dump(exclude_none=True))


@router.post("/datasets/{dataset_id}/validate")
def validate_dataset(dataset_id: str, db: Session = Depends(get_db)) -> dict:
    return dataset_service.validate_dataset(db, dataset_id)


@router.post("/datasets/{dataset_id}/split")
def create_split(dataset_id: str, payload: SplitConfig, db: Session = Depends(get_db)) -> dict:
    ratios = payload.ratios
    if ratios:
        total = sum(ratios.values())
        if any(v < 0 for v in ratios.values()) or abs(total - 1.0) > 0.01:
            from app.errors import ApiError

            raise ApiError("VALIDATION_ERROR", f"切分比例非法: {ratios}（和应为 1.0）", 422)
    split = dataset_service.create_split(db, dataset_id, ratios=ratios, seed=payload.seed)
    return _split_to_dict(split)


@router.get("/datasets/{dataset_id}/splits")
def list_splits(dataset_id: str, db: Session = Depends(get_db)) -> dict:
    rows = (
        db.query(DatasetSplit)
        .filter(DatasetSplit.dataset_id == dataset_id)
        .order_by(DatasetSplit.created_at.desc())
        .all()
    )
    return {"items": [_split_to_dict(s) for s in rows]}


@router.post("/datasets/{dataset_id}/drafts")
def create_draft(dataset_id: str, payload: DraftCreate, db: Session = Depends(get_db)) -> dict:
    draft = dataset_service.create_draft(db, dataset_id, [c.model_dump() for c in payload.changes], payload.name)
    return _dataset_to_dict(draft, db)


@router.post("/dataset-drafts/{draft_id}/commit")
def commit_draft(draft_id: str, db: Session = Depends(get_db)) -> dict:
    dataset = dataset_service.commit_draft(db, draft_id)
    return _dataset_to_dict(dataset, db)
