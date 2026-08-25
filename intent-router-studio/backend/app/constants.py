"""状态常量与状态机定义。"""
from __future__ import annotations


# ---- 上传状态 ----
class UploadStatus:
    PENDING = "PENDING"
    IMPORTED = "IMPORTED"
    FAILED = "FAILED"


# ---- 数据集版本状态：DRAFT 可编辑；FROZEN 不可变，仅可派生草稿 ----
class DatasetStatus:
    DRAFT = "DRAFT"
    FROZEN = "FROZEN"


# ---- 训练状态机（设计文档 6.1）----
class RunStatus:
    DRAFT = "DRAFT"
    QUEUED = "QUEUED"
    PREPARING = "PREPARING"
    TRAINING_EMBEDDING = "TRAINING_EMBEDDING"
    TRAINING_HEAD = "TRAINING_HEAD"
    CALIBRATING = "CALIBRATING"
    SEARCHING_THRESHOLDS = "SEARCHING_THRESHOLDS"
    EVALUATING = "EVALUATING"
    PACKAGING = "PACKAGING"
    SUCCEEDED = "SUCCEEDED"
    CANCELLING = "CANCELLING"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"


# Worker 执行的顺序阶段
RUN_STAGES = [
    RunStatus.PREPARING,
    RunStatus.TRAINING_EMBEDDING,
    RunStatus.TRAINING_HEAD,
    RunStatus.CALIBRATING,
    RunStatus.SEARCHING_THRESHOLDS,
    RunStatus.EVALUATING,
    RunStatus.PACKAGING,
]

ACTIVE_RUN_STATUSES = set(RUN_STAGES) | {RunStatus.CANCELLING}
TERMINAL_RUN_STATUSES = {
    RunStatus.SUCCEEDED,
    RunStatus.CANCELLED,
    RunStatus.FAILED,
    RunStatus.INTERRUPTED,
}

# 合法状态迁移（用于校验与测试；API 侧只触发 QUEUED/CANCELLED 相关迁移）
ALLOWED_RUN_TRANSITIONS: dict[str, set[str]] = {
    RunStatus.DRAFT: {RunStatus.QUEUED, RunStatus.CANCELLED},
    RunStatus.QUEUED: {RunStatus.PREPARING, RunStatus.CANCELLED},
    RunStatus.PREPARING: {
        RunStatus.TRAINING_EMBEDDING,
        RunStatus.FAILED,
        RunStatus.CANCELLING,
    },
    RunStatus.TRAINING_EMBEDDING: {
        RunStatus.TRAINING_HEAD,
        RunStatus.FAILED,
        RunStatus.CANCELLING,
    },
    RunStatus.TRAINING_HEAD: {
        RunStatus.CALIBRATING,
        RunStatus.FAILED,
        RunStatus.CANCELLING,
    },
    RunStatus.CALIBRATING: {
        RunStatus.SEARCHING_THRESHOLDS,
        RunStatus.FAILED,
        RunStatus.CANCELLING,
    },
    RunStatus.SEARCHING_THRESHOLDS: {
        RunStatus.EVALUATING,
        RunStatus.FAILED,
        RunStatus.CANCELLING,
    },
    RunStatus.EVALUATING: {
        RunStatus.PACKAGING,
        RunStatus.FAILED,
        RunStatus.CANCELLING,
    },
    RunStatus.PACKAGING: {
        RunStatus.SUCCEEDED,
        RunStatus.FAILED,
        RunStatus.CANCELLING,
    },
    RunStatus.CANCELLING: {RunStatus.CANCELLED, RunStatus.FAILED, RunStatus.INTERRUPTED},
    RunStatus.SUCCEEDED: set(),
    RunStatus.CANCELLED: set(),
    RunStatus.FAILED: set(),
    RunStatus.INTERRUPTED: set(),
}


class ModelStatus:
    CANDIDATE = "CANDIDATE"
    VALIDATED = "VALIDATED"
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


ALLOWED_MODEL_TRANSITIONS: dict[str, set[str]] = {
    ModelStatus.CANDIDATE: {ModelStatus.VALIDATED, ModelStatus.ARCHIVED},
    ModelStatus.VALIDATED: {ModelStatus.ACTIVE, ModelStatus.ARCHIVED},
    ModelStatus.ACTIVE: {ModelStatus.ARCHIVED},
    ModelStatus.ARCHIVED: set(),
}
