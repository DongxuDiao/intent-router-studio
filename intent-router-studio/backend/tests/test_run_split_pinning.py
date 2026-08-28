"""Run 创建时固定 Split（修改方案 V2 §3.6）。

不变量：
- create_run 时解析并写入 run.split_id：有 split 用最新，无 split 按训练
  种子立刻创建（原来由 Worker 执行时懒创建，创建到执行之间可能漂移）；
- Worker 侧 resolve_run_split 只认 run.split_id：之后数据集再新建 split
  也不影响已创建的 Run；split 不存在/不属于该数据集时明确失败；
- 旧版本创建的 Run（split_id 为空）在执行时一次性固定并持久化。
"""
from __future__ import annotations

import csv
import io

import pytest

from app.constants import RunStatus
from app.errors import ApiError
from app.models import DatasetSplit, TrainingRun
from app.services import dataset_service, run_service

LABELS = ("information", "read_only", "write_action", "unclear", "oos")


def _csv_bytes(rows: list[dict]) -> bytes:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=["text", "label", "group_id"])
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8-sig")


@pytest.fixture
def frozen_dataset(db, project_id):
    rows = [
        {"text": f"{lab} 的第 {g}-{i} 条样本问句", "label": lab, "group_id": f"{lab}-g{g}"}
        for lab in LABELS
        for g in range(4)
        for i in range(2)
    ]
    upload = dataset_service.save_upload(db, project_id, "splits.csv", _csv_bytes(rows), "text/csv")
    return dataset_service.import_upload(
        db, upload.id, {"mode": "prelabeled", "columns": {"text": "text", "label": "label", "group_id": "group_id"}}
    )


def test_create_run_pins_latest_split(db, frozen_dataset):
    first = dataset_service.create_split(db, frozen_dataset.id, seed=7)
    second = dataset_service.create_split(db, frozen_dataset.id, seed=9)

    run = run_service.create_run(db, frozen_dataset.project_id, frozen_dataset.id, "", None)
    assert run.split_id == second.id  # 固定为创建时刻的最新 split
    assert run.split_id != first.id


def test_create_run_creates_split_when_missing(db, frozen_dataset):
    assert db.query(DatasetSplit).filter(DatasetSplit.dataset_id == frozen_dataset.id).count() == 0
    run = run_service.create_run(
        db, frozen_dataset.project_id, frozen_dataset.id, "", {"train": {"seed": 123}}
    )
    assert run.split_id is not None
    split = db.get(DatasetSplit, run.split_id)
    assert split is not None
    assert split.seed == 123  # 与训练种子一致（原 Worker 懒创建行为）


def test_pinned_split_does_not_drift_after_new_splits(db, frozen_dataset):
    run = run_service.create_run(db, frozen_dataset.project_id, frozen_dataset.id, "", None)
    pinned_id = run.split_id

    # Run 创建之后数据集又新建了 split——Worker 解析仍返回固定值
    dataset_service.create_split(db, frozen_dataset.id, seed=100)
    db.expire_all()
    resolved = run_service.resolve_run_split(db, run)
    assert resolved.id == pinned_id
    assert run.split_id == pinned_id


def test_retry_keeps_split_and_upgrades_legacy_resource_config(db, frozen_dataset):
    run = run_service.create_run(db, frozen_dataset.project_id, frozen_dataset.id, "", {"train": {"num_iterations": 3}})
    run.status = RunStatus.INTERRUPTED
    legacy = dict(run.config)
    legacy_train = dict(legacy["train"])
    legacy_train.pop("max_embedding_pairs", None)
    legacy["train"] = legacy_train
    legacy["resource_preflight"] = {"status": "stale"}
    run.config = legacy
    db.commit()

    retried = run_service.retry_run(db, run.id)
    assert retried.split_id == run.split_id
    assert retried.config["train"]["max_embedding_pairs"] == 4_000
    assert retried.config["resource_preflight"]["effective_pair_samples"] <= 4_000


def test_resolve_run_split_legacy_run_pins_now(db, frozen_dataset):
    run = run_service.create_run(db, frozen_dataset.project_id, frozen_dataset.id, "", None)
    pinned_id = run.split_id
    # 模拟修复前创建的旧 Run：清空 split_id（列允许为空）
    run.split_id = None
    db.commit()

    newer = dataset_service.create_split(db, frozen_dataset.id, seed=55)
    db.expire_all()
    resolved = run_service.resolve_run_split(db, run)
    assert resolved.id == newer.id  # 此刻固定为最新
    assert run.split_id == newer.id  # 并已持久化，之后不再漂移
    assert newer.id != pinned_id


def test_resolve_run_split_legacy_run_creates_when_none(db, frozen_dataset):
    run = run_service.create_run(db, frozen_dataset.project_id, frozen_dataset.id, "", None)
    run.split_id = None
    db.commit()
    # 数据集的所有 split 一并删除，模拟"无 split"的旧场景
    db.query(DatasetSplit).filter(DatasetSplit.dataset_id == frozen_dataset.id).delete()
    db.commit()
    db.expire_all()

    seed = run.config["train"]["seed"]
    resolved = run_service.resolve_run_split(db, run)
    assert resolved.seed == seed
    assert run.split_id == resolved.id


def test_resolve_run_split_missing_or_foreign_fails(db, frozen_dataset):
    # 用瞬态 Run 对象构造异常输入（不落库，避开 split_id 外键约束）
    ghost = TrainingRun(
        id="run_ghost000000000000000",
        project_id=frozen_dataset.project_id,
        dataset_id=frozen_dataset.id,
        config={},
        split_id="spl_doesnotexist000000000",
    )
    with pytest.raises(RuntimeError, match="不存在"):
        run_service.resolve_run_split(db, ghost)

    # split 属于别的数据集：同样明确失败，绝不静默回退到"最新"
    other = dataset_service.create_split(db, frozen_dataset.id, seed=3)
    rows2 = [
        {"text": f"另一批 {lab} {i}", "label": lab, "group_id": f"b-{lab}-g{i % 4}"}
        for lab in LABELS
        for i in range(8)
    ]
    upload2 = dataset_service.save_upload(db, frozen_dataset.project_id, "other.csv", _csv_bytes(rows2), "text/csv")
    other_ds = dataset_service.import_upload(
        db, upload2.id, {"mode": "prelabeled", "columns": {"text": "text", "label": "label", "group_id": "group_id"}}
    )
    foreign = TrainingRun(
        id="run_foreign00000000000000",
        project_id=frozen_dataset.project_id,
        dataset_id=other_ds.id,
        config={},
        split_id=other.id,
    )
    with pytest.raises(RuntimeError, match="不属于"):
        run_service.resolve_run_split(db, foreign)


def test_create_run_fail_fast_when_split_infeasible(db, frozen_dataset, monkeypatch):
    """切分结构性不可行（组数不足）时，Run 创建即刻失败，不再等到 Worker 执行。"""

    def boom(*args, **kwargs):
        raise ApiError("INSUFFICIENT_GROUPS", "语义组数不足以切分", 409)

    monkeypatch.setattr(dataset_service, "create_split", boom)
    with pytest.raises(ApiError) as exc:
        run_service.create_run(db, frozen_dataset.project_id, frozen_dataset.id, "", None)
    assert exc.value.code == "INSUFFICIENT_GROUPS"
    # Run 行未落库
    assert db.query(TrainingRun).filter(TrainingRun.dataset_id == frozen_dataset.id).count() == 0
