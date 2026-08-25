"""推理接口（设计文档 9.5）：单条、批量、A/B 对比。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas import BatchPredictRequest, CompareRequest, PredictRequest
from app.services import inference_service

router = APIRouter(prefix="/inference", tags=["inference"])


@router.post("/predict")
def predict(payload: PredictRequest, db: Session = Depends(get_db)) -> dict:
    return inference_service.predict(
        db,
        payload.project_id,
        payload.text,
        payload.context,
        payload.model_version_id,
        payload.threshold_overrides,
        payload.debug,
        payload.rewrite.model_dump() if payload.rewrite else None,
    )


@router.post("/batch")
def predict_batch(payload: BatchPredictRequest, db: Session = Depends(get_db)) -> dict:
    results = inference_service.predict_batch(
        db, payload.project_id, [item.model_dump() for item in payload.items]
    )
    return {"count": len(results), "results": results}


@router.post("/compare")
def compare(payload: CompareRequest, db: Session = Depends(get_db)) -> dict:
    return inference_service.compare(db, payload.project_id, payload.text, payload.context, payload.model_a, payload.model_b)
