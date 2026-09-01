"""确定性决策门（设计文档 3.3）。

模型给出概率，策略层决定是否接受。禁止强制选择 Top1：
低置信度或低 margin 时返回 unclear。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Any

from app.router_core.system_effects import (
    EFFECT_CEILING,
    REQUIRED_NEXT_GATE,
    SYSTEM_EFFECT_TYPES,
    effect_ceiling_for,
    required_gate_for,
)

# 冷启动默认阈值（仅用于首次实验；正式阈值必须来自 validation 搜索）
DEFAULT_THRESHOLDS = {
    "default_min_confidence": 0.65,
    "write_min_confidence": 0.85,
    "oos_min_confidence": 0.70,
    "min_margin": 0.15,
}


@dataclass(frozen=True)
class Thresholds:
    default_min_confidence: float = 0.65
    write_min_confidence: float = 0.85
    oos_min_confidence: float = 0.70
    min_margin: float = 0.15

    def to_dict(self) -> dict[str, float]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> Thresholds:
        data = data or {}
        return cls(
            default_min_confidence=float(data.get("default_min_confidence", 0.65)),
            write_min_confidence=float(data.get("write_min_confidence", 0.85)),
            oos_min_confidence=float(data.get("oos_min_confidence", 0.70)),
            min_margin=float(data.get("min_margin", 0.15)),
        )

    def with_overrides(self, overrides: dict[str, Any] | None) -> Thresholds:
        if not overrides:
            return self
        valid = {k: float(v) for k, v in overrides.items() if k in self.to_dict()}
        return replace(self, **valid)


@dataclass
class PolicyResult:
    intent: str | None             # 业务意图标签（拒识时保留 top-1 候选）
    route: str                     # 兼容字段 = effect_type（可能为 unclear）
    effect_type: str               # 固定系统效果类型（拒识时为 unclear）
    decision: str                  # accept | unclear
    confidence: float              # top1 概率（校准后）
    margin: float                  # top1 - top2
    top_k: list[dict[str, Any]]    # [{label, effect_type, probability}]
    reason_codes: list[str] = field(default_factory=list)
    effect_ceiling: str = "none"
    required_next_gate: str = "none"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _effect_threshold(effect_type: str, thresholds: Thresholds) -> float:
    """阈值按系统效果类型选择（Review 修复 §5.1）：映射为 write_action 的
    任意业务标签都必须用写入专用阈值。"""
    if effect_type == "write_action":
        return thresholds.write_min_confidence
    if effect_type == "oos":
        return thresholds.oos_min_confidence
    return thresholds.default_min_confidence


def _resolve_effect(label: str, effect_type_for: dict[str, str] | None) -> str | None:
    """标签 → 系统效果类型；缺省恒等（五分类），未知返回 None（fail closed）。"""
    effect = (effect_type_for or {}).get(label, label)
    return effect if effect in SYSTEM_EFFECT_TYPES else None


def decide(
    probabilities: dict[str, float],
    thresholds: Thresholds,
    effect_type_for: dict[str, str] | None = None,
) -> PolicyResult:
    """对单个样本的概率分布执行确定性决策门（Review 修复 §5.1 顺序）：

    1. 取 top-1 业务标签；2. 服务端 Schema 映射得 effect type；
    3. 未知映射 fail closed 为 unclear（reason MODEL_SCHEMA_MISMATCH）；
    4. 按 effect type 选择阈值；5. 通过后输出业务意图与系统策略。

    probabilities: label -> 概率（应使用校准后的概率）
    """
    ranked = sorted(probabilities.items(), key=lambda kv: kv[1], reverse=True)
    top1_label, top1_prob = ranked[0]
    top2_prob = ranked[1][1] if len(ranked) > 1 else 0.0
    margin = top1_prob - top2_prob

    top_k = [
        {
            "label": label,
            "effect_type": _resolve_effect(label, effect_type_for),
            "probability": round(prob, 6),
        }
        for label, prob in ranked
    ]

    effect = _resolve_effect(top1_label, effect_type_for)

    reason_codes: list[str] = []
    if effect is None:
        reason_codes.append("MODEL_SCHEMA_MISMATCH")
    else:
        if top1_prob < _effect_threshold(effect, thresholds):
            reason_codes.append("LOW_CONFIDENCE")
        if margin < thresholds.min_margin:
            reason_codes.append("LOW_MARGIN")

    if reason_codes:
        return PolicyResult(
            intent=top1_label,
            route="unclear",
            effect_type="unclear",
            decision="unclear",
            confidence=round(top1_prob, 6),
            margin=round(margin, 6),
            top_k=top_k,
            reason_codes=reason_codes,
            effect_ceiling=EFFECT_CEILING["unclear"],
            required_next_gate=REQUIRED_NEXT_GATE["unclear"],
        )

    prefix = {
        "write_action": "WRITE",
        "oos": "OOS",
    }.get(effect, "DEFAULT")
    reason_codes = [f"{prefix}_THRESHOLD_PASSED", "MARGIN_PASSED"]

    return PolicyResult(
        intent=top1_label,
        route=effect,  # 兼容字段：第一阶段等于 effect_type（§5.2）
        effect_type=effect,
        decision="accept",
        confidence=round(top1_prob, 6),
        margin=round(margin, 6),
        top_k=top_k,
        reason_codes=reason_codes,
        effect_ceiling=effect_ceiling_for(effect),
        required_next_gate=required_gate_for(effect),
    )


def decide_batch(
    prob_matrix,
    labels: list[str],
    thresholds: Thresholds,
    effect_by_label: dict[str, str] | None = None,
) -> list[PolicyResult]:
    """批量决策：prob_matrix 形状 (n, len(labels))，列顺序与 labels 一致。"""
    results = []
    for row in prob_matrix:
        probs = {label: float(p) for label, p in zip(labels, row, strict=True)}
        results.append(decide(probs, thresholds, effect_by_label))
    return results
