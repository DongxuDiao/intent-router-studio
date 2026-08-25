#!/usr/bin/env python3
"""回归 Smoke：四项代码审查修复的端到端验证（修改方案 §8）。

覆盖：
  1. 小样本分组切分：合法数据三 split 全非空且五类全覆盖；组数不足被结构化阻断
  2. 制品完整性：篡改 setfit_model/ 权重后显式版本加载被拒，且不影响 ACTIVE 服务
  3. Debug / 普通推理缓存隔离：命中缓存的请求仍按本次参数输出 debug
  4. 阈值搜索指标：n_feasible / n_retained_candidates 语义正确

用法:
  python scripts/smoke_regression.py --base-url http://127.0.0.1:8000/api/v1
"""
from __future__ import annotations

import argparse
import io
import sys
import time
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[1]
DEFAULT_CSV = REPO / "examples" / "queries.csv"
LABELS = ["information", "read_only", "write_action", "unclear", "oos"]


def fail(message: str) -> None:
    print(f"❌ {message}")
    sys.exit(1)


def csv_bytes(rows: list[dict], header: list[str]) -> bytes:
    import csv

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=header)
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8-sig")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000/api/v1")
    parser.add_argument("--csv", default=str(DEFAULT_CSV))
    parser.add_argument("--project-name", default=f"smoke-regression-{int(time.time())}")
    parser.add_argument("--epochs", type=int, default=2)
    parser.add_argument("--iterations", type=int, default=8)
    parser.add_argument("--timeout-minutes", type=int, default=45)
    args = parser.parse_args()

    client = httpx.Client(base_url=args.base_url, timeout=120.0)

    # ---------- 0. 健康 ----------
    health = client.get("/health")
    assert health.status_code == 200, health.text
    print("✅ API 健康")

    # ---------- 1. 项目 + 数据集 ----------
    project = client.post("/projects", json={"name": args.project_name, "description": "回归 smoke"}).json()
    project_id = project["id"]
    csv_path = Path(args.csv)
    upload = client.post(
        f"/projects/{project_id}/uploads",
        files={"file": (csv_path.name, csv_path.read_bytes(), "text/csv")},
    )
    assert upload.status_code == 200, upload.text
    dataset = client.post(
        f"/uploads/{upload.json()['id']}/import",
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
            "name": "回归数据集",
        },
    ).json()
    assert dataset["status"] == "FROZEN", dataset
    print(f"✅ 数据集 {dataset['id']} 样本 {dataset['sample_count']}")

    # ---------- 2. 切分（Fix 1）：全 split 非空 + 五类覆盖 ----------
    split = client.post(f"/datasets/{dataset['id']}/split", json={"seed": 42})
    assert split.status_code == 200, split.text
    stats = split.json()["stats"]
    rows = stats["rows"]
    assert all(rows[s] > 0 for s in ("train", "validation", "test")), f"存在空 split: {rows}"
    for part in ("validation", "test"):
        covered = set(stats[f"{part}_label_distribution"])
        missing = set(LABELS) - covered
        assert not missing, f"{part} 缺少类别: {missing}"
    assert stats.get("risk_test_rows", 0) >= 0
    print(f"✅ 切分 train/val/test = {rows['train']}/{rows['validation']}/{rows['test']}，五类全覆盖，risk_test {stats.get('risk_test_rows')}")

    # ---------- 3. 切分阻断（Fix 1）：每类仅 2 组 ----------
    header = ["text", "label", "group_id", "risk_slice", "is_hard_negative"]
    few_rows = [
        {"text": f"样本 {lab} {g} {i}", "label": lab, "group_id": f"{lab}-g{g}", "risk_slice": "", "is_hard_negative": "false"}
        for lab in LABELS
        for g in range(2)
        for i in range(4)
    ]
    few_upload = client.post(
        f"/projects/{project_id}/uploads",
        files={"file": ("few-groups.csv", csv_bytes(few_rows, header), "text/csv")},
    )
    few_dataset = client.post(
        f"/uploads/{few_upload.json()['id']}/import",
        json={"mode": "prelabeled", "columns": {"text": "text", "label": "label", "group_id": "group_id"}, "name": "两组数据"},
    ).json()
    blocked = client.post(f"/datasets/{few_dataset['id']}/split", json={"seed": 42})
    assert blocked.status_code == 422, blocked.text
    err = blocked.json()["error"]
    assert err["code"] == "INSUFFICIENT_SPLIT_GROUPS", err
    assert err["details"]["group_count"] == 2 and err["details"]["required_group_count"] == 3, err
    print(f"✅ 组数不足被阻断: {err['code']} details={err['details']}")

    # ---------- 4. 训练 ----------
    run = client.post(
        f"/projects/{project_id}/runs",
        json={
            "dataset_version_id": dataset["id"],
            "name": "smoke-regression",
            "config": {"seed": 42, "num_epochs": args.epochs, "num_iterations": args.iterations, "batch_size": 16},
        },
    )
    assert run.status_code == 200, run.text
    run_id = run.json()["id"]
    print(f"⏳ Run {run_id} 训练中...")
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
    print("✅ 训练成功")

    # ---------- 5. 阈值搜索指标（Fix 4）----------
    metrics = client.get(f"/runs/{run_id}/metrics").json()
    ts = metrics.get("threshold_search") or {}
    assert ts.get("n_candidates", 0) > 0, ts
    assert isinstance(ts.get("n_feasible"), int) and ts["n_feasible"] >= (1 if ts.get("feasible") else 0), ts
    assert isinstance(ts.get("n_retained_candidates"), int), f"缺少 n_retained_candidates: {sorted(ts)}"
    assert 0 <= ts["n_retained_candidates"] <= ts["n_feasible"] <= ts["n_candidates"], ts
    print(f"✅ 阈值搜索: 可行 {ts['n_feasible']}/{ts['n_candidates']}，保留候选 {ts['n_retained_candidates']}，pareto {len(ts.get('pareto', []))}")

    # ---------- 6. 注册 + 激活 ----------
    model = client.post(f"/runs/{run_id}/register-model", json={"name": "regression-model"})
    assert model.status_code == 200, model.text
    model_id = model.json()["id"]
    activated = client.post(f"/models/{model_id}/activate")
    assert activated.status_code == 200, activated.text
    print(f"✅ 模型 {model_id} 已激活")

    # ---------- 7. 缓存隔离（Fix 3）----------
    probe_text = "查一下实验 42 的状态"
    r1 = client.post("/inference/predict", json={"project_id": project_id, "text": probe_text}).json()
    assert "debug" not in r1 and "cache_hit" not in r1, r1
    r2 = client.post("/inference/predict", json={"project_id": project_id, "text": probe_text, "debug": True}).json()
    assert r2.get("cache_hit") is True and "debug" in r2 and "thresholds_applied" in r2["debug"], r2
    r3 = client.post("/inference/predict", json={"project_id": project_id, "text": probe_text}).json()
    assert r3.get("cache_hit") is True and "debug" not in r3, r3
    ov_text = "帮我删除实验 7"
    r4 = client.post(
        "/inference/predict",
        json={"project_id": project_id, "text": ov_text, "threshold_overrides": {"write_min_confidence": 0.99}},
    ).json()
    r5 = client.post("/inference/predict", json={"project_id": project_id, "text": ov_text}).json()
    assert "cache_hit" not in r4 and "cache_hit" not in r5, "覆盖阈值请求不得读写共享缓存"
    print("✅ 缓存隔离: 普通请求命中缓存仍可附 debug，覆盖阈值请求绕过缓存")

    # ---------- 8. 制品篡改（Fix 2）----------
    # 按 §8 篡改「复制的制品」：再注册一个模型得到独立制品目录。绝不改动
    # 已加载制品：truncate/rewrite 会切断运行时对 safetensors 的 mmap 映射
    # （SIGBUS 静默杀进程），rename 替换在 Docker bind mount 上有目录项传播
    # 延迟（容器短暂 ENOENT）。副本从未被任何运行时加载，两种风险都不存在。
    copy = client.post(f"/runs/{run_id}/register-model", json={"name": "tamper-target"})
    assert copy.status_code == 200, copy.text
    copy_id = copy.json()["id"]
    copy_dir = REPO / "var" / "models" / copy_id
    weight_files = sorted((copy_dir / "setfit_model").glob("*.safetensors"))
    assert weight_files, f"找不到权重文件: {copy_dir}"
    weight_file = weight_files[0]
    original = weight_file.read_bytes()
    try:
        weight_file.write_bytes(original + b"\x00tampered")
        tampered = client.post("/inference/predict", json={"project_id": project_id, "text": "查询实验", "model_version_id": copy_id})
        assert tampered.status_code == 409, tampered.text
        terr = tampered.json()["error"]
        assert terr["code"] == "HASH_MISMATCH", terr
        assert any("setfit_model" in p for p in terr["details"].get("problems", [])), terr
        print(f"✅ 篡改复制制品后显式版本加载被拒: {terr['code']} problems={terr['details']['problems'][:2]}")
        still = client.post("/inference/predict", json={"project_id": project_id, "text": probe_text})
        assert still.status_code == 200, f"ACTIVE 服务受影响: {still.text}"
        print("✅ 校验失败不影响 ACTIVE 模型服务")
    finally:
        weight_file.write_bytes(original)
    restored = client.post("/inference/predict", json={"project_id": project_id, "text": "查询实验恢复", "model_version_id": copy_id})
    assert restored.status_code == 200, restored.text
    print("✅ 恢复权重后显式版本加载正常")

    # ---------- 9. 批量 + A-B ----------
    batch = client.post("/inference/batch", json={"project_id": project_id, "items": [{"text": "查询实验"}, {"text": "帮我撤回 Review 9"}]})
    assert batch.status_code == 200 and len(batch.json()["results"]) == 2, batch.text
    compare = client.post(
        "/inference/compare",
        json={"project_id": project_id, "text": "查一下实验 42 的状态", "model_a": model_id, "model_b": copy_id},
    )
    assert compare.status_code == 200 and "diff" in compare.json(), compare.text
    print("✅ 批量推理与 A-B 对比正常")

    print("\n🎉 回归 smoke 全流程通过")


if __name__ == "__main__":
    main()
