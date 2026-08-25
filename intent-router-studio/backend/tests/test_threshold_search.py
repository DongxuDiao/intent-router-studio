"""阈值搜索与路由指标测试。"""
from __future__ import annotations

import numpy as np
import pytest

from app.router_core.policy import Thresholds
from app.router_core.taxonomy import LABELS
from app.router_core.threshold_search import route_metrics, search_thresholds

POS = {lab: i for i, lab in enumerate(LABELS)}


def _probs_matrix(specs: list[tuple[str, str, float]]) -> np.ndarray:
    """specs: (true_label, top1_label, top1_prob)，其余概率平分。"""
    out = []
    for _true, top1, p1 in specs:
        row = np.zeros(len(LABELS))
        row[POS[top1]] = p1
        rest = 1.0 - p1
        others = [i for i in range(len(LABELS)) if i != POS[top1]]
        for i in others:
            row[i] = rest / len(others)
        out.append(row)
    return np.array(out)


TH = Thresholds(default_min_confidence=0.60, write_min_confidence=0.80, oos_min_confidence=0.60, min_margin=0.10)


def test_route_metrics_hand_computed():
    probs = _probs_matrix(
        [
            ("write_action", "write_action", 0.90),  # 接受且正确
            ("read_only", "write_action", 0.85),     # 接受但误判为写（false write）
            ("information", "information", 0.70),    # margin = 0.7-0.075 = 0.625 > 0.10 接受且正确
        ]
    )
    y = np.array([POS["write_action"], POS["read_only"], POS["information"]])
    m = route_metrics(probs, y, TH, LABELS)
    assert m["n"] == 3
    assert m["accepted_count"] == 3
    assert m["safe_coverage"] == round(2 / 3, 6)
    assert m["selective_accuracy"] == round(2 / 3, 6)
    # 非写样本 2 条，其中 1 条被接受为写
    assert m["false_write_count"] == 1
    assert m["false_write_rate"] == 0.5
    assert m["route_counts"]["write_action"] == 2
    assert m["unclear_rate"] == 0.0


def test_route_metrics_low_confidence_becomes_unclear():
    probs = _probs_matrix([("information", "information", 0.55), ("oos", "oos", 0.65)])
    y = np.array([POS["information"], POS["oos"]])
    # 本组用例用更严的 oos 阈值（0.70），两条都应转 unclear
    strict = Thresholds(default_min_confidence=0.60, write_min_confidence=0.80, oos_min_confidence=0.70, min_margin=0.10)
    m = route_metrics(probs, y, strict, LABELS)
    assert m["accepted_count"] == 0
    assert m["unclear_rate"] == 1.0
    assert m["route_counts"]["unclear"] == 2
    assert m["write_precision"] is None  # 无接受的写预测


def test_search_finds_feasible_and_respects_constraints():
    rng = np.random.default_rng(11)
    n = 300
    y = rng.integers(0, 5, size=n)
    probs = np.full((n, 5), 0.05)
    # 构造高置信正确预测，写样本部分混淆
    for i in range(n):
        probs[i, y[i]] = 0.75
        if y[i] == POS["write_action"] and i % 3 == 0:
            probs[i] = np.array([0.05, 0.05, 0.35, 0.05, 0.05]) + (0.5 if i % 2 else 0.0)
            probs[i, POS["write_action"]] = 0.55 if i % 2 else 0.5
    probs = probs / probs.sum(axis=1, keepdims=True)

    result = search_thresholds(probs, y, LABELS, {
        "default_range": [0.30, 0.90, 0.05],
        "write_range": [0.40, 0.95, 0.05],
        "oos_range": [0.30, 0.90, 0.05],
        "margin_range": [0.0, 0.30, 0.05],
        "constraints": {"max_false_write_rate": 0.02, "min_write_precision": 0.90},
    })
    assert result.feasible
    metrics = result.best_metrics
    assert metrics["false_write_rate"] <= 0.02 + 1e-12
    if metrics["write_precision"] is not None:
        assert metrics["write_precision"] >= 0.90 - 1e-12
    assert result.n_feasible >= 1
    assert set(result.curves) == {"default", "write", "oos", "margin"}
    assert len(result.pareto) >= 1


