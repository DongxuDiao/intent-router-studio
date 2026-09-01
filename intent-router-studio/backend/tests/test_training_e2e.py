"""真实训练 Run E2E（Review 修复 §10.3）。

三分类 Schema（faq/status_query/create_task）走完整 RunExecutor 到 SUCCEEDED：
发布 Schema → 导入（断言 schema_id 绑定）→ 校验/切分 → Run（stub trainer，
跳过真实 SetFit 微调，其余链路全真）→ 阈值搜索 → intent/effect 两层指标 →
label_schema.json 打包校验 → 注册 → 激活 → 在线预测 create_task 走写阈值与确认门。

对照 §10.4 回归：旧五分类全量测试、阈值搜索数值一致性、改写安全用例、
项目删除级联、Alembic upgrade/downgrade 均由既有测试文件覆盖。
"""
from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest

from app.constants import RunStatus
from app.db import SessionLocal
from app.models import DatasetVersion, TrainingRun
from app.router_core.training import TrainedRouter
from app.worker import queue
from app.worker.run_executor import RunExecutor

LABELS = ["faq", "status_query", "create_task"]


# ---------------------------------------------------------------- 桩训练器

class _SeparableStubModel:
    """按关键词给高置信分布的桩模型（列数 = 传入标签表）。

    命中类 0.92、其余 0.04，保证阈值搜索可行且
    create_task（write_action）需要写入专用阈值才能被接受。
    """

    def __init__(self, keys: list[str] | None = None) -> None:
        self.keys = list(keys or LABELS)

    def _match(self, text: str) -> str:
        if "创建" in text or "部署" in text:
            return "create_task" if "create_task" in self.keys else "write_action"
        if "状态" in text or "查" in text:
            return "status_query" if "status_query" in self.keys else "read_only"
        if "删除" in text or "停止" in text:
            return "write_action"
        if "天气" in text:
            return "oos"
        if "那个" in text or "算了" in text:
            return "unclear"
        return "faq" if "faq" in self.keys else "information"

    def predict_proba(self, texts: list[str]) -> np.ndarray:
        rows = []
        for t in texts:
            row = np.full(len(self.keys), 0.04)
            hit = self._match(t)
            row[self.keys.index(hit) if hit in self.keys else 0] = 0.92
            rows.append(row)
        return np.array(rows)

    def save_pretrained(self, path: str) -> None:
        target = Path(path)
        target.mkdir(parents=True, exist_ok=True)
        (target / "model.safetensors").write_bytes(b"stub-weights-e2e")
        (target / "config.json").write_text('{"model_type": "stub"}', encoding="utf-8")


def _stub_train_router(config, train_texts, train_labels, workdir, progress_cb=None, label_order=None):
    """替换 app.router_core.training.train_router：保留 label_order 契约。"""
    if progress_cb:
        progress_cb(0.5, "stub 训练完成")
    return TrainedRouter(_SeparableStubModel(list(label_order or LABELS)), list(label_order or LABELS))


def _stub_five_class_loader():
    """按制品 label_schema.json 标签表构造桩模型（注册后激活加载用）。"""
    def _load(path: str):
        keys = json.loads((Path(path).parent / "label_schema.json").read_text(encoding="utf-8"))["labels"]
        return _SeparableStubModel(keys)

    return _load


# ---------------------------------------------------------------- 辅助

def _claim_until(db, worker_id: str, run_id: str):
    """领取队首任务直到拿到目标 Run。

    全量套件共用一个测试库，更早执行的测试可能遗留 QUEUED 任务
    （如 test_project_delete 的 run_busy）；先将其转成 INTERRUPTED
    移出队列，避免 claim_next_run 按创建时间一直领到别人的任务。
    """
    for _ in range(64):
        claimed = queue.claim_next_run(db, worker_id)
        if claimed is None:
            raise AssertionError("队列已空，目标 Run 未被领取")
        if claimed.id == run_id:
            return claimed
        queue.transition_status(
            db,
            claimed.id,
            [RunStatus.PREPARING],
            RunStatus.INTERRUPTED,
            error={"code": "TEST_QUEUE_DRAIN", "message": "清理其他测试遗留的队列任务"},
            finished_at=datetime.now(UTC).isoformat(),
        )
    raise AssertionError("队列清理次数超限")


