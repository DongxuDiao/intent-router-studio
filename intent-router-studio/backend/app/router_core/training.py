"""SetFit 训练封装（设计文档 6.4）。

- 加载冻结数据与 Label Schema → 设置全部随机种子 → 加载基础模型
  （默认禁止 trust_remote_code）→ SetFit 微调 → 输出验证/测试概率
- 不平衡数据在进入 Trainer 前做过采样（等价于 sampling_strategy=oversampling，
  且不依赖 SetFit 版本参数）
"""
from __future__ import annotations

import logging
import math
import os
import random
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

# 参数范围（与前端表单一致，用于 API 侧校验）
PARAM_RANGES = {
    "batch_size": (4, 64),
    "num_epochs": (1, 20),
    "body_learning_rate": (1e-6, 1e-4),
    "max_length": (64, 512),
    "seed": (0, 2**31 - 1),
    "num_iterations": (1, 50),
    "max_embedding_pairs": (500, 20_000),
}

PRESETS = {
    "quick": {"num_epochs": 1, "num_iterations": 3, "batch_size": 4, "max_embedding_pairs": 2_000},
    "standard": {"num_epochs": 2, "num_iterations": 5, "batch_size": 8, "max_embedding_pairs": 4_000},
    "strict": {"num_epochs": 5, "num_iterations": 10, "batch_size": 8, "max_embedding_pairs": 8_000},
}


@dataclass
class TrainConfig:
    base_model_id: str = "BAAI/bge-small-zh-v1.5"
    seed: int = 42
    device: str = "auto"
    max_length: int = 128
    batch_size: int = 8
    num_epochs: int = 2
    body_learning_rate: float = 2e-5
    sampling_strategy: str = "oversampling"
    num_iterations: int = 5
    max_embedding_pairs: int = 4_000
    fine_tune_embeddings: bool = False
    normalize_embeddings: bool = True
    max_text_chars: int = 4000

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict | None) -> TrainConfig:
        data = dict(data or {})
        allowed = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        cfg = cls(**allowed)
        errors = validate_config(cfg)
        if errors:
            raise ValueError(f"训练参数超出范围: {errors}")
        return cfg


def validate_config(cfg: TrainConfig) -> dict[str, str]:
    errors = {}
    for key, (lo, hi) in PARAM_RANGES.items():
        value = getattr(cfg, key, None)
        if value is None or not (lo <= value <= hi):
            errors[key] = f"{value} 不在允许范围 [{lo}, {hi}]"
    if cfg.device not in ("auto", "cpu", "cuda", "mps"):
        errors["device"] = f"不支持的设备 {cfg.device}"
    return errors


def requested_training_pairs(sample_count: int, config: TrainConfig) -> int:
    """SetFit 默认会为每轮生成正负两组配对。"""
    return max(0, int(sample_count)) * max(1, int(config.num_iterations)) * 2


def build_resource_plan(sample_count: int, config: TrainConfig) -> dict[str, int | str | bool]:
    """构造有界嵌入训练计划，超出的配对不会在内存中实例化。"""
    requested_pairs = requested_training_pairs(sample_count, config)
    effective_pairs = min(requested_pairs, config.max_embedding_pairs) if config.fine_tune_embeddings else 0
    classifier_batch_size = min(64, max(16, config.batch_size * 8))
    return {
        "sample_count": int(sample_count),
        "num_iterations": int(config.num_iterations),
        "requested_pair_samples": requested_pairs,
        "effective_pair_samples": effective_pairs,
        "max_embedding_pairs": int(config.max_embedding_pairs),
        "embedding_max_steps": max(1, math.ceil(effective_pairs / config.batch_size)) if effective_pairs else 0,
        "classifier_batch_size": classifier_batch_size,
        "capped": requested_pairs > effective_pairs,
        "mode": "fine_tune_embeddings" if config.fine_tune_embeddings else "frozen_encoder",
        "status": "ok",
    }


