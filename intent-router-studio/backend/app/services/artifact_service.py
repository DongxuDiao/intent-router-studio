"""制品库：本地文件制品的路径管理、哈希与原子发布（设计文档 4.1 / 8）。

- 路径一律由服务端 UUID/ULID 拼接，不接受用户直接输入的路径
- Run 先写入 {id}.tmp 目录，完成后 rename 原子发布
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tarfile
from pathlib import Path

from app.config import get_settings
from app.errors import ApiError
from app.utils.hashing import hash_tree, sha256_file

MANIFEST_SCHEMA_VERSION = 1

# 服务端模型 ID 格式（mdl_ 前缀 + ULID 主体），导出/读取前先做格式白名单
MODEL_ID_PATTERN = re.compile(r"^mdl_[0-9A-HJKMNP-TV-Za-hk-z]{10,40}$")


def _root() -> Path:
    return get_settings().artifact_root_path


def safe_join(base: Path, *parts: str) -> Path:
    """受限路径拼接：拒绝绝对路径、.. 与符号链接逃逸。"""
    target = base
    for part in parts:
        if not part or part.startswith(("/", "\\")) or part in (".", "..") or "\x00" in part:
            raise ApiError("INVALID_PATH", f"非法路径片段: {part!r}", 400)
        target = target / part
    resolved = target.resolve()
    base_resolved = base.resolve()
    if not resolved.is_relative_to(base_resolved):
        raise ApiError("INVALID_PATH", "路径越界", 400)
    return resolved


# ---- Run 制品 ----
def run_tmp_dir(run_id: str) -> Path:
    d = _root() / "runs" / f"{run_id}.tmp"
    d.mkdir(parents=True, exist_ok=True)
    return d


def run_final_dir(run_id: str) -> Path:
    return _root() / "runs" / run_id


def publish_run(run_id: str) -> Path:
    """原子发布：{id}.tmp → {id}。"""
    tmp = run_tmp_dir(run_id)
    final = run_final_dir(run_id)
    if final.exists():
        shutil.rmtree(final)
    os.replace(tmp, final)
    return final


def discard_run_tmp(run_id: str) -> None:
    tmp = _root() / "runs" / f"{run_id}.tmp"
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)


# ---- 数据集制品 ----
def dataset_dir(project_id: str, dataset_id: str) -> Path:
    d = _root() / "projects" / project_id / "datasets" / dataset_id
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---- 模型制品 ----
def model_dir(model_version_id: str) -> Path:
    d = _root() / "models" / model_version_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def build_manifest(
    artifact_dir: Path,
    base: dict,
    skip_suffixes: tuple[str, ...] = (".log", ".jsonl"),
) -> dict:
    """为制品目录生成 manifest（含 artifact_hashes）。"""
    hashes = hash_tree(artifact_dir)
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        **base,
        "artifact_hashes": {
            rel: f"sha256:{h}"
            for rel, h in sorted(hashes.items())
            if not rel.endswith(skip_suffixes) and not rel.startswith("setfit_model/")
        },
        "artifact_hashes_model": {
            rel: f"sha256:{h}" for rel, h in sorted(hashes.items()) if rel.startswith("setfit_model/")
        },
    }
    write_json(artifact_dir / "manifest.json", manifest)
    return manifest


def verify_manifest(artifact_dir: Path) -> dict:
    """校验 manifest 覆盖的所有文件哈希；返回 manifest。"""
    manifest_path = Path(artifact_dir) / "manifest.json"
    if not manifest_path.is_file():
        raise ApiError("ARTIFACT_INCOMPLETE", f"缺少 manifest.json: {artifact_dir}", 409)
    manifest = read_json(manifest_path)
    all_hashes = {**(manifest.get("artifact_hashes") or {}), **(manifest.get("artifact_hashes_model") or {})}
    mismatches = []
    for rel, expected in all_hashes.items():
        target = Path(artifact_dir) / rel
        if not target.is_file():
            mismatches.append(f"缺失: {rel}")
            continue
        expected_hex = expected[7:] if expected.startswith("sha256:") else expected
        if sha256_file(target) != expected_hex:
            mismatches.append(f"哈希不匹配: {rel}")
    if mismatches:
        raise ApiError("HASH_MISMATCH", "制品完整性校验失败", 409, {"problems": mismatches[:20]})
    return manifest


def check_disk_space(required_bytes: int, multiple: float = 3.0) -> None:
    """训练前磁盘检查：至少保留预计制品大小的 3 倍。"""
    usage = shutil.disk_usage(_root())
    required = int(required_bytes * multiple)
    if usage.free < required:
        raise ApiError(
            "INSUFFICIENT_DISK",
            f"磁盘空间不足：需要至少 {required / 1e9:.1f}GB，剩余 {usage.free / 1e9:.1f}GB",
            409,
        )


# ---- 安全导出（修改方案 V2 §3.1）----
# 威胁模型：CLI 参数可任意指定 --model-id/--out。历史上 --out 存在即 rmtree，
# 等于把"任意递归删除"暴露给命令行参数。

def resolve_model_artifact(model_version_id: str) -> Path:
    """模型 ID → 已通过完整性校验的制品目录。

    链路：ID 格式白名单 → 数据库解析 artifact_path → 目录必须位于
    models_dir/<model_id>（防数据库脏数据指向别处）→ Manifest 哈希复核。
    """
    if not MODEL_ID_PATTERN.match(model_version_id or ""):
        raise ApiError("INVALID_PATH", f"非法模型 ID: {model_version_id!r}", 400)
    from app.db import SessionLocal
    from app.models.tables import ModelVersion

    with SessionLocal() as session:
        model = session.get(ModelVersion, model_version_id)
        if model is None:
            raise ApiError("MODEL_NOT_FOUND", f"模型不存在: {model_version_id}", 404)
        db_artifact_path = str(model.artifact_path or "")

    src = safe_join(get_settings().models_dir, model_version_id)
    db_path = Path(db_artifact_path)
    if db_path.is_absolute():
        if db_path.resolve() != src:
            raise ApiError("INVALID_PATH", "数据库制品路径与 models/<id> 不一致，拒绝导出", 409)
    elif safe_join(_root(), *db_path.split("/")) != src:
        raise ApiError("INVALID_PATH", "数据库制品路径与 models/<id> 不一致，拒绝导出", 409)
    if not src.is_dir():
        raise ApiError("MODEL_NOT_FOUND", f"模型制品不存在: {src}", 404)
    verify_manifest(src)
    return src


def _no_symlink_in_chain(path: Path) -> None:
    """路径上任何已存在组件都不得是符号链接（resolve 只保证最终归一化）。"""
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ApiError("INVALID_PATH", f"路径包含符号链接: {part}", 400)


def validate_export_target(out: Path, src: Path, force: bool, extra_protected: tuple[Path, ...] = ()) -> Path:
    """导出目标校验：默认必须不存在；--force 也无权触碰受保护路径。

    受保护目标 = 文件系统根 / 用户目录 / 制品根 / 当前工作目录 / 仓库根 /
    源模型目录，以及它们之中任何一个的祖先（删除祖先等于删除全部内容）。
    """
    out = Path(out).expanduser()
    _no_symlink_in_chain(out)
    resolved = out.resolve()

    models_root = get_settings().models_dir.resolve()
    protected = [Path(resolved.anchor), Path.home().resolve(), _root().resolve(),
                 Path.cwd().resolve(), models_root, src.resolve(), src.resolve().parent,
                 *(p.resolve() for p in extra_protected)]
    for target_root in protected:
        if resolved == target_root or resolved in target_root.parents:
            raise ApiError("INVALID_PATH", f"导出目标受保护: {resolved}", 400)
    # 目标不能与源互相嵌套（复制进自身会让哈希复核失效）
    if src.resolve() in resolved.parents:
        raise ApiError("INVALID_PATH", "导出目标不能位于源模型目录内", 400)

    if resolved.exists() and not force:
        raise ApiError("PATH_EXISTS", f"输出路径已存在（如需覆盖请显式 --force）: {resolved}", 409)
    return resolved


def _remove_path(path: Path) -> None:
    """删除文件或目录（导出换位清理用）。"""
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path, ignore_errors=True)


def _assert_tree_identical(src: Path, copied: Path) -> None:
    """复制后哈希复核：确保拷贝过程无损坏、无多余文件。"""
    src_hashes, copied_hashes = hash_tree(src), hash_tree(copied)
    if src_hashes != copied_hashes:
        raise ApiError("HASH_MISMATCH", "导出副本与源制品哈希不一致，已放弃", 409)


def export_model_dir(src: Path, out: Path, force: bool = False, extra_protected: tuple[Path, ...] = ()) -> Path:
    """目录导出：同级临时目录复制 → 哈希复核 → 原子换位。

    任何时刻中断，目标目录要么完整旧内容、要么不存在，绝不出现
    "看似完整实为半成品"的状态；被换下的旧内容保留为 .old-<pid> 兄弟目录。
    """
    out = validate_export_target(out, src, force, extra_protected)
    parent = out.parent
    parent.mkdir(parents=True, exist_ok=True)
    tmp = parent / f".{out.name}.tmp-{os.getpid()}"
    old = parent / f".{out.name}.old-{os.getpid()}"
    for stale in (tmp, old):
        _remove_path(stale)
    try:
        shutil.copytree(src, tmp, symlinks=False)
        _assert_tree_identical(src, tmp)
        if out.exists():
            os.replace(out, old)
            try:
                os.replace(tmp, out)
            except OSError:
                os.replace(old, out)  # 换位失败回滚，旧内容原样恢复
                raise
            _remove_path(old)
        else:
            os.replace(tmp, out)
    finally:
        if tmp.exists():
            _remove_path(tmp)
    _assert_tree_identical(src, out)
    return out


def export_model_tar(src: Path, out_file: Path, force: bool = False, extra_protected: tuple[Path, ...] = ()) -> Path:
    """tar.gz 导出：Python tarfile（不依赖外部命令），同样禁止静默覆盖。"""
    out_file = Path(out_file).expanduser()
    if not out_file.name.endswith(".tar.gz"):
        out_file = out_file.with_name(out_file.name + ".tar.gz")
    out_file = validate_export_target(out_file, src, force, extra_protected)
    if out_file.exists() and out_file.is_dir():
        raise ApiError("INVALID_PATH", f"tar 输出目标不能是目录: {out_file}", 400)
    tmp = out_file.with_name(f".{out_file.name}.tmp-{os.getpid()}")
    if tmp.exists():
        tmp.unlink()
    members = sorted(hash_tree(src))
    try:
        with tarfile.open(tmp, "w:gz") as tar:
            for rel in members:
                try:
                    tar.add(src / rel, arcname=rel, recursive=False, filter="data")
                except TypeError:  # Python < 3.11.4 无 filter 参数；arcname 为服务端可控相对路径
                    tar.add(src / rel, arcname=rel, recursive=False)
        with tarfile.open(tmp, "r:gz") as tar:
            packed = sorted(m.name for m in tar.getmembers() if m.isfile())
        if packed != members:
            raise ApiError("EXPORT_INCOMPLETE", "tar 成员与制品清单不一致，已放弃", 409)
        os.replace(tmp, out_file)
    finally:
        if tmp.exists():
            tmp.unlink()
    return out_file
