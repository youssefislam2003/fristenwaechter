"""T7 — end-to-end web flow against real Postgres, driving the definition of
done: signup → add vehicle + driver → record a FAILED FSK (driver locked, case
opened, email+SMS staged in the outbox) → a PASSED re-check unlocks & resolves
→ the history timeline shows the hash-chained log.

Runs the ASGI app via httpx.ASGITransport inside the test's own event loop, with
the web session factory pointed at the app_user (RLS) engine — so the whole
stack, RLS included, is exercised exactly as in production.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

import app.db as db
from app.main import app as fastapi_app
from app.models import (
    ComplianceCheckLog,
    Deadline,
    Driver,
    EscalationCase,
    EscalationStatus,
    OutboxChannel,
    OutboxMessage,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture()
async def client(app_engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    """ASGI client whose web session factory is bound to the app_user engine
    created on THIS test's event loop (asyncpg pools are loop-bound)."""
    db._session_factory = async_sessionmaker(app_engine, expire_on_commit=False)
    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver",
        follow_redirects=False,
    ) as c:
        yield c
    db._session_factory = None


async def _signup(client: httpx.AsyncClient) -> None:
    r = await client.post("/signup", data={
        "company_name": "Muster Handwerk GmbH", "email": "chef@muster.de",
        "phone_e164": "+4915112345678", "password": "sehr-geheim-123"})
    assert r.status_code == 303, r.text
    assert client.cookies.get("fw_session")


class TestWebFlow:
    async def test_full_compliance_flow(self, client, admin_sf) -> None:
        # 1) signup → authenticated session cookie
        await _signup(client)

        # 2) dashboard reachable, empty state
        r = await client.get("/dashboard")
        assert r.status_code == 200
        assert "Übersicht" in r.text

        # 3) add a vehicle → HU + UVV auto-spawned
        r = await client.post("/vehicles", data={
            "kennzeichen": "b-ab 1234", "hu_interval_months": "24"})
        assert r.status_code == 200
        assert "B-AB 1234" in r.text  # normalized + listed

        # 4) add a driver → FSK auto-spawned
        r = await client.post("/drivers", data={
            "first_name": "Max", "last_name": "Mustermann",
            "employment_start": "2026-01-10"})
        assert r.status_code == 200

        # look up the ids as superuser (bypasses RLS)
        async with admin_sf() as s:
            driver_id = await s.scalar(select(Driver.id))
            deadlines = await s.scalar(select(func.count()).select_from(Deadline))
        assert deadlines == 3  # HU + UVV + FSK

        # 5) record a FAILED Führerscheinkontrolle → E6
        r = await client.post(f"/drivers/{driver_id}/checks",
                              data={"result": "FAILED", "kind": "FSK",
                                    "performed_on": "2026-07-11"})
        assert r.status_code == 303

        async with admin_sf() as s:
            driver = await s.get(Driver, driver_id)
            open_cases = await s.scalar(select(func.count()).select_from(
                EscalationCase).where(EscalationCase.status == EscalationStatus.OPEN))
            email_rows = await s.scalar(select(func.count()).select_from(
                OutboxMessage).where(
                    OutboxMessage.channel == OutboxChannel.EMAIL,
                    OutboxMessage.template == "fsk_failed_critical"))
            sms_rows = await s.scalar(select(func.count()).select_from(
                OutboxMessage).where(OutboxMessage.channel == OutboxChannel.SMS))
        assert driver.is_authorized_to_drive is False
        assert driver.lock_reason == "FSK_FAILED"
        assert open_cases == 1
        assert email_rows == 1 and sms_rows == 1   # §21 broadcast staged

        # 6) dashboard now pins the locked driver with the § 21 banner
        r = await client.get("/dashboard")
        assert "§ 21" in r.text and "GESPERRT" in r.text

        # 7) a PASSED re-check unlocks and resolves the case
        r = await client.post(f"/drivers/{driver_id}/checks",
                              data={"result": "PASSED", "kind": "FSK",
                                    "performed_on": "2026-07-14"})
        assert r.status_code == 303

        async with admin_sf() as s:
            driver = await s.get(Driver, driver_id)
            case = await s.scalar(select(EscalationCase))
            checks_n = await s.scalar(select(func.count()).select_from(
                ComplianceCheckLog).where(ComplianceCheckLog.driver_id == driver_id))
        assert driver.is_authorized_to_drive is True
        assert case.status == EscalationStatus.RESOLVED_PASSED
        assert checks_n == 2  # FAILED then PASSED, both chained

        # 8) history timeline shows the hash-chained evidence
        r = await client.get(f"/drivers/{driver_id}/history")
        assert r.status_code == 200
        assert "Nachweis-Kette" in r.text
        assert "PASSED" in r.text and "FAILED" in r.text

    async def test_cross_tenant_history_is_404(self, client, admin_sf) -> None:
        """A driver id from another tenant must not be viewable (RLS → None →
        404), proving I5 holds at the HTTP layer."""
        await _signup(client)
        # Seed a second company + driver directly (superuser).
        from app.models import Company

        async with admin_sf() as s, s.begin():
            other = Company(name="Fremd GmbH")
            s.add(other)
            await s.flush()
            foreign = Driver(company_id=other.id, first_name="Erika",
                             last_name="Fremd")
            s.add(foreign)
            await s.flush()
            foreign_id = foreign.id

        r = await client.get(f"/drivers/{foreign_id}/history")
        assert r.status_code == 404
