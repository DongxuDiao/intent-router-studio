"""测试夹具：所有测试使用独立临时目录与 SQLite。"""
from __future__ import annotations

import os
import tempfile

# 必须在导入 app 之前设置环境变量
_TMP = tempfile.mkdtemp(prefix="irs-test-")
os.environ["ARTIFACT_ROOT"] = _TMP
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"
os.environ["HF_HOME"] = f"{_TMP}/hf-cache"
os.environ["LOG_RAW_TEXT"] = "false"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _init_db():
    init_db()
    yield


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client():
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def project_id(client) -> str:
    resp = client.post("/api/v1/projects", json={"name": "测试项目", "description": "pytest"})
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]
