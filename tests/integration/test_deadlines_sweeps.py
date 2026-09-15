"""T6 — deadline roll-forward, hash-chained evidence, and the sweeps, against
real Postgres. Uses admin_sf (superuser bypasses RLS, mirroring the app_jobs
BYPASSRLS worker role) so these tests exercise the state machine, not RLS —
which is covered separately in test_auth_rls.
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import func, select

from app.dates import BERLIN, escalation_deadline, next_fsk_due, now_berlin
from app.models import (
    AccountUser,
    Company,
    ComplianceCheckLog,
    Deadline,
    DeadlineKind,
    DeadlineStatus,
    Driver,
    EscalationCase,
    EscalationStatus,
    OutboxChannel,
    OutboxMessage,
    Vehicle,
)
from app.services import checks
from app.services.e6 import Recipient, run_e6
from app.jobs import sweeps

pytestmark = pytest.mark.asyncio


class _NullHeartbeat:
    def __init__(self) -> None:
        self.pings = 0

    async def ping(self) -> None:
        self.pings += 1


async def _seed(admin_sf) -> dict:
    async with admin_sf() as s, s.begin():
        company = Company(name="Fuhrpark GmbH")
        s.add(company)
        await s.flush()
        owner = AccountUser(company_id=company.id, email="chef@fp.de",
                            role="OWNER", phone_e164="+4915100000001")
        driver = Driver(company_id=company.id, first_name="Max",
                        last_name="Mustermann")
        vehicle = Vehicle(company_id=company.id, kennzeichen="B-AB 123",
                          hu_interval_months=24)
        s.add_all([owner, driver, vehicle])
        await s.flush()
        return {"company_id": company.id, "owner_id": owner.id,
                "driver_id": driver.id, "vehicle_id": vehicle.id}


# ══════════════════════ roll-forward ══════════════════════


class TestCompleteDeadline:
    async def test_hu_passed_spawns_next_hu_24_months_out(self, admin_sf) -> None:
        seed = await _seed(admin_sf)
        async with admin_sf() as s, s.begin():
            dl = Deadline(company_id=seed["company_id"], kind=DeadlineKind.HU,
                          vehicle_id=seed["vehicle_id"], due_on=date(2026, 7, 31))
            s.add(dl)
            await s.flush()
            dl_id = dl.id

        async with admin_sf() as s, s.begin():
            res = await checks.complete_deadline(
                s, deadline_id=dl_id, company_id=seed["company_id"],
                result=checks.RESULT_PASSED, completed_on=date(2026, 7, 20))

        async with admin_sf() as s:
            successor = await s.get(Deadline, res.successor_deadline_id)
            old = await s.get(Deadline, dl_id)
        assert old.status == DeadlineStatus.COMPLETED
        assert successor.kind == DeadlineKind.HU
        assert successor.due_on == date(2028, 7, 31)   # +24mo, month-granular
        assert successor.is_nachpruefung is False

    async def test_hu_failed_defects_spawns_28_day_nachpruefung(
        self, admin_sf
    ) -> None:
        seed = await _seed(admin_sf)
        async with admin_sf() as s, s.begin():
            dl = Deadline(company_id=seed["company_id"], kind=DeadlineKind.HU,
                          vehicle_id=seed["vehicle_id"], due_on=date(2026, 7, 31))
            s.add(dl)
            await s.flush()
            dl_id = dl.id

        async with admin_sf() as s, s.begin():
            res = await checks.complete_deadline(
                s, deadline_id=dl_id, company_id=seed["company_id"],
                result=checks.RESULT_FAILED_DEFECTS, completed_on=date(2026, 7, 20))

        assert res.is_nachpruefung is True
        async with admin_sf() as s:
            successor = await s.get(Deadline, res.successor_deadline_id)
        assert successor.is_nachpruefung is True
        assert successor.due_on == date(2026, 8, 17)   # 20.07 + 28 days

    async def test_fsk_passed_resolves_open_case_and_unlocks_driver(
        self, admin_sf
    ) -> None:
        seed = await _seed(admin_sf)
        # First: a FAILED FSK locks the driver and opens a case (via E6).
        async with admin_sf() as s, s.begin():
            e6 = await run_e6(s, seed["driver_id"],
                              company_id=seed["company_id"],
                              trigger_check_id=uuid.uuid4(),
                              recipients=[Recipient(email="chef@fp.de",
                                                    phone_e164="+4915100000001")])
        # An OPEN FSK deadline exists for the driver.
        async with admin_sf() as s, s.begin():
            dl = Deadline(company_id=seed["company_id"], kind=DeadlineKind.FSK,
                          driver_id=seed["driver_id"], due_on=date(2026, 7, 15))
            s.add(dl)
            await s.flush()
            dl_id = dl.id

        # Now: a PASSED re-check resolves the case and unlocks.
        async with admin_sf() as s, s.begin():
            res = await checks.complete_deadline(
                s, deadline_id=dl_id, company_id=seed["company_id"],
                result=checks.RESULT_PASSED, completed_on=date(2026, 7, 14),
                performed_by_id=seed["owner_id"])

        assert res.resolved_case_id == e6.case_id
        async with admin_sf() as s:
            driver = await s.get(Driver, seed["driver_id"])
            case = await s.get(EscalationCase, e6.case_id)
            successor = await s.get(Deadline, res.successor_deadline_id)
        assert driver.is_authorized_to_drive is True
        assert driver.lock_reason is None
        assert case.status == EscalationStatus.RESOLVED_PASSED
        assert successor.due_on == next_fsk_due(date(2026, 7, 14))

    async def test_check_log_hash_chain_links(self, admin_sf) -> None:
        seed = await _seed(admin_sf)
        async with admin_sf() as s, s.begin():
            first = await checks.record_check(
                s, company_id=seed["company_id"], kind="FSK",
                result="PASSED", performed_on=date(2026, 1, 10),
                driver_id=seed["driver_id"])
            first_hash = first.entry_hash
        async with admin_sf() as s, s.begin():
            second = await checks.record_check(
                s, company_id=seed["company_id"], kind="FSK",
                result="PASSED", performed_on=date(2026, 7, 10),
                driver_id=seed["driver_id"])
            second_prev = second.prev_hash
            second_hash = second.entry_hash

        assert first.prev_hash is None            # genesis entry
        assert second_prev == first_hash          # chain link holds
        assert second_hash != first_hash
        # Distinct vehicle chain doesn't interfere with the driver chain.
        async with admin_sf() as s, s.begin():
            v = await checks.record_check(
                s, company_id=seed["company_id"], kind="HU",
                result="PASSED", performed_on=date(2026, 7, 10),
                vehicle_id=seed["vehicle_id"])
        assert v.prev_hash is None                # first entry in vehicle chain


# ══════════════════════ escalation sweep ══════════════════════


class TestEscalationSweep:
    async def _open_case(self, admin_sf, seed, *, deadline_at) -> uuid.UUID:
        async with admin_sf() as s, s.begin():
            case = EscalationCase(company_id=seed["company_id"],
                                  driver_id=seed["driver_id"],
                                  deadline_at=deadline_at)
            s.add(case)
            await s.flush()
            return case.id

    async def test_nag_once_per_day_then_no_duplicate(self, admin_sf) -> None:
        seed = await _seed(admin_sf)
        # Deadline in the future → nag, not breach.
        await self._open_case(admin_sf, seed,
                              deadline_at=now_berlin() + timedelta(days=3))
        hb = _NullHeartbeat()

        await sweeps.sweep_escalations(admin_sf, hb)
        await sweeps.sweep_escalations(admin_sf, hb)   # same day → no new rows

        async with admin_sf() as s:
            nag_rows = await s.scalar(select(func.count()).select_from(
                OutboxMessage).where(OutboxMessage.template == "escalation_nag"))
        # One OWNER with email+phone → 2 rows, and only once for the day.
        assert nag_rows == 2
        assert hb.pings == 2

    async def test_breach_past_deadline_flips_and_broadcasts(self, admin_sf) -> None:
        seed = await _seed(admin_sf)
        case_id = await self._open_case(
            admin_sf, seed, deadline_at=now_berlin() - timedelta(hours=1))
        hb = _NullHeartbeat()

        await sweeps.sweep_escalations(admin_sf, hb)

        async with admin_sf() as s:
            case = await s.get(EscalationCase, case_id)
            breached = await s.scalar(select(func.count()).select_from(
                OutboxMessage).where(
                    OutboxMessage.template == "escalation_breached"))
        assert case.status == EscalationStatus.BREACHED
        assert breached == 2  # email + SMS to the OWNER


# ══════════════════════ reminder sweep ══════════════════════


class TestReminderSweep:
    async def _open_deadline(self, admin_sf, seed, *, due_on) -> uuid.UUID:
        async with admin_sf() as s, s.begin():
            dl = Deadline(company_id=seed["company_id"], kind=DeadlineKind.HU,
                          vehicle_id=seed["vehicle_id"], due_on=due_on)
            s.add(dl)
            await s.flush()
            return dl.id

    async def test_reminder_enqueued_and_idempotent(self, admin_sf) -> None:
        seed = await _seed(admin_sf)
        # 7 days out → notch 7.
        due = now_berlin().date() + timedelta(days=7)
        dl_id = await self._open_deadline(admin_sf, seed, due_on=due)
        hb = _NullHeartbeat()

        await sweeps.send_reminders(admin_sf, hb)
        await sweeps.send_reminders(admin_sf, hb)   # same notch → no duplicate

        async with admin_sf() as s:
            rows = (await s.scalars(select(OutboxMessage).where(
                OutboxMessage.template == "deadline_reminder"))).all()
        assert len(rows) == 2  # email + SMS, once each
        for r in rows:
            assert r.dedup_key.startswith(f"rem:{dl_id}:7:")
            assert r.payload["notch"] == 7
            assert r.payload["company_id"] == str(seed["company_id"])

    async def test_catchup_sends_only_most_urgent_missed_notch(
        self, admin_sf
    ) -> None:
        """Worker was down; the deadline is now 5 days out (past the 90 and 30
        notches). The sweep must send ONLY notch 7 — not a backlog of 90/30."""
        seed = await _seed(admin_sf)
        due = now_berlin().date() + timedelta(days=5)
        dl_id = await self._open_deadline(admin_sf, seed, due_on=due)
        hb = _NullHeartbeat()

        await sweeps.send_reminders(admin_sf, hb)

        async with admin_sf() as s:
            rows = (await s.scalars(select(OutboxMessage).where(
                OutboxMessage.template == "deadline_reminder"))).all()
        tokens = {r.dedup_key.split(":")[2] for r in rows}
        assert tokens == {"7"}, tokens   # only the most urgent notch
