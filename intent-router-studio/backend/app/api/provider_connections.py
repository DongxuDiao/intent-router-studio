"""改写模型连接接口（外部模型 API 接入 V1 §7.1）。

所有响应不携带 API Key / 密文；密钥只经 POST/PATCH 写入。
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import provider_connection_service as svc

router = APIRouter(tags=["rewrite"])


class ConnectionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    provider_type: str  # glm | openai_compatible
    model_id: str = Field(min_length=1, max_length=200)
    base_url: str | None = None  # openai_compatible 必填；GLM 由 glm_endpoint 映射
    glm_endpoint: Literal["general", "coding"] | None = None  # GLM 端点档位，默认 general
    api_key: str = Field(min_length=8, max_length=2000)
    generation_config: dict | None = None
    egress_acknowledged: bool


class ConnectionPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    model_id: str | None = Field(default=None, min_length=1, max_length=200)
    base_url: str | None = None
    glm_endpoint: Literal["general", "coding"] | None = None
    generation_config: dict | None = None
    enabled: bool | None = None
    # 空字符串 = 保留旧值（服务层判断）；非空时长度 ≥8 由服务层校验
    api_key: str | None = Field(default=None, max_length=2000)


class CredentialClear(BaseModel):
    confirm: bool = False


@router.get("/rewrite/provider-connections")
def list_provider_connections(db: Session = Depends(get_db)) -> dict:
    return {"items": svc.list_connections(db)}


@router.post("/rewrite/provider-connections")
def create_provider_connection(payload: ConnectionCreate, db: Session = Depends(get_db)) -> dict:
    row = svc.create_connection(db, payload.model_dump())
    return svc.get_connection_dict(db, row.id)


@router.get("/rewrite/provider-connections/{connection_id}")
def get_provider_connection(connection_id: str, db: Session = Depends(get_db)) -> dict:
    return svc.get_connection_dict(db, connection_id)


@router.patch("/rewrite/provider-connections/{connection_id}")
def patch_provider_connection(connection_id: str, payload: ConnectionPatch, db: Session = Depends(get_db)) -> dict:
    data = payload.model_dump(exclude_unset=True)
    row = svc.update_connection(db, connection_id, data)
    return svc.get_connection_dict(db, row.id)


@router.post("/rewrite/provider-connections/{connection_id}/test")
def test_provider_connection(connection_id: str, db: Session = Depends(get_db)) -> dict:
    if connection_id == svc.BUILTIN_LOCAL_QWEN:
        from app.errors import ApiError

        raise ApiError(
            "BUILTIN_CONNECTION_IMMUTABLE",
            "内置本地连接的可用性见 /inference/rewrite/health，无需显式测试",
            422,
        )
    return svc.test_connection(db, connection_id)


@router.delete("/rewrite/provider-connections/{connection_id}")
def delete_provider_connection(connection_id: str, db: Session = Depends(get_db)) -> dict:
    svc.delete_connection(db, connection_id)
    return {"deleted": connection_id}


@router.delete("/rewrite/provider-connections/{connection_id}/credential")
def clear_provider_credential(connection_id: str, payload: CredentialClear, db: Session = Depends(get_db)) -> dict:
    row = svc.clear_credential(db, connection_id, payload.confirm)
    return svc.get_connection_dict(db, row.id)
