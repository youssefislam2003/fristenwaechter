#!/usr/bin/env python3
"""Idempotent cluster bootstrap for docker-compose / first deploy.

Mirrors scripts/bootstrap_roles.sql but in Python (asyncpg is already a
dependency, so the slim image needs no psql). Connects as the superuser and
creates migration_admin / app_user / app_jobs if absent, wires schema
privileges, and grants app_jobs → migration_admin (so migration 0002 can own
the login-lookup function as the BYPASSRLS role). Safe to run repeatedly.

Env:
  SUPERUSER_DATABASE_URL  postgres superuser DSN (asyncpg)
  MIGRATION_ADMIN_PASSWORD / APP_USER_PASSWORD / APP_JOBS_PASSWORD
"""
from __future__ import annotations

import asyncio
import os
import sys

import asyncpg


async def _ensure_role(conn, name: str, password: str, *, bypassrls: bool) -> None:
    exists = await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", name)
    attrs = "LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE " + (
        "BYPASSRLS" if bypassrls else "NOBYPASSRLS")
    # Password is interpolated (asyncpg can't parameterize role DDL); values
    # come from env/secrets, never user input.
    pw = password.replace("'", "''")
    if exists:
        await conn.execute(f"ALTER ROLE {name} WITH {attrs} PASSWORD '{pw}'")
    else:
        await conn.execute(f"CREATE ROLE {name} WITH {attrs} PASSWORD '{pw}'")


async def main() -> int:
    su_url = os.environ["SUPERUSER_DATABASE_URL"].replace(
        "postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(su_url)
    try:
        await _ensure_role(conn, "migration_admin",
                           os.environ.get("MIGRATION_ADMIN_PASSWORD", "migadmin"),
                           bypassrls=False)
        await _ensure_role(conn, "app_user",
                           os.environ.get("APP_USER_PASSWORD", "appuser"),
                           bypassrls=False)
        await _ensure_role(conn, "app_jobs",
                           os.environ.get("APP_JOBS_PASSWORD", "appjobs"),
                           bypassrls=True)
        # Schema ownership + least privilege.
        await conn.execute("ALTER SCHEMA public OWNER TO migration_admin")
        await conn.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        await conn.execute("GRANT USAGE ON SCHEMA public TO app_user, app_jobs")
        # gen_random_uuid() lives in pgcrypto on older images; create it now as
        # the superuser (migration_admin is not superuser). Also grant
        # migration_admin CREATE on the DB so the migration's idempotent
        # `CREATE EXTENSION IF NOT EXISTS` (pgcrypto is a trusted extension) is
        # permitted rather than denied.
        dbname = await conn.fetchval("SELECT current_database()")
        await conn.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
        await conn.execute(f'GRANT CREATE ON DATABASE "{dbname}" TO migration_admin')
        # migration_admin must be a member of app_jobs to reassign the
        # SECURITY DEFINER login function's ownership (migration 0002).
        await conn.execute("GRANT app_jobs TO migration_admin")
        print("bootstrap_db: roles ready")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
