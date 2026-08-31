#!/usr/bin/env python3
"""对运行中的服务执行 Query 改写评测（修改方案 §14 / §15）。

用法（容器内或本机 venv）：
  python scripts/eval_rewrite.py \
    --base-url http://127.0.0.1:8000/api/v1 \
    --project prj_xxx --mode shadow \
    --eval-file examples/rewrite_eval.jsonl \
    --out var/rewrite_eval_report.json

前置：项目已有 ACTIVE 模型（双路分类需要）；rewriter 可用或降级也被统计
（fallback_rate 是产品指标之一）。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "backend"))

from app.query_rewrite.evaluation import load_eval_cases, run_eval  # noqa: E402


def post_json(base_url: str, path: str, body: dict, timeout: float = 30.0) -> tuple[dict, float]:
    url = f"{base_url.rstrip('/')}{path}"
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    started = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data, round((time.perf_counter() - started) * 1000, 1)


def main() -> int:
    parser = argparse.ArgumentParser(description="Query 改写评测")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000/api/v1")
    parser.add_argument("--project", required=True, help="项目 ID（需有 ACTIVE 模型）")
    parser.add_argument("--mode", default="shadow", choices=["off", "normalize_only", "shadow", "safe_apply"])
    parser.add_argument("--eval-file", default=str(REPO / "examples" / "rewrite_eval.jsonl"))
    parser.add_argument("--out", default=None, help="报告输出 JSON 路径（缺省打印不落盘）")
    parser.add_argument("--terminology", default=None, help="可选：先上传术语表 JSON 再评测")
    parser.add_argument(
        "--provider-connection", default=None,
        help="可选：切换项目改写模型连接（builtin:local_qwen 或 rpc_*）后再评测（外部模型 V1 §13 阶段5）",
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()

    if args.provider_connection:
        # 保留现有策略字段，仅切换模型连接（保存为新配置版本，可回滚）
        with urllib.request.urlopen(
            f"{args.base_url.rstrip('/')}/projects/{args.project}/rewrite-config", timeout=10
        ) as resp:
            current = json.loads(resp.read())["active"]["config"]
        current["provider_connection_id"] = args.provider_connection
        req = urllib.request.Request(
            f"{args.base_url.rstrip('/')}/projects/{args.project}/rewrite-config",
            data=json.dumps({"config": current}).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="PUT",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            saved = json.loads(resp.read())
        print(f"改写模型已切换: {args.provider_connection}（配置版本 v{saved['version']}，测试前请确认连接已测试通过）")

    if args.terminology:
        terms = json.loads(Path(args.terminology).read_text(encoding="utf-8"))
        req = urllib.request.Request(
            f"{args.base_url.rstrip('/')}/projects/{args.project}/terminology",
            data=json.dumps({"terms": terms}).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="PUT",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f"术语表已上传: {json.loads(resp.read())['version']=} ")

    cases = load_eval_cases(args.eval_file)
    print(f"评测用例 {len(cases)} 条，模式 {args.mode}")

    pairs = []
    for i, case in enumerate(cases, 1):
        try:
            payload, total_ms = post_json(
                args.base_url,
                "/inference/rewrite",
                {
                    "project_id": args.project,
                    "text": case.text,
                    "context": case.context,
                    "mode": args.mode,
                },
                timeout=args.timeout,
            )
            payload["_total_latency_ms"] = total_ms
        except Exception as exc:  # noqa: BLE001 单条失败不中止评测
            print(f"  [{i}/{len(cases)}] {case.id} 请求失败: {exc}")
            continue
        marker = "改写" if payload.get("rewrite", {}).get("changed") else "原样"
        fallback = payload.get("safety_decision") == "fallback_original"
        print(
            f"  [{i}/{len(cases)}] {case.id} [{case.slice}] {marker}"
            f"{'（降级 ' + str(payload.get('fallback_reason')) + '）' if fallback else ''}"
            f" conf={payload.get('rewrite', {}).get('confidence', 0):.2f}"
        )
        pairs.append((case, payload))

    if not pairs:
        print("没有任何成功样本，评测中止")
        return 1

    report = run_eval(pairs)
    metrics = report["metrics"]
    print("\n==== 指标 ====")
    for key, value in metrics.items():
        print(f"  {key}: {value}")
    print("\n==== 切片 ====")
    for slice_name, stat in report["by_slice"].items():
        print(f"  {slice_name}: {stat}")
    print("\n==== 验收门槛（§15）====")
    for key, gate in report["gates"].items():
        if key == "all_pass":
            print(f"  >>> 全部门槛通过: {gate}")
        else:
            mark = "✅" if gate["pass"] else "❌"
            print(f"  {mark} {key} {gate['value']} {gate['op']} {gate['threshold']}")

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        slim = {k: v for k, v in report.items() if k != "events"}
        slim["events"] = [
            {k: v for k, v in e.items() if k != "total_latency_ms"} for e in report["events"]
        ]
        out_path.write_text(
            json.dumps(slim, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        print(f"\n报告已写入 {out_path}")
    return 0 if report["gates"]["all_pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
