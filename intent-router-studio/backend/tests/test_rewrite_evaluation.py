"""改写评测指标测试（§14 / §15）。"""
from __future__ import annotations

from app.query_rewrite.evaluation import (
    EvalCase,
    aggregate,
    check_gates,
    evaluate_case,
    load_eval_cases,
    run_eval,
)


def _case(slice="qa_vs_write", required=None, forbidden=None, should_change=True, **kw):  # noqa: A002
    return EvalCase(
        id=kw.get("id", "t1"),
        slice=slice,
        text=kw.get("text", "这个怎么停？"),
        context=kw.get("context"),
        expected_route=kw.get("expected_route", "information"),
        should_change=should_change,
        required_terms=required or [],
        forbidden_terms=forbidden or [],
    )


def _payload(
    original="information",
    rewrite="information",
    changed=True,
    standalone="怎么停？ 实验 123",
    reason_codes=(),
    escalation=False,
    downgrade=False,
    safety_present=True,
    fallback=False,
    route_consistent=True,
    confidence=0.9,
    latency_ms=12.0,
):
    return {
        "mode": "shadow",
        "rewrite": {
            "standalone_query": standalone,
            "changed": changed,
            "confidence": confidence,
            "latency_ms": latency_ms,
        },
        "original_route": {"route": original},
        "rewrite_route": {"route": rewrite},
        "route_consistent": route_consistent,
        "safety": None
        if not safety_present
        else {
            "reason_codes": list(reason_codes),
            "escalation": escalation,
            "downgrade": downgrade,
        },
        "safety_decision": "fallback_original" if fallback else "allow_rewrite_shadow",
        "downstream_query": "这个怎么停？",
        "downstream_query_source": "original",
        "final_route": original,
    }


def test_eval_case_marks_and_gates():
    events = [
        evaluate_case(_case(required=["实验 123"]), _payload()),
        evaluate_case(_case(id="t2", slice="negation"), _payload(changed=False)),
        evaluate_case(
            _case(id="t3", slice="number_and_id_preservation", required=["888"]),
            _payload(standalone="调整实验 888", changed=True),
        ),
    ]
    report = aggregate(events)
    m = report["metrics"]
    assert m["n"] == 3 and m["n_evaluated"] == 2
    assert m["intent_preservation_rate"] == 1.0
    assert m["entity_hallucination_rate"] == 0.0
    assert m["number_id_preservation_rate"] == 1.0
    assert m["false_write_escalation_rate"] == 0.0
    assert m["final_route_always_original"] is True
    gates = check_gates(m)
    assert gates["false_write_escalation_rate"]["pass"] is True


def test_escalation_fails_gate():
    events = [
        evaluate_case(_case(), _payload(original="information", rewrite="write_action", escalation=True, route_consistent=False)),
    ]
    m = aggregate(events)["metrics"]
    assert m["false_write_escalation_rate"] == 1.0
    assert check_gates(m)["all_pass"] is False


def test_required_term_missing_counts_against_recall():
    events = [
        evaluate_case(
            _case(slice="number_and_id_preservation", required=["888"]),
            _payload(standalone="调整实验 999", changed=True),
        ),
    ]
    m = aggregate(events)["metrics"]
    assert m["required_fact_recall"] == 0.0
    assert m["number_id_preservation_rate"] == 0.0


def test_forbidden_term_detected():
    events = [
        evaluate_case(
            _case(forbidden=["帮我停止"]),
            _payload(standalone="帮我停止实验 123"),
        ),
    ]
    m = aggregate(events)["metrics"]
    assert m["forbidden_fact_rate"] == 1.0


def test_fallback_not_evaluated_but_counted():
    events = [
        evaluate_case(_case(), _payload(fallback=True, changed=False, safety_present=False)),
        evaluate_case(_case(id="t2"), _payload()),
    ]
    m = aggregate(events)["metrics"]
    assert m["fallback_rate"] == 0.5
    assert m["n_evaluated"] == 1
    # 降级视为意图保留（原文兜底）
    assert m["intent_preservation_rate"] == 1.0


def test_load_eval_file_covers_twelve_slices():
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "examples" / "rewrite_eval.jsonl"
    cases = load_eval_cases(path)
    slices = {c.slice for c in cases}
    assert len(cases) >= 60
    assert slices == {
        "qa_vs_write", "readonly_vs_write", "negation", "ambiguous_reference",
        "missing_object", "multi_turn_correction", "term_ambiguity",
        "number_and_id_preservation", "oos_near_domain", "no_rewrite_needed",
        "context_injection", "long_context",
    }
    report = run_eval([(c, _payload(changed=c.should_change)) for c in cases])
    assert report["metrics"]["n"] == len(cases)
