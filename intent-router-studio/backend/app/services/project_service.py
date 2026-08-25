"""项目服务：创建项目时初始化默认五分类 Label Schema。"""
from __future__ import annotations

import hashlib
import json

from sqlalchemy.orm import Session

from app import ids
from app.errors import ApiError, NotFoundError
from app.models import LabelSchemaVersion, Project
from app.router_core.taxonomy import default_label_schema


def create_project(db: Session, name: str, description: str = "") -> Project:
    if not name or not name.strip():
        raise ApiError("VALIDATION_ERROR", "项目名称不能为空", 422)
    project = Project(id=ids.prefixed(ids.PROJECT), name=name.strip(), description=description or "")
    db.add(project)
    db.flush()  # 先落 Project 行，保证 schema 外键可满足

    schema = default_label_schema()
    schema_json = json.dumps(schema, ensure_ascii=False, sort_keys=True)
    db.add(
        LabelSchemaVersion(
            id=ids.prefixed(ids.LABEL_SCHEMA),
            project_id=project.id,
            version=1,
            schema_json=schema,
            hash=hashlib.sha256(schema_json.encode("utf-8")).hexdigest(),
        )
    )
    db.commit()
    db.refresh(project)
    return project


def get_project(db: Session, project_id: str) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError("Project", project_id)
    return project


def get_label_schema(db: Session, project_id: str) -> dict:
    get_project(db, project_id)
    row = (
        db.query(LabelSchemaVersion)
        .filter(LabelSchemaVersion.project_id == project_id)
        .order_by(LabelSchemaVersion.version.desc())
        .first()
    )
    if row is None:
        return default_label_schema()
    return row.schema_json
