"""Runtime configuration — the single typed source of every external secret
and tunable. Loaded once at import time via ``get_settings()``.

Two database URLs, never one: the web process connects as the least-privilege
``app_user`` role (RLS applies), the worker connects as ``app_jobs``
(BYPASSRLS, so the relay can drain the outbox across every tenant). Handing
the web app the jobs URL would silently defeat row-level tenant isolation —
so they are separate settings, validated separately, and wired to separate
engines in ``app/db.py``.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-driven configuration.

    Values come from (in precedence order) the process environment, then a
    local ``.env`` file for development. Secrets are ``SecretStr`` so they
    never leak into logs, tracebacks, or ``repr()`` by accident.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── databases ──────────────────────────────────────────────────────
    # Web/API runtime — the NOBYPASSRLS app_user role.
    DATABASE_URL: str = Field(
        ...,
        description="asyncpg DSN for the app_user role (RLS enforced).",
    )
    # Worker runtime — the BYPASSRLS app_jobs role (relay + sweeps).
    JOBS_DATABASE_URL: str = Field(
        ...,
        description="asyncpg DSN for the app_jobs role (cross-tenant relay).",
    )

    # ── notification providers (EU-only subprocessors) ─────────────────
    BREVO_API_KEY: SecretStr = Field(..., description="Brevo transactional email.")
    SEVEN_IO_API_KEY: SecretStr = Field(..., description="seven.io SMS gateway.")

    # Sender identity. Defaults are safe placeholders; production overrides via
    # env. The SMS sender must be ≤11 alphanumeric chars (GSM alphanumeric
    # originator) — seven.io rejects longer.
    MAIL_FROM_EMAIL: str = Field(default="no-reply@fristenwaechter.de")
    MAIL_FROM_NAME: str = Field(default="Fristenwächter")
    SMS_SENDER: str = Field(default="Fristenwch", max_length=11)

    # ── ops ────────────────────────────────────────────────────────────
    # Healthchecks.io-style dead-man switch; the worker pings it after every
    # successful relay cycle, its SILENCE is the alert.
    HEARTBEAT_URL: str = Field(
        default="",
        description="Dead-man switch URL pinged after each healthy relay cycle.",
    )
    # Local liveness file the worker touches each healthy cycle; the container
    # healthcheck fails when it goes stale (see scripts/worker_healthcheck.py).
    WORKER_HEARTBEAT_FILE: str = Field(default="/tmp/fristen-worker.alive")

    # ── security & links ───────────────────────────────────────────────
    SECRET_KEY: SecretStr = Field(
        ...,
        description="Signs session cookies / invitation tokens. Rotate ⇒ logout-all.",
    )
    BASE_URL: str = Field(
        default="http://localhost:8000",
        description="Public origin, used to build deep-links in notifications.",
    )

    # ── product limits ─────────────────────────────────────────────────
    # Hard cap on billable SMS per company per calendar month. When exceeded
    # the notifier skips SMS and raises a WARNING SystemAlert instead of
    # silently burning the customer's budget (see app/notifiers/seven.py).
    SMS_MONTHLY_CAP_PER_COMPANY: int = Field(default=200, ge=0)

    @field_validator("DATABASE_URL", "JOBS_DATABASE_URL")
    @classmethod
    def _require_async_driver(cls, v: str) -> str:
        """Both engines are async — a sync ``postgresql://`` DSN would blow up
        at engine construction with a far less obvious error, so reject it at
        the config boundary where the fix is nameable."""
        if not v.startswith("postgresql+asyncpg://"):
            raise ValueError(
                "must be an asyncpg DSN (postgresql+asyncpg://…); "
                f"got {v.split('://', 1)[0]}://…"
            )
        return v

    @field_validator("BASE_URL")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide singleton. ``lru_cache`` makes it lazy (nothing is read
    until first call, so imports stay side-effect-free) and memoized (every
    caller shares one validated instance). Tests override by clearing the
    cache: ``get_settings.cache_clear()``."""
    return Settings()  # type: ignore[call-arg]  # values sourced from env
