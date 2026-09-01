"""训练 Run 服务与模型注册（设计文档 6 / 8 / 9.4 / 9.5）。"""
from __future__ import annotations

import contextlib
import json
import shutil
import threading
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
from sqlalchemy.orm import Session

from app import ids
from app.constants import TERMINAL_RUN_STATUSES, ModelStatus, RunStatus
from app.errors import ApiError, NotFoundError
from app.models import AuditEvent, DatasetSplit, DatasetVersion, ModelVersion, Project, RunEvent, ThresholdVersion, TrainingRun
from app.router_core.policy import Thresholds
from app.router_core.threshold_search import DEFAULT_SEARCH_SPEC, route_metrics
from app.router_core.training import TrainConfig, build_resource_plan
from app.services import artifact_service, dataset_service

# ---------------------------------------------------------------- 生命周期互斥锁（V2 §3.5）

_LIFECYCLE_LOCKS: dict[str, threading.Lock] = {}
_LIFECYCLE_GUARD = threading.Lock()


@contextlib.contextmanager
def project_lifecycle_lock(project_id: str):
    """激活 / 回滚 / 停用按项目互斥：并发切换不会产生双 ACTIVE 或丢失指针更新。"""
    with _LIFECYCLE_GUARD:
        lock = _LIFECYCLE_LOCKS.setdefault(project_id, threading.Lock())
    lock.acquire()
    try:
        yield
    finally:
        lock.release()


def record_audit(
    db: Session,
    project_id: str,
    event: str,
    from_model_id: str | None = None,
    to_model_id: str | None = None,
    details: dict | None = None,
) -> None:
    """写入结构化审计事件（随调用方事务提交）。"""
    db.add(
        AuditEvent(
            id=ids.prefixed("aud"),
            project_id=project_id,
            event=event,
            from_model_id=from_model_id,
            to_model_id=to_model_id,
            details=details or {},
        )
    )


