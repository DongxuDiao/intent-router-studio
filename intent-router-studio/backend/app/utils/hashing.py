"""文件与目录哈希工具。"""
from __future__ import annotations

import hashlib
from pathlib import Path

_CHUNK = 1024 * 1024


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_tree(root: str | Path, relative_to: str | Path | None = None) -> dict[str, str]:
    """对目录下全部常规文件生成 {相对路径: sha256}（跳过符号链接）。"""
    root = Path(root)
    base = Path(relative_to) if relative_to else root
    result: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if p.is_symlink() or not p.is_file():
            continue
        rel = p.relative_to(base).as_posix()
        result[rel] = sha256_file(p)
    return result
