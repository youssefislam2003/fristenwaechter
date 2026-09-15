"""Async engine + session-factory wiring.

Two engines, two roles, one process each:

  * ``engine`` / ``session_factory``      — web/API, connects as app_user
    (NOBYPASSRLS). Every request opens a transaction and pins
    ``app.current_company`` (see app/deps tenant_session) so RLS scopes it.
  * ``jobs_engine`` / ``jobs_session_factory`` — worker, connects as app_jobs
    (BYPASSRLS). The relay and sweeps legitimately cross tenants, so they do
    NOT set the GUC and RLS does not constrain them.

Both factories use ``expire_on_commit=False``: after commit we frequently
still read the ORM objects we just wrote (e.g. to render a response or log an
id), and re-fetching from an expired instance would emit a surprise query —
or, post-commit with the transaction closed, fail outright.

Engines are created lazily the first time a factory is requested, so importing
this module never opens a socket (keeps ``alembic``, unit tests, and ``--help``
fast and side-effect-free). ``dispose_engines()`` is the graceful-shutdown hook.
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.settings import get_settings

# Module-level singletons, populated on first use.
_engine: AsyncEngine | None = None
_jobs_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None
_jobs_session_factory: async_sessionmaker[AsyncSession] | None = None


def _build_engine(url: str, *, application_name: str) -> AsyncEngine:
    """One engine, tuned for a small always-on service (5–50-vehicle fleets:
    low absolute concurrency, but every connection must be responsive)."""
    return create_async_engine(
        url,
        pool_size=5,
        max_overflow=5,
        pool_pre_ping=True,   # a Postgres restart shouldn't 500 the next request
        pool_recycle=1800,    # pre-empt idle-connection reaping by proxies
        # ``application_name`` shows up in pg_stat_activity — makes it obvious
        # in a live DB whether a query came from web or worker.
        connect_args={"server_settings": {"application_name": application_name}},
    )


# ─────────────────────────── web / app_user ───────────────────────────


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = _build_engine(
            get_settings().DATABASE_URL, application_name="fristen-web"
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            get_engine(), expire_on_commit=False
        )
    return _session_factory


# ─────────────────────────── worker / app_jobs ───────────────────────────


def get_jobs_engine() -> AsyncEngine:
    global _jobs_engine
    if _jobs_engine is None:
        _jobs_engine = _build_engine(
            get_settings().JOBS_DATABASE_URL, application_name="fristen-worker"
        )
    return _jobs_engine


def get_jobs_session_factory() -> async_sessionmaker[AsyncSession]:
    global _jobs_session_factory
    if _jobs_session_factory is None:
        _jobs_session_factory = async_sessionmaker(
            get_jobs_engine(), expire_on_commit=False
        )
    return _jobs_session_factory


# ─────────────────────────────── shutdown ───────────────────────────────


async def dispose_engines() -> None:
    """Close pools on graceful shutdown. Safe to call when engines were never
    built (idempotent) and resets the singletons so tests get fresh pools."""
    global _engine, _jobs_engine, _session_factory, _jobs_session_factory
    for eng in (_engine, _jobs_engine):
        if eng is not None:
            await eng.dispose()
    _engine = _jobs_engine = None
    _session_factory = _jobs_session_factory = None
