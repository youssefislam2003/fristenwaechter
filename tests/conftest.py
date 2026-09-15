"""Root test configuration — applies to the whole suite.

Only sets *defaults* (via ``setdefault``) for the environment variables
``app.settings`` requires, so unit tests that import ``app.main`` /
``app.settings`` work without a real ``.env``. It never overrides values the
integration suite or a developer has already exported, and it touches no
database — the Docker-backed fixtures live in ``tests/integration/conftest.py``.
"""
from __future__ import annotations

import os

_DEFAULT_ENV = {
    "DATABASE_URL": "postgresql+asyncpg://app_user:x@localhost:5432/fristen",
    "JOBS_DATABASE_URL": "postgresql+asyncpg://app_jobs:x@localhost:5432/fristen",
    "BREVO_API_KEY": "test-brevo-key",
    "SEVEN_IO_API_KEY": "test-seven-key",
    "SECRET_KEY": "test-secret-key-not-for-production",
    # BASE_URL intentionally omitted: it has a model default, and pinning it
    # here would leak into tests that assert the default value.
}

for _k, _v in _DEFAULT_ENV.items():
    os.environ.setdefault(_k, _v)
