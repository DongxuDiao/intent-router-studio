"""导出命令安全回归（修改方案 V2 §3.1）。

威胁：旧实现 --out 存在即 shutil.rmtree，等于 CLI 参数任意递归删除；
--model-id 直接拼接目录可越界。
"""
from __future__ import annotations

import tarfile
from pathlib import Path

import pytest

from app.config import get_settings
from app.db import SessionLocal
from app.errors import ApiError
from app.models.tables import ModelVersion
from app.services import artifact_service
from app.utils.hashing import hash_tree


@pytest.fixture
def registered_model():
    """构造一个带 Manifest 与完整外键链（项目/数据集/Run）的模型制品。"""
    # 注意避开 ULID 排除字符 I/L/O/U，保证通过 ID 格式白名单
    model_id = "mdl_01JTESTEXPART00"
    project_id, dataset_id, run_id = "prj_exporttest", "dsv_exporttest", "run_exporttest"
    src = get_settings().models_dir / model_id
    src.mkdir(parents=True, exist_ok=True)
    (src / "thresholds.json").write_text('{"conf_min": 0.5}', encoding="utf-8")
    (src / "setfit_model").mkdir(exist_ok=True)
    (src / "setfit_model" / "model.safetensors").write_bytes(b"fake-weights" * 16)
    # build_manifest 会把已存在的 manifest.json 一并计入哈希，先清掉旧的
    (src / "manifest.json").unlink(missing_ok=True)
    manifest = artifact_service.build_manifest(src, {"model_id": model_id})

    from app.models.tables import DatasetVersion, Project, TrainingRun

    with SessionLocal() as session:
        session.merge(Project(id=project_id, name="导出安全测试"))
        session.flush()  # 无 ORM relationship，需按外键顺序手动落库
        session.merge(DatasetVersion(id=dataset_id, project_id=project_id,
                                     parquet_path="export-test.parquet"))
        session.flush()
        session.merge(TrainingRun(id=run_id, project_id=project_id, dataset_id=dataset_id,
                                  config={}, status="SUCCEEDED"))
        session.flush()
        session.merge(ModelVersion(
            id=model_id, project_id=project_id, run_id=run_id,
            threshold_id=None, name="export-test", status="CANDIDATE",
            artifact_path=str(src), manifest_hash="x" * 64, manifest=manifest,
        ))
        session.commit()
    return model_id, src


def _expect_reject(exc_info, *codes):
    assert exc_info.value.code in codes, exc_info.value.code


# ---------------- 模型解析 ----------------

def test_model_id_traversal_rejected(registered_model):
    for bad in ("../../x", "mdl_../../x", "mdl_a", "../" + registered_model[0], "run_01ABC"):
        with pytest.raises(ApiError) as exc:
            artifact_service.resolve_model_artifact(bad)
        _expect_reject(exc, "INVALID_PATH")


def test_unknown_model_id_rejected():
    # 注意避开 ULID 排除字符 I/L/O/U，否则命中的是格式校验而非未找到
    with pytest.raises(ApiError) as exc:
        artifact_service.resolve_model_artifact("mdl_01ZZREGARD0000")
    _expect_reject(exc, "MODEL_NOT_FOUND")


def test_db_path_mismatch_rejected(registered_model, monkeypatch):
    model_id, src = registered_model
    with SessionLocal() as session:
        model = session.get(ModelVersion, model_id)
        model.artifact_path = str(get_settings().artifact_root_path / "elsewhere")
        session.commit()
    with pytest.raises(ApiError) as exc:
        artifact_service.resolve_model_artifact(model_id)
    _expect_reject(exc, "INVALID_PATH")


def test_resolve_success_verifies_manifest(registered_model):
    model_id, src = registered_model
    assert artifact_service.resolve_model_artifact(model_id) == src.resolve()
    # 制品被篡改后拒绝导出
    (src / "thresholds.json").write_text("tampered", encoding="utf-8")
    with pytest.raises(ApiError) as exc:
        artifact_service.resolve_model_artifact(model_id)
    _expect_reject(exc, "HASH_MISMATCH")
    (src / "thresholds.json").write_text('{"conf_min": 0.5}', encoding="utf-8")


# ---------------- 导出目标保护 ----------------

def test_protected_targets_rejected_even_with_force(registered_model, tmp_path):
    _, src = registered_model
    protected = [
        Path("/"), Path("/tmp"), Path.home(), Path.cwd(),
        get_settings().artifact_root_path, get_settings().models_dir,
        src, src.parent,
    ]
    for target in protected:
        with pytest.raises(ApiError) as exc:
            artifact_service.export_model_dir(src, target, force=True)
        _expect_reject(exc, "INVALID_PATH")
        assert target.exists(), f"受保护路径被删除: {target}"