def resolve_device(requested: str = "auto") -> str:
    """设备选择（设计文档 6.3）。Docker 容器内为 cpu；Mac 原生优先 MPS。"""
    import torch

    if requested != "auto":
        if requested == "cuda" and not torch.cuda.is_available():
            raise ValueError("请求 cuda 但不可用")
        if requested == "mps" and not (hasattr(torch.backends, "mps") and torch.backends.mps.is_available()):
            raise ValueError("请求 mps 但不可用")
        return requested
    if torch.cuda.is_available():
        return "cuda"
    if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def set_all_seeds(seed: int) -> None:
    import torch
    import transformers

    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)
    transformers.set_seed(seed)


def oversample(texts: list[str], labels: list[str], seed: int = 42) -> tuple[list[str], list[str]]:
    """少数类过采样到类别均衡。"""
    rng = random.Random(seed)
    by_label: dict[str, list[int]] = {}
    for i, lab in enumerate(labels):
        by_label.setdefault(lab, []).append(i)
    max_count = max(len(v) for v in by_label.values())
    out_texts: list[str] = []
    out_labels: list[str] = []
    for lab, indices in by_label.items():
        out_texts.extend(texts[i] for i in indices)
        out_labels.extend(lab for _ in indices)
        deficit = max_count - len(indices)
        if deficit > 0:
            extra = rng.choices(indices, k=deficit)
            out_texts.extend(texts[i] for i in extra)
            out_labels.extend(lab for _ in extra)
    return out_texts, out_labels


class TrainedRouter:
    """训练完成的模型封装：概率列顺序与标签的可靠映射。"""

    def __init__(self, model, prob_labels: list[str]) -> None:
        self.model = model
        self.prob_labels = list(prob_labels)

    def predict_proba(self, texts: list[str]) -> np.ndarray:
        probs = self.model.predict_proba(list(texts))
        arr = np.asarray(probs, dtype=np.float64)
        if arr.ndim == 1:
            arr = arr.reshape(1, -1)
        return arr

    def logits(self, texts: list[str]) -> np.ndarray:
        probs = self.predict_proba(texts)
        return np.log(np.clip(probs, 1e-12, 1.0))

    def save_pretrained(self, path: str | Path) -> None:
        self.model.save_pretrained(str(path))

    @property
    def label_order(self) -> list[str]:
        return list(self.prob_labels)


def _prob_columns(model, fallback: list[str]) -> list[str]:
    """确定 predict_proba 输出列对应的标签顺序。

    优先 sklearn 头的 classes_；否则 SetFit 的 labels；
    最后回退固定五分类顺序（并记录告警）。
    """
    head = getattr(model, "model_head", None)
    classes = getattr(head, "classes_", None)
    if classes is not None and len(classes) > 0:
        return [str(c) for c in classes]
    labels = getattr(model, "labels", None)
    if labels:
        return [str(lb) for lb in labels]
    logger.warning("无法确定概率列顺序，回退默认标签顺序")
    return list(fallback)


