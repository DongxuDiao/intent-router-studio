"""Alembic 环境：metadata 来自 app.models，URL 来自 app.config。

- 优先读 ALEMBIC_DATABASE_URL（autogenerate 对照库 / 测试用）
- SQLite 必须开 batch 模式（ALTER 支持有限，删列需要表重建）
"""
from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config, pool

from alembic import context

# 保证能 import app（alembic 从 backend/ 目录启动）
BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.db import Base  # noqa: E402
from app.models import tables  # noqa: E402, F401 确保模型注册到 metadata

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

override_url = os.environ.get("ALEMBIC_DATABASE_URL")
if override_url:
    config.set_main_option("sqlalchemy.url", override_url)
elif not config.get_main_option("sqlalchemy.url"):
    # 未显式指定（alembic.ini 留空且调用方未注入）时回退 app 配置
    from app.config import get_settings

    config.set_main_option("sqlalchemy.url", get_settings().database_url_absolute)


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            # 已有业务库不做 autoload 比较（避免 SQLite 反射代价）；依赖显式迁移
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
