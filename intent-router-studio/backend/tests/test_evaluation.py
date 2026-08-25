"""评测指标测试。"""
from __future__ import annotations

import numpy as np

from app.router_core.evaluation import (
    classification_metrics,
    latency_stats,
    slice_metrics,
    wilson_interval,
)
from app.router_core.policy import Thresholds
from app.router_core.taxonomy import LABELS


def test_classification_metrics_basic():
    y = np.array([0, 1, 2, 3, 4, 0])
    pred = np.array([0, 1, 2, 3, 0, 1])
    m = classification_metrics(y, pred, LABELS)
    assert m["support"] == 6
    assert m["accuracy"] == round(4 / 6, 6)
    assert 0 <= m["macro_f1"] <= 1
    assert len(m["per_class"]) == 5
    assert m["confusion_matrix"]["labels"] == LABELS
    assert len(m["confusion_matrix"]["matrix"]) == 5
    # write_action (idx 2) 全对
    write = next(p for p in m["per_class"] if p["label"] == "write_action")
    assert write["f1"] == 1.0 and write["support"] == 1


def test_wilson_interval_zero_count_small_sample():
    lo, hi = wilson_interval(0, 50)
    assert lo == 0.0
    assert hi > 0.0  # 观测为 0 不能断言真实概率为 0
    lo2, hi2 = wilson_interval(0, 300)
    assert hi2 < hi  # 样本更多区间更窄


def test_slice_metrics_partitions():
    rng = np.random.default_rng(2)
    n = 40
    y = rng.integers(0, 5, size=n)
    probs = np.full((n, 5), 0.15)
    probs[np.arange(n), y] = 0.4
    flags = np.array(["negation"] * 10 + ["none"] * 30)
    result = slice_metrics(y, probs, Thresholds(), flags, LABELS)
    assert set(result.keys()) == {"negation", "none"}
    assert result["negation"]["support"] == 10
    assert result["none"]["support"] == 30


def test_latency_stats():
    stats = latency_stats([10, 20, 30, 40, 50, 60, 70, 80, 90, 100])
    assert stats["n"] == 10
    assert stats["p50"] == 55.0
    assert stats["p95"] >= stats["p50"]
    assert latency_stats([])["p50"] is None
