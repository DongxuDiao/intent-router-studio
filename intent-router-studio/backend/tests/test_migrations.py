"""Alembic 迁移测试（修改方案 §11.4：upgrade / downgrade / 遗留库 stamp）。"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect

BACKEND = Path(__file__).resolve().parents[1]
NEW_TABLES = {"rewrite_config_versions", "terminology_versions", "rewrite_feedback"}


def _run_alembic(url: str, *args: str) -> None:
    env = {**os.environ, "ALEMBIC_DATABASE_URL": url}
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"alembic {' '.join(args)} 失败:\n{proc.stdout}\n{proc.stderr}"


@pytest.fixture
def fresh_url(tmp_path):
    return f"sqlite:///{tmp_path / 'fresh.db'}"


def test_fresh_upgrade_creates_all_tables(fresh_url):
    _run_alembic(fresh_url, "upgrade", "head")
    eng = create_engine(fresh_url)
    tables = set(inspect(eng).get_table_names())
    assert NEW_TABLES <= tables
    assert {"projects", "model_versions", "training_runs", "audit_events"} <= tables
    cols = {c["name"] for c in inspect(eng).get_columns("projects")}
    assert "active_rewrite_config_id" in cols


def test_downgrade_drops_rewrite_tables(fresh_url):
    _run_alembic(fresh_url, "upgrade", "head")
    # 回滚到 rewrite 迁移之前（audit_events → rewrite → baseline）
    _run_alembic(fresh_url, "downgrade", "3896c839d8e4")
    eng = create_engine(fresh_url)
    tables = set(inspect(eng).get_table_names())
    assert not (NEW_TABLES & tables)
    assert "audit_events" not in tables
    cols = {c["name"] for c in inspect(eng).get_columns("projects")}
    assert "active_rewrite_config_id" not in cols
    assert {"projects", "training_runs"} <= tables  # 基线表仍在


def test_legacy_database_stamped_and_upgraded(tmp_path):
    """模拟 Phase 2 遗留库：baseline schema、无 alembic_version → run_migrations 自动接管。"""
    from app.db_migrate import run_migrations

    db_path = tmp_path / "legacy.db"
    url = f"sqlite:///{db_path}"
    # 构造 Phase 2 遗留库：只有 baseline 表、无 alembic_version
    _run_alembic(url, "upgrade", "head")
    _run_alembic(url, "downgrade", "3896c839d8e4")
    eng = create_engine(url)
    with eng.connect() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS alembic_version")
        conn.commit()
    tables = set(inspect(eng).get_table_names())
    assert "projects" in tables and not (NEW_TABLES & tables)

    backups = tmp_path / "backups"
    run_migrations(eng=eng, url=url, db_path=db_path, artifact_root=tmp_path, lock_dir=tmp_path)
    assert list(backups.glob("*.bak")), "遗留库迁移前必须有备份"
    cols = {c["name"] for c in inspect(eng).get_columns("projects")}
    assert "active_rewrite_config_id" in cols
    assert NEW_TABLES <= set(inspect(eng).get_table_names())
    from app.db_migrate import _is_legacy_database

    assert _is_legacy_database(eng) is False
    # 幂等：再次运行不报错、不再新增备份
    n_backups = len(list(backups.glob("*.bak")))
    run_migrations(eng=eng, url=url, db_path=db_path, artifact_root=tmp_path, lock_dir=tmp_path)
    assert len(list(backups.glob("*.bak"))) == n_backups


# ---------------------------------------------------------------- 迁移锁（V2 §4.1）

def _hold_lock(lock_path):
    """以另一 fd 持有 flock，模拟并发迁移进程。"""
    import fcntl

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_WRONLY, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


def test_migration_lock_timeout_fails_closed(tmp_path, monkeypatch):
    """锁被他人持有时超时抛错，绝不放行并发迁移。"""
    import fcntl

    from app import db_migrate

    monkeypatch.setattr(db_migrate, "_LOCK_TIMEOUT_S", 0.2)
    lock_path = db_migrate._default_lock_path(db_migrate._NS(tmp_path))
    holder = _hold_lock(lock_path)
    try:
        with pytest.raises(RuntimeError, match="迁移锁"):
            db_migrate._acquire_lock(lock_path)
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        os.close(holder)

    # 释放后可立即获取，且可重复获取/释放
    got = db_migrate._acquire_lock(lock_path)
    assert got >= 0
    db_migrate._release_lock(lock_path, got)
    again = db_migrate._acquire_lock(lock_path)
    assert again >= 0
    db_migrate._release_lock(lock_path, again)


def test_run_migrations_refuses_while_lock_held(tmp_path, monkeypatch):
    """端到端：持锁状态下 run_migrations 失败关闭，数据库不被触碰。"""
    import fcntl

    from sqlalchemy import inspect

    from app import db_migrate

    monkeypatch.setattr(db_migrate, "_LOCK_TIMEOUT_S", 0.2)
    db_path = tmp_path / "fresh.db"
    url = f"sqlite:///{db_path}"
    lock_path = db_migrate._default_lock_path(db_migrate._NS(tmp_path))
    holder = _hold_lock(lock_path)
    eng = create_engine(url)
    try:
        with pytest.raises(RuntimeError, match="迁移锁"):
            db_migrate.run_migrations(eng=eng, url=url, db_path=db_path, artifact_root=tmp_path, lock_dir=tmp_path)
        assert inspect(eng).get_table_names() == []  # 未建任何表
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        os.close(holder)
        eng.dispose()