def _publish_three_class(client, project_id) -> str:
    labels = [
        {"key": "faq", "name": "常见问题", "effect_type": "information", "order": 0},
        {"key": "status_query", "name": "查询状态", "effect_type": "read_only", "order": 10},
        {"key": "create_task", "name": "创建任务", "effect_type": "write_action", "order": 20},
    ]
    draft = client.post(
        f"/api/v1/projects/{project_id}/label-schemas/drafts", json={"change_summary": "三分类 E2E"}
    ).json()
    assert client.patch(
        f"/api/v1/projects/{project_id}/label-schemas/{draft['id']}",
        json={"expected_hash": draft["hash"], "labels": labels},
    ).status_code == 200
    current = client.get(f"/api/v1/projects/{project_id}/label-schemas/{draft['id']}").json()
    published = client.post(
        f"/api/v1/projects/{project_id}/label-schemas/{draft['id']}/publish",
        json={"expected_hash": current["hash"], "confirm_breaking_changes": True},
    )
    assert published.status_code == 200, published.text
    return published.json()["id"]


def _three_class_csv(rows_per_class: int = 15) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["text", "label", "group_id"])
    for i in range(rows_per_class):
        writer.writerow([f"什么是实验平台 {i}", "faq", f"faq-g{i % 4}"])
        writer.writerow([f"查一下任务 {i} 的状态", "status_query", f"st-g{i % 4}"])
        writer.writerow([f"帮我创建任务 T{i}", "create_task", f"ct-g{i % 4}"])
    return buf.getvalue().encode("utf-8")


def _five_class_csv(rows_per_class: int = 10) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["text", "label", "group_id"])
    rows = [
        ("什么是实验平台 {i}", "information"),
        ("查一下任务 {i} 的状态", "read_only"),
        ("帮我创建任务 T{i}", "write_action"),
        ("这个…那个…算了 {i}", "unclear"),
        ("今天天气怎么样 {i}", "oos"),
    ]
    for i in range(rows_per_class):
        for text, label in rows:
            writer.writerow([text.format(i=i), label, f"{label}-g{i % 3}"])
    return buf.getvalue().encode("utf-8")


# ---------------------------------------------------------------- E2E

def test_three_class_full_run_e2e(client, project_id, db, monkeypatch):
    monkeypatch.setattr("app.router_core.training.train_router", _stub_train_router)
    monkeypatch.setattr("setfit.SetFitModel.from_pretrained", lambda path: _SeparableStubModel())

    # 1. 发布三分类 Schema 并导入（每类 15 条，满足切分与训练要求）
    schema_id = _publish_three_class(client, project_id)
    upload = client.post(
        f"/api/v1/projects/{project_id}/uploads",
        files={"file": ("three.csv", io.BytesIO(_three_class_csv()), "text/csv")},
    ).json()
    imported = client.post(
        f"/api/v1/uploads/{upload['id']}/import",
        json={
            "mode": "prelabeled",
            "columns": {"text": "text", "label": "label", "group_id": "group_id"},
            "name": "三分类 E2E 数据集",
        },
    )
    assert imported.status_code == 200, imported.text
    dataset_id = imported.json()["id"]

    # 2. 数据集绑定 Schema（Review 修复 §3.1）
    dataset = db.get(DatasetVersion, dataset_id)
    assert dataset.schema_id == schema_id
    assert dataset.manifest["label_order"] == LABELS

    # 3. 校验 + 切分
    assert client.post(f"/api/v1/datasets/{dataset_id}/validate").json()["errors"] == []
    split = client.post(f"/api/v1/datasets/{dataset_id}/split", json={"seed": 42})
    assert split.status_code == 200, split.text
    assert split.json()["stats"]["rows"]["train"] > 0

    # 4. 创建并完整执行 Run（真实 RunExecutor，仅训练器为桩）
    run_resp = client.post(
        f"/api/v1/projects/{project_id}/runs",
        json={"dataset_version_id": dataset_id, "name": "e2e-run"},
    )
    assert run_resp.status_code == 200, run_resp.text
    run_id = run_resp.json()["id"]

    _claim_until(db, "e2e-worker", run_id)
    final = RunExecutor("e2e-worker", lambda: False).execute(run_id)
    assert final == RunStatus.SUCCEEDED, f"Run 未成功: {final}"

    # 5. 制品与指标
    db.rollback()  # 结束本会话只读快照，读取 Executor 提交后的最新行
    run = db.get(TrainingRun, run_id)
    art = Path(run.artifacts_dir)
    schema_payload = json.loads((art / "label_schema.json").read_text(encoding="utf-8"))
    assert schema_payload["schema_id"] == schema_id
    assert schema_payload["labels"] == LABELS  # 分类头顺序
    assert {d["key"]: d["effect_type"] for d in schema_payload["label_definitions"]} == {
        "faq": "information", "status_query": "read_only", "create_task": "write_action",
    }
    assert len(schema_payload["schema_hash"]) == 64

    metrics = client.get(f"/api/v1/runs/{run_id}/metrics").json()
    assert metrics["schema_id"] == schema_id
    assert metrics["label_order"] == LABELS
    # intent / effect 两层指标并存（Review 修复 §6.3）
    test_eval = metrics["test"]
    assert test_eval["classification"]["confusion_matrix"]["labels"] == LABELS
    effects = test_eval["effects"]
    assert effects["classification"]["accuracy"] == pytest.approx(1.0, abs=1e-6)
    assert effects["false_write_count"] == 0
    assert "effect_safe_coverage" in test_eval["routing"]
    assert test_eval["routing"]["route_counts"]["write_action"] > 0

    # 6. 注册 → 激活 → 在线预测
    model = client.post(f"/api/v1/runs/{run_id}/register-model", json={})
    assert model.status_code == 200, model.text
    model_id = model.json()["id"]
    activated = client.post(f"/api/v1/models/{model_id}/activate")
    assert activated.status_code == 200, activated.text

    write_pred = client.post(
        "/api/v1/inference/predict",
        json={"project_id": project_id, "text": "帮我创建任务 T99"},
    ).json()
    assert write_pred["intent"] == {"key": "create_task", "name": "创建任务"}
    assert write_pred["route"] == "write_action" and write_pred["effect_type"] == "write_action"
    assert write_pred["decision"] == "accept"  # 0.92 越过写入专用阈值
    assert write_pred["effect_ceiling"] == "external_write_candidate"
    assert write_pred["required_next_gate"] == "skill_match_and_confirmation"
    assert write_pred["schema_id"] == schema_id

    read_pred = client.post(
        "/api/v1/inference/predict",
        json={"project_id": project_id, "text": "查一下任务 7 的状态"},
    ).json()
    assert read_pred["intent"]["key"] == "status_query"
    assert read_pred["route"] == "read_only"
    assert read_pred["required_next_gate"] == "readonly_skill_match"