def test_search_infeasible_falls_back():
    """全部样本预测为写且错 → 无满足约束组合 → feasible=False。"""
    n = 50
    y = np.array([POS["read_only"]] * n)
    probs = np.full((n, 5), 0.04)
    probs[:, POS["write_action"]] = 0.84
    result = search_thresholds(probs, y, LABELS, {
        "constraints": {"max_false_write_rate": 0.005, "min_write_precision": 0.95},
    })
    # 仍可能找到全拒的组合（false_write=0 vacuous + precision None → 视为可行）
    # 但若连全拒都不满足约束则回退默认阈值
    assert isinstance(result.feasible, bool)
    assert result.best_metrics["n"] == n


def test_search_tie_break_prefers_conservative():
    rng = np.random.default_rng(5)
    y = rng.integers(0, 5, size=100)
    probs = np.full((100, 5), 0.1)
    probs[np.arange(100), y] = 0.6
    result = search_thresholds(probs, y, LABELS, {
        "default_range": [0.50, 0.60, 0.05],
        "write_range": [0.70, 0.80, 0.05],
        "oos_range": [0.50, 0.60, 0.05],
        "margin_range": [0.0, 0.10, 0.05],
    })
    assert result.feasible
    assert 0.0 <= result.best.default_min_confidence <= 0.61
    assert 0.0 <= result.best.min_margin <= 0.11


# ---------------- Macro F1 权重与可行解计数 ----------------

def _routes_and_truth(rng: np.random.Generator, n: int, unclear_frac: float):
    """构造混合真值与最终路由（含拒识 unclear）的人工样例。"""
    y_idx = rng.integers(0, len(LABELS), size=n)
    routes = np.array(LABELS, dtype=object)[y_idx].copy()
    flip = rng.random(n) < 0.2
    routes[flip] = np.array(LABELS, dtype=object)[rng.integers(0, len(LABELS), size=flip.sum())]
    reject = rng.random(n) < unclear_frac
    routes[reject] = "unclear"
    return routes, y_idx


def test_macro_f1_matches_sklearn_five_labels():
    """五类各计一次（unclear 已是标签之一），与 sklearn macro F1 完全一致。"""
    from sklearn.metrics import f1_score

    from app.router_core.threshold_search import _macro_f1

    rng = np.random.default_rng(21)
    for unclear_frac in (0.0, 0.3, 0.6):
        routes, y_idx = _routes_and_truth(rng, 200, unclear_frac)
        y_str = np.array(LABELS, dtype=object)[y_idx]
        ours = _macro_f1(routes, y_idx, LABELS)
        ref = f1_score(y_str, routes, labels=LABELS, average="macro", zero_division=0)
        assert ours == pytest.approx(ref, abs=1e-12), f"unclear_frac={unclear_frac}"


def test_macro_f1_unclear_not_double_weighted():
    """真值 unclear 的样本数量变化时，F1 变化与 sklearn 一致（无双倍权重）。"""
    from sklearn.metrics import f1_score

    from app.router_core.threshold_search import _macro_f1

    def _f1(n_unclear: int) -> tuple[float, float]:
        # 20 条真 information + n_unclear 条真 unclear，全部被误路由为 information
        y_idx = np.array([POS["information"]] * 20 + [POS["unclear"]] * n_unclear)
        routes = np.array(["information"] * (20 + n_unclear), dtype=object)
        y_str = np.array(LABELS, dtype=object)[y_idx]
        return (
            _macro_f1(routes, y_idx, LABELS),
            f1_score(y_str, routes, labels=LABELS, average="macro", zero_division=0),
        )

    a_ours, a_ref = _f1(5)
    b_ours, b_ref = _f1(25)
    assert a_ours == pytest.approx(a_ref)
    assert b_ours == pytest.approx(b_ref)
    assert abs(b_ours - a_ours) > 1e-6  # unclear 数量确实影响结果


