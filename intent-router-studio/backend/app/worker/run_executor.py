"""Run 执行器：阶段状态机、事件、日志、资源监控、取消、制品打包（设计文档 6 / 11）。"""
from __future__ import annotations

import json
import logging
import platform
import threading
import time
import traceback
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from app import ids
from app.constants import RunStatus
from app.db import SessionLocal
from app.models import DatasetVersion, RunMetric, ThresholdVersion, TrainingRun
from app.router_core.calibration import calibrate, fit_and_report, reliability_diagram, softmax
from app.router_core.evaluation import (
    classification_metrics,
    confidence_margin_distribution,
    evaluate_split,
    latency_stats,
    slice_metrics,
)
from app.router_core.label_schema import ensure_training_labels
from app.router_core.normalization import encode_input
from app.router_core.policy import Thresholds, decide
from app.router_core.splitting import ensure_splits_trainable
from app.router_core.threshold_search import route_metrics, search_thresholds
from app.services import artifact_service, dataset_service, run_service
from app.worker import queue

logger = logging.getLogger(__name__)

# 阶段进度区间
STAGE_RANGES = {
    RunStatus.PREPARING: (0, 5),
    RunStatus.TRAINING_EMBEDDING: (5, 45),
    RunStatus.TRAINING_HEAD: (45, 55),
    RunStatus.CALIBRATING: (55, 65),
    RunStatus.SEARCHING_THRESHOLDS: (65, 75),
    RunStatus.EVALUATING: (75, 92),
    RunStatus.PACKAGING: (92, 99),
}


class RunCancelled(Exception):
    """用户请求取消。"""


class RunInterrupted(Exception):
    """Worker 收到停机信号。"""


class ResourceMonitor(threading.Thread):
    """采样峰值 RSS 并更新心跳（每 2s / 每 10s）。"""

    def __init__(self, db_factory: Callable, run_id: str) -> None:
        super().__init__(daemon=True)
        self.db_factory = db_factory
        self.run_id = run_id
        self.peak_rss_mb = 0.0
        self.cpu_percent = 0.0
        self._stop = threading.Event()

    def run(self) -> None:
        import psutil

        proc = psutil.Process()
        last_heartbeat = 0.0
        while not self._stop.is_set():
            try:
                rss = proc.memory_info().rss / 1024 / 1024
                self.peak_rss_mb = max(self.peak_rss_mb, rss)
                self.cpu_percent = proc.cpu_percent()
                if time.time() - last_heartbeat > 10:
                    db = self.db_factory()
                    try:
                        queue.update_heartbeat(db, self.run_id)
                    finally:
                        db.close()
                    last_heartbeat = time.time()
            except Exception:  # 监控失败不影响训练
                pass
            self._stop.wait(2.0)

    def stop(self) -> None:
        self._stop.set()


