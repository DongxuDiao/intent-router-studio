#!/usr/bin/env python3
"""Query 改写 E2E Smoke（修改方案 §16.4 / §20 DoD）。

前置：compose 栈运行中，且已存在带 ACTIVE 模型的项目
（可先跑 scripts/smoke_regression.py 或 smoke_train.py 产生）。

覆盖：
  1. rewriter 健康透传 + 指标
  2. L0 术语归一（normalize_only，不依赖生成服务）
  3. shadow 双路结构不变量（final_route 恒为原文路由；下游用原文）
  4. safe_apply 不变量（采用与否都不得改变正式路由）
  5. /predict 兼容（不带 rewrite = 无 query_understanding；带 = 路由逐字段一致）
  6. 批量 ≤100 逐条降级
  7. 反馈默认只落哈希
  8. 超时降级 + rewriter 停机降级（绝不 5xx）+ 恢复
  9. off 模式一键回退 + 收尾恢复 shadow

用法:
  python scripts/smoke_rewrite.py --base-url http://127.0.0.1:8000/api/v1

容器内运行（停机检查需挂载 docker socket）：
  docker run --rm --network host -v "$PWD:/repo" \
    -v /var/run/docker.sock:/var/run/docker.sock -w /repo \
    intent-router-studio:latest python scripts/smoke_rewrite.py
（无 docker CLI 且无 socket 时自动跳过停机降级检查）
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[1]
REWRITER_CONTAINER = os.environ.get("REWRITER_CONTAINER", "irs-rewriter")


def fail(message: str) -> None:
    print(f"❌ {message}")
    sys.exit(1)


def compose(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "compose", *args], cwd=REPO, check=check,
        capture_output=True, text=True,
    )


def engine_api(method: str, path: str) -> bool:
    """通过 Docker Engine API（unix socket）控制 rewriter 容器。"""
    sock = os.environ.get("DOCKER_HOST", "").replace("unix://", "") or "/var/run/docker.sock"
    if not Path(sock).exists():
        return False
    try:
        with httpx.Client(transport=httpx.HTTPTransport(uds=sock), timeout=30.0) as c:
            resp = c.request(method, f"http://localhost{path}")
            return resp.status_code in (200, 204, 304)
    except Exception:
        return False


def stop_rewriter() -> bool:
    if shutil.which("docker") and compose("stop", "rewriter", check=False).returncode == 0:
        return True
    return engine_api("POST", f"/containers/{REWRITER_CONTAINER}/stop?t=10")


def start_rewriter() -> bool:
    if shutil.which("docker") and compose("start", "rewriter", check=False).returncode == 0:
        return True
    return engine_api("POST", f"/containers/{REWRITER_CONTAINER}/start")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000/api/v1")
    parser.add_argument("--rewriter-wait-seconds", type=int, default=240)
    args = parser.parse_args()

    client = httpx.Client(base_url=args.base_url, timeout=150.0)  # 覆盖 CPU 慢速档 ~75s 改写

    def put_config(**config):
        # timeout_ms=110000：本机 CPU 生成实测一条 JSON 改写 ~50-75s；
        # 产品的 5s 默认值面向 GPU 部署，CPU 环境用慢速档跑通功能链路
        body = {
            "mode": "shadow", "provider": "local_qwen", "model_id": "Qwen/Qwen3-0.6B",
            "max_context_chars": 4000, "max_new_tokens": 256, "timeout_ms": 110000,
            "min_rewrite_confidence": 0.8, "require_route_consistency": True,
            "fallback": "original", "store_raw_text": False,
        }
        body.update(config)
        resp = client.put(f"/projects/{PROJECT_ID}/rewrite-config", json={"config": body})
        assert resp.status_code == 200, resp.text
        return resp.json()

    def rewrite(text, context=None, mode=None, retries=1):
        """CPU 生成 ~50-75s 贴近超时上限，TIMEOUT 降级时重试一次（失败不缓存）。"""
        for attempt in range(retries + 1):
            resp = client.post(
                "/inference/rewrite",
                json={"project_id": PROJECT_ID, "text": text, "context": context, "mode": mode},
            )
            assert resp.status_code == 200, resp.text
            payload = resp.json()
            if attempt < retries and payload.get("fallback_reason") == "TIMEOUT":
                print(f"  … TIMEOUT 降级，重试（{attempt + 1}/{retries}）")
                continue
            return payload
        return payload

    # ---------- 0. 找到带 ACTIVE 模型的项目 ----------
    projects = client.get("/projects").json()["items"]
    with_model = [p for p in projects if p.get("active_model_id")]
    if not with_model:
        fail("没有带 ACTIVE 模型的项目：请先运行 scripts/smoke_regression.py")
    project = with_model[0]
    PROJECT_ID = project["id"]
    print(f"✅ 使用项目 {project['name']}（ACTIVE 模型 {project['active_model_name']}）")

    # ---------- 1. rewriter 健康 ----------
    health = client.get("/inference/rewrite/health")
    assert health.status_code == 200, health.text
    h = health.json()
    assert h["breaker_state"] in ("closed", "open", "half-open")
    assert "metrics" in h and "requests_total" in h["metrics"]
    print(f"✅ 改写健康端点（breaker={h['breaker_state']}，rewriter={h.get('rewriter', {}).get('ok')}）")

    # ---------- 2. L0 术语归一（不依赖 rewriter） ----------
    put_config(mode="normalize_only")
    term = client.put(
        f"/projects/{PROJECT_ID}/terminology",
        json={"terms": {"terms": [{"canonical": "Libra 实验", "aliases": ["libra exp"]}]}},
    )
    assert term.status_code == 200, term.text
    l0 = rewrite("libra exp 看下状态")
    assert "Libra 实验" in l0["rewrite"]["standalone_query"], l0["rewrite"]
    assert l0["rewrite"]["model"]["provider"] == "terminology"
    assert l0["downstream_query"] == "libra exp 看下状态"
    assert l0["downstream_query_source"] == "original"
    assert l0["final_route"] == l0["original_route"]["route"]
    print(f"✅ L0 术语归一：libra exp → Libra 实验（版本 v{term.json()['version']}，下游仍用原文）")

    # ---------- 3. 等 rewriter 模型就绪 ----------
    put_config(mode="shadow")
    deadline = time.time() + args.rewriter_wait_seconds
    ok = False
    while time.time() < deadline:
        info = client.get("/inference/rewrite/health").json()
        if info.get("rewriter", {}).get("ok"):
            ok = True
            break
        time.sleep(5)
    if not ok:
        fail(f"rewriter {args.rewriter_wait_seconds}s 内未就绪（health={info.get('rewriter')}）")
    print("✅ rewriter 服务就绪（Qwen 已加载）")

    # ---------- 4. shadow 双路不变量 ----------
    s = rewrite("这个怎么停？", context="当前讨论实验 123")
    assert s["mode"] == "shadow"
    assert s["final_route"] == s["original_route"]["route"], s
    assert s["downstream_query"] == "这个怎么停？" and s["downstream_query_source"] == "original"
    assert s["safety_decision"] in ("allow_rewrite_shadow", "blocked", "fallback_original")
    assert s["rewrite"]["changed"] is True, s["rewrite"]
    print(f"✅ shadow：改写发生（{s['rewrite']['standalone_query']!r}），路由 {s['original_route']['route']}→"
          f"{s['rewrite_route']['route']}，下游用原文，final_route={s['final_route']}")

    # ---------- 5. safe_apply 不变量 ----------
    put_config(mode="safe_apply")
    sa = rewrite("这个怎么停？", context="当前讨论实验 999888")  # 不同上下文避免缓存命中
    assert sa["final_route"] == sa["original_route"]["route"], sa
    if sa["downstream_query_source"] == "rewrite":
        assert sa["safety_decision"] == "allow_rewrite" and sa["safety"]["allow"] is True
        assert sa["downstream_query"] == sa["rewrite"]["standalone_query"]
    else:
        assert sa["downstream_query"] == "这个怎么停？"
    assert not (sa.get("safety") or {}).get("escalation"), sa.get("safety")
    print(f"✅ safe_apply：source={sa['downstream_query_source']}，正式路由不变（{sa['final_route']}）")

    # ---------- 6. /predict 兼容 ----------
    put_config(mode="shadow")
    plain = client.post(
        "/inference/predict",
        json={"project_id": PROJECT_ID, "text": "查看实验 42 的状态", "context": None},
    ).json()
    assert "query_understanding" not in plain, plain
    with_rw = client.post(
        "/inference/predict",
        json={
            "project_id": PROJECT_ID, "text": "查看实验 42 的状态", "context": None,
            "rewrite": {"enabled": True, "mode": "shadow", "include_trace": True},
        },
    ).json()
    assert "query_understanding" in with_rw and with_rw["query_understanding"]["available"] is True
    volatile = {"request_id", "created_at", "timestamp"}
    for key in plain:  # 逐字段对比共同字段（排除每次请求唯一标识；个别决策下无 probabilities）
        if key in with_rw and key not in volatile:
            assert with_rw[key] == plain[key], f"{key} 被改写链路改变"
    print("✅ /predict 兼容：不带参数行为不变；带参数仅新增 query_understanding")

    # ---------- 7. 批量 ----------
    batch = client.post(
        "/inference/rewrite/batch",
        # 批量内部串行生成，3 条 × ~75s，需独立于全局超时的预算
        timeout=400.0,
        json={
            "project_id": PROJECT_ID,
            "items": [
                {"text": "这个怎么停？", "context": "当前讨论实验 777"},
                {"text": "查看今天的日程"},
                {"text": "帮我删了"},
            ],
        },
    ).json()
    assert batch["count"] == 3 and len(batch["results"]) == 3
    assert isinstance(batch["rewrite_failed_count"], int) and batch["rewrite_failed_count"] <= 3
    assert all(r["final_route"] == r["original_route"]["route"] for r in batch["results"])
    print(f"✅ 批量改写：count=3，降级 {batch['rewrite_failed_count']} 条，逐条 final_route 不变")

    # ---------- 8. 反馈哈希 ----------
    fb = client.post(
        f"/projects/{PROJECT_ID}/rewrite-feedback",
        json={
            "text": "这个怎么停？", "context": "当前讨论实验 123",
            "proposed_rewrite": s["rewrite"]["standalone_query"],
            "verdict": "reject", "reason_codes": ["指代解析错误"],
            "original_route": s["original_route"]["route"],
            "rewrite_route": s["rewrite_route"]["route"],
        },
    )
    assert fb.status_code == 200 and fb.json()["stored_raw_text"] is False, fb.text
    listed = client.get(f"/projects/{PROJECT_ID}/rewrite-feedback").json()["items"]
    assert listed and listed[0]["has_raw_text"] is False
    print("✅ 反馈闭环：默认仅哈希，原文未落库")

    # ---------- 9. 超时降级（不依赖 docker，独立验证 TIMEOUT 路径） ----------
    put_config(mode="shadow", timeout_ms=200)
    tmo = rewrite(f"实验 {int(time.time())} 的指标怎么看", context="在看大盘", retries=0)
    assert tmo["safety_decision"] == "fallback_original" and tmo["fallback_reason"] == "TIMEOUT", tmo
    assert tmo["downstream_query"] == tmo["rewrite"]["original_query"]
    put_config(mode="shadow", timeout_ms=110000)
    print("✅ 超时降级：timeout_ms=200 时自动回退原文（TIMEOUT），绝不 5xx")

    # ---------- 10. rewriter 停机降级 + 恢复 ----------
    stopped = stop_rewriter()
    if not stopped:
        print("⚠️ 无法控制 rewriter 容器（无 docker CLI 且无 /var/run/docker.sock），跳过停机降级检查")
    else:
        try:
            time.sleep(2)
            unique = f"实验 {int(time.time())} 的告警怎么处理"
            deg = rewrite(unique, context="正在排查线上问题")
            assert deg["safety_decision"] == "fallback_original", deg
            assert deg["fallback_reason"] in ("PROVIDER_UNAVAILABLE", "TIMEOUT"), deg
            assert deg["downstream_query"] == unique
            assert deg["final_route"] == deg["original_route"]["route"]
            predict_deg = client.post(
                "/inference/predict",
                json={"project_id": PROJECT_ID, "text": unique, "rewrite": {"enabled": True}},
            )
            assert predict_deg.status_code == 200
            assert predict_deg.json()["route"] == deg["original_route"]["route"]
            print(f"✅ 停机降级：/inference/rewrite 与 /predict 均 200，fallback={deg['fallback_reason']}，路由不受影响")
        finally:
            if not start_rewriter():
                print("⚠️ rewriter 重新启动失败，请手动 docker compose start rewriter")

    # ---------- 10. off 一键回退 + 恢复默认 ----------
    put_config(mode="off")
    off = rewrite("这个怎么停？", context="当前讨论实验 123", mode=None)  # 跟随项目配置
    assert off["mode"] == "off" and off["safety_decision"] == "mode_off"
    assert off["downstream_query"] == "这个怎么停？"
    plain_off = client.post(
        "/inference/predict",
        json={"project_id": PROJECT_ID, "text": "这个怎么停？", "context": "当前讨论实验 123"},
    ).json()
    assert "query_understanding" not in plain_off
    put_config(mode="shadow")  # 收尾恢复默认 shadow
    print("✅ off 模式：一键回退现有链路，/predict 完全无感；已恢复 shadow 默认")

    print("\n🎉 改写 E2E Smoke 全部通过（11 项）")


if __name__ == "__main__":
    main()