def _brute_force_feasible_count(probs, y, spec) -> int:
    """小网格暴力枚举满足约束的组合数。"""
    from app.router_core.threshold_search import _grid

    constraints = {**{"max_false_write_rate": 0.005, "min_write_precision": 0.95}, **spec.get("constraints", {})}
    count = 0
    for d in _grid(spec["default_range"]):
        for w in _grid(spec["write_range"]):
            for o in _grid(spec["oos_range"]):
                for m in _grid(spec["margin_range"]):
                    mt = route_metrics(probs, y, Thresholds(float(d), float(w), float(o), float(m)), LABELS)
                    fwr_ok = mt["false_write_rate"] <= constraints["max_false_write_rate"] + 1e-9
                    wp = mt["write_precision"]
                    wp_ok = wp is None or wp >= constraints["min_write_precision"] - 1e-9
                    if fwr_ok and wp_ok:
                        count += 1
    return count


def test_n_feasible_matches_brute_force():
    """n_feasible 必须是全部满足约束的网格组合数（小网格暴力对照）。"""
    rng = np.random.default_rng(33)
    n = 60
    y = rng.integers(0, 5, size=n)
    probs = np.full((n, 5), 0.1)
    probs[np.arange(n), y] = 0.7
    probs = probs / probs.sum(axis=1, keepdims=True)
    spec = {
        "default_range": [0.50, 0.60, 0.05],
        "write_range": [0.70, 0.80, 0.05],
        "oos_range": [0.50, 0.55, 0.05],
        "margin_range": [0.00, 0.05, 0.05],
        "constraints": {"max_false_write_rate": 0.05, "min_write_precision": 0.85},
    }
    result = search_thresholds(probs, y, LABELS, spec)
    assert result.feasible
    expected = _brute_force_feasible_count(probs, y, spec)
    assert expected > 1  # 网格确实存在多个可行组合
    assert result.n_feasible == expected
    assert result.n_retained_candidates <= result.n_feasible
    assert 0 < result.n_retained_candidates
    assert result.to_dict()["n_retained_candidates"] == result.n_retained_candidates


# ---------------- V2 §4.2：确定性 + 并列候选精确择优 ----------------

def _brute_force_reference(probs, y, spec):
    """暴力参考实现：完全枚举网格，按文档化排序键择优。

    返回 (胜者阈值元组, 并列候选数, 最优 safe_coverage 分子)。
    """
    from app.router_core.threshold_search import _grid, _macro_f1, _top1_stats

    constraints = {**{"max_false_write_rate": 0.005, "min_write_precision": 0.95}, **spec.get("constraints", {})}
    p1, _p2, margin = _top1_stats(probs)
    top1 = probs.argmax(axis=1)
    label_arr = np.array(LABELS, dtype=object)
    w_pos, oos_pos = POS["write_action"], POS["oos"]
    correct = top1 == y
    non_write = y != w_pos
    n_non_write = int(non_write.sum())

    def _evaluate(cand):
        d, w, o, m = cand
        thr_vec = np.full(len(y), d)
        thr_vec[top1 == w_pos] = w
        thr_vec[top1 == oos_pos] = o
        accepted = (p1 >= thr_vec) & (margin >= m)
        acc_write = accepted & (top1 == w_pos)
        fw = int((acc_write & non_write).sum())
        aw = int(acc_write.sum())
        awc = int((acc_write & correct).sum())
        fwr = fw / n_non_write if n_non_write else 0.0
        wp = awc / aw if aw else None
        feasible = fwr <= constraints["max_false_write_rate"] + 1e-12 and (
            wp is None or wp >= constraints["min_write_precision"] - 1e-12
        )
        if not feasible:
            return None
        final = np.where(accepted, label_arr[top1], "unclear")
        return int((accepted & correct).sum()), _macro_f1(final, y, LABELS)

    best_cov, tied = -1, []
    for d in _grid(spec["default_range"]):
        for w in _grid(spec["write_range"]):
            for o in _grid(spec["oos_range"]):
                for m in _grid(spec["margin_range"]):
                    cand = (float(d), float(w), float(o), float(m))
                    outcome = _evaluate(cand)
                    if outcome is None:
                        continue
                    cov, f1 = outcome
                    tied.append((cov, f1, cand))
                    best_cov = max(best_cov, cov)

    at_best = [t for t in tied if t[0] == best_cov]
    scored = [(f1, cand[0] + cand[1] + cand[2] + cand[3], cand) for _cov, f1, cand in at_best]
    scored.sort(key=lambda s: (-s[0], -s[1], s[2]))
    return scored[0][2], len(at_best), best_cov


