"""Effect-aware Policy 与评估测试（Review 修复 §5/§6，§10.1-1/2/3）。

覆盖：
- 自定义写意图使用 write_min_confidence（0.80 默认阈值通过但写阈值拒绝）；
- 多个业务标签映射同一 write_action 时 false-write 按效果组聚合；
- 无 write_action/oos 业务标签名的 Schema 阈值搜索不崩；
- 未知标签 fail closed（MODEL_SCHEMA_MISMATCH）；
- route=effect_type、intent=业务标签的两层契约；
- intent/effect 两层指标（safe_coverage 与 effect_safe_coverage 口径区分）。
"""
from __future__ import annotations

import numpy as np
import pytest

from app.router_core.evaluation import evaluate_split
from app.router_core.policy import Thresholds, decide
from app.router_core.threshold_search import route_metrics, search_thresholds

TH = Thresholds(default_min_confidence=0.65, write_min_confidence=0.85, oos_min_confidence=0.70, min_margin=0.15)

TWO_CLASS_EFFECTS = {"faq": "information", "create_task": "write_action"}


# ---------------------------------------------------------------- Policy（§5）

def test_custom_write_intent_uses_write_threshold():
    """create_task→write_action：confidence=0.80、default=0.65、write=0.85 → 拒绝。"""
    probs = {"faq": 0.20, "create_task": 0.80}
    result = decide(probs, TH, TWO_CLASS_EFFECTS)
    assert result.decision == "unclear"
    assert result.route == "unclear" and result.effect_type == "unclear"
    assert "LOW_CONFIDENCE" in result.reason_codes
    assert result.intent == "create_task"  # 拒识仍保留 top-1 候选

    high = {"faq": 0.10, "create_task": 0.90}
    accepted = decide(high, TH, TWO_CLASS_EFFECTS)
    assert accepted.decision == "accept"
    assert accepted.intent == "create_task"
    assert accepted.route == "write_action" and accepted.effect_type == "write_action"
    assert accepted.effect_ceiling == "external_write_candidate"
    assert accepted.required_next_gate == "skill_match_and_confirmation"


def test_route_is_effect_intent_is_label():
    """两层契约：route/effect_type 为系统效果，intent 为业务标签；top_k 带 effect。"""
    probs = {"faq": 0.10, "status_query": 0.90}
    result = decide(probs, TH, {"faq": "information", "status_query": "read_only"})
    assert result.decision == "accept"
    assert result.intent == "status_query"
    assert result.route == "read_only" and result.effect_type == "read_only"
    top = result.top_k[0]
    assert top["label"] == "status_query" and top["effect_type"] == "read_only"
    assert result.top_k[1]["effect_type"] == "information"


def test_unknown_label_fails_closed():
    """未知映射：unclear + MODEL_SCHEMA_MISMATCH，即使置信度很高也不得 accept。"""
    probs = {"mystery_intent": 0.99, "faq": 0.01}
    result = decide(probs, TH, TWO_CLASS_EFFECTS)
    assert result.decision == "unclear"
    assert "MODEL_SCHEMA_MISMATCH" in result.reason_codes
    assert result.effect_ceiling == "none"
    assert result.required_next_gate == "clarification"


# ---------------------------------------------------------------- route_metrics / 搜索（§6）

def test_multiple_write_labels_aggregate_false_write():
    """w1/w2 都映射 write_action：两个列上的误写都要计入 false write。"""
    labels = ["a_read", "w1", "w2"]
    effects = {"a_read": "read_only", "w1": "write_action", "w2": "write_action"}
    # 行 0/1：真值 a_read，模型高置信预测 w1/w2（写入专用阈值 0.85 以上会被接受 → 误写）
    # 行 2：真值 w1，预测 w1 正确接受；行 3：真值 a_read 预测 a_read 正确
    probs = np.array(
        [
            [0.02, 0.90, 0.08],
            [0.02, 0.08, 0.90],
            [0.02, 0.90, 0.08],
            [0.90, 0.06, 0.04],
        ]
    )
    y = np.array([0, 0, 1, 0])
    m = route_metrics(probs, y, TH, labels, effects)
    assert m["false_write_count"] == 2  # w1 与 w2 各一次，聚合在效果组
    assert m["false_write_rate"] == pytest.approx(2 / 3, abs=1e-6)
    assert m["route_counts"]["write_action"] == 3
    # 漏写：真值写但预测效果非写（无此样本）
    assert m["missed_write_count"] == 0


def test_threshold_search_with_custom_labels_and_no_oos():
    """三分类（无 write_action/oos 标签名、无 OOS 训练标签）搜索可完成。"""
    labels = ["faq", "status_query", "create_task"]
    effects = {"faq": "information", "status_query": "read_only", "create_task": "write_action"}
    rng = np.random.default_rng(7)
    n = 90
    y = np.arange(n) % 3
    probs = rng.dirichlet([0.3, 0.3, 0.3], size=n)
    # 给真值列拉高置信度，保证有可行阈值
    probs[np.arange(n), y] += 0.6
    probs /= probs.sum(axis=1, keepdims=True)

    result = search_thresholds(probs, y, label_list=labels, effect_by_label=effects)
    assert result.feasible is True
    best_metrics = result.best_metrics
    assert best_metrics["false_write_rate"] <= 0.005 + 1e-9
    # route_counts 键是效果类型（含 unclear），不是业务标签
    assert set(best_metrics["route_counts"]) <= {"information", "read_only", "write_action", "unclear"}


def test_two_layer_metrics_distinguish_intent_and_effect():
    """预测错业务标签但效果正确：safe_coverage 与 effect_safe_coverage 分口径。"""
    labels = ["faq", "doc_qa", "create_task"]
    effects = {"faq": "information", "doc_qa": "information", "create_task": "write_action"}
    # 行 0：真 faq 预测 doc_qa——意图错、效果对；行 1：真 doc_qa 预测 doc_qa 全对
    probs = np.array(
        [
            [0.10, 0.85, 0.05],
            [0.05, 0.90, 0.05],
        ]
    )
    y = np.array([0, 1])
    eval = evaluate_split(y, probs.copy(), probs.copy(), TH, labels, effects)
    routing = eval["routing"]
    assert routing["safe_coverage"] == pytest.approx(0.5, abs=1e-6)      # 仅行 1 意图正确
    assert routing["effect_safe_coverage"] == pytest.approx(1.0, abs=1e-6)  # 两行效果都对
    # 效果层分类：information 内部混淆不影响 effect accuracy
    assert eval["effects"]["classification"]["accuracy"] == pytest.approx(1.0, abs=1e-6)
    assert eval["classification"]["accuracy"] == pytest.approx(0.5, abs=1e-6)
    assert eval["effects"]["missed_write_count"] == 0
