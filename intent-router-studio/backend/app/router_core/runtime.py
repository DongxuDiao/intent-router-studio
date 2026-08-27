"""推理运行时（设计文档第 13 节）。

- 每个项目一把读写锁保护模型引用
- 激活新模型先在临时对象完成加载与 smoke inference，再原子替换
- 进程内 LRU 缓存，key = sha256(model_version + threshold_version + 规范化文本)
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

import numpy as np

from app.router_core.calibration import calibrate
from app.router_core.normalization import encode_input, normalize_text
from app.router_core.policy import Thresholds, decide
from app.router_core.taxonomy import LABELS


class ModelRuntime:
    """单个模型版本的加载态：模型 + 校准温度 + 阈值。"""

    def __init__(
        self,
        model: Any,
        labels: list[str],
        temperature: float,
        thresholds: Thresholds,
        model_version_id: str,
        threshold_version_id: str | None = None,
    ) -> None:
        self.model = model
        self.labels = list(labels)
        self.temperature = float(temperature)
        self.thresholds = thresholds
        self.model_version_id = model_version_id
        self.threshold_version_id = threshold_version_id

    @classmethod
    def load(cls, artifact_dir: str | Path, model_version_id: str, verify: bool = True) -> ModelRuntime:
        """从制品目录加载；verify=True 时校验 manifest 覆盖的全部文件哈希。

        校验委托 artifact_service.verify_manifest，同时覆盖 artifact_hashes（普通制品文件）
        与 artifact_hashes_model（setfit_model/ 权重）——所有业务加载入口
        （激活、重启后 ACTIVE 懒加载、Playground、A-B、回滚）必须保持 verify=True；
        仅离线诊断允许 verify=False。
        """
        artifact_dir = Path(artifact_dir)
        from app.services import artifact_service  # 延迟导入，避免循环依赖

        if verify:
            manifest = artifact_service.verify_manifest(artifact_dir)
        else:
            manifest_path = artifact_dir / "manifest.json"
            if not manifest_path.is_file():
                raise FileNotFoundError(f"制品缺少 manifest.json: {artifact_dir}")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        label_path = artifact_dir / "label_schema.json"
        labels = LABELS
        if label_path.is_file():
            schema = json.loads(label_path.read_text(encoding="utf-8"))
            loaded = [item["key"] if isinstance(item, dict) else str(item) for item in schema.get("labels", [])]
            if loaded:
                labels = loaded

        temperature = 1.0
        calib_path = artifact_dir / "calibration.json"
        if calib_path.is_file():
            calib = json.loads(calib_path.read_text(encoding="utf-8"))
            temperature = float(calib.get("temperature", 1.0))

        thresholds = Thresholds()
        thr_path = artifact_dir / "thresholds.json"
        if thr_path.is_file():
            thresholds = Thresholds.from_dict(json.loads(thr_path.read_text(encoding="utf-8")))

        from setfit import SetFitModel

        # setfit 1.x 从本地目录加载用 from_pretrained（load_pretrained 已不存在）
        model = SetFitModel.from_pretrained(str(artifact_dir / "setfit_model"))

        runtime = cls(
            model=model,
            labels=labels,
            temperature=temperature,
            thresholds=thresholds,
            model_version_id=model_version_id,
            threshold_version_id=manifest.get("threshold_version_id"),
        )
        runtime.smoke_check()
        return runtime

    def smoke_check(self) -> None:
        """加载后 smoke inference，确保模型可服务。"""
        probs = self.model.predict_proba(["你好", "帮我创建一个实验"])
        arr = np.asarray(probs, dtype=np.float64)
        if arr.ndim != 2 or arr.shape[0] != 2 or arr.shape[1] != len(self.labels):
            raise ValueError(f"smoke inference 输出形状异常: {arr.shape} vs labels={len(self.labels)}")

    def _raw_probs(self, texts: list[str]) -> np.ndarray:
        probs = self.model.predict_proba(list(texts))
        return np.asarray(probs, dtype=np.float64)

    def calibrated_probs(self, texts: list[str]) -> np.ndarray:
        raw = self._raw_probs(texts)
        logits = np.log(np.clip(raw, 1e-12, 1.0))
        return calibrate(logits, self.temperature)

    def predict(
        self,
        text: str,
        context: str | None = None,
        threshold_overrides: dict | None = None,
    ) -> dict[str, Any]:
        start = time.perf_counter()
        model_input = encode_input(text, context)
        thresholds = self.thresholds.with_overrides(threshold_overrides)
        probs = self.calibrated_probs([model_input])[0]
        prob_map = {label: float(p) for label, p in zip(self.labels, probs, strict=True)}
        result = decide(prob_map, thresholds).to_dict()
        latency_ms = round((time.perf_counter() - start) * 1000, 2)
        result["model_version_id"] = self.model_version_id
        result["latency_ms"] = latency_ms
        return result


class _LRUCache:
    def __init__(self, capacity: int = 10_000) -> None:
        self.capacity = capacity
        self._data: OrderedDict[str, dict] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> dict | None:
        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
                return self._data[key]
            return None

    def put(self, key: str, value: dict) -> None:
        with self._lock:
            self._data[key] = value
            self._data.move_to_end(key)
            while len(self._data) > self.capacity:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)


class InferenceRuntime:
    """项目级模型注册表 + 显式版本 LRU + 预测缓存。"""

    MAX_EXPLICIT_VERSIONS = 4

    def __init__(self, cache_capacity: int = 10_000) -> None:
        self._runtimes: dict[str, ModelRuntime] = {}
        self._versions: OrderedDict[str, ModelRuntime] = OrderedDict()
        self._locks: dict[str, threading.RLock] = {}
        self._guard = threading.Lock()
        self.cache = _LRUCache(cache_capacity)

    def _lock_for(self, project_id: str) -> threading.RLock:
        with self._guard:
            if project_id not in self._locks:
                self._locks[project_id] = threading.RLock()
            return self._locks[project_id]

    def get(self, project_id: str) -> ModelRuntime | None:
        with self._guard:
            return self._runtimes.get(project_id)

    def set(self, project_id: str, runtime: ModelRuntime) -> None:
        """原子替换引用；加载失败时调用方不会走到这里，旧模型继续服务。"""
        with self._guard:
            self._runtimes[project_id] = runtime
        self.cache.clear()

    def evict(self, project_id: str) -> None:
        with self._guard:
            self._runtimes.pop(project_id, None)
        self.cache.clear()

    def evict_project(self, project_id: str, model_version_ids: list[str] | None = None) -> None:
        """删除项目时同时驱逐默认运行时、显式版本 LRU 和预测缓存。"""
        with self._guard:
            self._runtimes.pop(project_id, None)
            for model_id in model_version_ids or []:
                self._versions.pop(model_id, None)
            self._locks.pop(project_id, None)
        self.cache.clear()

    # ---- 显式指定版本（Playground / A-B 对比），小容量 LRU ----
    def get_version(self, model_version_id: str) -> ModelRuntime | None:
        with self._guard:
            rt = self._versions.get(model_version_id)
            if rt is not None:
                self._versions.move_to_end(model_version_id)
            return rt

    def set_version(self, model_version_id: str, runtime: ModelRuntime) -> None:
        with self._guard:
            self._versions[model_version_id] = runtime
            self._versions.move_to_end(model_version_id)
            while len(self._versions) > self.MAX_EXPLICIT_VERSIONS:
                self._versions.popitem(last=False)
        self.cache.clear()

    def predict_with(
        self,
        runtime: ModelRuntime,
        text: str,
        context: str | None = None,
        threshold_overrides: dict | None = None,
        debug: bool = False,
    ) -> dict[str, Any]:
        # 缓存只存基础路由结果；debug / cache_hit / latency_ms 均为请求级字段，不入缓存。
        # threshold_overrides 请求完全绕过共享缓存（不读也不写）。
        cache_key = hashlib.sha256(
            (
                runtime.model_version_id
                + "|"
                + str(runtime.threshold_version_id)
                + "|"
                + normalize_text(text)
                + "|"
                + normalize_text(context or "")
            ).encode("utf-8")
        ).hexdigest()
        if threshold_overrides is None:
            cached = self.cache.get(cache_key)
            if cached is not None:
                return self._finalize(dict(cached), runtime, threshold_overrides, debug, cache_hit=True)
        result = runtime.predict(text, context, threshold_overrides)
        result["model_version"] = f"intent-router-{runtime.model_version_id[-8:]}"
        if threshold_overrides is None:
            self.cache.put(cache_key, {k: v for k, v in result.items() if k != "latency_ms"})
        return self._finalize(result, runtime, threshold_overrides, debug, cache_hit=False)

    @staticmethod
    def _finalize(
        result: dict,
        runtime: ModelRuntime,
        threshold_overrides: dict | None,
        debug: bool,
        cache_hit: bool,
    ) -> dict:
        """请求级输出组装：cache_hit 标记与 debug 详情按本次请求附加。"""
        if cache_hit:
            result["cache_hit"] = True
        if debug:
            result["debug"] = {
                "thresholds_applied": runtime.thresholds.with_overrides(threshold_overrides).to_dict(),
                "temperature": runtime.temperature,
                "label_order": runtime.labels,
                "threshold_version_id": runtime.threshold_version_id,
            }
        return result

    def predict(
        self,
        project_id: str,
        text: str,
        context: str | None = None,
        threshold_overrides: dict | None = None,
        debug: bool = False,
    ) -> dict[str, Any]:
        runtime = self.get(project_id)
        if runtime is None:
            raise LookupError("MODEL_NOT_ACTIVE")
        return self.predict_with(runtime, text, context, threshold_overrides, debug)

    def predict_batch(
        self,
        project_id: str,
        texts: list[str],
        contexts: list[str | None] | None = None,
        threshold_overrides: dict | None = None,
    ) -> list[dict[str, Any]]:
        runtime = self.get(project_id)
        if runtime is None:
            raise LookupError("MODEL_NOT_ACTIVE")
        contexts = contexts or [None] * len(texts)
        return [runtime.predict(t, c, threshold_overrides) for t, c in zip(texts, contexts, strict=True)]
