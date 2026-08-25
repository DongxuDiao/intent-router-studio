"""确定性决策门（设计文档 3.3）。

模型给出概率，策略层决定是否接受。禁止强制选择 Top1：
低置信度或低 margin 时返回 unclear。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Any

from app.router_core.taxonomy import EFFECT_CEILING, REQUIRED_NEXT_GATE

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
    route: str                      # 最终路由（可能为 unclear）
    decision: str                   # accept | unclear
    confidence: float               # top1 概率（校准后）
    margin: float                   # top1 - top2
    top_k: list[dict[str, Any]]     # [{label, probability}]
    reason_codes: list[str] = field(default_factory=list)
    effect_ceiling: str = "none"
    required_next_gate: str = "none"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _label_threshold(label: str, thresholds: Thresholds) -> float:
    if label == "write_action":
        return thresholds.write_min_confidence
    if label == "oos":
        return thresholds.oos_min_confidence
    return thresholds.default_min_confidence


def decide(probabilities: dict[str, float], thresholds: Thresholds) -> PolicyResult:
    """对单个样本的概率分布执行确定性决策门。

    probabilities: label -> 概率（应使用校准后的概率）
    """
    ranked = sorted(probabilities.items(), key=lambda kv: kv[1], reverse=True)
    top1_label, top1_prob = ranked[0]
    top2_prob = ranked[1][1] if len(ranked) > 1 else 0.0
    margin = top1_prob - top2_prob

    top_k = [{"label": label, "probability": round(prob, 6)} for label, prob in ranked]

    min_confidence = _label_threshold(top1_label, thresholds)

    reason_codes: list[str] = []
    if top1_prob < min_confidence:
        reason_codes.append("LOW_CONFIDENCE")
    if margin < thresholds.min_margin:
        reason_codes.append("LOW_MARGIN")

    if reason_codes:
        return PolicyResult(
            route="unclear",
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
        "information": "DEFAULT",
        "read_only": "DEFAULT",
        "unclear": "DEFAULT",
    }.get(top1_label, "DEFAULT")
    reason_codes = [f"{prefix}_THRESHOLD_PASSED", "MARGIN_PASSED"]

    return PolicyResult(
        route=top1_label,
        decision="accept",
        confidence=round(top1_prob, 6),
        margin=round(margin, 6),
        top_k=top_k,
        reason_codes=reason_codes,
        effect_ceiling=EFFECT_CEILING[top1_label],
        required_next_gate=REQUIRED_NEXT_GATE[top1_label],
    )


def decide_batch(prob_matrix, labels: list[str], thresholds: Thresholds) -> list[PolicyResult]:
    """批量决策：prob_matrix 形状 (n, len(labels))，列顺序与 labels 一致。"""
    results = []
    for row in prob_matrix:
        probs = {label: float(p) for label, p in zip(labels, row, strict=True)}
        results.append(decide(probs, thresholds))
    return results