# ---------------------------------------------------------------- runs
def create_run(db: Session, project_id: str, dataset_version_id: str, name: str, config: dict | None) -> TrainingRun:
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError("Project", project_id)
    dataset = db.get(DatasetVersion, dataset_version_id)
    if dataset is None or dataset.project_id != project_id:
        raise NotFoundError("DatasetVersion", dataset_version_id)
    if dataset.status != "FROZEN":
        raise ApiError("DATASET_NOT_FROZEN", "训练只能使用已冻结的数据集版本", 409)

    # Review 修复 §7.1：无 Schema 数据集禁止启动新 Run（历史数据需先迁移回填）
    if not dataset.schema_id:
        raise ApiError(
            "DATASET_SCHEMA_MISMATCH",
            "数据集未绑定标签 Schema（历史数据需迁移回填），禁止启动训练",
            409,
            {"dataset_id": dataset.id},
        )
    from app.services.label_schema_service import resolve_dataset_schema

    schema = resolve_dataset_schema(db, dataset)

    report = dataset_service.latest_report(db, dataset_version_id)
    if report and report.get("errors"):
        raise ApiError(
            "QUALITY_ERRORS",
            "数据集存在阻断级错误，禁止训练",
            409,
            {"errors": report["errors"]},
        )

    distribution = dataset.label_distribution or {}
    missing = [lab for lab in schema.label_keys if not distribution.get(lab)]
    if missing:
        raise ApiError(
            "MISSING_CLASS",
            f"数据集缺少类别 {missing}，训练需要 Schema 全部类别样本",
            409,
            {"missing": missing, "distribution": distribution},
        )

    user_cfg = dict(config or {})
    search_spec = {**DEFAULT_SEARCH_SPEC, **(user_cfg.pop("threshold_search", {}) or {})}
    # 兼容两种形状：{"train": {...}} 或扁平 {"num_epochs": ...}
    if isinstance(user_cfg.get("train"), dict):
        user_cfg = {**user_cfg.pop("train"), **{k: v for k, v in user_cfg.items() if k in TrainConfig.__dataclass_fields__}}
    try:
        train_cfg = TrainConfig.from_dict(user_cfg)
    except ValueError as exc:
        raise ApiError("VALIDATION_ERROR", str(exc), 422) from exc

    resource_preflight = build_resource_plan(dataset.sample_count, train_cfg)

    full_config = {"train": train_cfg.to_dict(), "threshold_search": search_spec, "resource_preflight": resource_preflight}

    # V2 §3.6：Run 创建时固定 Split。Worker 执行时按 run.split_id 解析，
    # 不再取"最新"——杜绝创建到执行之间新建 split 导致的训练数据漂移；
    # 无 split 时此刻按训练种子创建（与原 Worker 懒创建行为一致）。
    split = (
        db.query(DatasetSplit)
        .filter(DatasetSplit.dataset_id == dataset_version_id)
        .order_by(DatasetSplit.created_at.desc(), DatasetSplit.id.desc())
        .first()
    )
    if split is None:
        split = dataset_service.create_split(db, dataset_version_id, seed=train_cfg.seed)

    run = TrainingRun(
        id=ids.prefixed(ids.RUN),
        project_id=project_id,
        dataset_id=dataset_version_id,
        split_id=split.id,
        name=name or f"run-{datetime.now(UTC).strftime('%m%d-%H%M')}",
        config=full_config,
        status=RunStatus.QUEUED,
        progress=0.0,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def resolve_run_split(db: Session, run: TrainingRun) -> DatasetSplit:
    """Worker 侧 Split 解析：只认 Run 固定的 split_id。

    修复前创建的旧 Run（split_id 为空）在此刻固定当前最新 split（无则按
    训练种子创建）并持久化，此后不再随数据集新 split 漂移。
    """
    if run.split_id:
        split = db.get(DatasetSplit, run.split_id)
        if split is None:
            raise RuntimeError(f"Run 固定的 split 不存在: {run.split_id}")
        if split.dataset_id != run.dataset_id:
            raise RuntimeError("Run 固定的 split 不属于其数据集")
        return split
    split = (
        db.query(DatasetSplit)
        .filter(DatasetSplit.dataset_id == run.dataset_id)
        .order_by(DatasetSplit.created_at.desc(), DatasetSplit.id.desc())
        .first()
    )
    if split is None:
        seed = (run.config or {}).get("train", {}).get("seed", 42)
        split = dataset_service.create_split(db, run.dataset_id, seed=seed)
    run.split_id = split.id
    db.commit()
    return split


def get_run(db: Session, run_id: str) -> TrainingRun:
    run = db.get(TrainingRun, run_id)
    if run is None:
        raise NotFoundError("Run", run_id)
    return run


def list_runs(db: Session, project_id: str) -> list[TrainingRun]:
    return (
        db.query(TrainingRun)
        .filter(TrainingRun.project_id == project_id)
        .order_by(TrainingRun.created_at.desc())
        .all()
    )


def cancel_run(db: Session, run_id: str) -> TrainingRun:
    run = get_run(db, run_id)
    if run.status in TERMINAL_RUN_STATUSES:
        raise ApiError("RUN_TERMINATED", f"Run 已处于终态 {run.status}", 409)
    if run.status == RunStatus.QUEUED and not run.worker_id:
        run.status = RunStatus.CANCELLED
        run.finished_at = datetime.now(UTC)
    else:
        run.cancel_requested = True
    db.commit()
    db.refresh(run)
    return run


def retry_run(db: Session, run_id: str) -> TrainingRun:
    run = get_run(db, run_id)
    if run.status not in (RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.INTERRUPTED):
        raise ApiError("RUN_NOT_RETRYABLE", f"仅失败/取消/中断的 Run 可重试，当前 {run.status}", 409)
    dataset = db.get(DatasetVersion, run.dataset_id)
    if dataset is None:
        raise NotFoundError("DatasetVersion", run.dataset_id)
    retry_config = dict(run.config or {})
    train_cfg = TrainConfig.from_dict(retry_config.get("train", {}))
    retry_config["train"] = train_cfg.to_dict()
    retry_config["resource_preflight"] = build_resource_plan(dataset.sample_count, train_cfg)
    new_run = TrainingRun(
        id=ids.prefixed(ids.RUN),
        project_id=run.project_id,
        dataset_id=run.dataset_id,
        split_id=run.split_id,
        name=f"{run.name}-retry",
        config=retry_config,
        status=RunStatus.QUEUED,
        parent_run_id=run.id,
    )
    db.add(new_run)
    db.commit()
    db.refresh(new_run)
    return new_run


def append_event(db: Session, run_id: str, event_type: str, payload: dict) -> RunEvent:
    last = (
        db.query(RunEvent.sequence)
        .filter(RunEvent.run_id == run_id)
        .order_by(RunEvent.sequence.desc())
        .first()
    )
    seq = (last[0] + 1) if last else 1
    event = RunEvent(run_id=run_id, sequence=seq, event_type=event_type, payload=payload)
    db.add(event)
    db.commit()
    return event


def run_events(db: Session, run_id: str, after: int = 0, limit: int = 1000) -> list[RunEvent]:
    get_run(db, run_id)
    return (
        db.query(RunEvent)
        .filter(RunEvent.run_id == run_id, RunEvent.sequence > after)
        .order_by(RunEvent.sequence.asc())
        .limit(limit)
        .all()
    )


# ---------------------------------------------------------------- metrics & errors
def _run_artifact_dir(run: TrainingRun) -> Path:
    if not run.artifacts_dir:
        raise ApiError("ARTIFACT_INCOMPLETE", "Run 尚无制品目录", 409)
    path = Path(run.artifacts_dir)
    if not path.is_dir():
        raise ApiError("ARTIFACT_INCOMPLETE", "Run 制品目录不存在", 409, {"dir": path.name})
    return path


def run_metrics(db: Session, run_id: str) -> dict:
    run = get_run(db, run_id)
    if run.status != RunStatus.SUCCEEDED:
        return {"status": run.status, "available": False}
    art = _run_artifact_dir(run)
    metrics_file = art / "metrics.json"
    if not metrics_file.is_file():
        raise ApiError("ARTIFACT_INCOMPLETE", "metrics.json 缺失", 409)
    return {"status": run.status, "available": True, **artifact_service.read_json(metrics_file)}


def _load_predictions(run: TrainingRun, split: str | None = None) -> pd.DataFrame:
    art = _run_artifact_dir(run)
    pred_file = art / "per_sample_predictions.parquet"
    if not pred_file.is_file():
        raise ApiError("ARTIFACT_INCOMPLETE", "per_sample_predictions.parquet 缺失", 409)
    df = pd.read_parquet(pred_file)
    if split:
        df = df[df["split"] == split]
    return df


def run_errors(db: Session, run_id: str, page: int = 1, page_size: int = 50) -> dict:
    run = get_run(db, run_id)
    if run.status != RunStatus.SUCCEEDED:
        return {"status": run.status, "total": 0, "errors": []}
    df = _load_predictions(run)
    errors = df[(~df["correct_raw"]) | (~df["correct_final"])]
    errors = errors.sort_values(["margin"])
    total = int(len(errors))
    start = (page - 1) * page_size
    rows = errors.iloc[start : start + page_size]
    items = []
    for _, row in rows.iterrows():
        items.append(
            {
                "sample_id": row["sample_id"],
                "text": row["text"],
                "context": row.get("context") if not pd.isna(row.get("context")) else None,
                "true_label": row["true_label"],
                "raw_prediction": row["raw_pred"],
                "final_route": row["final_route"],
                "decision": row["decision"],
                "margin": float(row["margin"]),
                "top_k": json.loads(row["top_k"]) if isinstance(row["top_k"], str) else row["top_k"],
                "reason_codes": row["reason_codes"].split("|") if isinstance(row["reason_codes"], str) else [],
                "risk_slice": row.get("risk_slice") if not pd.isna(row.get("risk_slice")) else None,
                "source": row.get("source") if not pd.isna(row.get("source")) else None,
                "group_id": row.get("group_id") if not pd.isna(row.get("group_id")) else None,
                "split": row["split"],
            }
        )
    return {"status": run.status, "total": total, "page": page, "page_size": page_size, "errors": items}


# ---------------------------------------------------------------- thresholds
def _run_effect_map(run: TrainingRun) -> dict[str, str]:
    """从 Run 制品 label_schema.json 读取 effect 映射（Review 修复 §6.1：
    模拟阈值与训练同源，禁止在函数内部用全局标签推断效果）。"""
    art = _run_artifact_dir(run)
    schema_path = art / "label_schema.json"
    if not schema_path.is_file():
        return {}
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    return {
        item["key"]: item["effect_type"]
        for item in schema.get("label_definitions", [])
        if isinstance(item, dict) and item.get("key") and item.get("effect_type")
    }


def simulate_thresholds(db: Session, run_id: str, overrides: dict | None) -> dict:
    """在 validation 预测上模拟阈值（不落盘、不影响 Run 指标）。"""
    run = get_run(db, run_id)
    df = _load_predictions(run, split="validation")
    prob_cols = [c for c in df.columns if c.startswith("prob_")]
    labels = [c[5:] for c in prob_cols]
    probs = df[prob_cols].to_numpy(dtype="float64")
    y = df["true_label"].map({lab: i for i, lab in enumerate(labels)}).to_numpy(dtype=int)

    thresholds = Thresholds.from_dict(overrides)
    metrics = route_metrics(probs, y, thresholds, labels, _run_effect_map(run))
    spec = (run.config or {}).get("threshold_search", {}).get("constraints", DEFAULT_SEARCH_SPEC["constraints"])
    violations = []
    fwr = metrics.get("false_write_rate") or 0.0
    if fwr > spec.get("max_false_write_rate", 0.005) + 1e-12:
        violations.append(
            {"code": "FALSE_WRITE_EXCEEDED", "message": f"false write rate {fwr} 超过上限 {spec['max_false_write_rate']}"}
        )
    wp = metrics.get("write_precision")
    if wp is not None and wp < spec.get("min_write_precision", 0.95) - 1e-12:
        violations.append(
            {"code": "WRITE_PRECISION_BELOW", "message": f"write precision {wp} 低于下限 {spec['min_write_precision']}"}
        )
    return {"thresholds": thresholds.to_dict(), "metrics": metrics, "violations": violations, "n": int(len(df))}


def save_threshold_version(db: Session, run_id: str, config: dict) -> ThresholdVersion:
    simulation = simulate_thresholds(db, run_id, config)
    if simulation["violations"]:
        raise ApiError(
            "THRESHOLD_CONSTRAINT_VIOLATION",
            "阈值组合违反安全约束，禁止保存",
            422,
            {"violations": simulation["violations"], "metrics": simulation["metrics"]},
        )
    latest = (
        db.query(ThresholdVersion)
        .filter(ThresholdVersion.run_id == run_id)
        .order_by(ThresholdVersion.version.desc())
        .first()
    )
    version = ThresholdVersion(
        id=ids.prefixed(ids.THRESHOLD),
        run_id=run_id,
        version=(latest.version + 1) if latest else 1,
        config=Thresholds.from_dict(config).to_dict(),
        metrics=simulation["metrics"],
        source="manual",
    )
    db.add(version)
    db.commit()
    db.refresh(version)
    return version


# ---------------------------------------------------------------- model registry
def register_model(db: Session, run_id: str, threshold_version_id: str | None = None, name: str | None = None) -> ModelVersion:
    run = get_run(db, run_id)
    if run.status != RunStatus.SUCCEEDED:
        raise ApiError("RUN_NOT_SUCCEEDED", "只有成功的 Run 才能注册模型", 409)

    art = _run_artifact_dir(run)
    required = ["setfit_model", "label_schema.json", "calibration.json", "thresholds.json", "metrics.json", "per_sample_predictions.parquet"]
    missing = [f for f in required if not (art / f).exists()]
    if missing:
        raise ApiError("ARTIFACT_INCOMPLETE", "Run 制品不完整", 409, {"missing": missing})

    artifact_service.verify_manifest(art)

    threshold_row = None
    if threshold_version_id:
        threshold_row = db.get(ThresholdVersion, threshold_version_id)
        if threshold_row is None or threshold_row.run_id != run_id:
            raise NotFoundError("ThresholdVersion", threshold_version_id)

    model_id = ids.prefixed(ids.MODEL)
    target = artifact_service.model_dir(model_id)
    if target.exists():
        shutil.rmtree(target)

    # 复制制品
    shutil.copytree(art / "setfit_model", target / "setfit_model")
    for fname in ("label_schema.json", "calibration.json", "metrics.json", "per_sample_predictions.parquet", "environment.json", "model_card.md"):
        if (art / fname).exists():
            shutil.copy2(art / fname, target / fname)

    if threshold_row is not None:
        artifact_service.write_json(target / "thresholds.json", threshold_row.config)
        threshold_id_used = threshold_row.id
    else:
        shutil.copy2(art / "thresholds.json", target / "thresholds.json")
        threshold_id_used = None

    metrics = artifact_service.read_json(art / "metrics.json")
    test_metrics = metrics.get("test", {}).get("routing", {})
    cls_metrics = metrics.get("test", {}).get("classification", {})
    manifest = artifact_service.build_manifest(
        target,
        {
            "model_version": f"intent-router-{model_id[-8:]}",
            "base_model": run.config["train"]["base_model_id"],
            "dataset_version_id": run.dataset_id,
            "split_id": run.split_id,
            "label_schema_version": "labels-v1",
            "run_id": run_id,
            "threshold_version_id": threshold_id_used,
            "seed": run.config["train"]["seed"],
            "created_at": datetime.now(UTC).isoformat(),
            "metrics_summary": {
                "macro_f1": cls_metrics.get("macro_f1"),
                "false_write_rate": test_metrics.get("false_write_rate"),
                "safe_coverage": test_metrics.get("safe_coverage"),
            },
        },
    )
    manifest_hash = artifact_service.read_json(target / "manifest.json")["artifact_hashes"].get("thresholds.json", "")

    model = ModelVersion(
        id=model_id,
        project_id=run.project_id,
        run_id=run_id,
        threshold_id=threshold_id_used,
        name=name or f"intent-router-{model_id[-8:]}",
        status=ModelStatus.CANDIDATE,
        artifact_path=str(target),
        manifest_hash=manifest_hash,
        manifest=manifest,
    )
    db.add(model)
    db.commit()
    db.refresh(model)
    return model


def list_models(db: Session, project_id: str) -> list[ModelVersion]:
    return (
        db.query(ModelVersion)
        .filter(ModelVersion.project_id == project_id)
        .order_by(ModelVersion.created_at.desc())
        .all()
    )


def get_model(db: Session, model_id: str) -> ModelVersion:
    model = db.get(ModelVersion, model_id)
    if model is None:
        raise NotFoundError("ModelVersion", model_id)
    return model


def archive_model(db: Session, model_id: str) -> ModelVersion:
    """停用（归档）模型。当前 ACTIVE 模型禁止归档（V2 §3.5）。

    旧行为会把 ACTIVE 指针悄悄置空，让项目瞬间失去在线模型且运行时缓存
    仍在服务已归档模型；现在必须先激活/回滚到其他版本。
    """
    model = get_model(db, model_id)
    with project_lifecycle_lock(model.project_id):
        if model.status == ModelStatus.ARCHIVED:
            return model
        if model.status == ModelStatus.ACTIVE:
            project = db.get(Project, model.project_id)
            if project is not None and project.active_model_id == model_id:
                raise ApiError(
                    "CANNOT_ARCHIVE_ACTIVE",
                    "不能归档当前 ACTIVE 模型：请先激活或回滚到其他版本",
                    409,
                )
        previous_status = model.status
        model.status = ModelStatus.ARCHIVED
        record_audit(
            db,
            model.project_id,
            "model_archived",
            from_model_id=model_id,
            details={"previous_status": previous_status},
        )
        db.commit()
        db.refresh(model)
        return model
