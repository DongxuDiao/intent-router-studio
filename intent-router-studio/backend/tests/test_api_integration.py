"""API 集成测试：上传 → 导入 → 校验 → 切分 → Run 生命周期 → 推理错误结构。"""
from __future__ import annotations

import csv
import io


def _csv_bytes(rows: int = 40) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["text", "label", "group_id", "risk_slice", "is_hard_negative"])
    labels = ["information", "read_only", "write_action", "unclear", "oos"]
    for i in range(rows):
        lab = labels[i % 5]
        writer.writerow([f"{lab} 样本问句 {i}", lab, f"{lab}-g{i % 4}", "", "false"])
    return buf.getvalue().encode("utf-8")


def test_full_api_flow(client, project_id):
    # 健康 + 系统
    assert client.get("/api/v1/health").json()["status"] == "ok"
    info = client.get("/api/v1/system/info").json()
    assert "python" in info
    config = client.get("/api/v1/system/config").json()
    assert config["base_model_default"] == "BAAI/bge-small-zh-v1.5"

    # 标签 Schema
    schema = client.get(f"/api/v1/projects/{project_id}/label-schema").json()
    assert [item["key"] for item in schema["labels"]] == [
        "information", "read_only", "write_action", "unclear", "oos",
    ]

    # 上传
    upload_resp = client.post(
        f"/api/v1/projects/{project_id}/uploads",
        files={"file": ("queries.csv", _csv_bytes(), "text/csv")},
    )
    assert upload_resp.status_code == 200, upload_resp.text
    upload = upload_resp.json()
    assert upload["status"] == "PENDING"
    assert "request_id" in upload

    # 预览
    preview = client.get(f"/api/v1/uploads/{upload['id']}/preview").json()
    assert preview["columns"][0] == "text"
    assert preview["row_count"] == 40

    # 导入
    import_resp = client.post(
        f"/api/v1/uploads/{upload['id']}/import",
        json={"mode": "prelabeled", "columns": {"text": "text", "label": "label", "group_id": "group_id"}, "name": "API 测试集"},
    )
    assert import_resp.status_code == 200, import_resp.text
    dataset = import_resp.json()
    assert dataset["status"] == "FROZEN"
    assert dataset["sample_count"] == 40

    # 校验
    report = client.post(f"/api/v1/datasets/{dataset['id']}/validate").json()
    assert report["errors"] == []

    # 切分
    split_resp = client.post(f"/api/v1/datasets/{dataset['id']}/split", json={"seed": 42})
    assert split_resp.status_code == 200
    assert split_resp.json()["stats"]["rows"]["train"] > 0

    # 创建 Run
    run_resp = client.post(
        f"/api/v1/projects/{project_id}/runs",
        json={"dataset_version_id": dataset["id"], "name": "api-run", "config": {"seed": 42}},
    )
    assert run_resp.status_code == 200, run_resp.text
    run = run_resp.json()
    assert run["status"] == "QUEUED"

    # 参数越界被拒
    bad = client.post(
        f"/api/v1/projects/{project_id}/runs",
        json={"dataset_version_id": dataset["id"], "config": {"batch_size": 999}},
    )
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "VALIDATION_ERROR"

    # 未训练完成不能取指标 / 注册模型
    assert client.get(f"/api/v1/runs/{run['id']}/metrics").json()["available"] is False
    reg = client.post(f"/api/v1/runs/{run['id']}/register-model", json={})
    assert reg.status_code == 409

    # 取消排队任务（无 Worker 领取）→ 直接 CANCELLED
    cancel = client.post(f"/api/v1/runs/{run['id']}/cancel")
    assert cancel.json()["status"] == "CANCELLED"

    # 重试 → 新 Run 排队
    retry = client.post(f"/api/v1/runs/{run['id']}/retry")
    assert retry.status_code == 200
    assert retry.json()["parent_run_id"] == run["id"]
    client.post(f"/api/v1/runs/{retry.json()['id']}/cancel")

    # 未激活模型推理 → MODEL_NOT_ACTIVE 错误结构
    pred = client.post(
        "/api/v1/inference/predict",
        json={"project_id": project_id, "text": "帮我创建一个实验"},
    )
    assert pred.status_code == 409
    error = pred.json()["error"]
    assert error["code"] == "MODEL_NOT_ACTIVE"
    assert error["request_id"].startswith("req_")

    # 冻结数据集样本不可改
    samples = client.get(f"/api/v1/datasets/{dataset['id']}/samples").json()
    sample_id = samples["samples"][0]["sample_id"]
    patch = client.patch(f"/api/v1/datasets/{dataset['id']}/samples/{sample_id}", json={"label": "oos"})
    assert patch.status_code == 409
    assert patch.json()["error"]["code"] == "DATASET_IMMUTABLE"

    # 错误的批量大小
    batch = client.post(
        "/api/v1/inference/batch",
        json={"project_id": project_id, "items": [{"text": f"q{i}"} for i in range(1001)]},
    )
    # 无 ACTIVE 模型先报 MODEL_NOT_ACTIVE；有模型才轮到 BATCH_TOO_LARGE
    assert batch.status_code in (409, 422)


def test_sse_events_endpoint(client, project_id, db):
    from app.services import run_service as svc

    run = svc.create_run(db, project_id, _frozen_dataset(client, project_id), "", {"seed": 1})
    svc.append_event(db, run.id, "log", {"level": "INFO", "message": "hello"})
    svc.append_event(db, run.id, "terminal", {"status": "CANCELLED"})

    with client.stream("GET", f"/api/v1/runs/{run.id}/events") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        collected = b""
        for chunk in response.iter_bytes():
            collected += chunk
    text = collected.decode("utf-8")
    assert "event: log" in text
    assert "event: terminal" in text
    assert "id: 1" in text


def _frozen_dataset(client, project_id) -> str:
    upload = client.post(
        f"/api/v1/projects/{project_id}/uploads",
        files={"file": ("seed.csv", _csv_bytes(40), "text/csv")},
    ).json()
    dataset = client.post(
        f"/api/v1/uploads/{upload['id']}/import",
        json={"mode": "prelabeled", "columns": {"text": "text", "label": "label", "group_id": "group_id"}},
    ).json()
    return dataset["id"]
