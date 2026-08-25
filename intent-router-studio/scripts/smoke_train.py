#!/usr/bin/env python3
"""Smoke Train：从上传到 Playground 的端到端闭环验证（设计文档 16.4）。

用法:
  python scripts/smoke_train.py --base-url http://127.0.0.1:8000/api/v1 [--csv examples/queries.csv]
  可选: --epochs 2 --iterations 8（快速档）
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[1]
DEFAULT_CSV = REPO / "examples" / "queries.csv"

PROBE_QUERIES = [
    {"text": "Libra 怎么创建实验？", "expect_route": "information"},
    {"text": "查一下实验 123 的状态", "expect_route": "read_only"},
    {"text": "帮我撤回 Review 123", "expect_route": "write_action"},
    {"text": "帮我预订会议室", "expect_route": "oos"},
]


def fail(message: str) -> None:
    print(f"❌ {message}")
    sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000/api/v1")
    parser.add_argument("--csv", default=str(DEFAULT_CSV))
    parser.add_argument("--project-name", default="smoke-train-project")
    parser.add_argument("--epochs", type=int, default=2)
    parser.add_argument("--iterations", type=int, default=8)
    parser.add_argument("--timeout-minutes", type=int, default=45)
    args = parser.parse_args()

    client = httpx.Client(base_url=args.base_url, timeout=60.0)

    # 0. 健康
    health = client.get("/health")
    assert health.status_code == 200, health.text
    print("✅ API 健康")

    # 1. 项目
    projects = client.get("/projects").json()["items"]
    project = next((p for p in projects if p["name"] == args.project_name), None)
    if project is None:
        project = client.post("/projects", json={"name": args.project_name, "description": "smoke train"}).json()
    project_id = project["id"]
    print(f"✅ 项目 {project['name']} ({project_id})")

    # 2. 上传
    csv_path = Path(args.csv)
    if not csv_path.is_file():
        fail(f"示例数据不存在: {csv_path}（先运行 python scripts/make_example_data.py）")
    upload = client.post(
        f"/projects/{project_id}/uploads",
        files={"file": (csv_path.name, csv_path.read_bytes(), "text/csv")},
    )
    if upload.status_code != 200:
        fail(f"上传失败: {upload.text}")
    upload_id = upload.json()["id"]
    print(f"✅ 上传 {csv_path.name} -> {upload_id}")

    preview = client.get(f"/uploads/{upload_id}/preview").json()
    print(f"✅ 预览: {preview['row_count']} 行, 列 {preview['columns'][:4]}")

    # 3. 导入
    imported = client.post(
        f"/uploads/{upload_id}/import",
        json={
            "mode": "prelabeled",
            "columns": {
                "text": "text",
                "label": "label",
                "group_id": "group_id",
                "context": "context",
                "risk_slice": "risk_slice",
                "is_hard_negative": "is_hard_negative",
            },
            "name": "smoke 数据集",
        },
    )
    if imported.status_code != 200:
        fail(f"导入失败: {imported.text}")
    dataset = imported.json()
    assert dataset["status"] == "FROZEN", dataset
    errors = (dataset.get("quality_report") or {}).get("errors", [])
    assert not errors, f"数据质量错误: {errors}"
    print(f"✅ 导入 -> {dataset['id']} 样本 {dataset['sample_count']} 分布 {dataset['label_distribution']}")

    # 4. 切分
    split = client.post(f"/datasets/{dataset['id']}/split", json={"seed": 42})
    assert split.status_code == 200, split.text
    print(f"✅ 切分: {split.json()['stats']['rows']}")

    # 5. 训练
    run = client.post(
        f"/projects/{project_id}/runs",
        json={
            "dataset_version_id": dataset["id"],
            "name": "smoke-train",
            "config": {
                "seed": 42,
                "num_epochs": args.epochs,
                "num_iterations": args.iterations,
                "batch_size": 16,
            },
        },
    )
    if run.status_code != 200:
        fail(f"创建 Run 失败: {run.text}")
    run_id = run.json()["id"]
    print(f"⏳ Run {run_id} 已排队，等待训练完成...")

    deadline = time.time() + args.timeout_minutes * 60
    terminal = None
    while time.time() < deadline:
        detail = client.get(f"/runs/{run_id}").json()
        if detail["status"] in ("SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED"):
            terminal = detail
            break
        print(f"   status={detail['status']} stage={detail['stage']} progress={detail['progress']:.0f}%", flush=True)
        time.sleep(10)
    if terminal is None:
        fail(f"等待训练超时（{args.timeout_minutes} 分钟）")
    if terminal["status"] != "SUCCEEDED":
        fail(f"训练未成功: {terminal['status']} error={terminal.get('error')}")
    print(f"✅ 训练成功 progress={terminal['progress']}")

    # 6. 指标
    metrics = client.get(f"/runs/{run_id}/metrics").json()
    test = metrics.get("test", {})
    print("✅ 测试指标:", json.dumps({
        "macro_f1": test.get("classification", {}).get("macro_f1"),
        "false_write_rate": test.get("routing", {}).get("false_write_rate"),
        "safe_coverage": test.get("routing", {}).get("safe_coverage"),
        "thresholds": metrics.get("thresholds"),
    }, ensure_ascii=False))

    # 7. 注册 + 激活
    model = client.post(f"/runs/{run_id}/register-model", json={"name": "smoke-model"})
    if model.status_code != 200:
        fail(f"注册失败: {model.text}")
    model_id = model.json()["id"]
    print(f"✅ 注册模型 {model_id} ({model.json()['status']})")

    activated = client.post(f"/models/{model_id}/activate")
    if activated.status_code != 200:
        fail(f"激活失败: {activated.text}")
    print(f"✅ 激活模型 {model_id}")

    # 8. Playground 验证
    all_ok = True
    for probe in PROBE_QUERIES:
        resp = client.post("/inference/predict", json={"project_id": project_id, "text": probe["text"], "debug": True})
        if resp.status_code != 200:
            fail(f"推理失败 [{probe['text']}]: {resp.text}")
        result = resp.json()
        route_ok = result["route"] == probe["expect_route"]
        all_ok = all_ok and route_ok
        mark = "✅" if route_ok else "⚠️ "
        print(
            f"{mark} [{probe['text']}] route={result['route']} decision={result['decision']} "
            f"conf={result['confidence']} margin={result['margin']} latency={result['latency_ms']}ms"
        )
        for key in ("request_id", "model_version", "top_k", "effect_ceiling", "required_next_gate", "reason_codes", "latency_ms"):
            assert key in result, f"推理契约缺少字段 {key}"

    # 9. Playground 反馈
    feedback = client.post(
        f"/projects/{project_id}/playground-cases",
        json={"text": "帮我撤回 Review 123", "expected_label": "write_action", "predicted_route": "write_action", "save_text": True},
    )
    assert feedback.status_code == 200, feedback.text
    print("✅ Playground 反馈已保存（含原文，用户显式勾选）")

    print("\n🎉 Smoke train 全流程通过" + ("" if all_ok else "（个别 probe 路由与预期不同，属正常模型波动）"))


if __name__ == "__main__":
    main()
