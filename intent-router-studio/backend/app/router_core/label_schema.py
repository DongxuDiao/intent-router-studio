"""项目标签 Schema 领域模型（自定义意图标签方案 §4.2 / §6.1）。

- Schema 文档两代格式：intent-schema-v1（旧五分类，effect_type=key 恒等）
  与 intent-schema-v2（业务意图 → 系统效果类型映射）
- labels 数组顺序即模型分类头顺序，训练制品必须原样保存
- hash 为归一化内容的 SHA-256，禁止发布完全相同的版本
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from app.errors import ApiError
from app.router_core.system_effects import SYSTEM_EFFECT_TYPES

SCHEMA_FORMAT_V1 = "intent-schema-v1"
SCHEMA_FORMAT_V2 = "intent-schema-v2"

# key 格式：小写字母开头，2~64 位，[a-z0-9_]
LABEL_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")

MIN_ACTIVE_LABELS = 2
MAX_ACTIVE_LABELS = 100

# 系统效果类型 key 本身合法（v1 恒等映射），自定义 key 不能与其冲突？
# 方案未禁止同名：v1 恒等标签与自定义标签同 key 会造成歧义，禁止。
RESERVED_SYSTEM_KEYS = frozenset({"nota"})


class ModelSchemaMismatch(ApiError):
    """Schema 映射缺失/非法：fail closed，禁止按普通标签继续（Review 修复 §2）。"""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("MODEL_SCHEMA_MISMATCH", message, 500, details or {})


@dataclass(frozen=True)
class ResolvedLabelSchema:
    """统一不可变 Schema 运行时上下文（Review 修复 §2）。

    - label_keys 顺序即分类头顺序；
    - effect_by_label 只能由服务端 Schema 解析，禁止各模块自行拼装；
    - 未知标签 / 非法 effect type 一律 fail closed。
    """

    schema_id: str | None
    schema_hash: str
    label_keys: tuple[str, ...]
    effect_by_label: Mapping[str, str]
    document: LabelSchemaDocument = field(repr=False)  # 完整文档（名称/示例），打包与展示用

    def effect_type_for(self, label: str) -> str:
        effect = self.effect_by_label.get(label)
        if effect not in SYSTEM_EFFECT_TYPES:
            raise ModelSchemaMismatch(
                f"标签 {label!r} 缺少合法 effect 映射",
                {"label": label, "schema_id": self.schema_id},
            )
        return effect

    def labels_for_effect(self, effect_type: str) -> tuple[str, ...]:
        return tuple(k for k in self.label_keys if self.effect_by_label.get(k) == effect_type)

    @classmethod
    def from_document(
        cls,
        doc: LabelSchemaDocument,
        schema_id: str | None,
        hash_value: str | None = None,
    ) -> ResolvedLabelSchema:
        return cls(
            schema_id=schema_id,
            schema_hash=hash_value or schema_hash(doc),
            label_keys=tuple(doc.label_keys_in_order()),
            effect_by_label={d.key: d.effect_type for d in doc.labels},
            document=doc,
        )


@dataclass
class LabelDefinition:
    """单个业务意图标签。"""

    key: str
    name: str
    effect_type: str
    description: str = ""
    status: str = "active"  # active | deprecated
    order: int = 0
    positive_examples: list[str] = field(default_factory=list)
    negative_examples: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "description": self.description,
            "effect_type": self.effect_type,
            "status": self.status,
            "order": self.order,
            "positive_examples": list(self.positive_examples),
            "negative_examples": list(self.negative_examples),
        }


@dataclass
class LabelSchemaDocument:
    """不可变 Schema 文档；数组顺序即分类头顺序。"""

    labels: list[LabelDefinition]
    schema_format: str = SCHEMA_FORMAT_V2
    created_from: str | None = None

    # ---- 读取
    def label_keys_in_order(self, include_deprecated: bool = False) -> list[str]:
        return [d.key for d in self.ordered(include_deprecated)]

    def ordered(self, include_deprecated: bool = False) -> list[LabelDefinition]:
        items = [d for d in self.labels if include_deprecated or d.status == "active"]
        return sorted(items, key=lambda d: (d.order, d.key))

    def active_labels(self) -> list[LabelDefinition]:
        return self.ordered(include_deprecated=False)

    def definition(self, key: str) -> LabelDefinition | None:
        return next((d for d in self.labels if d.key == key), None)

    def effect_type_for(self, key: str) -> str | None:
        d = self.definition(key)
        return d.effect_type if d is not None else None

    def to_dict(self) -> dict[str, Any]:
        doc: dict[str, Any] = {
            "schema_format": self.schema_format,
            "labels": [d.to_dict() for d in self.labels],
            "reserved_decisions": ["nota"],
        }
        if self.created_from:
            doc["created_from"] = self.created_from
        return doc


# ---------------------------------------------------------------- 校验

def validate_schema(doc: LabelSchemaDocument) -> list[str]:
    """发布前校验（§3.3）；返回问题列表，空列表 = 合法。"""
    problems: list[str] = []
    if doc.schema_format not in (SCHEMA_FORMAT_V1, SCHEMA_FORMAT_V2):
        problems.append(f"schema_format 必须是 {SCHEMA_FORMAT_V1}/{SCHEMA_FORMAT_V2}")
    keys: list[str] = []
    for d in doc.labels:
        if not LABEL_KEY_RE.match(d.key or ""):
            problems.append(f"标签 key 非法（需 ^[a-z][a-z0-9_]{{1,63}}$）: {d.key!r}")
        if d.key in RESERVED_SYSTEM_KEYS:
            problems.append(f"标签 key 保留: {d.key}")
        if not (d.name or "").strip() or len(d.name) > 100:
            problems.append(f"标签名称必须非空且 ≤100 字: {d.key!r}")
        if d.effect_type not in SYSTEM_EFFECT_TYPES:
            problems.append(f"标签 {d.key!r} 的 effect_type 非法: {d.effect_type!r}")
        if d.status not in ("active", "deprecated"):
            problems.append(f"标签 {d.key!r} 的 status 非法: {d.status!r}")
        keys.append(d.key)
    dupes = sorted({k for k in keys if keys.count(k) > 1})
    if dupes:
        problems.append(f"标签 key 重复: {dupes[:5]}")
    active = [d for d in doc.labels if d.status == "active"]
    if not (MIN_ACTIVE_LABELS <= len(active) <= MAX_ACTIVE_LABELS):
        problems.append(f"启用标签数必须为 {MIN_ACTIVE_LABELS}~{MAX_ACTIVE_LABELS}，当前 {len(active)}")
    if active and all(d.effect_type in ("unclear", "oos") for d in active):
        problems.append("至少需要一个非 unclear/oos 的正常业务标签")
    return problems


def normalize(doc: LabelSchemaDocument) -> LabelSchemaDocument:
    """归一化：去空白、补 order（缺省按数组顺序）。数组顺序保持原样。"""
    labels = []
    for i, d in enumerate(doc.labels):
        labels.append(
            LabelDefinition(
                key=(d.key or "").strip(),
                name=(d.name or "").strip(),
                description=(d.description or "").strip(),
                effect_type=(d.effect_type or "").strip(),
                status=d.status or "active",
                order=d.order if isinstance(d.order, int) else i * 10,
                positive_examples=[(p or "").strip() for p in d.positive_examples if (p or "").strip()],
                negative_examples=[(n or "").strip() for n in d.negative_examples if (n or "").strip()],
            )
        )
    return LabelSchemaDocument(
        labels=labels,
        schema_format=doc.schema_format,
        created_from=doc.created_from,
    )


def schema_hash(doc: LabelSchemaDocument) -> str:
    """归一化内容的确定性 SHA-256（排序键固定，数组顺序参与哈希）。"""
    payload = json.dumps(doc.to_dict(), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- v1 兼容

_V1_LEGACY_FIELDS = ("definition", "positive_example", "negative_example")


def is_v1_schema_json(schema_json: dict | None) -> bool:
    """缺 schema_format / 使用旧 labels-v1 的一律按 v1 处理（含 None/空）。"""
    if not isinstance(schema_json, dict):
        return True
    fmt = schema_json.get("schema_format") or schema_json.get("schema_version")
    return fmt in (None, "labels-v1", SCHEMA_FORMAT_V1)


def document_from_json(schema_json: dict | None) -> LabelSchemaDocument:
    """DB 行 schema_json → 文档；v1（labels-v1 / 无 schema_format）经适配器
    转为恒等映射的 v2 文档（effect_type = key，方案 §8.2）。"""
    if not isinstance(schema_json, dict) or not schema_json.get("labels"):
        return default_compat_document()
    labels = []
    for i, item in enumerate(schema_json.get("labels", [])):
        if not isinstance(item, dict) or not item.get("key"):
            continue
        effect_type = item.get("effect_type") or item.get("key")
        labels.append(
            LabelDefinition(
                key=str(item["key"]),
                name=str(item.get("name") or item["key"]),
                description=str(item.get("description") or item.get("definition") or ""),
                effect_type=str(effect_type),
                status=str(item.get("status") or "active"),
                order=item.get("order") if isinstance(item.get("order"), int) else i * 10,
                positive_examples=_examples(item, "positive"),
                negative_examples=_examples(item, "negative"),
            )
        )
    return normalize(LabelSchemaDocument(labels=labels, schema_format=SCHEMA_FORMAT_V2))


def _examples(item: dict, prefix: str) -> list[str]:
    values = item.get(f"{prefix}_examples")
    if isinstance(values, list):
        return [str(v) for v in values if v]
    single = item.get(f"{prefix}_example")
    return [str(single)] if single else []


def default_compat_document() -> LabelSchemaDocument:
    """兼容五分类的 v2 文档（新项目默认；effect_type 与 key 恒等）。"""
    from app.router_core.taxonomy import LABEL_INFO, LABELS

    labels = [
        LabelDefinition(
            key=key,
            name=LABEL_INFO[key]["name"],
            description=LABEL_INFO[key]["definition"],
            effect_type=key,
            status="active",
            order=i * 10,
            positive_examples=[LABEL_INFO[key]["positive_example"]],
            negative_examples=[LABEL_INFO[key]["negative_example"]],
        )
        for i, key in enumerate(LABELS)
    ]
    return LabelSchemaDocument(labels=labels, schema_format=SCHEMA_FORMAT_V2)


def ensure_training_labels(
    labels: list[str | None], doc: LabelSchemaDocument, *, require_all_active: bool = True
) -> None:
    """训练前契约断言（动态版 V2 §3.4）：
    - 越界标签 → INVALID_LABEL（数据损坏）
    - 训练集缺少 active 训练标签 → MISSING_LABEL_CLASS（分类头维度偏离）
    """
    active_keys = set(doc.label_keys_in_order())
    bad = sorted({lab for lab in labels if lab not in active_keys})
    if bad:
        raise ApiError("INVALID_LABEL", f"训练数据存在 Schema 外标签: {bad}", 422, {"labels": bad[:10]})
    if require_all_active:
        present = set(labels)
        missing = [k for k in doc.label_keys_in_order() if k not in present]
        if missing:
            raise ApiError(
                "MISSING_LABEL_CLASS",
                f"训练数据缺少类别 {missing}，分类头将偏离 Schema 契约",
                422,
                {"missing": missing, "present": sorted(present)},
            )
