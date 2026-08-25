"""状态机与制品哈希测试。"""
from __future__ import annotations

import pytest

from app import ids
from app.constants import (
    ALLOWED_MODEL_TRANSITIONS,
    ALLOWED_RUN_TRANSITIONS,
    TERMINAL_RUN_STATUSES,
    ModelStatus,
    RunStatus,
)
from app.db import SessionLocal
from app.errors import ApiError
from app.models import DatasetVersion, Project, TrainingRun
from app.services import artifact_service
from app.services.artifact_service import safe_join
from app.utils.hashing import sha256_bytes
from app.worker import queue


def test_all_statuses_covered():
    statuses = {
        RunStatus.DRAFT, RunStatus.QUEUED, RunStatus.PREPARING, RunStatus.TRAINING_EMBEDDING,
        RunStatus.TRAINING_HEAD, RunStatus.CALIBRATING, RunStatus.SEARCHING_THRESHOLDS,
        RunStatus.EVALUATING, RunStatus.PACKAGING, RunStatus.SUCCEEDED, RunStatus.CANCELLING,
        RunStatus.CANCELLED, RunStatus.FAILED, RunStatus.INTERRUPTED,
    }
    assert statuses == set(ALLOWED_RUN_TRANSITIONS.keys())
    for terminal in TERMINAL_RUN_STATUSES:
        assert ALLOWED_RUN_TRANSITIONS[terminal] == set()
    # 非法迁移示例
    assert RunStatus.SUCCEEDED not in ALLOWED_RUN_TRANSITIONS[RunStatus.QUEUED]
    assert RunStatus.TRAINING_HEAD in ALLOWED_RUN_TRANSITIONS[RunStatus.TRAINING_EMBEDDING]
    assert set(ALLOWED_MODEL_TRANSITIONS[ModelStatus.ACTIVE]) == {ModelStatus.ARCHIVED}


def test_transition_status_conditional_update():
    db = SessionLocal()
    try:
        project = Project(id=ids.prefixed(ids.PROJECT), name="状态机测试")
        db.add(project)
        db.flush()
        dataset = DatasetVersion(
            id=ids.prefixed(ids.DATASET),
            project_id=project.id,
            parquet_path="/tmp/unused.parquet",
        )
        db.add(dataset)
        db.flush()
        run = TrainingRun(
            id=ids.prefixed(ids.RUN),
            project_id=project.id,
            dataset_id=dataset.id,
            config={"train": {}},
            status=RunStatus.QUEUED,
        )
        db.add(run)
        db.commit()

        assert queue.transition_status(db, run.id, {RunStatus.QUEUED}, RunStatus.PREPARING, stage=RunStatus.PREPARING)
        # 已迁移后再从 QUEUED 迁移应失败（条件更新）
        assert not queue.transition_status(db, run.id, {RunStatus.QUEUED}, RunStatus.PREPARING)
        # 合法链路继续
        assert queue.transition_status(
            db, run.id,
            {RunStatus.PREPARING, RunStatus.TRAINING_EMBEDDING},
            RunStatus.TRAINING_EMBEDDING,
        )
        assert queue.transition_status(
            db, run.id,
            {RunStatus.TRAINING_EMBEDDING, RunStatus.CANCELLING},
            RunStatus.CANCELLING,
        )
        assert queue.transition_status(db, run.id, {RunStatus.CANCELLING}, RunStatus.CANCELLED, finished_at="2026-01-01T00:00:00")
        db.refresh(run)
        assert run.status == RunStatus.CANCELLED
    finally:
        db.query(TrainingRun).filter(TrainingRun.id == run.id).delete()
        db.query(DatasetVersion).filter(DatasetVersion.id == dataset.id).delete()
        db.query(Project).filter(Project.id == project.id).delete()
        db.commit()
        db.close()


def test_safe_join_rejects_traversal(tmp_path):
    with pytest.raises(ApiError):
        safe_join(tmp_path, "../escape")
    with pytest.raises(ApiError):
        safe_join(tmp_path, "/etc/passwd")
    ok = safe_join(tmp_path, "sub", "file.txt")
    assert str(ok).startswith(str(tmp_path.resolve()))


def test_manifest_build_and_verify(tmp_path):
    (tmp_path / "thresholds.json").write_text('{"a": 1}', encoding="utf-8")
    (tmp_path / "metrics.json").write_text("{}", encoding="utf-8")
    manifest = artifact_service.build_manifest(tmp_path, {"run_id": "run_x"})
    assert manifest["schema_version"] == 1
    assert "sha256:" in manifest["artifact_hashes"]["thresholds.json"]

    artifact_service.verify_manifest(tmp_path)  # 不抛错

    (tmp_path / "thresholds.json").write_text("tampered", encoding="utf-8")
    with pytest.raises(ApiError) as exc_info:
        artifact_service.verify_manifest(tmp_path)
    assert exc_info.value.code == "HASH_MISMATCH"


def test_hash_determinism():
    assert sha256_bytes(b"x") == sha256_bytes(b"x")
    assert sha256_bytes(b"x") != sha256_bytes(b"y")
