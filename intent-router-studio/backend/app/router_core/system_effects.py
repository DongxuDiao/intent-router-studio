"""系统效果类型（自定义意图标签方案 §2.2）。

平台固定枚举，项目不可增删改：业务意图（intent label）可以自定义，
但"该意图最多允许产生什么效果"只能从这里取值。effect ceiling 与
next gate 只能由服务端映射，不接受客户端提交。
"""
from __future__ import annotations

# 固定的系统效果类型；nota 继续作为策略层拒识决策值，不是训练标签
SYSTEM_EFFECT_TYPES: tuple[str, ...] = (
    "information",
    "read_only",
    "write_action",
    "unclear",
    "oos",
)

RESERVED_DECISIONS: tuple[str, ...] = ("nota",)

# effect_ceiling 只能限制后续允许的最大副作用，不能提升权限
EFFECT_CEILING: dict[str, str] = {
    "information": "none",
    "read_only": "read_only",
    "write_action": "external_write_candidate",
    "unclear": "none",
    "oos": "none",
    "nota": "none",
}

REQUIRED_NEXT_GATE: dict[str, str] = {
    "information": "answer_or_kb",
    "read_only": "readonly_skill_match",
    "write_action": "skill_match_and_confirmation",
    "unclear": "clarification",
    "oos": "capability_boundary",
    "nota": "skill_reselection",
}

EFFECT_INFO: dict[str, dict] = {
    "information": {"name": "只回答概念/规则/方法", "ceiling": "none"},
    "read_only": {"name": "查询真实状态/只读诊断", "ceiling": "read_only"},
    "write_action": {"name": "可能修改状态（仅候选资格）", "ceiling": "external_write_candidate"},
    "unclear": {"name": "信息不足需澄清", "ceiling": "none"},
    "oos": {"name": "超出能力范围", "ceiling": "none"},
}


def is_valid_effect_type(effect_type: str | None) -> bool:
    return effect_type in SYSTEM_EFFECT_TYPES


def effect_ceiling_for(effect_type: str) -> str:
    """未知效果类型属于内部错误：fail closed 到 none（不授予任何效果）。"""
    return EFFECT_CEILING.get(effect_type, "none")


def required_gate_for(effect_type: str) -> str:
    return REQUIRED_NEXT_GATE.get(effect_type, "clarification")