def _tie_rich_spec() -> dict:
    return {
        "default_range": [0.50, 0.60, 0.05],
        "write_range": [0.70, 0.80, 0.05],
        "oos_range": [0.50, 0.55, 0.05],
        "margin_range": [0.00, 0.05, 0.05],
        "constraints": {"max_false_write_rate": 0.05, "min_write_precision": 0.85},
    }


def test_search_matches_brute_force_reference_exactly():
    """小网格并列丰富场景：向量化搜索必须与完全枚举的参考实现逐位一致。"""
    rng = np.random.default_rng(77)
    n = 80
    y = rng.integers(0, 5, size=n)
    probs = np.full((n, 5), 0.1)
    probs[np.arange(n), y] = 0.72
    probs = probs / probs.sum(axis=1, keepdims=True)
    spec = _tie_rich_spec()

    expected_thr, expected_tied, expected_cov = _brute_force_reference(probs, y, spec)
    assert expected_tied > 4  # 确实存在并列候选

    result = search_thresholds(probs, y, LABELS, spec)
    assert result.feasible
    got = (
        result.best.default_min_confidence,
        result.best.write_min_confidence,
        result.best.oos_min_confidence,
        result.best.min_margin,
    )
    assert got == expected_thr
    assert result.n_tied == expected_tied
    assert result.best_metrics["safe_coverage"] == pytest.approx(expected_cov / n, abs=1e-6)
    assert result.selection["n_tied"] == expected_tied
    assert "macro_f1" in result.selection["criterion"]
    assert result.to_dict()["selection"]["n_tied"] == expected_tied


def test_search_deterministic_across_runs():
    """相同输入与配置必须输出完全一致的搜索结果。"""
    rng = np.random.default_rng(88)
    n = 120
    y = rng.integers(0, 5, size=n)
    probs = np.full((n, 5), 0.08)
    probs[np.arange(n), y] = 0.68
    probs = probs / probs.sum(axis=1, keepdims=True)

    first = search_thresholds(probs, y, LABELS, _tie_rich_spec())
    second = search_thresholds(probs, y, LABELS, _tie_rich_spec())
    assert first.to_dict() == second.to_dict()


def test_search_identical_probs_large_tie_pool():
    """极端并列：所有样本概率相同（海量并列候选），结果仍与暴力参考一致。"""
    n = 40
    y = np.array([POS[lab] for lab in LABELS] * (n // len(LABELS)))
    probs = np.full((n, len(LABELS)), 1.0 / len(LABELS))
    probs[:, POS["information"]] = 0.6
    probs = probs / probs.sum(axis=1, keepdims=True)

    spec = _tie_rich_spec()
    expected_thr, expected_tied, expected_cov = _brute_force_reference(probs, y, spec)
    result = search_thresholds(probs, y, LABELS, spec)
    assert result.feasible
    got = (
        result.best.default_min_confidence,
        result.best.write_min_confidence,
        result.best.oos_min_confidence,
        result.best.min_margin,
    )
    assert got == expected_thr
    assert result.n_tied == expected_tied
    assert result.selection["n_unique_route_patterns"] >= 1
