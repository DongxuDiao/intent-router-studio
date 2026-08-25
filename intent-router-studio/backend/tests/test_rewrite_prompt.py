"""Prompt 构造测试（修改方案 §8 / §16.1）。"""
from __future__ import annotations

import json

from app.query_rewrite.prompt import (
    FEW_SHOTS,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    build_messages,
)


def test_system_prompt_contains_core_rules():
    for rule in ("不得创造", "不得将", "不得删除或反转", "JSON Schema", "不表示执行授权"):
        assert rule in SYSTEM_PROMPT


def test_few_shots_pairs_and_valid_json_answers():
    # 3 组问答：咨询不变执行 / 无法解析不猜 / 保留否定
    users = [m for m in FEW_SHOTS if m["role"] == "user"]
    assistants = [m for m in FEW_SHOTS if m["role"] == "assistant"]
    assert len(users) == len(assistants) == 3
    parsed = [json.loads(a["content"]) for a in assistants]
    assert parsed[0]["standalone_query"].startswith("如何")  # 咨询保持疑问
    assert parsed[1]["rewrite_type"] == "none" and parsed[1]["reason_codes"] == ["AMBIGUOUS_REFERENCE"]
    assert "不要" in parsed[2]["standalone_query"]  # 否定保留


def test_build_messages_structure():
    msgs = build_messages("这个怎么停？", "当前讨论实验 123")
    assert msgs[0]["role"] == "system"
    assert msgs[-1]["role"] == "user"
    assert "original_query: 这个怎么停？" in msgs[-1]["content"]
    assert "context: 当前讨论实验 123" in msgs[-1]["content"]


def test_empty_context_placeholder():
    msgs = build_messages("把那个关了", None)
    assert "context: （空）" in msgs[-1]["content"]


def test_terminology_hint_included():
    msgs = build_messages("libra exp 看下", None, terminology={"libra exp": "Libra 实验"})
    assert "libra exp → Libra 实验" in msgs[-1]["content"]


def test_context_trimmed():
    msgs = build_messages("q", "x" * 100, max_context_chars=10)
    assert "x" * 10 in msgs[-1]["content"]
    assert "x" * 11 not in msgs[-1]["content"]


def test_prompt_version_pinned():
    # v2：few-shot 输出精简（省略空字段），约束 CPU 环境下的输出长度
    # v3：V2 §3.2 对象溯源——告知模型补全对象必须来自 context（服务端安全门独立复核）
    assert PROMPT_VERSION == "rewrite-prompt-v5"
