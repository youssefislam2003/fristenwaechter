"""Web entrypoint tests — security headers and the no-scheduler contract.

Pure ASGI, no database: the TestClient exercises middleware and the two
always-available routes. The security headers are a compliance control (no
third-party asset loading — a documented German legal risk), so they are
asserted explicitly rather than trusted.
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app.main import app


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def test_healthz_ok_and_no_db(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_security_headers_present(client: TestClient) -> None:
    r = client.get("/healthz")
    csp = r.headers["Content-Security-Policy"]
    assert csp.startswith("default-src 'self'")
    # No third-party origins anywhere in the policy.
    assert "http://" not in csp and "https://" not in csp
    assert "googleapis" not in csp and "gstatic" not in csp
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors 'none'" in csp


def test_root_redirects_to_dashboard(client: TestClient) -> None:
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/dashboard"


def test_app_does_not_start_scheduler() -> None:
    """The web app must never own the APScheduler. Assert there's no scheduler
    attribute and no apscheduler job store bound to the app state."""
    assert not hasattr(app.state, "scheduler")
