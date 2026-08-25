"""启动迁移（修改方案 §11.4）。

规则：
- 全量 schema 由 Alembic 管理（fresh 库 upgrade head 一次建齐）
- 遗留库（Phase 2 时代由 create_all 建表、无 alembic_version）：先 stamp baseline 再升级
- 迁移前自动备份 SQLite 文件到 var/backups/
- 文件锁串行化（API 与 Worker 同时启动时不竞争）；锁等待超时失败关闭，
  绝不放行并发迁移

engine / settings 可注入，便于测试。
"""
from __future__ import annotations

import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

logger = logging.getLogger("app")

BASELINE_REVISION = "3896c839d8e4"  # Phase 2 schema（create_all 时代等价物）
_LOCK_TIMEOUT_S = 60


def _alembic_config(url: str) -> Any:
    from alembic.config import Config

    backend_root = Path(__file__).resolve().parents[1]
    cfg = Config(str(backend_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend_root / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def _default_lock_path(settings: Any) -> Path:
    lock = settings.artifact_root_path / "tmp" / "migrate.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    return lock


def _acquire_lock(lock_path: Path) -> int:
    """迁移互斥锁：flock 等待，超时失败关闭（V2 §4.1）。

    flock 在持锁进程退出（含崩溃）时由内核自动释放，不存在遗留锁文件；
    超时不再放行——并发迁移可能与备份/写入交错，宁可启动失败也不抢跑。
    """
    import fcntl

    fd = os.open(lock_path, os.O_CREAT | os.O_WRONLY, 0o644)
    deadline = time.time() + _LOCK_TIMEOUT_S
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError:
            if time.time() >= deadline:
                os.close(fd)
                raise RuntimeError(
                    f"迁移锁等待超时（{_LOCK_TIMEOUT_S}s）：另一进程仍在迁移，拒绝并发执行"
                ) from None
            time.sleep(0.5)


def _release_lock(lock_path: Path, fd: int) -> None:
    if fd >= 0:
        import fcntl

        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _backup_database(db_path: Path, artifact_root: Path) -> Path | None:
    """迁移前备份 SQLite 主库文件（WAL 文件一并拷贝）。"""
    if not db_path.exists():
        return None
    backup_dir = artifact_root / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = backup_dir / f"{db_path.name}.pre-migrate.{stamp}.bak"
    shutil.copy2(db_path, target)
    for suffix in ("-wal", "-shm"):
        side = db_path.with_name(db_path.name + suffix)
        if side.exists():
            shutil.copy2(side, target.with_name(target.name + suffix))
    return target


def _is_legacy_database(eng: Engine) -> bool:
    """有业务表但没有 alembic_version → Phase 2 create_all 建的遗留库。"""
    tables = set(inspect(eng).get_table_names())
    if "alembic_version" in tables:
        return False
    return "projects" in tables


def _current_revision(eng: Engine) -> str | None:
    with eng.connect() as conn:
        return conn.execute(text("SELECT version_num FROM alembic_version")).scalar()


def run_migrations(
    eng: Engine | None = None,
    url: str | None = None,
    db_path: Path | None = None,
    artifact_root: Path | None = None,
    lock_dir: Path | None = None,
) -> None:
    """启动时调用：备份 → 遗留库 stamp → upgrade head。"""
    import alembic.command as alembic_cmd
    from alembic.script import ScriptDirectory

    if eng is None or url is None or db_path is None or artifact_root is None:
        from app.config import get_settings
        from app.db import engine as module_engine

        settings = get_settings()
        eng = eng or module_engine
        url = url or settings.database_url_absolute
        db_path = db_path or settings.sqlite_path
        artifact_root = artifact_root or settings.artifact_root_path

    lock_path = _default_lock_path(_NS(lock_dir or artifact_root))
    fd = _acquire_lock(lock_path)
    try:
        cfg = _alembic_config(url)
        script = ScriptDirectory.from_config(cfg)
        head = script.get_current_head()

        fresh = not inspect(eng).get_table_names()
        legacy = _is_legacy_database(eng)
        if legacy:
            backup = _backup_database(db_path, artifact_root)
            logger.info("检测到遗留数据库，已备份 %s，stamp baseline", backup)
            alembic_cmd.stamp(cfg, BASELINE_REVISION)
        elif not fresh:
            current = _current_revision(eng)
            if current != head:
                backup = _backup_database(db_path, artifact_root)
                logger.info("数据库版本 %s → %s，已备份 %s", current, head, backup)

        alembic_cmd.upgrade(cfg, "head")
        logger.info("数据库迁移完成 legacy_stamp=%s revision=%s", legacy, _current_revision(eng))
    finally:
        _release_lock(lock_path, fd)


class _NS:
    """极简命名空间：让 lock 目录可注入。"""

    def __init__(self, artifact_root: Path) -> None:
        self.artifact_root_path = artifact_root
