#!/usr/bin/env python3
"""导出模型制品目录（拷贝到指定位置或打包 tar.gz）。

安全约束（修改方案 V2 §3.1）：
- model_id 必须是服务端格式并通过数据库解析，禁止路径拼接；
- 输出路径默认必须不存在；已存在时非零退出，不做任何删除；
- --force 显式覆盖仍拒绝：根目录、用户目录、仓库根、制品根、
  当前工作目录、源模型目录，以及它们的祖先和符号链接路径；
- 目录导出：同级临时目录复制 → 哈希复核 → 原子换位，
  中断不会留下"看似完整"的半成品目标；
- tar 导出使用 Python tarfile，不依赖外部命令，同样禁止静默覆盖。

用法: python scripts/export_model.py --model-id mdl_xxx --out /tmp/my-model [--tar] [--force]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.errors import ApiError  # noqa: E402
from app.services import artifact_service  # noqa: E402
from app.utils.hashing import hash_tree  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--tar", action="store_true", help="输出 tar.gz 而不是目录")
    parser.add_argument("--force", action="store_true", help="允许覆盖已存在的输出路径（仍拒绝受保护路径）")
    args = parser.parse_args()

    try:
        src = artifact_service.resolve_model_artifact(args.model_id)
    except ApiError as exc:
        print(f"❌ 无法解析模型: {exc.code} {exc.message}")
        return 1

    print("制品清单:")
    for rel, digest in sorted(hash_tree(src).items()):
        print(f"  {rel}  {digest[:16]}...")

    try:
        if args.tar:
            out = artifact_service.export_model_tar(src, args.out, force=args.force,
                                                    extra_protected=(REPO_ROOT,))
            print(f"已导出 {out}")
        else:
            out = artifact_service.export_model_dir(src, args.out, force=args.force,
                                                    extra_protected=(REPO_ROOT,))
            print(f"已导出到 {out}（哈希已复核）")
    except ApiError as exc:
        print(f"❌ 导出被拒绝: {exc.code} {exc.message}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
