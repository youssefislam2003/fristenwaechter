"""Unit tests for the config boundary — pure, no DB, no Docker.

Guards the two properties that matter operationally: async-driver enforcement
(a sync DSN is a footgun that must fail loudly at config time, not at first
query) and secret masking (no provider key should ever reach a log).
"""
from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from app.settings import Settings

_BASE_ENV = {
    "DATABASE_URL": "postgresql+asyncpg://app_user:x@localhost:5432/fristen",
    "JOBS_DATABASE_URL": "postgresql+asyncpg://app_jobs:x@localhost:5432/fristen",
    "BREVO_API_KEY": "brevo-secret",
    "SEVEN_IO_API_KEY": "seven-secret",
    "SECRET_KEY": "cookie-signing-secret",
}


def _settings(**overrides: str) -> Settings:
    # _env_file=None ⇒ ignore any developer's local .env during tests.
    return Settings(_env_file=None, **{**_BASE_ENV, **overrides})  # type: ignore[arg-type]


def test_defaults_applied() -> None:
    s = _settings()
    assert s.SMS_MONTHLY_CAP_PER_COMPANY == 200
    assert s.BASE_URL == "http://localhost:8000"
    assert s.HEARTBEAT_URL == ""


def test_base_url_trailing_slash_stripped() -> None:
    assert _settings(BASE_URL="https://app.example.de/").BASE_URL == (
        "https://app.example.de"
    )


def test_secrets_are_masked() -> None:
    s = _settings()
    assert isinstance(s.BREVO_API_KEY, SecretStr)
    assert "brevo-secret" not in repr(s)
    assert "brevo-secret" not in str(s.BREVO_API_KEY)
    assert s.BREVO_API_KEY.get_secret_value() == "brevo-secret"


@pytest.mark.parametrize("field", ["DATABASE_URL", "JOBS_DATABASE_URL"])
def test_sync_dsn_rejected(field: str) -> None:
    with pytest.raises(ValidationError, match="asyncpg"):
        _settings(**{field: "postgresql://app_user:x@localhost/fristen"})


def test_negative_sms_cap_rejected() -> None:
    with pytest.raises(ValidationError):
        _settings(SMS_MONTHLY_CAP_PER_COMPANY="-1")
