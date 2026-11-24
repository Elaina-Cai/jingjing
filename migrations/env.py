from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from db.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# One migration tree serves both meta and account databases.  Callers choose
# the target with ALEMBIC_DB_PATH (or sqlalchemy.url in alembic.ini).
db_path = os.environ.get("ALEMBIC_DB_PATH") or config.get_main_option("sqlalchemy.url")
if db_path and not db_path.startswith("sqlite:"):
    db_path = f"sqlite:///{db_path}"

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=db_path,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(
        db_path,
        poolclass=pool.NullPool,
        connect_args={"check_same_thread": False},
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
