"""Pydantic 请求模型（v2）。"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.router_core.taxonomy import IntentLabel  # V2 §3.4：唯一五分类标签类型


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""


class ProjectPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None


class ProjectDeleteRequest(BaseModel):
    confirm_name: str | None = Field(default=None, max_length=200)


class ImportConfig(BaseModel):
    mode: Literal["prelabeled", "unlabeled", "single_label"] = "prelabeled"
    columns: dict[str, str | None] = Field(default_factory=dict)
    label_mapping: dict[str, str] = Field(default_factory=dict)
    default_label: IntentLabel | None = None
    encoding: str | None = None
    name: str | None = None


class SplitConfig(BaseModel):
    ratios: dict[str, float] | None = None
    seed: int = 42


class DraftChange(BaseModel):
    action: Literal["add", "update", "remove"]
    sample_id: str | None = None
    text: str | None = None
    label: IntentLabel | None = None  # 非法标签入口即 422
    context: str | None = None
    group_id: str | None = None
    risk_slice: str | None = None
    is_hard_negative: bool | None = None
    note: str | None = None
    source: str | None = None


class DraftCreate(BaseModel):
    changes: list[DraftChange]
    name: str | None = None


class SamplePatch(BaseModel):
    label: IntentLabel | None = None
    is_hard_negative: bool | None = None
    risk_slice: str | None = None
    group_id: str | None = None
    note: str | None = None


class RunCreate(BaseModel):
    dataset_version_id: str
    name: str | None = None
    config: dict[str, Any] | None = None


class ThresholdConfig(BaseModel):
    default_min_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    write_min_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    oos_min_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    min_margin: float | None = Field(default=None, ge=0.0, le=1.0)


class RegisterModelRequest(BaseModel):
    threshold_version_id: str | None = None
    name: str | None = None


class RewriteOptions(BaseModel):
    """§10.2 /predict 的可选改写参数：不传或 enabled=false = 现有行为完全不变。"""

    enabled: bool = False
    mode: Literal["project_default", "off", "normalize_only", "shadow", "safe_apply"] = "project_default"
    include_trace: bool = False


class PredictRequest(BaseModel):
    project_id: str
    text: str = Field(min_length=1)
    context: str | None = None
    model_version_id: str | None = None
    threshold_overrides: dict[str, float] | None = None
    debug: bool = False
    rewrite: RewriteOptions | None = None


class BatchPredictItem(BaseModel):
    text: str = Field(min_length=1)
    context: str | None = None
    model_version_id: str | None = None
    threshold_overrides: dict[str, float] | None = None


class BatchPredictRequest(BaseModel):
    project_id: str
    items: list[BatchPredictItem]


class CompareRequest(BaseModel):
    project_id: str
    text: str = Field(min_length=1)
    context: str | None = None
    model_a: str | None = None  # null = ACTIVE
    model_b: str


class PlaygroundCaseRequest(BaseModel):
    text: str
    context: str | None = None
    expected_label: IntentLabel | None = None
    predicted_route: str | None = None
    model_version_id: str | None = None
    tags: dict[str, Any] | None = None
    save_text: bool = False


class CleanupRequest(BaseModel):
    target: Literal["uploads_tmp", "playground_history"]
    project_id: str | None = None
