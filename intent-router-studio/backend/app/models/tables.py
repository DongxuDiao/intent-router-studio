"""数据库表定义（设计文档第 10 节）。

- ID 为带前缀 ULID 字符串
- 时间统一存 UTC
- JSON 字段存配置与汇总，逐样本数据存 Parquet
"""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    active_model_id: Mapped[str | None] = mapped_column(String(40), ForeignKey("model_versions.id", use_alter=True), nullable=True)
    # Query 改写能力（修改方案 §11.1）：指向当前生效的 RewriteConfigVersion
    active_rewrite_config_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class LabelSchemaVersion(Base):
    __tablename__ = "label_schema_versions"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(40), ForeignKey("projects.id"))
    version: Mapped[int] = mapped_column(Integer)
    schema_json: Mapped[dict] = mapped_column(JSON)
    hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Upload(Base):
    __tablename__ = "uploads"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(40), ForeignKey("projects.id"))
    original_name: Mapped[str] = mapped_column(String(500))
    safe_path: Mapped[str] = mapped_column(String(1000))
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(Integer)
    content_type: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    error_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class DatasetVersion(Base):
    __tablename__ = "dataset_versions"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(40), ForeignKey("projects.id"))
    parent_id: Mapped[str | None] = mapped_column(String(40), ForeignKey("dataset_versions.id"), nullable=True)
    schema_id: Mapped[str | None] = mapped_column(String(40), ForeignKey("label_schema_versions.id"), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    name: Mapped[str] = mapped_column(String(200), default="")
    # import: 上传导入直接冻结；draft: 派生草稿（可编辑）
    origin: Mapped[str] = mapped_column(String(20), default="import")
    status: Mapped[str] = mapped_column(String(20), default="DRAFT")
    parquet_path: Mapped[str] = mapped_column(String(1000))
    raw_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    labeled_count: Mapped[int] = mapped_column(Integer, default=0)
    label_distribution: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    manifest: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    change_summary: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(100), default="local")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    __table_args__ = (Index("ix_dataset_versions_project", "project_id"),)


class DatasetQualityReport(Base):
    __tablename__ = "dataset_quality_reports"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    dataset_id: Mapped[str] = mapped_column(String(40), ForeignKey("dataset_versions.id"))
    report_json: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class DatasetSplit(Base):
    __tablename__ = "dataset_splits"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    dataset_id: Mapped[str] = mapped_column(String(40), ForeignKey("dataset_versions.id"))
    seed: Mapped[int] = mapped_column(Integer)
    algorithm: Mapped[str] = mapped_column(String(50), default="group_stratified_v1")
    ratios: Mapped[dict] = mapped_column(JSON)
    parquet_path: Mapped[str] = mapped_column(String(1000))
    stats_json: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class TrainingRun(Base):
    __tablename__ = "training_runs"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(40), ForeignKey("projects.id"))
    dataset_id: Mapped[str] = mapped_column(String(40), ForeignKey("dataset_versions.id"))
    split_id: Mapped[str | None] = mapped_column(String(40), ForeignKey("dataset_splits.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    config: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(30), default="QUEUED")
    stage: Mapped[str | None] = mapped_column(String(30), nullable=True)
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    worker_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    parent_run_id: Mapped[str | None] = mapped_column(String(40), ForeignKey("training_runs.id"), nullable=True)
    error: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    artifacts_dir: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    __table_args__ = (Index("ix_training_runs_status", "status"),)


class RunEvent(Base):
    __tablename__ = "run_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(40), ForeignKey("training_runs.id"))
    sequence: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(30))
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_run_event_sequence"),
        Index("ix_run_events_run_seq", "run_id", "sequence"),
    )


class RunMetric(Base):
    __tablename__ = "run_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(40), ForeignKey("training_runs.id"))
    split: Mapped[str] = mapped_column(String(30), default="test")
    slice: Mapped[str] = mapped_column(String(50), default="all")
    metric_name: Mapped[str] = mapped_column(String(100))
    value: Mapped[float] = mapped_column(Float)
    support: Mapped[int] = mapped_column(Integer, default=0)


