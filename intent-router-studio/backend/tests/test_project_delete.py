"""项目级联删除：确认、路径归属、运行态阻断与崩溃恢复。"""
from __future__ import annotations

from pathlib import Path

from app.config import get_settings
from app.models import (
    AuditEvent,
    DatasetQualityReport,
    DatasetSplit,
    DatasetVersion,
    LabelSchemaVersion,
    ModelVersion,
    PlaygroundCase,
    Project,
    RewriteConfigVersion,
    RewriteFeedback,
    RunEvent,
    RunMetric,
    TerminologyVersion,
    ThresholdVersion,
    TrainingRun,
    Upload,
)
from app.services import project_service


def test_delete_empty_project_removes_project_and_default_schema(client, db):
    created = client.post("/api/v1/projects", json={"name": "待删除空项目"})
    assert created.status_code == 200
    project_id = created.json()["id"]

    response = client.delete(f"/api/v1/projects/{project_id}")

    assert response.status_code == 200, response.text
    assert response.json()["deleted"] is True
    assert response.json()["project_id"] == project_id
    db.expire_all()
    assert db.get(Project, project_id) is None
    assert db.query(LabelSchemaVersion).filter(LabelSchemaVersion.project_id == project_id).count() == 0
    assert client.get(f"/api/v1/projects/{project_id}").status_code == 404


