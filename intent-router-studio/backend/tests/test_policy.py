"""Policy Gate 边界值测试（设计文档 16.1 要求覆盖全部边界）。"""
from __future__ import annotations

from app.router_core.policy import DEFAULT_THRESHOLDS, Thresholds, decide
from app.router_core.taxonomy import EFFECT_CEILING, LABELS, REQUIRED_NEXT_GATE, default_label_schema


def _probs(**kwargs) -> dict:
    base = {label: 0.0 for label in LABELS}
    total = sum(kwargs.values())
    if total > 1.0:  # 仅当超出概率空间时归一化，保留边界值语义
        kwargs = {k: v / total for k, v in kwargs.items()}
    base.update(kwargs)
    # 补齐剩余概率到 unclear，保证和为 1
    rest = 1.0 - sum(base.values())
    base["unclear"] += max(rest, 0.0)
    return base


TH = Thresholds(default_min_confidence=0.65, write_min_confidence=0.85, oos_min_confidence=0.70, min_margin=0.15)


def test_accept_information_above_default_threshold():
    probs = _probs(information=0.80, read_only=0.10)
    result = decide(probs, TH)
    assert result.decision == "accept"
    assert result.route == "information"
    assert result.reason_codes == ["DEFAULT_THRESHOLD_PASSED", "MARGIN_PASSED"]
    assert result.effect_ceiling == "none"
    assert result.required_next_gate == "answer_or_kb"


def test_write_uses_higher_threshold():
    probs = _probs(write_action=0.80, read_only=0.10)
    result = decide(probs, TH)
    # 0.80 >= default 0.65 但 < write 0.85 → unclear
    assert result.decision == "unclear"
    assert result.route == "unclear"
    assert "LOW_CONFIDENCE" in result.reason_codes
    assert result.effect_ceiling == "none"


def test_boundary_exactly_at_threshold_accepts():
    probs = _probs(write_action=0.85, read_only=0.20)
    probs["write_action"] = 0.85
    probs["read_only"] = 0.20
    probs["unclear"] = round(1 - 0.85 - 0.20, 10)
    result = decide(probs, TH)
    assert result.decision == "accept"
    assert result.route == "write_action"
    assert result.reason_codes == ["WRITE_THRESHOLD_PASSED", "MARGIN_PASSED"]
    assert result.effect_ceiling == "external_write_candidate"
    assert result.required_next_gate == "skill_match_and_confirmation"


def test_boundary_just_below_threshold_rejects():
    probs = {label: 0.0 for label in LABELS}
    probs["write_action"] = 0.8499
    probs["read_only"] = 0.15
    probs["unclear"] = round(1 - 0.8499 - 0.15, 10)
    result = decide(probs, TH)
    assert result.decision == "unclear"
    assert "LOW_CONFIDENCE" in result.reason_codes


def test_low_margin_returns_unclear():
    probs = {label: 0.0 for label in LABELS}
    probs["information"] = 0.40
    probs["read_only"] = 0.35
    probs["unclear"] = 0.25
    result = decide(probs, TH)
    assert result.decision == "unclear"
    assert result.route == "unclear"
    assert "LOW_MARGIN" in result.reason_codes


def test_margin_boundary_exact():
    probs = {label: 0.0 for label in LABELS}
    probs["information"] = 0.80
    probs["read_only"] = 0.65  # margin 0.15 == min_margin → 通过
    probs["unclear"] = round(1 - 0.80 - 0.65, 10)
    probs["oos"] = 0.0
    result = decide(probs, TH)
    assert result.decision == "accept"


def test_both_codes_when_confidence_and_margin_fail():
    probs = {label: 0.0 for label in LABELS}
    probs["information"] = 0.34
    probs["read_only"] = 0.33
    probs["unclear"] = 0.33
    result = decide(probs, TH)
    assert set(result.reason_codes) == {"LOW_CONFIDENCE", "LOW_MARGIN"}


def test_oos_threshold_separate_from_default():
    th = Thresholds(default_min_confidence=0.65, write_min_confidence=0.85, oos_min_confidence=0.70, min_margin=0.15)
    probs = {label: 0.0 for label in LABELS}
    probs["oos"] = 0.68
    probs["information"] = 0.30
    probs["unclear"] = 0.02
    result = decide(probs, th)
    assert result.decision == "unclear"  # 0.68 < oos 0.70

    probs["oos"] = 0.71
    probs["information"] = 0.28
    result = decide(probs, th)
    assert result.decision == "accept" and result.route == "oos"


def test_topk_sorted_and_margin_computed():
    probs = _probs(write_action=0.60, read_only=0.30)
    result = decide(probs, TH)
    assert result.top_k[0]["label"] in ("write_action", "unclear")
    probs_sorted = [item["probability"] for item in result.top_k]
    assert probs_sorted == sorted(probs_sorted, reverse=True)


def test_effect_ceiling_mapping_complete():
    for label in LABELS:
        assert label in EFFECT_CEILING
        assert label in REQUIRED_NEXT_GATE
    assert EFFECT_CEILING["write_action"] == "external_write_candidate"
    assert EFFECT_CEILING["read_only"] == "read_only"
    assert EFFECT_CEILING["information"] == "none"


def test_thresholds_override_only_valid_keys():
    th = Thresholds.from_dict(DEFAULT_THRESHOLDS)
    over = th.with_overrides({"write_min_confidence": 0.9, "evil_key": 1})
    assert over.write_min_confidence == 0.9
    assert over.default_min_confidence == th.default_min_confidence


def test_nota_reserved_not_trained():
    schema = default_label_schema()
    assert "nota" in schema["reserved_routes"]
    assert "nota" not in LABELS


def test_write_never_grants_execution_authority():
    """write_action 只输出候选资格，不产生执行授权。"""
    probs = _probs(write_action=0.95, read_only=0.03)
    result = decide(probs, TH)
    assert result.route == "write_action"
    assert result.required_next_gate == "skill_match_and_confirmation"
    assert "confirm" in result.required_next_gate
