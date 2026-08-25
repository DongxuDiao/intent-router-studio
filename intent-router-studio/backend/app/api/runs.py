"""训练 Run 接口（设计文档 9.4 / 9.6）：创建、状态、SSE、取消、重试、指标、阈值。"""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.db import SessionLocal, get_db
from app.models import ThresholdVersion
from app.schemas import DraftCreate, RegisterModelRequest, RunCreate, ThresholdConfig
from app.services import dataset_service, run_service

router = APIRouter(tags=["runs"])

STAGE_ORDER = [
    "PREPARING",
    "TRAINING_EMBEDDING",
    "TRAINING_HEAD",
    "CALIBRATING",
    "SEARCHING_THRESHOLDS",
    "EVALUATING",
    "PACKAGING",
    "SUCCEEDED",
]


def run_to_dict(run) -> dict:
    return {
        "id": run.id,
        "project_id": run.project_id,
        "dataset_id": run.dataset_id,
        "split_id": run.split_id,
        "name": run.name,
        "config": run.config,
        "status": run.status,
        "stage": run.stage,
        "stage_index": STAGE_ORDER.index(run.stage) if run.stage in STAGE_ORDER else None,
        "progress": run.progress,
        "worker_id": run.worker_id,
        "cancel_requested": run.cancel_requested,
        "parent_run_id": run.parent_run_id,
        "error": run.error,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "heartbeat_at": run.heartbeat_at.isoformat() if run.heartbeat_at else None,
        "created_at": run.created_at.isoformat(),
    }


@router.post("/projects/{project_id}/runs")
def create_run(project_id: str, payload: RunCreate, db: Session = Depends(get_db)) -> dict:
    run = run_service.create_run(db, project_id, payload.dataset_version_id, payload.name or "", payload.config)
    return run_to_dict(run)


@router.get("/projects/{project_id}/runs")
def list_runs(project_id: str, db: Session = Depends(get_db)) -> dict:
    return {"items": [run_to_dict(r) for r in run_service.list_runs(db, project_id)]}


@router.get("/runs/{run_id}")
def get_run(run_id: str, db: Session = Depends(get_db)) -> dict:
    return run_to_dict(run_service.get_run(db, run_id))


@router.get("/runs/{run_id}/events")
async def stream_run_events(
    run_id: str,
    request: Request,
    last_event_id: str | None = Header(default=None),
    since: int = 0,
) -> StreamingResponse:
    """SSE 事件流。断线后前端用 Last-Event-ID 续传。"""
    run_service.get_run(SessionLocal(), run_id)  # 404 校验

    cursor = int(last_event_id or since or 0)

    async def event_stream():
        nonlocal cursor
        idle_ticks = 0
        deadline = asyncio.get_event_loop().time() + 7200  # 最长 2 小时
        while True:
            db = SessionLocal()
            try:
                events = run_service.run_events(db, run_id, after=cursor)
            finally:
                db.close()
            terminal = False
            for ev in events:
                cursor = ev.sequence
                payload = json.dumps(ev.payload, ensure_ascii=False)
                yield f"id: {ev.sequence}\nevent: {ev.event_type}\ndata: {payload}\n\n"
                if ev.event_type == "terminal":
                    terminal = True
            if terminal:
                return
            if await request.is_disconnected():
                return
            if asyncio.get_event_loop().time() > deadline:
                return
            await asyncio.sleep(1.0)
            idle_ticks += 1
            if idle_ticks % 15 == 0:
                yield ": ping\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@router.post("/runs/{run_id}/cancel")
def cancel_run(run_id: str, db: Session = Depends(get_db)) -> dict:
    return run_to_dict(run_service.cancel_run(db, run_id))


@router.post("/runs/{run_id}/retry")
def retry_run(run_id: str, db: Session = Depends(get_db)) -> dict:
    return run_to_dict(run_service.retry_run(db, run_id))


@router.get("/runs/{run_id}/metrics")
def run_metrics(run_id: str, db: Session = Depends(get_db)) -> dict:
    return run_service.run_metrics(db, run_id)


@router.get("/runs/{run_id}/errors")
def run_errors(run_id: str, page: int = 1, page_size: int = 50, db: Session = Depends(get_db)) -> dict:
    return run_service.run_errors(db, run_id, page, min(max(page_size, 1), 200))


@router.post("/runs/{run_id}/thresholds/simulate")
def simulate_thresholds(run_id: str, payload: ThresholdConfig, db: Session = Depends(get_db)) -> dict:
    return run_service.simulate_thresholds(db, run_id, payload.model_dump(exclude_none=True))


@router.post("/runs/{run_id}/threshold-versions")
def save_threshold_version(run_id: str, payload: ThresholdConfig, db: Session = Depends(get_db)) -> dict:
    version = run_service.save_threshold_version(db, run_id, payload.model_dump(exclude_none=True))
    return {
        "id": version.id,
        "run_id": version.run_id,
        "version": version.version,
        "config": version.config,
        "metrics": version.metrics,
        "source": version.source,
        "created_at": version.created_at.isoformat(),
    }


@router.get("/runs/{run_id}/threshold-versions")
def list_threshold_versions(run_id: str, db: Session = Depends(get_db)) -> dict:
    run_service.get_run(db, run_id)
    rows = (
        db.query(ThresholdVersion)
        .filter(ThresholdVersion.run_id == run_id)
        .order_by(ThresholdVersion.version.desc())
        .all()
    )
    return {
        "items": [
            {
                "id": v.id,
                "run_id": v.run_id,
                "version": v.version,
                "config": v.config,
                "metrics": v.metrics,
                "source": v.source,
                "created_at": v.created_at.isoformat(),
            }
            for v in rows
        ]
    }


@router.post("/runs/{run_id}/register-model")
def register_model(run_id: str, payload: RegisterModelRequest, db: Session = Depends(get_db)) -> dict:
    from app.api.models import model_to_dict

    model = run_service.register_model(db, run_id, payload.threshold_version_id, payload.name)
    return model_to_dict(model)


@router.post("/runs/{run_id}/errors/draft")
def errors_to_draft(run_id: str, payload: DraftCreate, db: Session = Depends(get_db)) -> dict:
    """错误样本回流：创建下一数据集版本草稿。"""
    run = run_service.get_run(db, run_id)
    draft = dataset_service.create_draft(db, run.dataset_id, [c.model_dump() for c in payload.changes], payload.name)
    from app.api.datasets import _dataset_to_dict

    return _dataset_to_dict(draft, db)
