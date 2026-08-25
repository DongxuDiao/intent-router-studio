"""RewriteResult 协议与 Provider 输出解析测试（修改方案 §6 / §16.1）。"""
from __future__ import annotations

import json

import pytest

from app.query_rewrite.schemas import (
    ProviderOutput,
    RewriteParseError,
    parse_provider_output,
)


def _valid_output(**overrides) -> dict:
    base = {
        "standalone_query": "如何停止实验 123？",
        "rewrite_type": "context_resolution",
        "should_use": True,
        "confidence": 0.93,
        "preserved_intent": True,
        "mentioned_action": "了解如何停止实验",
        "objects": [{"type": "experiment", "value": "123", "source": "context", "confidence": 1.0}],
        "constraints": {"current_state": "正在运行"},
        "missing_slots": [],
        "assumptions": [],
        "used_context_refs": ["context[0]"],
        "reason_codes": ["RESOLVED_PRONOUN"],
    }
    base.update(overrides)
    return base


def test_parse_plain_json():
    out = parse_provider_output(json.dumps(_valid_output(), ensure_ascii=False))
    assert out.standalone_query == "如何停止实验 123？"
    assert out.rewrite_type == "context_resolution"
    assert out.objects[0].source == "context"


def test_parse_json_inside_code_fence_and_prose():
    raw = "好的，改写结果如下：\n```json\n" + json.dumps(_valid_output(), ensure_ascii=False) + "\n```\n以上。"
    out = parse_provider_output(raw)
    assert out.confidence == pytest.approx(0.93)


def test_parse_leading_prose_without_fence():
    raw = "结果：" + json.dumps(_valid_output(), ensure_ascii=False) + "（完）"
    out = parse_provider_output(raw)
    assert out.preserved_intent is True


def test_invalid_json_raises():
    with pytest.raises(RewriteParseError):
        parse_provider_output("这不是 JSON")


def test_empty_output_raises():
    with pytest.raises(RewriteParseError):
        parse_provider_output("   ")


def test_unbalanced_json_raises():
    with pytest.raises(RewriteParseError):
        parse_provider_output('{"standalone_query": "x"')


def test_missing_required_field_rejected():
    bad = _valid_output()
    del bad["standalone_query"]
    with pytest.raises(RewriteParseError):
        parse_provider_output(json.dumps(bad, ensure_ascii=False))


def test_unknown_reason_code_rejected():
    with pytest.raises(RewriteParseError):
        parse_provider_output(json.dumps(_valid_output(reason_codes=["WHAT_IS_THIS"]), ensure_ascii=False))


def test_unknown_top_level_fields_ignored():
    out = parse_provider_output(json.dumps({**_valid_output(), "extra_thought": "嗯"}, ensure_ascii=False))
    assert out.standalone_query  # 未知字段不致命


def test_confidence_out_of_range_rejected():
    with pytest.raises(RewriteParseError):
        parse_provider_output(json.dumps(_valid_output(confidence=1.5), ensure_ascii=False))


def test_unknown_rewrite_type_rejected():
    with pytest.raises(RewriteParseError):
        parse_provider_output(json.dumps(_valid_output(rewrite_type="creative"), ensure_ascii=False))


def test_provider_output_model_defaults():
    out = ProviderOutput(standalone_query="原文不变", confidence=0.9)
    assert out.rewrite_type == "none"
    assert out.should_use is True
    assert out.objects == []
