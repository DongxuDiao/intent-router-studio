"""SQLAlchemy 引擎与会话：SQLite WAL + foreign_keys + busy_timeout。"""
from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


def _build_engine() -> Engine:
    settings = get_settings()
    settings.ensure_dirs()
    engine = create_engine(
        settings.database_url_absolute,
        connect_args={"check_same_thread": False, "timeout": 30},
        pool_pre_ping=True,
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

    return engine


engine = _build_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """建表与升级。schema 变更统一走 Alembic（修改方案 §11.4）：

    - 全新库：upgrade head 一次建齐
    - 遗留库（create_all 时代）：自动备份 → stamp baseline → 升级
    - 已在 Alembic 管理下：upgrade 为幂等 no-op
    """
    from app import db_migrate

    db_migrate.run_migrations()
