"""T8 — DSGVO purge job and self-service endpoints, against real Postgres.

The load-bearing assertion throughout: anonymization scrubs personal data but
NEVER touches the append-only evidence chain (invariant I1).
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date, timedelta

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

import app.db as db
from app.dates import add_months, now_berlin, today_berlin
from app.jobs import dsgvo as job
from app.main import app as fastapi_app
from app.models import (
    AccountUser,
    Company,
    ComplianceCheckLog,
    Driver,
    OutboxChannel,
    OutboxMessage,
)
from app.services import checks

pytestmark = pytest.mark.asyncio


# ─────────────────────────── purge job ───────────────────────────


async def _company_with_driver(admin_sf, *, retention=12) -> dict:
    async with admin_sf() as s, s.begin():
        c = Company(name="Retention GmbH", driver_retention_months=retention)
        s.add(c)
        await s.flush()
        d = Driver(company_id=c.id, first_name="Max", last_name="Mustermann")
        s.add(d)
        await s.flush()
        return {"company_id": c.id, "driver_id": d.id}


class TestPurgeJob:
    async def test_expired_driver_anonymized_but_evidence_kept(
        self, admin_sf
    ) -> None:
        seed = await _company_with_driver(admin_sf, retention=6)
        # Driver left long enough ago that retention has elapsed, and has a
        # check-log entry that must survive.
        async with admin_sf() as s, s.begin():
            d = await s.get(Driver, seed["driver_id"])
            d.employment_end = add_months(today_berlin(), -12)  # 12 mo ago
            await checks.record_check(
                s, company_id=seed["company_id"], kind="FSK", result="PASSED",
                performed_on=date(2025, 1, 1), driver_id=seed["driver_id"])

        async with admin_sf() as s, s.begin():
            n = await job.anonymize_expired_drivers(s)
        assert n == 1

        async with admin_sf() as s:
            d = await s.get(Driver, seed["driver_id"])
            log_n = await s.scalar(select(func.count()).select_from(
                ComplianceCheckLog).where(
                    ComplianceCheckLog.driver_id == seed["driver_id"]))
        assert d.first_name == "Gelöscht" and d.last_name == "Gelöscht"
        assert d.anonymized_at is not None
        assert log_n == 1  # evidence untouched

    async def test_recent_departure_not_yet_anonymized(self, admin_sf) -> None:
        seed = await _company_with_driver(admin_sf, retention=12)
        async with admin_sf() as s, s.begin():
            d = await s.get(Driver, seed["driver_id"])
            d.employment_end = add_months(today_berlin(), -3)  # within retention
        async with admin_sf() as s, s.begin():
            n = await job.anonymize_expired_drivers(s)
        assert n == 0

    async def test_prune_old_outbox(self, admin_sf) -> None:
        seed = await _company_with_driver(admin_sf)
        async with admin_sf() as s, s.begin():
            old = OutboxMessage(
                company_id=seed["company_id"], channel=OutboxChannel.EMAIL,
                recipient="a@b.de", template="deadline_reminder", payload={},
                dedup_key="old-1")
            s.add(old)
            await s.flush()
            # Force created_at to 30 months ago.
            from sqlalchemy import text, update
            await s.execute(update(OutboxMessage).where(OutboxMessage.id == old.id)
                            .values(created_at=text("now() - interval '30 months'")))
        async with admin_sf() as s, s.begin():
            pruned = await job.prune_old_outbox(s)
        assert pruned == 1
        async with admin_sf() as s:
            assert await s.scalar(select(func.count()).select_from(
                OutboxMessage)) == 0

    async def test_company_tombstoned_after_grace(self, admin_sf) -> None:
        seed = await _company_with_driver(admin_sf)
        async with admin_sf() as s, s.begin():
            c = await s.get(Company, seed["company_id"])
            c.deletion_requested_at = now_berlin() - timedelta(days=31)
            s.add(AccountUser(company_id=c.id, email="owner@ret.de",
                              role="OWNER", phone_e164="+491512223334"))
            await checks.record_check(
                s, company_id=c.id, kind="FSK", result="PASSED",
                performed_on=date(2025, 6, 1), driver_id=seed["driver_id"])

        async with admin_sf() as s, s.begin():
            n = await job.tombstone_deleted_companies(s)
        assert n == 1

        async with admin_sf() as s:
            c = await s.get(Company, seed["company_id"])
            d = await s.get(Driver, seed["driver_id"])
            user = await s.scalar(select(AccountUser).where(
                AccountUser.company_id == seed["company_id"]))
            log_n = await s.scalar(select(func.count()).select_from(
                ComplianceCheckLog).where(
                    ComplianceCheckLog.company_id == seed["company_id"]))
        assert c.name == "Gelöscht" and c.deleted_at is not None
        assert d.first_name == "Gelöscht"
        assert user.is_active is False and "invalid" in user.email
        assert log_n == 1  # evidence retained through tenant deletion


# ─────────────────────────── endpoints ───────────────────────────


@pytest.fixture()
async def client(app_engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    db._session_factory = async_sessionmaker(app_engine, expire_on_commit=False)
    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://testserver",
                                 follow_redirects=False) as c:
        yield c
    db._session_factory = None


async def _signup_owner(client) -> None:
    r = await client.post("/signup", data={
        "company_name": "DSGVO Test GmbH", "email": "owner@dsgvo.de",
        "password": "sehr-geheim-123"})
    assert r.status_code == 303


class TestDsgvoEndpoints:
    async def test_export_json_owner_only(self, client) -> None:
        await _signup_owner(client)
        r = await client.get("/export")
        assert r.status_code == 200
        body = r.json()
        assert "check_logs" in body and "drivers" in body
        assert r.headers["content-disposition"].endswith('.json"')

    async def test_export_csv(self, client) -> None:
        await _signup_owner(client)
        r = await client.get("/export.csv")
        assert r.status_code == 200
        assert "text/csv" in r.headers["content-type"]
        assert "entry_hash" in r.text

    async def test_deletion_request_sets_timestamp(self, client, admin_sf) -> None:
        await _signup_owner(client)
        r = await client.post("/company/delete-request")
        assert r.status_code == 303
        async with admin_sf() as s:
            c = await s.scalar(select(Company).where(
                Company.name == "DSGVO Test GmbH"))
        assert c.deletion_requested_at is not None

    async def test_legal_pages_public(self, client) -> None:
        for path in ("/impressum", "/datenschutz"):
            r = await client.get(path)
            assert r.status_code == 200
            assert "LAWYER-REVIEW-REQUIRED" in r.text
        r = await client.get("/avv")
        assert r.status_code == 200
        assert "LAWYER-REVIEW-REQUIRED" in r.text
