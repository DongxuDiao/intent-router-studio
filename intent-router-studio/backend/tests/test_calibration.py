"""温度校准数值稳定性测试。"""
from __future__ import annotations

import numpy as np

from app.router_core.calibration import (
    brier_score,
    calibrate,
    ece,
    fit_and_report,
    fit_temperature,
    nll,
    softmax,
)


def _onehot_logits(y: np.ndarray, confidence: float = 8.0) -> np.ndarray:
    logits = np.zeros((len(y), 5))
    logits[np.arange(len(y)), y] = confidence
    return logits


def test_softmax_stability_with_extreme_logits():
    logits = np.array([[1000.0, 0.0, 0.0, 0.0, 0.0], [-1000.0, 0.0, 0.0, 0.0, 0.0]])
    probs = softmax(logits, axis=1)
    assert np.all(np.isfinite(probs))
    assert np.allclose(probs.sum(axis=1), 1.0)


def test_temperature_one_identity():
    logits = np.random.default_rng(0).normal(size=(50, 5))
    assert np.allclose(calibrate(logits, 1.0), softmax(logits, axis=1))


def test_fit_temperature_overconfident_model():
    """过度自信的错误样本 → T > 1 且校准后 NLL 下降。"""
    rng = np.random.default_rng(42)
    y = rng.integers(0, 5, size=200)
    logits = _onehot_logits(y, confidence=10.0)
    # 制造 20% 错误：翻转部分样本的 argmax
    flip = rng.random(200) < 0.2
    logits[flip] = np.roll(logits[flip], 1, axis=1)

    t = fit_temperature(logits, y)
    assert t > 1.0
    before = nll(softmax(logits, axis=1), y)
    after = nll(calibrate(logits, t), y)
    assert after < before


def test_perfect_predictions_temperature_near_one():
    y = np.array([0, 1, 2, 3, 4] * 10)
    logits = _onehot_logits(y, confidence=6.0)
    t = fit_temperature(logits, y)
    # 完美预测时 NLL 随 T 增大而恶化 → T 收敛到接近下界或 1 附近，NLL 极小
    assert nll(calibrate(logits, t), y) <= nll(softmax(logits, axis=1), y) + 1e-9


def test_calibration_preserves_ranking():
    """校准不改变类别排序（设计文档 6.5）。"""
    rng = np.random.default_rng(7)
    logits = rng.normal(size=(100, 5)) * 3
    t = fit_temperature(logits, rng.integers(0, 5, size=100))
    raw_rank = softmax(logits, axis=1).argmax(axis=1)
    cal_rank = calibrate(logits, t).argmax(axis=1)
    assert (raw_rank == cal_rank).all()


def test_ece_brier_sane_values():
    y = np.array([0, 1, 2, 3, 4] * 4)
    probs = softmax(_onehot_logits(y, confidence=5.0), axis=1)
    assert 0.0 <= ece(probs, y) <= 1.0
    assert 0.0 <= brier_score(probs, y) <= 2.0
    # 均匀分布的 Brier = 1 - 1/5 = 0.8
    uniform = np.full((len(y), 5), 0.2)
    assert abs(brier_score(uniform, y) - 0.8) < 1e-9


def test_fit_and_report_structure():
    rng = np.random.default_rng(3)
    y = rng.integers(0, 5, size=120)
    logits = _onehot_logits(y, 4.0)
    flip = rng.random(120) < 0.15
    logits[flip] = np.roll(logits[flip], 1, axis=1)
    t, report, probs, diagram = fit_and_report(logits, y)
    assert t > 0
    assert report.after["nll"] <= report.before["nll"]
    assert probs.shape == (120, 5)
    assert len(diagram) == 15 and all("bin" in d and "count" in d for d in diagram)
