"""Alembic environment — async (asyncpg) with a sync bridge.

Connection precedence:
  1. env var ALEMBIC_DATABASE_URL   (what CI / testcontainers inject)
  2. sqlalchemy.url from alembic.ini

Migrations connect AS migration_admin. The runtime roles never receive DDL
rights, so `alembic upgrade` run with app_user credentials fails loudly —
that failure is a feature: it proves the privilege split is real.
"""
from __future__ import annotations

import asyncio
import os

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.models import Base

config = context.config

if url := os.environ.get("ALEMBIC_DATABASE_URL"):
    config.set_main_option("sqlalchemy.url", url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """--sql mode: emit DDL to stdout for DBA review, no connection."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        # every migration in ONE transaction: a failed step rolls back
        # cleanly instead of leaving a half-migrated evidence schema
        transaction_per_migration=False,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
