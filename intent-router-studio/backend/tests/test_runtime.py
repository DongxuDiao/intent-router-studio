"""推理运行时测试：制品完整性校验（含模型权重）与 Debug/普通缓存隔离。"""
from __future__ import annotations

import numpy as np
import pytest

from app.errors import ApiError
from app.router_core.policy import Thresholds
from app.router_core.runtime import InferenceRuntime, ModelRuntime
from app.router_core.taxonomy import LABELS
from app.services import artifact_service


class _StubSetFitModel:
    """替身 SetFitModel：predict_proba 输出固定分布，避免加载真实权重。"""

    def predict_proba(self, texts: list[str]) -> np.ndarray:
        out = []
        for t in texts:
            row = np.full(len(LABELS), 0.05)
            row[LABELS.index("read_only" if "查" in t else "write_action")] = 0.75
            out.append(row)
        return np.array(out)


def _build_artifact(tmp_path, monkeypatch) -> None:
    """构造最小合法制品目录：setfit_model/ 权重 + 配套文件 + manifest（全部被哈希覆盖）。"""
    monkeypatch.setattr("setfit.SetFitModel.from_pretrained", lambda path: _StubSetFitModel())
    model_dir = tmp_path / "setfit_model"
    model_dir.mkdir(parents=True)
    (model_dir / "model.safetensors").write_bytes(b"legit-weights-aaaa")
    (model_dir / "config.json").write_text('{"model_type": "stub"}', encoding="utf-8")
    artifact_service.write_json(tmp_path / "thresholds.json", Thresholds().to_dict())
    # Phase 2 §6.7：制品必须携带标签顺序（缺失 fail closed），manifest 覆盖其哈希
    # Review 修复 §7.3：v2 制品必须完整提供 label_definitions（恒等五分类）
    artifact_service.write_json(
        tmp_path / "label_schema.json",
        {"schema_format": "intent-schema-v2", "schema_id": None, "schema_hash": None,
         "labels": list(LABELS),
         "label_definitions": [{"key": k, "effect_type": k} for k in LABELS]},
    )
    artifact_service.build_manifest(tmp_path, {"run_id": "run_test"})


def test_load_verifies_model_weights(tmp_path, monkeypatch):
    """显式版本加载必须校验 setfit_model/ 下的权重哈希（artifact_hashes_model）。"""
    _build_artifact(tmp_path, monkeypatch)
    (tmp_path / "setfit_model" / "model.safetensors").write_bytes(b"tampered-weights")
    with pytest.raises(ApiError) as exc:
        ModelRuntime.load(tmp_path, "mdl_x", verify=True)
    assert exc.value.code == "HASH_MISMATCH"
    assert any("setfit_model/model.safetensors" in p for p in exc.value.details.get("problems", []))


def test_load_verifies_regular_artifacts(tmp_path, monkeypatch):
    """普通制品文件（thresholds.json）被篡改时同样拒绝，且错误结构统一。"""
    _build_artifact(tmp_path, monkeypatch)
    (tmp_path / "thresholds.json").write_text('{"default_min_confidence": 0.1}', encoding="utf-8")
    with pytest.raises(ApiError) as exc:
        ModelRuntime.load(tmp_path, "mdl_x", verify=True)
    assert exc.value.code == "HASH_MISMATCH"


def test_load_rejects_missing_model_file(tmp_path, monkeypatch):
    _build_artifact(tmp_path, monkeypatch)
    (tmp_path / "setfit_model" / "model.safetensors").unlink()
    with pytest.raises(ApiError) as exc:
        ModelRuntime.load(tmp_path, "mdl_x", verify=True)
    assert exc.value.code in ("HASH_MISMATCH", "ARTIFACT_INCOMPLETE")
    assert any("model.safetensors" in p for p in exc.value.details.get("problems", []))


def test_load_clean_artifact_passes_smoke(tmp_path, monkeypatch):
    _build_artifact(tmp_path, monkeypatch)
    runtime = ModelRuntime.load(tmp_path, "mdl_x", verify=True)
    assert runtime.labels == LABELS
    result = runtime.predict("查一下状态")
    assert result["route"] in LABELS


# ---------------- Debug / 普通推理缓存隔离 ----------------

def _runtime(model_id: str = "mdl_a") -> ModelRuntime:
    return ModelRuntime(
        model=_StubSetFitModel(),
        labels=list(LABELS),
        temperature=1.0,
        thresholds=Thresholds(),
        model_version_id=model_id,
    )


def test_plain_then_debug_gets_debug_on_cache_hit():
    ir = InferenceRuntime()
    rt = _runtime()
    first = ir.predict_with(rt, "查一下实验状态")
    assert "cache_hit" not in first and "debug" not in first
    second = ir.predict_with(rt, "查一下实验状态", None, None, debug=True)
    assert second.get("cache_hit") is True
    assert "debug" in second
    assert "thresholds_applied" in second["debug"]


def test_debug_then_plain_hides_debug_on_cache_hit():
    ir = InferenceRuntime()
    rt = _runtime()
    first = ir.predict_with(rt, "帮我删除实验", None, None, debug=True)
    assert "debug" in first
    second = ir.predict_with(rt, "帮我删除实验")
    assert second.get("cache_hit") is True
    assert "debug" not in second


def test_plain_then_plain_and_debug_then_debug():
    ir = InferenceRuntime()
    rt = _runtime()
    assert ir.predict_with(rt, "查询 A").get("cache_hit") is not True
    assert ir.predict_with(rt, "查询 A").get("cache_hit") is True
    ir.predict_with(rt, "帮我改 B", None, None, debug=True)
    again = ir.predict_with(rt, "帮我改 B", None, None, debug=True)
    assert again.get("cache_hit") is True
    assert "debug" in again


def test_threshold_overrides_not_shared_via_cache():
    ir = InferenceRuntime()
    rt = _runtime()
    with_override = ir.predict_with(rt, "帮我删除 C", None, {"write_min_confidence": 0.99})
    plain = ir.predict_with(rt, "帮我删除 C")
    assert "cache_hit" not in with_override  # 带覆盖不读缓存
    assert "cache_hit" not in plain  # 也不写缓存


def test_model_activation_clears_prediction_cache():
    ir = InferenceRuntime()
    ir.predict_with(_runtime("m1"), "查一下 D")
    ir.set("prj_1", _runtime("m2"))  # 激活新版本 → 清空预测缓存
    again = ir.predict_with(_runtime("m2"), "查一下 D")
    assert "cache_hit" not in again
