"""意图体系：五分类定义与路由输出契约常量（设计文档 3.1 / 3.2 / V2 §3.4）。"""
from __future__ import annotations

from typing import Literal, get_args

from app.errors import ApiError

LABEL_SCHEMA_VERSION = "labels-v1"

# 第一层五分类。nota 不作为标签训练，仅在契约中预留决策值。
LABELS: list[str] = ["information", "read_only", "write_action", "unclear", "oos"]

# V2 §3.4 唯一的标签类型：Schema 层（DraftChange/SamplePatch 等）、服务层、
# Worker 全链路复用，非法标签在入口即 422，不落库。
IntentLabel = Literal["information", "read_only", "write_action", "unclear", "oos"]
assert set(get_args(IntentLabel)) == set(LABELS), "IntentLabel 与 LABELS 必须保持一致"

# 预留的第二阶段决策值，不参与第一层训练
RESERVED_ROUTES: list[str] = ["nota"]

LABEL_INFO: dict[str, dict] = {
    "information": {
        "name": "了解信息",
        "definition": "获取概念、规则、方法或能力说明，不读取真实业务状态",
        "positive_example": "Libra 怎么创建实验？",
        "negative_example": "查实验 123 的状态",
        "color": "blue",
    },
    "read_only": {
        "name": "查询状态",
        "definition": "明确要求读取真实对象或执行只读诊断",
        "positive_example": "审批到哪了？",
        "negative_example": "帮我催一下审批",
        "color": "cyan",
    },
    "write_action": {
        "name": "修改状态",
        "definition": "明确要求创建、修改、发送、撤回、提交、启动等状态变化",
        "positive_example": "帮我撤回 Review 123",
        "negative_example": "怎么撤回 Review？",
        "color": "orange",
    },
    "unclear": {
        "name": "表达不清",
        "definition": "动作、对象或结果不足，无法安全决定",
        "positive_example": "帮我处理一下这个实验",
        "negative_example": "分析实验 123 为什么异常",
        "color": "purple",
    },
    "oos": {
        "name": "超出范围",
        "definition": "不属于当前 Agent 的业务和能力范围",
        "positive_example": "帮我预订会议室",
        "negative_example": "Libra 是否支持暂停实验？",
        "color": "gray",
    },
}

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


def default_label_schema() -> dict:
    """默认（第一版固定）标签 Schema。"""
    return {
        "schema_version": LABEL_SCHEMA_VERSION,
        "labels": [
            {
                "key": key,
                "name": LABEL_INFO[key]["name"],
                "definition": LABEL_INFO[key]["definition"],
                "positive_example": LABEL_INFO[key]["positive_example"],
                "negative_example": LABEL_INFO[key]["negative_example"],
            }
            for key in LABELS
        ],
        "reserved_routes": RESERVED_ROUTES,
    }


def is_valid_label(label: str | None) -> bool:
    return label in LABELS


def ensure_label_schema(labels: list[str | None]) -> None:
    """训练前的标签契约断言（V2 §3.4）：标签集必须与五分类完全一致。

    - 任何 None/空串/越界标签 → INVALID_LABEL（数据损坏，直接失败）
    - 缺类 → MISSING_LABEL_CLASS（分类头维度会偏离五分类契约）
    """
    bad = sorted({lab for lab in labels if lab not in LABELS})
    if bad:
        raise ApiError(
            "INVALID_LABEL",
            f"训练数据存在非法标签: {bad}",
            422,
            {"labels": bad[:10]},
        )
    present = set(labels)
    missing = [lab for lab in LABELS if lab not in present]
    if missing:
        raise ApiError(
            "MISSING_LABEL_CLASS",
            f"训练数据缺少类别 {missing}，分类头将偏离五分类契约",
            422,
            {"missing": missing, "present": sorted(present)},
        )
