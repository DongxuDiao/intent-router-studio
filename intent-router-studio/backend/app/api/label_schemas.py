"""标签 Schema 接口（自定义意图标签方案 §5）。

统一前缀 /projects/{project_id}/label-schemas；所有查询先校验项目归属
（§10.8 防跨项目引用）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import label_schema_service as svc

router = APIRouter(tags=["label-schemas"])


class DraftCreate(BaseModel):
    base_schema_id: str | None = None
    change_summary: str = ""


class DraftUpdate(BaseModel):
    expected_hash: str = Field(min_length=8, max_length=64)
    labels: list[dict]
    change_summary: str | None = None


class PublishRequest(BaseModel):
    expected_hash: str = Field(min_length=8, max_length=64)
    confirm_breaking_changes: bool = False


def _detail(db: Session, project_id: str, schema_id: str) -> dict:
    return svc.schema_detail(db, project_id, schema_id)


@router.get("/projects/{project_id}/label-schemas")
def list_label_schemas(project_id: str, db: Session = Depends(get_db)) -> dict:
    return {"items": svc.list_schemas(db, project_id)}


@router.get("/projects/{project_id}/label-schemas/active")
def get_active_label_schema(project_id: str, db: Session = Depends(get_db)) -> dict:
    row = svc.get_active_row(db, project_id)
    if row is None:
        from app.errors import NotFoundError

        raise NotFoundError("LabelSchemaVersion", "active")
    return _detail(db, project_id, row.id)


@router.get("/projects/{project_id}/label-schemas/{schema_id}")
def get_label_schema_detail(project_id: str, schema_id: str, db: Session = Depends(get_db)) -> dict:
    return _detail(db, project_id, schema_id)


@router.post("/projects/{project_id}/label-schemas/drafts")
def create_label_schema_draft(project_id: str, payload: DraftCreate, db: Session = Depends(get_db)) -> dict:
    row = svc.create_draft(db, project_id, payload.base_schema_id, payload.change_summary)
    return _detail(db, project_id, row.id)


@router.patch("/projects/{project_id}/label-schemas/{schema_id}")
def update_label_schema_draft(
    project_id: str, schema_id: str, payload: DraftUpdate, db: Session = Depends(get_db)
) -> dict:
    svc.update_draft(
        db, project_id, schema_id, payload.expected_hash, payload.labels, payload.change_summary
    )
    return _detail(db, project_id, schema_id)


@router.post("/projects/{project_id}/label-schemas/{schema_id}/impact")
def label_schema_impact(project_id: str, schema_id: str, db: Session = Depends(get_db)) -> dict:
    return svc.impact_analysis(db, project_id, schema_id)


@router.post("/projects/{project_id}/label-schemas/{schema_id}/publish")
def publish_label_schema(
    project_id: str, schema_id: str, payload: PublishRequest, db: Session = Depends(get_db)
) -> dict:
    row = svc.publish(db, project_id, schema_id, payload.expected_hash, payload.confirm_breaking_changes)
    return _detail(db, project_id, row.id)


@router.delete("/projects/{project_id}/label-schemas/{schema_id}")
def delete_label_schema_draft(project_id: str, schema_id: str, db: Session = Depends(get_db)) -> dict:
    svc.delete_draft(db, project_id, schema_id)
    return {"deleted": schema_id}