def test_delete_project_with_upload_requires_name_then_cascades(client, db):
    created = client.post("/api/v1/projects", json={"name": "有上传的项目"})
    project_id = created.json()["id"]
    upload = client.post(
        f"/api/v1/projects/{project_id}/uploads",
        files={"file": ("sample.txt", b"hello", "text/plain")},
    )
    assert upload.status_code == 200, upload.text
    from app.models import Upload

    upload_path = db.get(Upload, upload.json()["id"]).safe_path

    impact = client.get(f"/api/v1/projects/{project_id}/deletion-impact")
    assert impact.status_code == 200
    assert impact.json()["is_empty"] is False
    assert impact.json()["counts"]["uploads"] == 1

    response = client.delete(f"/api/v1/projects/{project_id}")

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "PROJECT_DELETE_CONFIRMATION_REQUIRED"
    assert error["details"]["counts"]["uploads"] == 1

    deleted = client.request(
        "DELETE",
        f"/api/v1/projects/{project_id}",
        json={"confirm_name": "有上传的项目"},
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["counts"]["uploads"] == 1
    assert client.get(f"/api/v1/projects/{project_id}").status_code == 404

    assert not Path(upload_path).exists()


def test_delete_project_cascades_all_database_rows_and_artifacts(client, db):
    project_id = client.post("/api/v1/projects", json={"name": "完整级联项目"}).json()["id"]
    project = db.get(Project, project_id)
    schema = db.query(LabelSchemaVersion).filter(LabelSchemaVersion.project_id == project_id).one()
    root = get_settings().artifact_root_path

    upload_path = root / "uploads" / "upl_cascade.csv"
    dataset_path = root / "projects" / project_id / "datasets" / "ds_cascade" / "data.parquet"
    split_path = dataset_path.parent / "split.parquet"
    run_path = root / "runs" / "run_cascade"
    model_path = root / "models" / "mdl_cascade"
    for path in (upload_path, dataset_path, split_path, run_path / "metrics.json", model_path / "manifest.json"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("test", encoding="utf-8")

    db.add(Upload(
        id="upl_cascade", project_id=project_id, original_name="data.csv", safe_path=str(upload_path),
        sha256="0" * 64, size_bytes=4, content_type="text/csv", status="IMPORTED",
    ))
    db.add(DatasetVersion(
        id="ds_cascade", project_id=project_id, parent_id=None, schema_id=schema.id, version=1,
        name="dataset", origin="import", status="FROZEN", parquet_path=str(dataset_path),
        raw_path=str(upload_path), sample_count=5, labeled_count=5,
    ))
    db.flush()
    db.add(DatasetQualityReport(id="qar_cascade", dataset_id="ds_cascade", report_json={}))
    db.add(DatasetSplit(
        id="spl_cascade", dataset_id="ds_cascade", seed=42, algorithm="test", ratios={},
        parquet_path=str(split_path), stats_json={},
    ))
    db.flush()
    db.add(TrainingRun(
        id="run_cascade", project_id=project_id, dataset_id="ds_cascade", split_id="spl_cascade",
        name="run", config={}, status="SUCCEEDED", progress=100, artifacts_dir=str(run_path),
    ))
    db.flush()
    db.add_all([
        RunEvent(run_id="run_cascade", sequence=1, event_type="terminal", payload={}),
        RunMetric(run_id="run_cascade", split="test", slice="all", metric_name="f1", value=1.0, support=5),
        ThresholdVersion(id="thr_cascade", run_id="run_cascade", version=1, config={}, metrics={}),
    ])
    db.flush()
    db.add(ModelVersion(
        id="mdl_cascade", project_id=project_id, run_id="run_cascade", threshold_id="thr_cascade",
        name="model", status="ACTIVE", artifact_path=str(model_path), manifest_hash="0" * 64, manifest={},
    ))
    db.add_all([
        PlaygroundCase(id="case_cascade", project_id=project_id, text_hash="0" * 64),
        RewriteConfigVersion(id="rwc_cascade", project_id=project_id, version=1, config={}, hash="0" * 64, status="ACTIVE"),
        TerminologyVersion(id="trm_cascade", project_id=project_id, version=1, terms={}, hash="0" * 64),
        RewriteFeedback(id="rwf_cascade", project_id=project_id, input_hash="0" * 64, verdict="accept"),
        AuditEvent(id="aud_cascade", project_id=project_id, event="model_activated"),
    ])
    project.active_model_id = "mdl_cascade"
    project.active_rewrite_config_id = "rwc_cascade"
    db.commit()

    response = client.request(
        "DELETE",
        f"/api/v1/projects/{project_id}",
        json={"confirm_name": "完整级联项目"},
    )

    assert response.status_code == 200, response.text
    expected_counts = {
        "uploads": 1, "datasets": 1, "dataset_quality_reports": 1, "dataset_splits": 1,
        "runs": 1, "run_events": 1, "run_metrics": 1, "threshold_versions": 1,
        "models": 1, "playground_cases": 1, "rewrite_configs": 1,
        "terminology_versions": 1, "rewrite_feedback": 1, "audit_events": 1,
    }
    assert response.json()["counts"] == expected_counts
    db.expire_all()
    assert db.get(Project, project_id) is None
    for model, row_id in (
        (Upload, "upl_cascade"), (DatasetVersion, "ds_cascade"),
        (DatasetQualityReport, "qar_cascade"), (DatasetSplit, "spl_cascade"),
        (TrainingRun, "run_cascade"), (ThresholdVersion, "thr_cascade"),
        (ModelVersion, "mdl_cascade"), (PlaygroundCase, "case_cascade"),
        (RewriteConfigVersion, "rwc_cascade"), (TerminologyVersion, "trm_cascade"),
        (RewriteFeedback, "rwf_cascade"), (AuditEvent, "aud_cascade"),
    ):
        assert db.get(model, row_id) is None
    assert db.query(RunEvent).filter(RunEvent.run_id == "run_cascade").count() == 0
    assert db.query(RunMetric).filter(RunMetric.run_id == "run_cascade").count() == 0
    assert db.query(LabelSchemaVersion).filter(LabelSchemaVersion.project_id == project_id).count() == 0
    for path in (upload_path, dataset_path.parent, run_path, model_path):
        assert not path.exists()


def test_delete_project_with_queued_run_is_blocked(client, db):
    project_id = client.post("/api/v1/projects", json={"name": "运行中项目"}).json()["id"]
    schema = db.query(LabelSchemaVersion).filter(LabelSchemaVersion.project_id == project_id).one()
    dataset_path = get_settings().projects_dir / project_id / "datasets" / "ds_busy" / "data.parquet"
    dataset_path.parent.mkdir(parents=True, exist_ok=True)
    dataset_path.write_text("test", encoding="utf-8")
    db.add(DatasetVersion(
        id="ds_busy", project_id=project_id, schema_id=schema.id, version=1, name="busy",
        origin="import", status="FROZEN", parquet_path=str(dataset_path), sample_count=1, labeled_count=1,
    ))
    db.flush()
    split_path = dataset_path.parent / "split.parquet"
    split_path.write_text("test", encoding="utf-8")
    db.add(DatasetSplit(
        id="spl_busy", dataset_id="ds_busy", seed=42, algorithm="test", ratios={},
        parquet_path=str(split_path), stats_json={},
    ))
    db.flush()
    db.add(TrainingRun(
        id="run_busy", project_id=project_id, dataset_id="ds_busy", split_id="spl_busy",
        name="queued", config={}, status="QUEUED", progress=0,
    ))
    db.commit()

    impact = client.get(f"/api/v1/projects/{project_id}/deletion-impact").json()
    assert impact["can_delete"] is False
    assert impact["active_runs"][0]["status"] == "QUEUED"
    response = client.request(
        "DELETE",
        f"/api/v1/projects/{project_id}",
        json={"confirm_name": "运行中项目"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PROJECT_HAS_ACTIVE_RUNS"
    assert client.get(f"/api/v1/projects/{project_id}").status_code == 200
    assert dataset_path.exists()


def test_delete_project_rejects_artifact_path_outside_root(client, db, tmp_path):
    project_id = client.post("/api/v1/projects", json={"name": "越界路径项目"}).json()["id"]
    outside = tmp_path / "must-not-delete.txt"
    outside.write_text("keep", encoding="utf-8")
    db.add(Upload(
        id="upl_outside", project_id=project_id, original_name="outside.txt", safe_path=str(outside),
        sha256="0" * 64, size_bytes=4, content_type="text/plain", status="PENDING",
    ))
    db.commit()

    response = client.request(
        "DELETE",
        f"/api/v1/projects/{project_id}",
        json={"confirm_name": "越界路径项目"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INVALID_ARTIFACT_PATH"
    assert outside.read_text(encoding="utf-8") == "keep"
    assert client.get(f"/api/v1/projects/{project_id}").status_code == 200


def test_delete_project_rejects_other_resource_path_inside_artifact_root(client, db):
    project_id = client.post("/api/v1/projects", json={"name": "跨项目路径"}).json()["id"]
    other_path = get_settings().uploads_dir / "upl_other.txt"
    other_path.write_text("keep", encoding="utf-8")
    db.add(Upload(
        id="upl_current", project_id=project_id, original_name="other.txt", safe_path=str(other_path),
        sha256="0" * 64, size_bytes=4, content_type="text/plain", status="PENDING",
    ))
    db.commit()

    response = client.request(
        "DELETE",
        f"/api/v1/projects/{project_id}",
        json={"confirm_name": "跨项目路径"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INVALID_ARTIFACT_PATH"
    assert other_path.read_text(encoding="utf-8") == "keep"


def test_recover_staged_project_deletion_restores_files_when_project_exists(client, db):
    project_id = client.post("/api/v1/projects", json={"name": "崩溃恢复项目"}).json()["id"]
    source = get_settings().projects_dir / project_id
    source.mkdir(parents=True)
    (source / "data.txt").write_text("recover", encoding="utf-8")
    trash, _moved = project_service._stage_artifacts(project_id, [source])
    assert not source.exists()
    assert (trash / "manifest.json").is_file()

    result = project_service.recover_staged_project_deletions(db)

    assert result["recovered"] == 1
    assert (source / "data.txt").read_text(encoding="utf-8") == "recover"
    assert not trash.exists()


def test_recover_staged_project_deletion_cleans_trash_after_database_commit(client, db):
    project_id = client.post("/api/v1/projects", json={"name": "提交后清理项目"}).json()["id"]
    source = get_settings().projects_dir / project_id
    source.mkdir(parents=True)
    (source / "data.txt").write_text("already deleted", encoding="utf-8")
    trash, _moved = project_service._stage_artifacts(project_id, [source])
    # 模拟“数据库已提交删除”：先解除 active Schema 指针再删 Schema 行（FK 约束）
    project = db.get(Project, project_id)
    project.active_label_schema_id = None
    db.flush()
    db.query(LabelSchemaVersion).filter(LabelSchemaVersion.project_id == project_id).delete()
    db.delete(project)
    db.commit()

    result = project_service.recover_staged_project_deletions(db)

    assert result["cleaned"] == 1
    assert not source.exists()
    assert not trash.exists()
