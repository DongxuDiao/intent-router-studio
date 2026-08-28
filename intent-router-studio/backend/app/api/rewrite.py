"""Query 改写接口（修改方案 §10）。

- POST /inference/rewrite 与 /predict 解耦：显式请求改写理解结果
- 配置 / 术语接口版本化：PUT 永远新建版本，不原地覆盖
- 反馈接口：默认只落 hash，原文需 store_raw_text 显式允许
- GET /inference/rewrite/health：rewriter 状态 + 熔断 + 指标（§9.4 / §17）
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import TerminologyVersion
from app.router_core.normalization import text_hash
from app.services import inference_service, rewrite_service

router = APIRouter(tags=["rewrite"])


# ---------------------------------------------------------------- 请求模型

class RewriteRequest(BaseModel):
    project_id: str
    text: str = Field(min_length=1)
    context: str | None = None
    model_version_id: str | None = None
    mode: Literal["project_default", "off", "normalize_only", "shadow", "safe_apply"] | None = None


class BatchRewriteItem(BaseModel):
    text: str = Field(min_length=1)
    context: str | None = None


class BatchRewriteRequest(BaseModel):
    project_id: str
    items: list[BatchRewriteItem] = Field(min_length=1)
    mode: Literal["project_default", "off", "normalize_only", "shadow", "safe_apply"] | None = None


class RewriteConfigPut(BaseModel):
    config: dict


class TerminologyPut(BaseModel):
    terms: dict


class RewriteFeedbackIn(BaseModel):
    input_hash: str | None = None
    text: str | None = None
    context: str | None = None
    proposed_rewrite: str | None = None
    edited_rewrite: str | None = None
    verdict: Literal["accept", "reject", "edit"]
    reason_codes: list[str] | None = None
    original_route: str | None = None
    rewrite_route: str | None = None
    model_id: str | None = None
    prompt_version: str | None = None
    store_raw_text: bool = False


# ---------------------------------------------------------------- 改写推理

def _resolve_runtime(db: Session, project_id: str, model_version_id: str | None):
    if model_version_id:
        return inference_service.ensure_version_runtime(db, model_version_id)
    return inference_service.ensure_project_runtime(db, project_id)


@router.post("/inference/rewrite")
def rewrite(payload: RewriteRequest, db: Session = Depends(get_db)) -> dict:
    runtime = _resolve_runtime(db, payload.project_id, payload.model_version_id)
    return rewrite_service.understand_query(
        db, payload.project_id, payload.text, payload.context, runtime,
        mode_override=payload.mode,
    )


@router.post("/inference/rewrite/batch")
def rewrite_batch(payload: BatchRewriteRequest, db: Session = Depends(get_db)) -> dict:
    runtime = _resolve_runtime(db, payload.project_id, None)
    return rewrite_service.understand_batch(
        db, payload.project_id, [i.model_dump() for i in payload.items], runtime,
        mode_override=payload.mode,
    )


@router.get("/inference/rewrite/health")
def rewrite_health() -> dict:
    client = rewrite_service.get_client()
    info = client.health()
    info["metrics"] = rewrite_service.metrics_snapshot()
    return info


# ---------------------------------------------------------------- 改写配置

def _config_to_dict(row) -> dict:
    return {
        "id": row.id,
        "version": row.version,
        "config": row.config,
        "hash": row.hash,
        "status": row.status,
        "created_at": row.created_at.isoformat(),
    }


@router.get("/projects/{project_id}/rewrite-config")
def get_rewrite_config(project_id: str, db: Session = Depends(get_db)) -> dict:
    spec = rewrite_service.get_project_rewrite_config(db, project_id)
    versions = rewrite_service.list_rewrite_configs(db, project_id)
    provider = spec["provider"]
    return {
        "active": {
            "id": spec["config_version_id"],
            "config": spec["config"],
        },
        "defaults": rewrite_service.REWRITE_CONFIG_DEFAULTS,
        # 外部模型 V1 §7.2：当前生效的模型连接（内置或远程）
        "selected_provider": {
            "id": provider["id"],
            "name": provider["name"],
            "provider_type": provider["provider_type"],
            "model_id": provider["model_id"],
            "revision": provider["revision"],
            "builtin": provider["builtin"],
            "enabled": provider["enabled"],
            "available": provider["available"],
            "last_test_status": provider["last_test_status"],
        },
        # V2 §4.3 方案A：生成模型参数来自部署（只读）。外部模型 V1 起该字段进入
        # 兼容期（deprecated）——仅描述 builtin 部署，前端改读 selected_provider
        "deployment": rewrite_service.deployment_info(),
        "versions": [_config_to_dict(v) for v in versions],
    }


@router.put("/projects/{project_id}/rewrite-config")
def update_rewrite_config(project_id: str, payload: RewriteConfigPut, db: Session = Depends(get_db)) -> dict:
    row = rewrite_service.put_rewrite_config(db, project_id, payload.config)
    return _config_to_dict(row)


@router.post("/projects/{project_id}/rewrite-config/validate")
def validate_rewrite_config(project_id: str, payload: RewriteConfigPut, db: Session = Depends(get_db)) -> dict:
    problems = rewrite_service.validate_rewrite_config(payload.config)
    return {"valid": not problems, "problems": problems}


# ---------------------------------------------------------------- 术语表

@router.get("/projects/{project_id}/terminology")
def get_terminology(project_id: str, db: Session = Depends(get_db)) -> dict:
    spec = rewrite_service.get_project_rewrite_config(db, project_id)
    version_id = spec["terminology_version_id"]
    terms = spec["terms"] if version_id != "none" else {"terms": []}
    versions = (
        db.query(TerminologyVersion)
        .filter(TerminologyVersion.project_id == project_id)
        .order_by(TerminologyVersion.version.desc())
        .all()
    )
    return {
        "active": {"id": version_id, "terms": terms.get("terms", [])},
        "versions": [
            {
                "id": v.id,
                "version": v.version,
                "count": len((v.terms or {}).get("terms", [])),
                "hash": v.hash,
                "created_at": v.created_at.isoformat(),
            }
            for v in versions
        ],
    }


@router.put("/projects/{project_id}/terminology")
def update_terminology(project_id: str, payload: TerminologyPut, db: Session = Depends(get_db)) -> dict:
    row = rewrite_service.put_terminology(db, project_id, payload.terms)
    return {
        "id": row.id,
        "version": row.version,
        "count": len((row.terms or {}).get("terms", [])),
        "hash": row.hash,
        "created_at": row.created_at.isoformat(),
    }


# ---------------------------------------------------------------- 反馈

@router.post("/projects/{project_id}/rewrite-feedback")
def create_rewrite_feedback(project_id: str, payload: RewriteFeedbackIn, db: Session = Depends(get_db)) -> dict:
    spec = rewrite_service.get_project_rewrite_config(db, project_id)
    store_raw = payload.store_raw_text or bool(spec["config"].get("store_raw_text", False))
    row = rewrite_service.save_feedback(
        db,
        project_id=project_id,
        input_hash=payload.input_hash or text_hash(payload.text or "", payload.context),
        original_text=payload.text,
        context=payload.context,
        proposed_rewrite=payload.proposed_rewrite,
        edited_rewrite=payload.edited_rewrite,
        verdict=payload.verdict,
        reason_codes=payload.reason_codes,
        original_route=payload.original_route,
        rewrite_route=payload.rewrite_route,
        model_id=payload.model_id,
        prompt_version=payload.prompt_version,
        store_raw_text=store_raw,
    )
    return {"id": row.id, "verdict": row.verdict, "stored_raw_text": store_raw and payload.text is not None}


@router.get("/projects/{project_id}/rewrite-feedback")
def list_rewrite_feedback(project_id: str, limit: int = 50, db: Session = Depends(get_db)) -> dict:
    rows = rewrite_service.list_feedback(db, project_id, limit=limit)
    return {
        "items": [
            {
                "id": r.id,
                "input_hash": r.input_hash,
                "verdict": r.verdict,
                "reason_codes": (r.reason_codes or {}).get("codes", []),
                "original_route": r.original_route,
                "rewrite_route": r.rewrite_route,
                "has_raw_text": r.original_text is not None,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ]
    }