def test_symlink_target_rejected(registered_model, tmp_path):
    _, src = registered_model
    real = tmp_path / "real-dir"
    real.mkdir()
    link = tmp_path / "link-dir"
    link.symlink_to(real)
    with pytest.raises(ApiError) as exc:
        artifact_service.export_model_dir(src, link, force=True)
    _expect_reject(exc, "INVALID_PATH")
    assert link.is_symlink() and real.exists()


def test_existing_target_never_deleted_without_force(registered_model, tmp_path):
    _, src = registered_model
    out = tmp_path / "existing"
    out.mkdir()
    (out / "sentinel.txt").write_text("keep me", encoding="utf-8")
    with pytest.raises(ApiError) as exc:
        artifact_service.export_model_dir(src, out)
    _expect_reject(exc, "PATH_EXISTS")
    assert (out / "sentinel.txt").read_text(encoding="utf-8") == "keep me"


# ---------------- 正常导出与原子性 ----------------

def test_dir_export_hash_matches_manifest(registered_model, tmp_path):
    _, src = registered_model
    out = artifact_service.export_model_dir(src, tmp_path / "exported")
    assert out.is_dir()
    assert hash_tree(out) == hash_tree(src)
    manifest = artifact_service.read_json(out / "manifest.json")
    for rel, expected in manifest["artifact_hashes"].items():
        assert expected == f"sha256:{hash_tree(out)[rel]}"


def test_force_overwrite_replaces_content_atomically(registered_model, tmp_path):
    _, src = registered_model
    out = tmp_path / "stale-export"
    out.mkdir()
    (out / "old-file.bin").write_bytes(b"old" * 100)
    artifact_service.export_model_dir(src, out, force=True)
    assert hash_tree(out) == hash_tree(src)
    assert not (out / "old-file.bin").exists()
    # 换位产生的临时/备份目录已清理
    leftovers = [p.name for p in out.parent.iterdir() if p.name.startswith(".")]
    assert leftovers == []


def test_failed_export_leaves_no_partial_target(registered_model, tmp_path, monkeypatch):
    _, src = registered_model
    out = tmp_path / "must-not-exist"

    def _boom(s, c):
        raise ApiError("HASH_MISMATCH", "模拟复制损坏", 409)

    monkeypatch.setattr(artifact_service, "_assert_tree_identical", _boom)
    with pytest.raises(ApiError):
        artifact_service.export_model_dir(src, out)
    assert not out.exists()
    assert [p for p in tmp_path.iterdir() if p.name.startswith(".")] == []


def test_tar_export_roundtrip_and_no_silent_overwrite(registered_model, tmp_path):
    _, src = registered_model
    tar_path = artifact_service.export_model_tar(src, tmp_path / "model")
    assert tar_path.name == "model.tar.gz" and tar_path.is_file()
    with tarfile.open(tar_path, "r:gz") as tar:
        members = sorted(m.name for m in tar.getmembers() if m.isfile())
    assert members == sorted(hash_tree(src))

    with pytest.raises(ApiError) as exc:
        artifact_service.export_model_tar(src, tmp_path / "model")
    _expect_reject(exc, "PATH_EXISTS")
    # --force 覆盖为合法 tar，成员完整
    tar_path2 = artifact_service.export_model_tar(src, tmp_path / "model", force=True)
    with tarfile.open(tar_path2, "r:gz") as tar:
        assert sorted(m.name for m in tar.getmembers() if m.isfile()) == members


def test_cli_refuses_traversal_and_existing_dir(registered_model, tmp_path):
    """脚本入口层面复验：非零退出且不删除任何内容。"""
    import os
    import subprocess
    import sys

    repo = Path(__file__).resolve().parents[2]
    existing = tmp_path / "keep"
    existing.mkdir()
    (existing / "data.txt").write_text("important", encoding="utf-8")

    env = dict(os.environ)  # 继承测试的 ARTIFACT_ROOT/DATABASE_URL，指向同一套数据
    for argv in (
        ["--model-id", "../../x", "--out", str(tmp_path / "o1")],
        ["--model-id", registered_model[0], "--out", str(existing)],
        ["--model-id", registered_model[0], "--out", "."],
    ):
        result = subprocess.run(
            [sys.executable, str(repo / "scripts" / "export_model.py"), *argv],
            capture_output=True, text=True, cwd=str(repo / "backend"), timeout=120, env=env,
        )
        assert result.returncode != 0, argv
        assert "❌" in result.stdout, result.stdout
    assert (existing / "data.txt").read_text(encoding="utf-8") == "important"
