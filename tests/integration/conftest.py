"""Integration harness — REAL PostgreSQL 16 via testcontainers.

Everything the suite guarantees is Postgres-specific (SKIP LOCKED, partial
unique indexes, savepoints, trigger errors, role privileges), so there is
deliberately no SQLite fallback: if Docker isn't available, the suite skips
loudly instead of green-washing with a database that can't fail correctly.

Layering:
  session-scoped : container ─▶ role bootstrap ─▶ `alembic upgrade head`
                   (the migration file itself is part of the system under test)
  function-scoped: fresh async engines per test (pytest-asyncio gives each
                   test its own event loop; asyncpg pools are loop-bound),
                   plus TRUNCATE-based isolation as the table owner.
"""
from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

def _docker_daemon_reachable() -> bool:
    """Package import succeeding says nothing about a running daemon — probe
    the actual socket so a missing/stopped Docker Desktop yields a clean
    skip instead of a wall of connection-refused ERRORs."""
    try:
        import docker

        client = docker.from_env()
        try:
            client.ping()
        finally:
            client.close()
        return True
    except Exception:
        return False


try:
    from testcontainers.postgres import PostgresContainer
    HAVE_DOCKER = _docker_daemon_reachable()
except Exception:  # pragma: no cover
    HAVE_DOCKER = False

pytestmark = pytest.mark.skipif(not HAVE_DOCKER, reason="Docker daemon not reachable")

# conftest lives at tests/integration/conftest.py → repo root is three
# parents up (integration → tests → root).
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

APP_ROLE_PW = "app-test-pw"
JOBS_ROLE_PW = "jobs-test-pw"

ALL_TABLES = (
    # child-first so RESTART IDENTITY CASCADE has nothing to fight
    "login_throttle", "invitation", "user_session",
    "deadline", "vehicle",
    "compliance_check_log", "outbox_message", "system_alert",
    "escalation_case", "driver", "account_user", "company",
)


# ────────────────────────── session-scoped setup ──────────────────────────


@pytest.fixture(scope="session")
def pg_container() -> Iterator[PostgresContainer]:
    # `pytestmark` on a conftest module does NOT propagate as a skip to
    # sibling test modules (only pytestmark inside the test module itself
    # would) — so the guard is re-asserted here, at the one fixture every
    # integration test depends on, to guarantee a clean skip either way.
    if not HAVE_DOCKER:
        pytest.skip("Docker daemon not reachable")
    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg


def _asyncpg_url(pg: PostgresContainer, user: str | None = None,
                 pw: str | None = None) -> str:
    host, port = pg.get_container_host_ip(), pg.get_exposed_port(5432)
    u = user or pg.username
    p = pw or pg.password
    return f"postgresql+asyncpg://{u}:{p}@{host}:{port}/{pg.dbname}"


@pytest.fixture(scope="session")
def database(pg_container: PostgresContainer) -> str:
    """Bootstrap roles, run the REAL migration, return the superuser URL."""
    su_url = _asyncpg_url(pg_container)

    async def bootstrap() -> None:
        engine = create_async_engine(su_url, isolation_level="AUTOCOMMIT")
        async with engine.connect() as conn:
            from sqlalchemy import text
            # mirror scripts/bootstrap_roles.sql (programmatic, test passwords)
            await conn.execute(text(
                f"CREATE ROLE app_user LOGIN PASSWORD '{APP_ROLE_PW}' "
                f"NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS"))
            await conn.execute(text(
                f"CREATE ROLE app_jobs LOGIN PASSWORD '{JOBS_ROLE_PW}' "
                f"NOSUPERUSER NOCREATEDB NOCREATEROLE BYPASSRLS"))
            await conn.execute(text("REVOKE CREATE ON SCHEMA public FROM PUBLIC"))
            await conn.execute(text("GRANT USAGE ON SCHEMA public TO app_user, app_jobs"))
        await engine.dispose()

    asyncio.run(bootstrap())

    # Run the actual Alembic migration — the revision file (partial index,
    # triggers, grant matrix) is itself under test here.
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    os.environ["ALEMBIC_DATABASE_URL"] = su_url
    command.upgrade(cfg, "head")
    return su_url


@pytest.fixture(scope="session")
def app_role_url(pg_container: PostgresContainer, database: str) -> str:
    return _asyncpg_url(pg_container, user="app_user", pw=APP_ROLE_PW)


# ───────────────────────── function-scoped engines ─────────────────────────


@pytest.fixture()
async def admin_engine(database: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(database, pool_size=10, max_overflow=10)
    yield engine
    await engine.dispose()


@pytest.fixture()
async def app_engine(app_role_url: str) -> AsyncIterator[AsyncEngine]:
    """Least-privilege runtime role — used by the privilege-boundary tests."""
    engine = create_async_engine(app_role_url)
    yield engine
    await engine.dispose()


@pytest.fixture()
def admin_sf(admin_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(admin_engine, expire_on_commit=False)


@pytest.fixture()
def app_sf(app_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Least-privilege (app_user, NOBYPASSRLS) session factory — the role the
    web process actually uses, so RLS and the grant matrix apply. Auth-flow
    and tenant-isolation tests run through this, not the superuser admin_sf."""
    return async_sessionmaker(app_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def _clean_tables(admin_engine: AsyncEngine) -> AsyncIterator[None]:
    """TRUNCATE isolation, executed as the table owner AFTER each test.
    (Runtime roles cannot TRUNCATE — by design.)"""
    yield
    from sqlalchemy import text
    async with admin_engine.begin() as conn:
        await conn.execute(
            text(f"TRUNCATE {', '.join(ALL_TABLES)} RESTART IDENTITY CASCADE"))


# ───────────────────────────── seed helpers ─────────────────────────────


@pytest.fixture()
async def seeded(admin_sf: async_sessionmaker[AsyncSession]) -> dict:
    """One company, one owner, one authorized driver."""
    from app.models import AccountUser, Company, Driver

    async with admin_sf() as s, s.begin():
        company = Company(name="Muster Handwerk GmbH")
        s.add(company)
        await s.flush()
        owner = AccountUser(company_id=company.id, email="chef@muster.de",
                            role="OWNER", phone_e164="+4915112345678")
        driver = Driver(company_id=company.id, first_name="Max",
                        last_name="Mustermann")
        s.add_all([owner, driver])
        await s.flush()
        return {"company_id": company.id, "owner_id": owner.id,
                "driver_id": driver.id}
