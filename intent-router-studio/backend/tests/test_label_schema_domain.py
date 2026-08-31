"""system_effects / label_schema 领域模型测试（自定义意图标签方案 §11.1）。"""
from __future__ import annotations

import pytest

from app.router_core import label_schema as ls
from app.router_core.system_effects import (
    EFFECT_CEILING,
    REQUIRED_NEXT_GATE,
    SYSTEM_EFFECT_TYPES,
    effect_ceiling_for,
    is_valid_effect_type,
    required_gate_for,
)


def _label(key: str, effect: str = "read_only", **kw) -> ls.LabelDefinition:
    return ls.LabelDefinition(key=key, name=kw.pop("name", key), effect_type=effect, **kw)


# ---------------------------------------------------------------- system effects

def test_effect_types_fixed_enumeration():
    assert SYSTEM_EFFECT_TYPES == ("information", "read_only", "write_action", "unclear", "oos")
    assert EFFECT_CEILING["write_action"] == "external_write_candidate"
    assert REQUIRED_NEXT_GATE["write_action"] == "skill_match_and_confirmation"


def test_unknown_effect_fails_closed():
    assert is_valid_effect_type("write_action") is True
    assert is_valid_effect_type("deploy_to_prod") is False
    # 未知效果 fail closed：不授予任何效果，也不绕过澄清门
    assert effect_ceiling_for("unknown") == "none"
    assert required_gate_for("unknown") == "clarification"


# ---------------------------------------------------------------- 校验

def test_two_label_schema_valid():
    doc = ls.LabelSchemaDocument(labels=[
        _label("faq", "information", order=0),
        _label("create_task", "write_action", order=10),
    ])
    assert ls.validate_schema(doc) == []


@pytest.mark.parametrize(
    "labels,expect_fragment",
    [
        # 少于 2 个 active
        ([_label("only_one", "information")], "启用标签数"),
        # key 非法
        ([_label("Faq", "information"), _label("create", "write_action")], "key 非法"),
        ([_label("a", "information"), _label("create", "write_action")], "key 非法"),
        # effect 非法
        ([_label("faq", "information"), _label("create", "deploy")], "effect_type 非法"),
        # 重复 key
        ([_label("faq", "information"), _label("faq", "read_only")], "重复"),
        # 名称空
        ([_label("faq", "information", name=""), _label("create", "write_action")], "名称"),
        # 全是 unclear/oos
        ([_label("vague", "unclear"), _label("outside", "oos")], "正常业务标签"),
    ],
)
def test_invalid_schemas(labels, expect_fragment):
    problems = ls.validate_schema(ls.LabelSchemaDocument(labels=labels))
    assert any(expect_fragment in p for p in problems), problems


def test_max_labels_enforced():
    labels = [_label(f"intent_{i:03d}", "information") for i in range(101)]
    problems = ls.validate_schema(ls.LabelSchemaDocument(labels=labels))
    assert any("启用标签数" in p for p in problems)


# ---------------------------------------------------------------- hash 与顺序

def test_hash_deterministic_and_order_sensitive():
    a = ls.LabelSchemaDocument(labels=[_label("faq", "information", order=0), _label("create", "write_action", order=10)])
    b = ls.LabelSchemaDocument(labels=[_label("faq", "information", order=0), _label("create", "write_action", order=10)])
    assert ls.schema_hash(a) == ls.schema_hash(b)
    # 数组顺序参与哈希（分类头顺序不同 = 不同 Schema）
    swapped = ls.LabelSchemaDocument(labels=[a.labels[1], a.labels[0]])
    assert ls.schema_hash(swapped) != ls.schema_hash(a)
    # 字段差异
    renamed = ls.LabelSchemaDocument(labels=[_label("faq", "information", name="常见问题", order=0), a.labels[1]])
    assert ls.schema_hash(renamed) != ls.schema_hash(a)


def test_label_keys_in_order_and_effect_type_for():
    doc = ls.LabelSchemaDocument(labels=[
        _label("c_label", "write_action", order=20),
        _label("a_label", "information", order=0),
        _label("b_label", "read_only", order=10, status="deprecated"),
    ])
    assert doc.label_keys_in_order() == ["a_label", "c_label"]  # deprecated 默认排除且按 order
    assert doc.label_keys_in_order(include_deprecated=True) == ["a_label", "b_label", "c_label"]
    assert doc.effect_type_for("c_label") == "write_action"
    assert doc.effect_type_for("missing") is None


# ---------------------------------------------------------------- v1 adapter

def test_v1_json_adapter_identity_effect_mapping():
    from app.router_core.taxonomy import default_label_schema

    doc = ls.document_from_json(default_label_schema())
    assert doc.schema_format == ls.SCHEMA_FORMAT_V2
    keys = doc.label_keys_in_order()
    assert keys == ["information", "read_only", "write_action", "unclear", "oos"]
    for key in keys:
        assert doc.effect_type_for(key) == key  # §8.2 恒等映射
    assert ls.validate_schema(doc) == []


def test_is_v1_detection():
    assert ls.is_v1_schema_json(None) is True
    assert ls.is_v1_schema_json({}) is True
    assert ls.is_v1_schema_json({"schema_version": "labels-v1", "labels": []}) is True
    assert ls.is_v1_schema_json({"schema_format": ls.SCHEMA_FORMAT_V2}) is False


def test_default_compat_document_matches_five_class():
    doc = ls.default_compat_document()
    assert len(doc.labels) == 5
    assert doc.effect_type_for("write_action") == "write_action"
    assert ls.schema_hash(doc) == ls.schema_hash(ls.default_compat_document())


def test_normalize_strips_and_fills_order():
    doc = ls.document_from_json({
        "schema_format": ls.SCHEMA_FORMAT_V2,
        "labels": [
            {"key": "faq", "name": "  常见问题  ", "effect_type": " information "},
            {"key": "create", "name": "创建", "effect_type": "write_action", "order": 5},
        ],
    })
    assert doc.labels[0].name == "常见问题"
    assert doc.labels[0].effect_type == "information"
    assert doc.labels[1].order == 5


def test_training_label_assertions_dynamic():
    doc = ls.LabelSchemaDocument(labels=[
        _label("faq", "information", order=0), _label("create", "write_action", order=10),
    ])
    ls.ensure_training_labels(["faq", "create", "faq"], doc)
    from app.errors import ApiError

    with pytest.raises(ApiError) as exc:
        ls.ensure_training_labels(["faq", "legacy_label"], doc)
    assert exc.value.code == "INVALID_LABEL"
    with pytest.raises(ApiError) as exc:
        ls.ensure_training_labels(["faq", "faq"], doc)
    assert exc.value.code == "MISSING_LABEL_CLASS"
    # 重复现 Run 不要求全覆盖时只校验越界
    ls.ensure_training_labels(["faq"], doc, require_all_active=False)
