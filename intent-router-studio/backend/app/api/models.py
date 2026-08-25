"""模型注册中心接口（设计文档 9.5）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas import PlaygroundCaseRequest
from app.services import inference_service, project_service, run_service

router = APIRouter(tags=["models"])


def model_to_dict(m) -> dict:
    return {
        "id": m.id,
        "project_id": m.project_id,
        "run_id": m.run_id,
        "threshold_id": m.threshold_id,
        "name": m.name,
        "status": m.status,
        "manifest": m.manifest,
        "metrics_summary": (m.manifest or {}).get("metrics_summary"),
        "created_at": m.created_at.isoformat(),
        "activated_at": m.activated_at.isoformat() if m.activated_at else None,
    }


@router.get("/projects/{project_id}/models")
def list_models(project_id: str, db: Session = Depends(get_db)) -> dict:
    project_service.get_project(db, project_id)
    return {"items": [model_to_dict(m) for m in run_service.list_models(db, project_id)]}


@router.get("/models/{model_id}")
def get_model(model_id: str, db: Session = Depends(get_db)) -> dict:
    return model_to_dict(run_service.get_model(db, model_id))


@router.post("/models/{model_id}/activate")
def activate_model(model_id: str, db: Session = Depends(get_db)) -> dict:
    """激活动作：制品校验 + 临时加载 smoke + 事务切换；失败不影响旧模型。"""
    return model_to_dict(inference_service.activate_model(db, model_id))


@router.post("/models/{model_id}/archive")
def archive_model(model_id: str, db: Session = Depends(get_db)) -> dict:
    return model_to_dict(run_service.archive_model(db, model_id))


@router.post("/models/{model_id}/rollback")
def rollback_model(model_id: str, db: Session = Depends(get_db)) -> dict:
    return model_to_dict(inference_service.rollback_model(db, model_id))


@router.get("/models/{model_id}/manifest")
def model_manifest(model_id: str, db: Session = Depends(get_db)) -> dict:
    model = run_service.get_model(db, model_id)
    return model.manifest


@router.get("/projects/{project_id}/audit-events")
def list_audit_events(project_id: str, limit: int = 100, db: Session = Depends(get_db)) -> dict:
    """模型生命周期审计事件（V2 §3.5）：激活/回滚/停用的 from/to 与时间线。"""
    project_service.get_project(db, project_id)
    events = inference_service.list_audit_events(db, project_id, min(max(limit, 1), 500))
    return {
        "items": [
            {
                "id": e.id,
                "event": e.event,
                "from_model_id": e.from_model_id,
                "to_model_id": e.to_model_id,
                "details": e.details,
                "created_at": e.created_at.isoformat(),
            }
            for e in events
        ]
    }


# ---------------- Playground 案例 ----------------
@router.post("/projects/{project_id}/playground-cases")
def save_playground_case(project_id: str, payload: PlaygroundCaseRequest, db: Session = Depends(get_db)) -> dict:
    project_service.get_project(db, project_id)
    case = inference_service.save_playground_case(
        db,
        project_id,
        payload.text,
        payload.context,
        payload.expected_label,
        payload.predicted_route,
        payload.model_version_id,
        payload.tags,
        payload.save_text,
    )
    return {
        "id": case.id,
        "text_hash": case.text_hash,
        "expected_label": case.expected_label,
        "predicted_route": case.predicted_route,
        "is_correct": case.is_correct,
        "created_at": case.created_at.isoformat(),
    }


@router.get("/projects/{project_id}/playground-cases")
def list_playground_cases(project_id: str, limit: int = 100, db: Session = Depends(get_db)) -> dict:
    project_service.get_project(db, project_id)
    cases = inference_service.list_playground_cases(db, project_id, min(limit, 500))
    return {
        "items": [
            {
                "id": c.id,
                "text_hash": c.text_hash,
                "text": c.text,
                "expected_label": c.expected_label,
                "predicted_route": c.predicted_route,
                "is_correct": c.is_correct,
                "created_at": c.created_at.isoformat(),
            }
            for c in cases
        ]
    }