def test_five_class_compat_run_still_works(client, project_id, db, monkeypatch):
    """§10.4：旧五分类（恒等 Schema）走同一 RunExecutor 链路不受影响。"""
    from app.router_core.taxonomy import LABELS as FIVE

    monkeypatch.setattr("app.router_core.training.train_router", _stub_train_router)
    monkeypatch.setattr("setfit.SetFitModel.from_pretrained", _stub_five_class_loader())

    upload = client.post(
        f"/api/v1/projects/{project_id}/uploads",
        files={"file": ("five.csv", io.BytesIO(_five_class_csv()), "text/csv")},
    ).json()
    imported = client.post(
        f"/api/v1/uploads/{upload['id']}/import",
        json={
            "mode": "prelabeled",
            "columns": {"text": "text", "label": "label", "group_id": "group_id"},
        },
    )
    assert imported.status_code == 200, imported.text
    dataset_id = imported.json()["id"]

    assert client.post(f"/api/v1/datasets/{dataset_id}/split", json={"seed": 7}).status_code == 200
    run_id = client.post(
        f"/api/v1/projects/{project_id}/runs",
        json={"dataset_version_id": dataset_id, "name": "compat-run"},
    ).json()["id"]
    db2 = SessionLocal()
    try:
        _claim_until(db2, "compat-worker", run_id)
    finally:
        db2.close()
    assert RunExecutor("compat-worker", lambda: False).execute(run_id) == RunStatus.SUCCEEDED

    db.rollback()
    run = db.get(TrainingRun, run_id)
    schema_payload = json.loads((Path(run.artifacts_dir) / "label_schema.json").read_text(encoding="utf-8"))
    # 默认兼容 Schema 也是 v2 制品（恒等五分类），加载校验完整通过
    assert schema_payload["labels"] == list(FIVE)
    assert {d["key"]: d["effect_type"] for d in schema_payload["label_definitions"]} == {
        k: k for k in FIVE
    }


# ---------------------------------------------------------------- 类数扩展冒烟（§12 验收）