def train_router(
    config: TrainConfig,
    train_texts: list[str],
    train_labels: list[str],
    workdir: Path,
    progress_cb: Callable[[float, str], None] | None = None,
    label_order: list[str] | None = None,
) -> TrainedRouter:
    """执行 SetFit 训练并返回封装后的模型。

    progress_cb(percent, message)：0-100 的训练阶段内部进度（尽力而为，
    不同 SetFit 版本可能只提供粗粒度进度）。
    """
    import time

    # SetFit/HuggingFace 的部分版本会把 checkpoint 默认写到相对路径
    # ``checkpoints``。Worker 的当前目录是镜像内的 /app，非 root 用户不可写，
    # 因而必须把训练过程的 cwd 固定到本次 Run 的可写临时制品目录。
    workdir = Path(workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    checkpoint_dir = workdir / "checkpoints"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    previous_cwd = Path.cwd()

    set_all_seeds(config.seed)
    device = resolve_device(config.device)

    from datasets import Dataset
    from setfit import SetFitModel

    if progress_cb:
        progress_cb(0.05, f"加载基础模型 {config.base_model_id}")
    # 自定义意图标签 §6.5：分类头顺序显式来自 Schema；缺省才回退 sorted(set(...))
    head_labels = list(label_order) if label_order else (sorted(set(train_labels)) or None)
    model = SetFitModel.from_pretrained(
        config.base_model_id,
        labels=head_labels,
    )
    if hasattr(model, "max_length"):
        try:
            model.max_length = config.max_length
        except Exception:  # 某些版本该属性只读
            pass
    if hasattr(model, "device"):
        try:
            model.to(device)
        except Exception:
            device = "cpu"
            model.to("cpu")

    if progress_cb:
        progress_cb(0.1, "构造对比学习样本")

    texts, labels = train_texts, train_labels
    if config.sampling_strategy == "oversampling":
        texts, labels = oversample(train_texts, train_labels, seed=config.seed)
    train_dataset = Dataset.from_dict({"text": list(texts), "label": list(labels)})

    from setfit import SetFitTrainer, TrainingArguments  # noqa: F401 兼容不同版本

    resource_plan = build_resource_plan(len(texts), config)
    training_args = TrainingArguments(
        output_dir=str(checkpoint_dir),
        batch_size=(config.batch_size, int(resource_plan["classifier_batch_size"])),
        num_epochs=config.num_epochs,
        max_steps=max(1, int(resource_plan["embedding_max_steps"])),
        sampling_strategy=config.sampling_strategy,
        # SetFit 1.1.x 在 num_iterations 非空时会忽略 max_pairs，并把全部
        # 配对 materialize 成 Dataset；显式置空后 max_steps 才真正限制内存。
        num_iterations=None,
        body_learning_rate=config.body_learning_rate,
        max_length=config.max_length,
        report_to="none",
        save_strategy="no",
    )

    os.chdir(workdir)
    try:
        try:
            trainer = SetFitTrainer(
                model=model,
                train_dataset=train_dataset,
                num_iterations=config.num_iterations,
                num_epochs=config.num_epochs,
                batch_size=config.batch_size,
                body_learning_rate=config.body_learning_rate,
                column_mapping={"text": "text", "label": "label"},
            )
        except TypeError:
            # 新版本 SetFit 只暴露 Trainer + TrainingArguments
            from setfit import Trainer

            trainer = Trainer(model=model, args=training_args, train_dataset=train_dataset)

        _attach_progress(trainer, progress_cb)

        start = time.time()
        if config.fine_tune_embeddings:
            if progress_cb:
                progress_cb(
                    0.15,
                    f"开始嵌入微调 device={device} 样本={len(texts)} "
                    f"对比配对={resource_plan['effective_pair_samples']}/{resource_plan['requested_pair_samples']} "
                    f"steps={resource_plan['embedding_max_steps']}",
                )
            trainer.train(args=training_args)
        else:
            if progress_cb:
                progress_cb(
                    0.15,
                    f"低内存模式：冻结 BGE 编码器，使用完整 {len(texts)} 条数据训练 SetFit 分类头 "
                    f"batch={resource_plan['classifier_batch_size']}",
                )
            trainer.train_classifier(*trainer.dataset_to_parameters(train_dataset), args=training_args)
    finally:
        os.chdir(previous_cwd)
    elapsed = round(time.time() - start, 1)
    if progress_cb:
        progress_cb(0.98, f"训练完成，耗时 {elapsed}s")

    return TrainedRouter(model, _prob_columns(model, label_order or sorted(set(train_labels))))


def _attach_progress(trainer, progress_cb) -> None:
    """尽力附加训练进度回调；不同 SetFit 版本回调接口不同，失败则忽略。"""
    if progress_cb is None:
        return
    try:
        from setfit import TrainerCallback

        class _ProgressCallback(TrainerCallback):  # type: ignore[misc]
            def __init__(self) -> None:
                super().__init__()
                self._steps = 0

            def on_embedding_step(self, args, state, control, **kwargs):
                self._steps += 1
                try:
                    total = max(state.max_steps, 1)
                    frac = min(0.95, 0.15 + 0.8 * (state.global_step / total))
                    progress_cb(float(frac), f"embedding step {state.global_step}/{total}")
                except Exception:
                    pass
                return control

        trainer.add_callback(_ProgressCallback())
    except Exception as exc:
        logger.debug("进度回调不可用: %s", exc)
