"""评测与错误分析（设计文档第 7 节）。

同时输出两类指标：
- 模型原始分类指标（argmax）
- 经过阈值门后的路由指标（policy gate）
"""
from __future__ import annotations

from typing import Any

import numpy as np
from sklearn.metrics import accuracy_score, confusion_matrix, f1_score, precision_recall_fscore_support

from app.router_core.calibration import calibration_metrics, reliability_diagram
from app.router_core.policy import Thresholds
from app.router_core.taxonomy import LABELS
from app.router_core.threshold_search import route_metrics


def classification_metrics(
    y_true: np.ndarray,
    y_pred: np.ndarray,
    labels: list[str] | None = None,
) -> dict[str, Any]:
    """模型原始分类指标（不经过阈值门）。"""
    labels = labels or LABELS
    precision, recall, f1, support = precision_recall_fscore_support(
        y_true, y_pred, labels=list(range(len(labels))), zero_division=0
    )
    per_class = []
    for i, label in enumerate(labels):
        per_class.append(
            {
                "label": label,
                "precision": round(float(precision[i]), 6),
                "recall": round(float(recall[i]), 6),
                "f1": round(float(f1[i]), 6),
                "support": int(support[i]),
            }
        )
    cm = confusion_matrix(y_true, y_pred, labels=list(range(len(labels))))
    return {
        "accuracy": round(float(accuracy_score(y_true, y_pred)), 6) if len(y_true) else None,
        "macro_f1": round(float(f1_score(y_true, y_pred, labels=list(range(len(labels))), average="macro", zero_division=0)), 6) if len(y_true) else None,
        "micro_f1": round(float(f1_score(y_true, y_pred, labels=list(range(len(labels))), average="micro", zero_division=0)), 6) if len(y_true) else None,
        "weighted_f1": round(float(f1_score(y_true, y_pred, labels=list(range(len(labels))), average="weighted", zero_division=0)), 6) if len(y_true) else None,
        "per_class": per_class,
        "confusion_matrix": {
            "labels": labels,
            "matrix": cm.tolist(),
        },
        "support": int(len(y_true)),
    }


def wilson_interval(k: int, n: int, z: float = 1.96) -> tuple[float | None, float | None]:
    """Wilson 置信区间：非写样本不足时，误判率 0 不能解释为真实概率为 0。"""
    if n == 0:
        return None, None
    p = k / n
    denom = 1 + z**2 / n
    center = (p + z**2 / (2 * n)) / denom
    half = z * np.sqrt(p * (1 - p) / n + z**2 / (4 * n**2)) / denom
    return round(max(0.0, center - half), 6), round(min(1.0, center + half), 6)


def evaluate_split(
    y_true: np.ndarray,
    probs_raw: np.ndarray,
    probs_calibrated: np.ndarray,
    thresholds: Thresholds,
    labels: list[str] | None = None,
) -> dict[str, Any]:
    """对单个 split 输出完整指标：原始分类 + 阈值门路由 + 校准。"""
    labels = labels or LABELS
    y_pred = probs_calibrated.argmax(axis=1)  # 校准不改变排序
    raw = classification_metrics(y_true, y_pred, labels)
    routed = route_metrics(probs_calibrated, y_true, thresholds, labels)

    pos = {lab: i for i, lab in enumerate(labels)}
    non_write = y_true != pos["write_action"]
    n_non_write = int(non_write.sum())

    result = {
        "classification": raw,
        "routing": routed,
        "false_write_confidence_interval": {
            "false_write_count": routed["false_write_count"],
            "non_write_support": n_non_write,
            "rate": routed["false_write_rate"],
            "wilson_95": wilson_interval(routed["false_write_count"], n_non_write),
            "note": "非写样本不足 300 时，误判率为 0 只是观测结果，不代表真实概率为 0"
            if n_non_write < 300
            else "",
        },
        "calibration": calibration_metrics(probs_calibrated, y_true),
    }
    return result


def slice_metrics(
    y_true: np.ndarray,
    probs: np.ndarray,
    thresholds: Thresholds,
    slice_flags: np.ndarray,
    labels: list[str] | None = None,
) -> dict[str, Any]:
    """风险切片指标：每个 slice 的样本数、Macro F1、false write、路由指标。"""
    labels = labels or LABELS
    y_pred = probs.argmax(axis=1)
    out = {}
    for name in np.unique(slice_flags):
        mask = slice_flags == name
        if mask.sum() == 0:
            continue
        cm = classification_metrics(y_true[mask], y_pred[mask], labels)
        routed = route_metrics(probs[mask], y_true[mask], thresholds, labels)
        out[str(name)] = {
            "support": int(mask.sum()),
            "macro_f1": cm["macro_f1"],
            "accuracy": cm["accuracy"],
            "false_write_rate": routed["false_write_rate"],
            "false_write_count": routed["false_write_count"],
            "coverage": routed["coverage"],
        }
    return out


def latency_stats(latencies_ms: list[float]) -> dict[str, float | None]:
    arr = np.asarray(latencies_ms, dtype=np.float64)
    if len(arr) == 0:
        return {"p50": None, "p95": None, "p99": None, "mean": None, "n": 0}
    return {
        "p50": round(float(np.percentile(arr, 50)), 3),
        "p95": round(float(np.percentile(arr, 95)), 3),
        "p99": round(float(np.percentile(arr, 99)), 3),
        "mean": round(float(arr.mean()), 3),
        "n": int(len(arr)),
    }


def confidence_margin_distribution(probs: np.ndarray, n_bins: int = 20) -> dict[str, list]:
    """Top1 confidence 与 margin 分布（直方图数据）。"""
    part = -np.partition(-probs, 1, axis=1)
    conf, p2 = part[:, 0], part[:, 1]
    margin = conf - p2
    conf_hist, conf_edges = np.histogram(conf, bins=n_bins, range=(0, 1))
    margin_hist, margin_edges = np.histogram(margin, bins=n_bins, range=(0, 1))
    return {
        "confidence": {
            "edges": [round(float(e), 4) for e in conf_edges],
            "counts": conf_hist.tolist(),
        },
        "margin": {
            "edges": [round(float(e), 4) for e in margin_edges],
            "counts": margin_hist.tolist(),
        },
    }


def reliability_data(probs: np.ndarray, y_true: np.ndarray) -> list[dict]:
    return reliability_diagram(probs, y_true)