def _publish_n_class(client, project_id, n: int) -> tuple[str, list[dict]]:
    """发布 n 分类 Schema：意图键 ext_00..ext_{n-1}，效果按余数轮转。"""
    effects = ["information", "read_only", "write_action", "unclear", "oos"]
    labels = [
        {"key": f"ext_{i:02d}", "name": f"意图 {i}", "effect_type": effects[i % 5], "order": i * 10}
        for i in range(n)
    ]
    draft = client.post(
        f"/api/v1/projects/{project_id}/label-schemas/drafts", json={"change_summary": f"{n} 分类冒烟"}
    ).json()
    assert client.patch(
        f"/api/v1/projects/{project_id}/label-schemas/{draft['id']}",
        json={"expected_hash": draft["hash"], "labels": labels},
    ).status_code == 200
    current = client.get(f"/api/v1/projects/{project_id}/label-schemas/{draft['id']}").json()
    published = client.post(
        f"/api/v1/projects/{project_id}/label-schemas/{draft['id']}/publish",
        json={"expected_hash": current["hash"], "confirm_breaking_changes": True},
    )
    assert published.status_code == 200, published.text
    return published.json()["id"], labels


class _IndexStubModel:
    """按文本中「意图N」序号命中的桩模型：任意类数下预测与真值一致。"""

    def __init__(self, keys: list[str]) -> None:
        self.keys = list(keys)

    def predict_proba(self, texts: list[str]) -> np.ndarray:
        import re

        rows = []
        for t in texts:
            m = re.search(r"意图(\d+)", t)
            idx = min(int(m.group(1)), len(self.keys) - 1) if m else 0
            row = np.full(len(self.keys), 0.04)
            row[idx] = 0.92
            rows.append(row)
        return np.array(rows)

    def save_pretrained(self, path: str) -> None:
        target = Path(path)
        target.mkdir(parents=True, exist_ok=True)
        (target / "model.safetensors").write_bytes(b"stub-weights-smoke")
        (target / "config.json").write_text('{"model_type": "stub"}', encoding="utf-8")


def _index_train_router(config, train_texts, train_labels, workdir, progress_cb=None, label_order=None):
    return TrainedRouter(_IndexStubModel(list(label_order)), list(label_order))


def _index_loader():
    def _load(path: str):
        keys = json.loads((Path(path).parent / "label_schema.json").read_text(encoding="utf-8"))["labels"]
        return _IndexStubModel(keys)

    return _load


@pytest.mark.parametrize("n", [2, 7, 20])
def test_n_class_run_smoke(client, project_id, db, monkeypatch, n):
    """2/7/20 分类 Schema 均可完整训练到 SUCCEEDED 并产出可加载制品。"""
    monkeypatch.setattr("app.router_core.training.train_router", _index_train_router)
    monkeypatch.setattr("setfit.SetFitModel.from_pretrained", _index_loader())

    schema_id, labels = _publish_n_class(client, project_id, n)
    keys = [item["key"] for item in labels]

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["text", "label", "group_id"])
    for i in range(6):
        for j, key in enumerate(keys):
            writer.writerow([f"意图{j}的样本 {i}", key, f"g{j}-{i % 3}"])
    upload = client.post(
        f"/api/v1/projects/{project_id}/uploads",
        files={"file": (f"n{n}.csv", io.BytesIO(buf.getvalue().encode("utf-8")), "text/csv")},
    ).json()
    imported = client.post(
        f"/api/v1/uploads/{upload['id']}/import",
        json={"mode": "prelabeled", "columns": {"text": "text", "label": "label", "group_id": "group_id"}},
    )
    assert imported.status_code == 200, imported.text
    dataset_id = imported.json()["id"]
    assert db.get(DatasetVersion, dataset_id).schema_id == schema_id

    assert client.post(f"/api/v1/datasets/{dataset_id}/split", json={"seed": 42}).status_code == 200
    run_id = client.post(
        f"/api/v1/projects/{project_id}/runs",
        json={"dataset_version_id": dataset_id, "name": f"smoke-{n}"},
    ).json()["id"]
    db2 = SessionLocal()
    try:
        _claim_until(db2, f"smoke-{n}", run_id)
    finally:
        db2.close()
    assert RunExecutor(f"smoke-{n}", lambda: False).execute(run_id) == RunStatus.SUCCEEDED

    db.rollback()
    run = db.get(TrainingRun, run_id)
    payload = json.loads((Path(run.artifacts_dir) / "label_schema.json").read_text(encoding="utf-8"))
    assert payload["labels"] == keys  # 分类头维度 = Schema 类数
    metrics = json.loads((Path(run.artifacts_dir) / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["schema_id"] == schema_id
    assert len(metrics["label_order"]) == n
