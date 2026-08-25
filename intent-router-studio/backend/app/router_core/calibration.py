"""多分类温度校准（设计文档 6.5）。

calibrated_probability = softmax(logits / T)
仅在 validation 集拟合标量 T > 0，目标 NLL 最小。
校准不改变类别排序，但影响接受/拒识阈值。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.optimize import minimize_scalar

EPS = 1e-12
N_BINS = 15


def softmax(logits: np.ndarray, axis: int = -1) -> np.ndarray:
    shifted = logits - logits.max(axis=axis, keepdims=True)
    exp = np.exp(shifted)
    return exp / exp.sum(axis=axis, keepdims=True)


def nll(probs: np.ndarray, labels: np.ndarray) -> float:
    p = np.clip(probs[np.arange(len(labels)), labels], EPS, 1.0)
    return float(-np.mean(np.log(p)))


def ece(probs: np.ndarray, labels: np.ndarray, n_bins: int = N_BINS) -> float:
    """Expected Calibration Error（top-1 置信度分桶）。"""
    conf = probs.max(axis=1)
    pred = probs.argmax(axis=1)
    correct = (pred == labels).astype(float)
    bin_edges = np.linspace(0.0, 1.0, n_bins + 1)
    total = len(labels)
    if total == 0:
        return 0.0
    value = 0.0
    for i in range(n_bins):
        lo, hi = bin_edges[i], bin_edges[i + 1]
        mask = (conf > lo) & (conf <= hi) if i > 0 else (conf >= lo) & (conf <= hi)
        if mask.sum() == 0:
            continue
        value += mask.sum() / total * abs(conf[mask].mean() - correct[mask].mean())
    return float(value)


def brier_score(probs: np.ndarray, labels: np.ndarray) -> float:
    """多分类 Brier Score：mean( sum_k (p_k - y_k)^2 )。"""
    onehot = np.zeros_like(probs)
    onehot[np.arange(len(labels)), labels] = 1.0
    return float(np.mean(np.sum((probs - onehot) ** 2, axis=1)))


def fit_temperature(logits: np.ndarray, labels: np.ndarray) -> float:
    """在 validation 上拟合温度 T（NLL 最小）。

    logits: (n, k) 原始 logits（或 log 概率，softmax 不变性使其等价）。
    数值稳定性：在 log(T) 空间一维优化，T 限定在 [0.01, 100]。
    """
    logits = np.asarray(logits, dtype=np.float64)
    labels = np.asarray(labels, dtype=int)

    def objective(log_t: float) -> float:
        t = float(np.exp(log_t))
        return nll(softmax(logits / t, axis=1), labels)

    result = minimize_scalar(objective, bounds=(np.log(0.01), np.log(100.0)), method="bounded")
    t = float(np.exp(result.x)) if result.success else 1.0
    return max(t, 1e-6)


def calibrate(logits: np.ndarray, temperature: float) -> np.ndarray:
    return softmax(np.asarray(logits, dtype=np.float64) / temperature, axis=1)


@dataclass
class CalibrationReport:
    method: str = "temperature_scaling"
    temperature: float = 1.0
    before: dict = None  # type: ignore[assignment]
    after: dict = None  # type: ignore[assignment]

    def to_dict(self) -> dict:
        return {
            "method": self.method,
            "temperature": round(self.temperature, 6),
            "before": self.before,
            "after": self.after,
        }


def calibration_metrics(probs: np.ndarray, labels: np.ndarray) -> dict:
    return {
        "nll": round(nll(probs, labels), 6),
        "ece": round(ece(probs, labels), 6),
        "brier": round(brier_score(probs, labels), 6),
    }


def reliability_diagram(probs: np.ndarray, labels: np.ndarray, n_bins: int = N_BINS) -> list[dict]:
    """校准曲线数据：每桶的平均置信度 / 实际准确率 / 样本数。"""
    conf = probs.max(axis=1)
    pred = probs.argmax(axis=1)
    correct = (pred == labels).astype(float)
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    diagram = []
    for i in range(n_bins):
        lo, hi = edges[i], edges[i + 1]
        mask = (conf > lo) & (conf <= hi) if i > 0 else (conf >= lo) & (conf <= hi)
        count = int(mask.sum())
        diagram.append(
            {
                "bin": round((lo + hi) / 2, 4),
                "confidence": round(float(conf[mask].mean()), 4) if count else round(lo, 4),
                "accuracy": round(float(correct[mask].mean()), 4) if count else 0.0,
                "count": count,
            }
        )
    return diagram


def fit_and_report(
    val_logits: np.ndarray, val_labels: np.ndarray
) -> tuple[float, CalibrationReport, np.ndarray, list[dict]]:
    """拟合温度并返回 (T, 报告, 校准后概率, 校准后 reliability diagram)。"""
    t = fit_temperature(val_logits, val_labels)
    before_probs = softmax(val_logits, axis=1)
    after_probs = calibrate(val_logits, t)
    report = CalibrationReport(
        temperature=t,
        before=calibration_metrics(before_probs, val_labels),
        after=calibration_metrics(after_probs, val_labels),
    )
    return t, report, after_probs, reliability_diagram(after_probs, val_labels)