class RunLogger:
    """同时写 DB 结构化事件、events.jsonl 与人类可读 run.log。"""

    def __init__(self, db, run: TrainingRun, workdir: Path) -> None:
        self.db = db
        self.run_id = run.id
        self.workdir = workdir
        self.events_path = workdir / "events.jsonl"
        self.log_path = workdir / "run.log"
        self._seq = self._max_seq() - 1  # _emit 先自增，保证首条 sequence = max+1

    def _max_seq(self) -> int:
        from app.models import RunEvent

        row = (
            self.db.query(RunEvent.sequence)
            .filter(RunEvent.run_id == self.run_id)
            .order_by(RunEvent.sequence.desc())
            .first()
        )
        return (row[0] + 1) if row else 1

    def _emit(self, event_type: str, payload: dict) -> None:
        from app.models import RunEvent

        self._seq += 1
        ts = datetime.now(UTC).isoformat()
        record = {"ts": ts, "type": event_type, "payload": payload}
        try:
            with open(self.events_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:  # 原子发布重命名后目录可能已不存在
            pass
        try:
            self.db.add(RunEvent(run_id=self.run_id, sequence=self._seq, event_type=event_type, payload=payload))
            self.db.commit()
        except Exception:
            self.db.rollback()

    def relocate(self, new_dir: Path) -> None:
        """原子发布把工作目录 .tmp 重命名为最终目录后，文件输出切换到新目录。"""
        self.workdir = new_dir
        self.events_path = new_dir / "events.jsonl"
        self.log_path = new_dir / "run.log"

    def _touch_run(self, **fields) -> None:
        run = self.db.get(TrainingRun, self.run_id)
        if run is None:
            return
        for key, value in fields.items():
            setattr(run, key, value)
        run.heartbeat_at = datetime.now(UTC)
        self.db.commit()

    def log(self, level: str, message: str) -> None:
        line = f"{datetime.now(UTC).isoformat()} [{level}] {message}"
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:  # 目录已被原子发布重命名时仅落 DB 事件
            pass
        self._emit("log", {"level": level, "message": message})
        logger.info("run=%s %s", self.run_id, message)

    def progress(self, stage: str, percent: float, message: str = "") -> None:
        self._emit("progress", {"stage": stage, "percent": round(percent, 2), "message": message})
        fields: dict = {"stage": stage, "progress": max(0.0, min(100.0, percent))}
        # 阶段值均为 RunStatus 成员：status 随 stage 同步推进，
        # 否则终态条件更新（如 PACKAGING→SUCCEEDED）会因 from 集不匹配而落空。
        if stage in queue.RUNNING_STATUSES and stage != RunStatus.CANCELLING:
            fields["status"] = stage
        self._touch_run(**fields)

    def metric(self, name: str, value: float, step: int | None = None) -> None:
        payload = {"name": name, "value": value}
        if step is not None:
            payload["step"] = step
        self._emit("metric", payload)

    def terminal(self, status: str, extra: dict | None = None) -> None:
        self._emit("terminal", {"status": status, **(extra or {})})


class RunExecutor:
    def __init__(self, worker_id: str, stop_check: Callable[[], bool]) -> None:
        self.worker_id = worker_id
        self.stop_check = stop_check

    # ------------------------------------------------------------------
    def _check_cancel(self, run_id: str, counter: dict) -> None:
        counter["n"] = counter.get("n", 0) + 1
        if counter["n"] % 10 != 0:
            return
        db = SessionLocal()
        try:
            if queue.is_cancel_requested(db, run_id):
                raise RunCancelled("用户请求取消")
        finally:
            db.close()
        if self.stop_check():
            raise RunInterrupted("Worker 停机")

    def execute(self, run_id: str) -> str:
        db = SessionLocal()
        status = RunStatus.FAILED
        try:
            run = db.get(TrainingRun, run_id)
            if run is None:
                logger.error("Run 不存在: %s", run_id)
                return RunStatus.FAILED
            workdir = artifact_service.run_tmp_dir(run_id)
            log = RunLogger(db, run, workdir)
            monitor = ResourceMonitor(SessionLocal, run_id)
            monitor.start()
            start_ts = time.time()
            log.log("INFO", f"Worker {self.worker_id} 领取任务 {run_id}")
            try:
                status = self._pipeline(db, run, log, monitor, workdir, start_ts)
            except RunCancelled as exc:
                # cancel_run 仅置 cancel_requested；运行态可能停在任一阶段，from 集须覆盖全部
                queue.transition_status(
                    db, run_id, set(queue.RUNNING_STATUSES), RunStatus.CANCELLING, stage=RunStatus.CANCELLING
                )
                log.log("WARN", f"取消中: {exc}")
                final = RunStatus.CANCELLED
                queue.transition_status(
                    db,
                    run_id,
                    {RunStatus.CANCELLING},
                    RunStatus.CANCELLED,
                    stage=RunStatus.CANCELLED,
                    finished_at=datetime.now(UTC).isoformat(),
                )
                log.terminal(final)
                return final
            except RunInterrupted as exc:
                log.log("WARN", f"Worker 停机中断: {exc}")
                queue.transition_status(
                    db,
                    run_id,
                    {RunStatus.PREPARING, RunStatus.TRAINING_EMBEDDING, RunStatus.TRAINING_HEAD, RunStatus.CALIBRATING, RunStatus.SEARCHING_THRESHOLDS, RunStatus.EVALUATING, RunStatus.PACKAGING},
                    RunStatus.INTERRUPTED,
                    stage=RunStatus.INTERRUPTED,
                    finished_at=datetime.now(UTC).isoformat(),
                    error={"code": "WORKER_SHUTDOWN", "message": str(exc)},
                )
                log.terminal(RunStatus.INTERRUPTED)
                return RunStatus.INTERRUPTED
            except Exception as exc:
                log.log("ERROR", f"训练失败: {exc}\n{traceback.format_exc()}")
                queue.transition_status(
                    db,
                    run_id,
                    {RunStatus.PREPARING, RunStatus.TRAINING_EMBEDDING, RunStatus.TRAINING_HEAD, RunStatus.CALIBRATING, RunStatus.SEARCHING_THRESHOLDS, RunStatus.EVALUATING, RunStatus.PACKAGING, RunStatus.CANCELLING},
                    RunStatus.FAILED,
                    stage=RunStatus.FAILED,
                    finished_at=datetime.now(UTC).isoformat(),
                    error={"code": type(exc).__name__, "message": str(exc)[:2000]},
                )
                log.terminal(RunStatus.FAILED)
                return RunStatus.FAILED
            finally:
                monitor.stop()
            return status
        finally:
            db.close()

    # ------------------------------------------------------------------
    def _pipeline(self, db, run: TrainingRun, log: RunLogger, monitor: ResourceMonitor, workdir: Path, start_ts: float) -> str:
        run_id = run.id
        counter: dict = {}

        def bounded_percent(stage: str, fraction: float) -> float:
            lo, hi = STAGE_RANGES[stage]
            return lo + (hi - lo) * max(0.0, min(1.0, fraction))

        # ---------- PREPARING ----------
        dataset = db.get(DatasetVersion, run.dataset_id)
        if dataset is None:
            raise RuntimeError(f"数据集不存在 {run.dataset_id}")
        # V2 §3.6：只认 Run 创建时固定的 split；旧 Run（无 split_id）在此刻固定
        pinned = run.split_id
        split = run_service.resolve_run_split(db, run)
        if not pinned:
            log.log("INFO", f"Run 未固定 split（旧版本创建），已固定为 {split.id}")

        data = dataset_service.load_dataset_frame(dataset)
        split_map = dataset_service.load_split_frame(split)
        merged = data.merge(split_map, on="sample_id", how="inner")
        if len(merged) == 0:
            raise RuntimeError("数据集与 split 不匹配（样本数为 0）")

        # 训练前二道防线：空 split / 组泄漏在加载模型前即失败（设计修改方案 3.2C）
        ensure_splits_trainable(merged)

        label_order = None  # 训练后确定
        texts_by_split: dict[str, list[str]] = {}
        labels_by_split: dict[str, list[str]] = {}
        for split_name in ("train", "validation", "test"):
            sub = merged[merged["split"] == split_name]
            texts_by_split[split_name] = [encode_input(t, c) for t, c in zip(sub["text"], sub["context"].where(sub["context"].notna(), None), strict=True)]
            labels_by_split[split_name] = sub["label"].tolist()

        # 自定义意图标签 §6.5 / Review 修复 §7.1：训练只认数据集绑定 Schema；
        # 无 schema_id 的历史 v1 数据仅读取兼容（create_run 已拦截新 Run）
        from app.services.label_schema_service import resolve_dataset_schema

        schema = resolve_dataset_schema(db, dataset)
        schema_label_order = list(schema.label_keys)
        ensure_training_labels(labels_by_split["train"], schema.document)
        log.log("INFO", f"训练 Schema 标签 {len(schema_label_order)} 类: {schema_label_order}")
        log.progress(RunStatus.PREPARING, 4, f"样本分布 train={len(texts_by_split['train'])} val={len(texts_by_split['validation'])} test={len(texts_by_split['test'])}")
        artifact_service.check_disk_space(500 * 1024 * 1024, multiple=3.0)
        self._check_cancel(run_id, counter)

        # ---------- TRAINING_EMBEDDING ----------
        from app.router_core.training import TrainConfig, resolve_device, set_all_seeds, train_router

        train_cfg = TrainConfig.from_dict(run.config.get("train", {}))
        device = resolve_device(train_cfg.device)
        set_all_seeds(train_cfg.seed)
        log.progress(RunStatus.TRAINING_EMBEDDING, 6, f"device={device} base={train_cfg.base_model_id}")

        def progress_cb(fraction: float, message: str) -> None:
            self._check_cancel(run_id, counter)
            log.progress(RunStatus.TRAINING_EMBEDDING, bounded_percent(RunStatus.TRAINING_EMBEDDING, fraction), message)

        router = train_router(
            train_cfg,
            texts_by_split["train"],
            labels_by_split["train"],
            workdir / "_train_tmp",
            progress_cb=progress_cb,
            label_order=schema_label_order,
        )
        label_order = router.label_order
        log.metric("train_samples", len(texts_by_split["train"]))
        self._check_cancel(run_id, counter)

        # ---------- TRAINING_HEAD：应用分类头得到 val/test 概率 ----------
        log.progress(RunStatus.TRAINING_HEAD, 46, "输出 validation/test 概率")
        val_logits = router.logits(texts_by_split["validation"])
        test_logits = router.logits(texts_by_split["test"])
        label_pos = {lab: i for i, lab in enumerate(label_order)}
        val_y = np.array([label_pos[lab] for lab in labels_by_split["validation"]], dtype=int)
        test_y = np.array([label_pos[lab] for lab in labels_by_split["test"]], dtype=int)
        log.progress(RunStatus.TRAINING_HEAD, 55, "概率输出完成")
        self._check_cancel(run_id, counter)

        # ---------- CALIBRATING ----------
        log.progress(RunStatus.CALIBRATING, 57, "validation 温度校准")
        temperature, calib_report, val_probs, diagram_after = fit_and_report(val_logits, val_y)
        diagram_before = reliability_diagram(softmax(val_logits, axis=1), val_y)
        log.log("INFO", f"温度 T={temperature:.4f}，NLL {calib_report.before['nll']} → {calib_report.after['nll']}")
        log.metric("calibration_nll_after", calib_report.after["nll"])
        log.progress(RunStatus.CALIBRATING, 65, f"T={temperature:.3f}")
        self._check_cancel(run_id, counter)

        # ---------- SEARCHING_THRESHOLDS ----------
        log.progress(RunStatus.SEARCHING_THRESHOLDS, 66, "约束阈值搜索（validation）")
        search_result = search_thresholds(val_probs, val_y, label_list=label_order, spec=run.config.get("threshold_search"))
        thresholds = search_result.best if search_result.feasible else Thresholds()
        if not search_result.feasible:
            log.log("WARN", f"未找到满足约束的阈值：{search_result.note}，使用冷启动默认阈值")
        artifact_service.write_json(workdir / "thresholds.json", thresholds.to_dict())
        artifact_service.write_json(workdir / "threshold_search.json", search_result.to_dict())
        db.add(
            ThresholdVersion(
                id=ids.prefixed(ids.THRESHOLD),
                run_id=run_id,
                version=1,
                config=thresholds.to_dict(),
                metrics=search_result.best_metrics,
                source="searched",
            )
        )
        db.commit()
        log.log("INFO", f"选中阈值 {thresholds.to_dict()}")
        log.progress(RunStatus.SEARCHING_THRESHOLDS, 75, "阈值搜索完成")
        self._check_cancel(run_id, counter)

        # ---------- EVALUATING ----------
        log.progress(RunStatus.EVALUATING, 76, "冻结阈值评估 test")
        test_probs = calibrate(test_logits, temperature)
        val_eval = evaluate_split(val_y, softmax(val_logits, axis=1), val_probs, thresholds, label_order)
        test_eval = evaluate_split(test_y, softmax(test_logits, axis=1), test_probs, thresholds, label_order)

        # 风险切片
        test_sub = merged[merged["split"] == "test"].reset_index(drop=True)
        slice_flags = test_sub["risk_slice"].fillna("none").replace("", "none")
        slice_flags = slice_flags.where(~test_sub["is_hard_negative"].astype(bool), "hard_negative")
        slices = slice_metrics(test_y, test_probs, thresholds, slice_flags.to_numpy(), label_order)

        risk_mask = test_sub["is_risk_test"].astype(bool).to_numpy() if "is_risk_test" in test_sub.columns else np.zeros(len(test_sub), dtype=bool)
        risk_eval = None
        if risk_mask.sum() > 0:
            risk_eval = {
                "support": int(risk_mask.sum()),
                "classification": classification_metrics(test_y[risk_mask], test_probs[risk_mask].argmax(axis=1), label_order),
                "routing": route_metrics(test_probs[risk_mask], test_y[risk_mask], thresholds, label_order),
            }

        distributions = confidence_margin_distribution(test_probs)

        # 逐样本预测（validation + test）
        latency_texts = texts_by_split["test"][:30] or texts_by_split["validation"][:30]
        latencies = []
        for text in latency_texts:
            t0 = time.perf_counter()
            router.predict_proba([text])
            latencies.append((time.perf_counter() - t0) * 1000)
        latency = latency_stats(latencies)
        log.log("INFO", f"推理延迟 P95={latency['p95']}ms（{device}）")

        per_sample_frames = []
        for split_name, probs, y_true in (
            ("validation", val_probs, val_y),
            ("test", test_probs, test_y),
        ):
            sub = merged[merged["split"] == split_name].reset_index(drop=True)
            decisions = []
            for row_probs in probs:
                prob_map = {lab: float(p) for lab, p in zip(label_order, row_probs, strict=True)}
                decisions.append(decide(prob_map, thresholds))
            frame = pd.DataFrame(
                {
                    "sample_id": sub["sample_id"],
                    "text": sub["text"],
                    "context": sub["context"],
                    "true_label": [label_order[i] for i in y_true],
                    "raw_pred": [label_order[i] for i in probs.argmax(axis=1)],
                    "final_route": [d.route for d in decisions],
                    "decision": [d.decision for d in decisions],
                    "margin": [d.margin for d in decisions],
                    "confidence": [d.confidence for d in decisions],
                    "top_k": [json.dumps(d.top_k, ensure_ascii=False) for d in decisions],
                    "reason_codes": ["|".join(d.reason_codes) for d in decisions],
                    "risk_slice": sub["risk_slice"],
                    "source": sub["source"],
                    "group_id": sub["group_id"],
                    "split": split_name,
                    "correct_raw": [label_order[i] == label_order[t] for i, t in zip(probs.argmax(axis=1), y_true, strict=True)],
                    "correct_final": [d.route == label_order[t] for d, t in zip(decisions, y_true, strict=True)],
                }
            )
            for lab in label_order:
                frame[f"prob_{lab}"] = probs[:, label_order.index(lab)]
            per_sample_frames.append(frame)
        per_sample = pd.concat(per_sample_frames, ignore_index=True)
        per_sample_path = workdir / "per_sample_predictions.parquet"
        per_sample.to_parquet(per_sample_path, index=False)

        duration_s = round(time.time() - start_ts, 1)
        environment = {
            "python": platform.python_version(),
            "device": device,
            "cpu_count": __import__("os").cpu_count(),
            "peak_rss_mb": round(monitor.peak_rss_mb, 1),
            "duration_s": duration_s,
            "created_at": datetime.now(UTC).isoformat(),
        }
        try:
            import sklearn
            import torch
            import transformers

            environment["versions"] = {
                "torch": torch.__version__,
                "transformers": transformers.__version__,
                "sklearn": sklearn.__version__,
                "numpy": np.__version__,
                "pandas": pd.__version__,
            }
            try:
                import setfit

                environment["versions"]["setfit"] = getattr(setfit, "__version__", "unknown")
            except Exception:
                pass
        except Exception:
            pass

        metrics_doc = {
            "run_id": run_id,
            "dataset_version_id": run.dataset_id,
            "split_id": run.split_id,
            "created_at": datetime.now(UTC).isoformat(),
            "label_order": label_order,
            "calibration": {
                **calib_report.to_dict(),
                "reliability_before": diagram_before,
                "reliability_after": diagram_after,
            },
            "thresholds": thresholds.to_dict(),
            "threshold_search": search_result.to_dict(),
            "validation": val_eval,
            "test": test_eval,
            "risk_test": risk_eval,
            "slices": slices,
            "distributions": distributions,
            "latency": latency,
            "environment": environment,
        }
        artifact_service.write_json(workdir / "metrics.json", metrics_doc)

        # 指标入库（供列表页快速索引）
        for metric_name, value in (
            ("macro_f1", test_eval["classification"]["macro_f1"]),
            ("accuracy", test_eval["classification"]["accuracy"]),
            ("false_write_rate", test_eval["routing"]["false_write_rate"]),
            ("safe_coverage", test_eval["routing"]["safe_coverage"]),
            ("coverage", test_eval["routing"]["coverage"]),
            ("selective_accuracy", test_eval["routing"]["selective_accuracy"]),
            ("unclear_rate", test_eval["routing"]["unclear_rate"]),
            ("write_precision", test_eval["routing"]["write_precision"]),
            ("write_recall", test_eval["routing"]["write_recall"]),
            ("ece", test_eval["calibration"]["ece"]),
        ):
            if value is not None:
                db.add(RunMetric(run_id=run_id, split="test", slice="all", metric_name=metric_name, value=float(value), support=int(test_eval["classification"]["support"])))
        for slice_name, slice_data in slices.items():
            db.add(RunMetric(run_id=run_id, split="test", slice=slice_name, metric_name="macro_f1", value=float(slice_data["macro_f1"] or 0), support=int(slice_data["support"])))
            db.add(RunMetric(run_id=run_id, split="test", slice=slice_name, metric_name="false_write_rate", value=float(slice_data["false_write_rate"] or 0), support=int(slice_data["support"])))
        db.commit()
        log.progress(RunStatus.EVALUATING, 92, "评估完成")
        self._check_cancel(run_id, counter)

        # ---------- PACKAGING ----------
        log.progress(RunStatus.PACKAGING, 93, "保存模型制品")
        router.save_pretrained(workdir / "setfit_model")
        # §6.5：制品保存完整 Schema 快照（顺序=分类头顺序）与 hash
        artifact_service.write_json(
            workdir / "label_schema.json",
            {
                "schema_format": schema.document.schema_format,
                "schema_id": schema.schema_id,
                "schema_hash": schema.schema_hash,
                "labels": label_order,
                "label_definitions": schema.document.to_dict()["labels"],
            },
        )
        artifact_service.write_json(
            workdir / "calibration.json",
            {
                **calib_report.to_dict(),
                "fitted_on_dataset_version": run.dataset_id,
            },
        )
        artifact_service.write_json(workdir / "environment.json", environment)
        (workdir / "model_card.md").write_text(self._model_card(run, thresholds, test_eval, environment), encoding="utf-8")

        artifact_service.build_manifest(
            workdir,
            {
                "run_id": run_id,
                "dataset_version_id": run.dataset_id,
                "base_model": train_cfg.base_model_id,
                "seed": train_cfg.seed,
                "created_at": datetime.now(UTC).isoformat(),
                "metrics_summary": {
                    "macro_f1": test_eval["classification"]["macro_f1"],
                    "false_write_rate": test_eval["routing"]["false_write_rate"],
                    "safe_coverage": test_eval["routing"]["safe_coverage"],
                },
            },
        )
        artifact_service.verify_manifest(workdir)
        log.progress(RunStatus.PACKAGING, 98, "原子发布制品")

        final_dir = artifact_service.publish_run(run_id)
        log.relocate(final_dir)  # .tmp 已重命名为最终目录，日志/事件改写到新目录
        ok = queue.transition_status(
            db,
            run_id,
            set(queue.RUNNING_STATUSES),
            RunStatus.SUCCEEDED,
            stage=RunStatus.SUCCEEDED,
            progress=100.0,
            artifacts_dir=str(final_dir),
            finished_at=datetime.now(UTC).isoformat(),
        )
        if not ok:
            logger.warning("run=%s 终态更新未命中（状态可能已被并发修改）", run_id)
        log.log("INFO", f"Run 成功，制品目录 {final_dir.name}")
        log.terminal(RunStatus.SUCCEEDED)
        return RunStatus.SUCCEEDED

    def _model_card(self, run: TrainingRun, thresholds: Thresholds, test_eval: dict, environment: dict) -> str:
        cls = test_eval["classification"]
        routing = test_eval["routing"]
        return f"""# Intent Router Model Card

- Run: `{run.id}`
- 数据集版本: `{run.dataset_id}`
- 基础模型: `{run.config['train']['base_model_id']}`
- Seed: `{run.config['train']['seed']}`
- 训练设备: `{environment.get('device')}`
- 生成时间: {environment.get('created_at')}

## 阈值策略

```json
{json.dumps(thresholds.to_dict(), ensure_ascii=False, indent=2)}
```

## 测试集指标

| 指标 | 值 |
|---|---|
| Macro F1 | {cls.get('macro_f1')} |
| Accuracy | {cls.get('accuracy')} |
| False Write Rate | {routing.get('false_write_rate')} |
| Safe Coverage | {routing.get('safe_coverage')} |
| Selective Accuracy | {routing.get('selective_accuracy')} |

## 使用约束

- 分类结果不等于执行授权；`write_action` 仅允许后续系统召回写 Skill，
  真实写入仍必须经过对象校验、参数校验、风险门禁和用户确认。
"""
