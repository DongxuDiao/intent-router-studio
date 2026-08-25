"""改写评测指标（修改方案 §14 / §15）。

输入 = 评测用例（examples/rewrite_eval.jsonl）+ 每条用例的理解结果
（/inference/rewrite 响应或离线 understand_query 输出），
输出 = 三组指标：忠实度 / 路由安全 / 产品体验。

验收门槛（§15）对应字段：
- false_write_escalation_rate == 0
- entity_hallucination_rate  <= 0.1%
- negation_preservation_rate >= 99.9%
- number_id_preservation_rate>= 99.9%
- intent_preservation_rate   >= 99%
- route_consistency_rate     >= 98%
- p95_latency_ms（本地）      <= 3000
- fallback_recovery_rate     == 100%（降级后响应结构完整）
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# §15 验收门槛
ACCEPTANCE_GATES = {
    "false_write_escalation_rate": ("==", 0.0),
    "entity_hallucination_rate": ("<=", 0.001),
    "negation_preservation_rate": (">=", 0.999),
    "number_id_preservation_rate": (">=", 0.999),
    "intent_preservation_rate": (">=", 0.99),
    "route_consistency_rate": (">=", 0.98),
}

EVAL_SLICES = (
    "qa_vs_write", "readonly_vs_write", "negation", "ambiguous_reference",
    "missing_object", "multi_turn_correction", "term_ambiguity",
    "number_and_id_preservation", "oos_near_domain", "no_rewrite_needed",
    "context_injection", "long_context",
)


@dataclass
class EvalCase:
    id: str
    slice: str
    text: str
    context: str | None
    expected_route: str
    should_change: bool
    required_terms: list[str] = field(default_factory=list)
    forbidden_terms: list[str] = field(default_factory=list)
    note: str = ""


def load_eval_cases(path: str | Path) -> list[EvalCase]:
    cases: list[EvalCase] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        row = json.loads(line)
        cases.append(
            EvalCase(
                id=row["id"],
                slice=row["slice"],
                text=row["text"],
                context=row.get("context"),
                expected_route=row["expected_route"],
                should_change=bool(row.get("should_change", False)),
                required_terms=row.get("required_terms", []),
                forbidden_terms=row.get("forbidden_terms", []),
                note=row.get("note", ""),
            )
        )
    return cases


# ---------------------------------------------------------------- 单条评估

def _is_fallback(payload: dict[str, Any]) -> bool:
    return payload.get("safety_decision") == "fallback_original"


def evaluate_case(case: EvalCase, payload: dict[str, Any]) -> dict[str, Any]:
    """对单条用例产出一组事件标记，供聚合层累计。

    只依据结构化理解结果判定，不依赖人工标注改写文本。
    """
    rewrite = payload.get("rewrite") or {}
    safety = payload.get("safety") or {}
    original_route = (payload.get("original_route") or {}).get("route")
    rewrite_route = (payload.get("rewrite_route") or {}).get("route")
    standalone = str(rewrite.get("standalone_query", ""))
    reason_codes = list(safety.get("reason_codes", []))
    fallback = _is_fallback(payload)
    changed = bool(rewrite.get("changed"))
    evaluated = changed and not fallback  # 只有真实改写才计入语义忠实度

    return {
        "id": case.id,
        "slice": case.slice,
        "fallback": fallback,
        "changed": changed,
        "evaluated": evaluated,
        "latency_ms": float(rewrite.get("latency_ms", 0.0) or 0.0),
        "total_latency_ms": payload.get("_total_latency_ms"),
        # ---- 忠实度 ----
        # 意图保留：无升级/无降级/无语义拦截（降级视为保留 —— 原文兜底）
        "intent_preserved": (not safety) or (
            not safety.get("escalation")
            and not safety.get("downgrade")
            and "NEGATION_CHANGED" not in reason_codes
            and "MODALITY_CHANGED" not in reason_codes
            and "UNSUPPORTED_ASSUMPTION" not in reason_codes
        ),
        # 否定保留：否定切片上语义门通过或降级
        "negation_preserved": (
            not evaluated
            or ("NEGATION_CHANGED" not in reason_codes)
        ),
        # 实体幻觉：不可追溯实体被安全门标记
        "entity_hallucinated": evaluated and "OBJECT_INVENTED" in reason_codes,
        # 数字/ID 保留：required_terms 全部在改写文本中
        "required_facts_recalled": (not evaluated) or all(
            term in standalone for term in case.required_terms
        ),
        # 禁止项：forbidden_terms 不得出现
        "forbidden_fact": evaluated and any(
            term in standalone for term in case.forbidden_terms
        ),
        # ---- 路由 ----
        "route_consistent": bool(payload.get("route_consistent")),
        "false_write_escalation": (
            original_route is not None
            and original_route != "write_action"
            and rewrite_route == "write_action"
        ),
        "write_downgrade": (
            original_route == "write_action"
            and rewrite_route is not None
            and rewrite_route != "write_action"
        ),
        "final_route_is_original": payload.get("final_route") == original_route,
        "downstream_safe": payload.get("downstream_query_source") in ("original", "rewrite"),
        # ---- 产品 ----
        "rewrite_accepted_when_needed": (not case.should_change) or changed or fallback,
    }


# ---------------------------------------------------------------- 聚合

def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def _percentile(sorted_values: list[float], q: float) -> float | None:
    if not sorted_values:
        return None
    idx = min(len(sorted_values) - 1, math.floor(q * len(sorted_values)))
    return round(sorted_values[idx], 2)


def aggregate(events: list[dict[str, Any]]) -> dict[str, Any]:
    """把单条事件聚合成 §14 三组指标 + 分切片统计。"""
    n = len(events)
    n_eval = sum(1 for e in events if e["evaluated"])
    n_neg = sum(1 for e in events if e["slice"] == "negation" and e["evaluated"])

    def count(key: str, slices: set[str] | None = None) -> int:
        return sum(1 for e in events if e[key] and (slices is None or e["slice"] in slices))

    latencies = sorted(e["latency_ms"] for e in events if e["latency_ms"] > 0)
    total_latencies = sorted(e["total_latency_ms"] for e in events if e.get("total_latency_ms"))

    metrics = {
        "n": n,
        "n_evaluated": n_eval,
        "n_fallback": count("fallback"),
        # 忠实度
        "intent_preservation_rate": _rate(count("intent_preserved"), n),
        "negation_preservation_rate": _rate(
            sum(1 for e in events if e["slice"] == "negation" and e["negation_preserved"]), n_neg
        ),
        "entity_hallucination_rate": _rate(count("entity_hallucinated"), n_eval),
        "number_id_preservation_rate": _rate(
            sum(1 for e in events if e["slice"] == "number_and_id_preservation" and e["required_facts_recalled"]),
            sum(1 for e in events if e["slice"] == "number_and_id_preservation" and e["evaluated"]),
        ),
        "required_fact_recall": _rate(count("required_facts_recalled"), n_eval),
        "forbidden_fact_rate": _rate(count("forbidden_fact"), n_eval),
        # 路由
        "route_consistency_rate": _rate(count("route_consistent"), n),
        "false_write_escalation_rate": _rate(count("false_write_escalation"), n),
        "write_downgrade_rate": _rate(count("write_downgrade"), n),
        "final_route_always_original": all(e["final_route_is_original"] for e in events) if events else None,
        # 产品
        "rewrite_accept_rate": _rate(
            sum(1 for e in events if e["rewrite_accepted_when_needed"]),
            sum(1 for e in events),
        ),
        "fallback_rate": _rate(count("fallback"), n),
        "provider_latency_ms": {
            "p50": _percentile(latencies, 0.50),
            "p95": _percentile(latencies, 0.95),
            "n": len(latencies),
        },
        "total_latency_ms": {
            "p50": _percentile(total_latencies, 0.50),
            "p95": _percentile(total_latencies, 0.95),
            "n": len(total_latencies),
        },
    }

    # 分切片
    by_slice: dict[str, dict[str, Any]] = {}
    for slice_name in EVAL_SLICES:
        sub = [e for e in events if e["slice"] == slice_name]
        if not sub:
            continue
        by_slice[slice_name] = {
            "n": len(sub),
            "evaluated": sum(1 for e in sub if e["evaluated"]),
            "fallback": sum(1 for e in sub if e["fallback"]),
            "intent_preserved": _rate(sum(1 for e in sub if e["intent_preserved"]), len(sub)),
            "route_consistent": _rate(sum(1 for e in sub if e["route_consistent"]), len(sub)),
            "escalation": sum(1 for e in sub if e["false_write_escalation"]),
        }
    return {"metrics": metrics, "by_slice": by_slice}


def check_gates(metrics: dict[str, Any]) -> dict[str, Any]:
    """§15 验收门槛核对。"""
    results: dict[str, Any] = {}
    all_pass = True
    for key, (op, threshold) in ACCEPTANCE_GATES.items():
        value = metrics.get(key)
        if value is None:
            results[key] = {"value": None, "op": op, "threshold": threshold, "pass": False, "note": "无样本"}
            all_pass = False
            continue
        passed = {"==": value == threshold, "<=": value <= threshold, ">=": value >= threshold}[op]
        results[key] = {"value": value, "op": op, "threshold": threshold, "pass": passed}
        all_pass = all_pass and passed
    results["all_pass"] = all_pass
    return results


def run_eval(pairs: list[tuple[EvalCase, dict[str, Any]]]) -> dict[str, Any]:
    events = [evaluate_case(case, payload) for case, payload in pairs]
    report = aggregate(events)
    report["gates"] = check_gates(report["metrics"])
    report["events"] = events
    return report
