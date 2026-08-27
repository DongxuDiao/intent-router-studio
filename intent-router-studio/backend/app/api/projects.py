"""项目与标签接口（设计文档 9.2）。"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import DatasetVersion, ModelVersion, Project, TrainingRun
from app.schemas import ProjectCreate, ProjectDeleteRequest, ProjectPatch
from app.services import project_service

router = APIRouter(tags=["projects"])


def project_to_dict(p: Project, db: Session) -> dict:
    dataset_count = db.query(DatasetVersion).filter(DatasetVersion.project_id == p.id).count()
    run_count = db.query(TrainingRun).filter(TrainingRun.project_id == p.id).count()
    active_model = db.get(ModelVersion, p.active_model_id) if p.active_model_id else None
    return {
        "id": p.id,
        "name": p.name,
        "description": p.description,
        "active_model_id": p.active_model_id,
        "active_model_name": active_model.name if active_model else None,
        "dataset_count": dataset_count,
        "run_count": run_count,
        "created_at": p.created_at.isoformat(),
    }


@router.post("/projects")
def create_project(payload: ProjectCreate, db: Session = Depends(get_db)) -> dict:
    project = project_service.create_project(db, payload.name, payload.description)
    return project_to_dict(project, db)


@router.get("/projects")
def list_projects(db: Session = Depends(get_db)) -> dict:
    projects = db.query(Project).order_by(Project.created_at.desc()).all()
    return {"items": [project_to_dict(p, db) for p in projects]}


@router.get("/projects/{project_id}")
def get_project(project_id: str, db: Session = Depends(get_db)) -> dict:
    return project_to_dict(project_service.get_project(db, project_id), db)


@router.patch("/projects/{project_id}")
def patch_project(project_id: str, payload: ProjectPatch, db: Session = Depends(get_db)) -> dict:
    project = project_service.get_project(db, project_id)
    if payload.name is not None:
        project.name = payload.name
    if payload.description is not None:
        project.description = payload.description
    db.commit()
    return project_to_dict(project, db)


@router.get("/projects/{project_id}/deletion-impact")
def get_project_deletion_impact(project_id: str, db: Session = Depends(get_db)) -> dict:
    return project_service.project_deletion_impact(db, project_id)


@router.delete("/projects/{project_id}")
def delete_project(
    project_id: str,
    payload: ProjectDeleteRequest | None = Body(default=None),
    db: Session = Depends(get_db),
) -> dict:
    return project_service.delete_project(db, project_id, payload.confirm_name if payload else None)


@router.get("/projects/{project_id}/label-schema")
def get_label_schema(project_id: str, db: Session = Depends(get_db)) -> dict:
    return project_service.get_label_schema(db, project_id)