class ThresholdVersion(Base):
    __tablename__ = "threshold_versions"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(40), ForeignKey("training_runs.id"))
    version: Mapped[int] = mapped_column(Integer, default=1)
    config: Mapped[dict] = mapped_column(JSON)
    metrics: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    source: Mapped[str] = mapped_column(String(20), default="searched")  # searched | manual
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class ModelVersion(Base):
    __tablename__ = "model_versions"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(40), ForeignKey("projects.id"))
    run_id: Mapped[str] = mapped_column(String(40), ForeignKey("training_runs.id"))
    threshold_id: Mapped[str | None] = mapped_column(String(40), ForeignKey("threshold_versions.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(20), default="CANDIDATE")
    artifact_path: Mapped[str] = mapped_column(String(1000))
    manifest_hash: Mapped[str] = mapped_column(String(64))
    manifest: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    activated_at: Mapped[datetime | None] = mapped_column(nullable=True)


class PlaygroundCase(Base):
    __tablename__ = "playground_cases"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(40), ForeignKey("projects.id"))
    text_hash: Mapped[str] = mapped_column(String(64))
    # 默认隐私模式不保存原文；用户显式勾选后才保存
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    context: Mapped[str | None] = mapped_column(Text, nullable=True)
    expected_label: Mapped[str | None] = mapped_column(String(30), nullable=True)
    predicted_route: Mapped[str | None] = mapped_column(String(30), nullable=True)
    model_version_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    is_correct: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    tags: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    __table_args__ = (Index("ix_playground_cases_project", "project_id"),)


# ---------------------------------------------------------------- Query 改写（修改方案 §11）


class RewriteConfigVersion(Base):
    """项目级 Query 改写配置，版本化不可变（§11.1）。"""

    __tablename__ = "rewrite_config_versions"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(40), ForeignKey("projects.id"))
    version: Mapped[int] = mapped_column(Integer, default=1)
    config: Mapped[dict] = mapped_column(JSON)
    hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="DRAFT")  # DRAFT / ACTIVE / ARCHIVED
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    __table_args__ = (Index("ix_rewrite_config_project", "project_id"),)


class TerminologyVersion(Base):
    """项目术语表，版本化不可变（§11.2）。terms 结构见修改方案 §11.2。"""

    __tablename__ = "terminology_versions"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(40), ForeignKey("projects.id"))
    version: Mapped[int] = mapped_column(Integer, default=1)
    terms: Mapped[dict] = mapped_column(JSON)
    hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    __table_args__ = (Index("ix_terminology_project", "project_id"),)


class RewriteFeedback(Base):
    """改写人工反馈（§11.3）。默认只保存 hash；原文仅在用户显式允许时保存。"""

    __tablename__ = "rewrite_feedback"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(40), ForeignKey("projects.id"))
    input_hash: Mapped[str] = mapped_column(String(64))
    original_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    context: Mapped[str | None] = mapped_column(Text, nullable=True)
    proposed_rewrite: Mapped[str | None] = mapped_column(Text, nullable=True)
    edited_rewrite: Mapped[str | None] = mapped_column(Text, nullable=True)
    verdict: Mapped[str] = mapped_column(String(20))  # accept / reject / edit
    reason_codes: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    original_route: Mapped[str | None] = mapped_column(String(30), nullable=True)
    rewrite_route: Mapped[str | None] = mapped_column(String(30), nullable=True)
    model_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    __table_args__ = (Index("ix_rewrite_feedback_project", "project_id"),)


class RewriteProviderConnection(Base):
    """改写模型连接（外部模型 API 接入 V1 §6.1）。

    - 系统级可复用资源，不随项目删除；项目配置只保存引用
    - api_key_ciphertext 为 AES-256-GCM 密文（AAD = id:revision），
      明文 Key 永不落库、永不回显；DELETE /credential 后两列置空
    - revision 随影响输出/鉴权的字段更新自增，用于 Provider 缓存与缓存键隔离
    """

    __tablename__ = "rewrite_provider_connections"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    provider_type: Mapped[str] = mapped_column(String(30))  # glm | openai_compatible
    base_url: Mapped[str] = mapped_column(String(500))
    model_id: Mapped[str] = mapped_column(String(200))
    api_key_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    api_key_nonce: Mapped[str | None] = mapped_column(String(100), nullable=True)
    api_key_hint: Mapped[str] = mapped_column(String(16), default="****")
    generation_config: Mapped[dict] = mapped_column(JSON, default=dict)
    capabilities: Mapped[dict] = mapped_column(JSON, default=dict)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    egress_acknowledged_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_test_status: Mapped[str | None] = mapped_column(String(20), nullable=True)  # SUCCESS | FAILED
    last_test_error_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    last_test_latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_tested_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)

    __table_args__ = (Index("ix_provider_connections_type", "provider_type"),)


# ---------------------------------------------------------------- 审计（修改方案 V2 §3.5）


class AuditEvent(Base):
    """模型生命周期结构化审计事件：激活 / 回滚 / 停用（含旧→新 model_id）。"""

    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(40), ForeignKey("projects.id"))
    event: Mapped[str] = mapped_column(String(30))  # model_activated / model_rolled_back / model_archived
    from_model_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    to_model_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    __table_args__ = (Index("ix_audit_events_project", "project_id"),)
