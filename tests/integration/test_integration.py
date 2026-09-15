"""Integration suite — every guarantee tested against real PostgreSQL 16.

Test map:
  §1 E6 atomic lockout
     concurrency race · sequential idempotent replay · optimistic version
     guard · all-or-nothing rollback
  §2 Outbox relay
     two concurrent relays partition via SKIP LOCKED · exponential backoff
     to dead-letter with CRITICAL escalation
  §3 Calendar
     7-day window straddling CET→CEST survives a timestamptz round-trip
  §4 Hardening
     append-only triggers block UPDATE/DELETE even for the table owner ·
     app_user role: DML allowed exactly where granted, DDL/DELETE/evidence-
     UPDATE all refused by the server
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm.exc import StaleDataError

from app.dates import BERLIN, escalation_deadline
from app.jobs import relay
from app.jobs.relay import NotifierError, relay_outbox
from app.models import (
    Company,
    ComplianceCheckLog,
    Driver,
    EscalationCase,
    EscalationStatus,
    OutboxChannel,
    OutboxMessage,
    SystemAlert,
)
from app.services.e6 import Recipient, StaleDriverState, run_e6

# async tests collected via asyncio_mode=auto (pytest.ini) — no marks needed

RECIPIENTS = [Recipient(email="chef@muster.de", phone_e164="+4915112345678")]


async def _one_scalar(sf, stmt):
    async with sf() as s:
        return await s.scalar(stmt)


# ══════════════════════ §1 — E6 atomic lockout ══════════════════════


class TestE6Concurrency:
    async def test_concurrent_race_yields_exactly_one_open_case(
        self, admin_sf: async_sessionmaker[AsyncSession], seeded: dict
    ) -> None:
        """Two tasks fire E6 for the same driver simultaneously.

        Expected mechanics: the loser blocks on the winner's SELECT…FOR
        UPDATE of the driver row; after the winner commits, the loser's
        case INSERT trips uq_escalation_open_per_driver, the SAVEPOINT in
        run_e6 rolls back cleanly, and the loser returns the WINNER's case
        as an idempotent replay. No exception escapes; no duplicate state.
        """
        async def attempt() -> object:
            async with admin_sf() as s, s.begin():
                return await run_e6(
                    s, seeded["driver_id"],
                    company_id=seeded["company_id"],
                    trigger_check_id=uuid.uuid4(),   # distinct checks, same driver
                    recipients=RECIPIENTS,
                )

        r1, r2 = await asyncio.gather(attempt(), attempt())

        winners = [r for r in (r1, r2) if not r.already_open]
        replays = [r for r in (r1, r2) if r.already_open]
        assert len(winners) == 1 and len(replays) == 1
        assert replays[0].case_id == winners[0].case_id      # same case surfaced
        assert replays[0].outbox_message_ids == ()           # no dup broadcast

        open_cases = await _one_scalar(admin_sf, select(func.count()).where(
            EscalationCase.driver_id == seeded["driver_id"],
            EscalationCase.status == EscalationStatus.OPEN))
        assert open_cases == 1                               # partial index held

        driver = await _one_scalar(admin_sf, select(Driver).where(
            Driver.id == seeded["driver_id"]))
        assert driver.is_authorized_to_drive is False
        assert driver.lock_reason == "FSK_FAILED"

        outbox = await _one_scalar(admin_sf, select(func.count()).select_from(
            OutboxMessage))
        assert outbox == 2  # exactly one EMAIL + one SMS — from the winner only

    async def test_sequential_replay_is_idempotent(
        self, admin_sf, seeded
    ) -> None:
        check = uuid.uuid4()
        for expect_open in (False, True):     # same trigger check, retried
            async with admin_sf() as s, s.begin():
                res = await run_e6(s, seeded["driver_id"],
                                   company_id=seeded["company_id"],
                                   trigger_check_id=check,
                                   recipients=RECIPIENTS)
            assert res.already_open is expect_open
        assert await _one_scalar(admin_sf, select(func.count()).select_from(
            OutboxMessage)) == 2

    async def test_optimistic_version_guard_raises_stale(
        self, admin_sf, seeded
    ) -> None:
        """version_id_col in action: session A reads the driver WITHOUT a
        row lock, session B mutates+commits (version bump), A's flush must
        raise StaleDataError — the guard the E6 service maps to HTTP 409."""
        async with admin_sf() as s_a:
            stale = await s_a.get(Driver, seeded["driver_id"])

            async with admin_sf() as s_b, s_b.begin():
                fresh = await s_b.get(Driver, seeded["driver_id"])
                fresh.lock_reason = "TOUCHED_BY_B"

            stale.first_name = "Racy"
            with pytest.raises(StaleDataError):
                await s_a.flush()
            await s_a.rollback()

    async def test_e6_is_all_or_nothing(self, admin_sf, seeded) -> None:
        """ZERO network I/O inside the transaction means rollback must erase
        the ENTIRE transition — no locked-driver-without-alert limbo."""
        async with admin_sf() as s:
            async with s.begin():
                await run_e6(s, seeded["driver_id"],
                             company_id=seeded["company_id"],
                             trigger_check_id=uuid.uuid4(),
                             recipients=RECIPIENTS)
                await s.rollback()

        for model in (EscalationCase, SystemAlert, OutboxMessage):
            assert await _one_scalar(
                admin_sf, select(func.count()).select_from(model)) == 0
        driver = await _one_scalar(admin_sf, select(Driver).where(
            Driver.id == seeded["driver_id"]))
        assert driver.is_authorized_to_drive is True         # lock rolled back


# ══════════════════════ §2 — outbox relay ══════════════════════


class RecordingNotifier:
    """Slow enough to force temporal overlap between two relay cycles."""

    def __init__(self, tag: str, delay: float = 0.25) -> None:
        self.tag, self.delay = tag, delay
        self.delivered: list[str] = []            # dedup_keys, in order

    async def send(self, *, recipient, template, payload, idempotency_key) -> str:
        await asyncio.sleep(self.delay)           # hold claimed rows locked
        self.delivered.append(idempotency_key)
        return f"{self.tag}-{len(self.delivered)}"


class FailingNotifier:
    def __init__(self) -> None:
        self.calls = 0

    async def send(self, **_) -> str:
        self.calls += 1
        raise NotifierError(f"provider down (call {self.calls})")


class NullHeartbeat:
    def __init__(self) -> None:
        self.pings = 0

    async def ping(self) -> None:
        self.pings += 1


async def _seed_outbox(admin_sf, company_id, n: int = 5) -> list[str]:
    keys = []
    async with admin_sf() as s, s.begin():
        for i in range(n):
            key = f"seed:{i}"
            keys.append(key)
            s.add(OutboxMessage(
                company_id=company_id, channel=OutboxChannel.EMAIL,
                recipient=f"user{i}@muster.de", template="reminder",
                payload={"i": i}, priority=100, dedup_key=key))
    return keys


class TestOutboxRelay:
    async def test_two_relays_partition_via_skip_locked(
        self, admin_sf, seeded, monkeypatch
    ) -> None:
        """5 pending rows, BATCH_SIZE forced to 3, two relays in parallel.

        FOR UPDATE SKIP LOCKED must make them partition the queue: whichever
        relay claims first locks ≤3 rows; the other's claim query SKIPS the
        locked rows and takes the remainder. Assertions are timing-agnostic:
        disjoint sets, full coverage, exactly-once delivery per key.
        """
        monkeypatch.setattr(relay, "BATCH_SIZE", 3)
        keys = set(await _seed_outbox(admin_sf, seeded["company_id"], n=5))

        n1, n2 = RecordingNotifier("relay-1"), RecordingNotifier("relay-2")
        hb = NullHeartbeat()
        await asyncio.gather(
            relay_outbox(admin_sf, {OutboxChannel.EMAIL: n1,
                                    OutboxChannel.SMS: n1}, hb),
            relay_outbox(admin_sf, {OutboxChannel.EMAIL: n2,
                                    OutboxChannel.SMS: n2}, hb),
        )

        set1, set2 = set(n1.delivered), set(n2.delivered)
        assert set1.isdisjoint(set2), "SKIP LOCKED failed: double-claim"
        assert set1 | set2 == keys, "a pending message was never claimed"
        assert len(n1.delivered) + len(n2.delivered) == 5   # exactly once each
        assert hb.pings == 2                                # both cycles healthy

        async with admin_sf() as s:
            rows = (await s.scalars(select(OutboxMessage))).all()
        assert all(r.sent_at is not None and r.attempts == 0 for r in rows)
        assert len({r.provider_message_id for r in rows}) == 5

    async def test_backoff_then_dead_letter_escalates(
        self, admin_sf, seeded
    ) -> None:
        await _seed_outbox(admin_sf, seeded["company_id"], n=1)
        notifier, hb = FailingNotifier(), NullHeartbeat()
        notifiers = {OutboxChannel.EMAIL: notifier, OutboxChannel.SMS: notifier}

        for expected_attempts in range(1, len(relay.BACKOFF) + 1):
            await relay_outbox(admin_sf, notifiers, hb)
            async with admin_sf() as s:
                row = await s.scalar(select(OutboxMessage))
                assert row.attempts == expected_attempts
                assert "provider down" in row.last_error
                if expected_attempts < len(relay.BACKOFF):
                    assert row.dead_lettered_at is None
                    assert row.next_attempt_at > datetime.now(timezone.utc)
                    # time-travel: yank next_attempt_at into the past so the
                    # next cycle retries immediately (backoff without sleeping)
                    await s.execute(update(OutboxMessage).values(
                        next_attempt_at=func.now() - text("interval '1 hour'")))
                    await s.commit()

        async with admin_sf() as s:
            row = await s.scalar(select(OutboxMessage))
            assert row.dead_lettered_at is not None
            assert notifier.calls == len(relay.BACKOFF)
            alert = await s.scalar(select(SystemAlert).where(
                SystemAlert.code == "NOTIFICATION_UNDELIVERABLE"))
            assert alert is not None and alert.severity == "CRITICAL"

        # dead-lettered rows are invisible to further cycles
        await relay_outbox(admin_sf, notifiers, hb)
        assert notifier.calls == len(relay.BACKOFF)


# ══════════════════════ §3 — DST round-trip ══════════════════════


class TestDstRoundTrip:
    async def test_seven_day_window_across_cet_cest_survives_timestamptz(
        self, admin_sf, seeded
    ) -> None:
        """EU DST 2027: Sunday 28.03., 02:00 CET → 03:00 CEST.
        Case opened Wed 24.03. 15:00 (+01:00); the 7-day window straddles
        the jump. Postgres stores timestamptz as UTC — the assertion is that
        after INSERT + SELECT the deadline still resolves to the correct
        BERLIN civil day with the POST-transition offset, and the elapsed
        real time is 7×24h minus the lost hour."""
        opened = datetime(2027, 3, 24, 15, 0, tzinfo=BERLIN)
        assert opened.utcoffset() == timedelta(hours=1)      # CET

        deadline = escalation_deadline(opened, days=7)
        async with admin_sf() as s, s.begin():
            s.add(EscalationCase(company_id=seeded["company_id"],
                                 driver_id=seeded["driver_id"],
                                 deadline_at=deadline))

        async with admin_sf() as s:                          # fresh session:
            stored = await s.scalar(select(EscalationCase))  # true DB round-trip
        local = stored.deadline_at.astimezone(BERLIN)

        assert (local.year, local.month, local.day) == (2027, 3, 31)
        assert (local.hour, local.minute, local.second) == (23, 59, 59)
        assert local.utcoffset() == timedelta(hours=2)       # CEST on deadline day
        lost_hour = 3600
        assert stored.deadline_at.timestamp() - opened.timestamp() == (
            7 * 24 * 3600 - lost_hour
            + (23 * 3600 + 59 * 60 + 59) - (15 * 3600))


# ══════════════════════ §4 — hardening boundaries ══════════════════════


async def _seed_check_log(admin_sf, seeded) -> uuid.UUID:
    async with admin_sf() as s, s.begin():
        row = ComplianceCheckLog(
            company_id=seeded["company_id"], driver_id=seeded["driver_id"],
            kind="FSK", result="PASSED", method="SICHT_ORIGINAL",
            performed_on=datetime(2026, 7, 1).date(),
            params={"license_presented": True},
            entry_hash=uuid.uuid4().hex + uuid.uuid4().hex)
        s.add(row)
        await s.flush()
        return row.id


class TestAppendOnlyTrigger:
    async def test_update_and_delete_blocked_even_for_table_owner(
        self, admin_sf, seeded
    ) -> None:
        log_id = await _seed_check_log(admin_sf, seeded)
        for stmt in (
            update(ComplianceCheckLog)
            .where(ComplianceCheckLog.id == log_id)
            .values(result="FAILED"),                        # rewrite history?
            text("DELETE FROM compliance_check_log"),        # erase history?
        ):
            async with admin_sf() as s:
                with pytest.raises(DBAPIError, match="append-only"):
                    async with s.begin():
                        await s.execute(stmt)
        # evidence intact:
        assert await _one_scalar(admin_sf, select(func.count()).select_from(
            ComplianceCheckLog)) == 1


class TestRuntimeRolePrivileges:
    async def test_granted_dml_works_and_everything_else_is_refused(
        self, admin_sf, app_engine, seeded
    ) -> None:
        await _seed_check_log(admin_sf, seeded)
        app_sf = async_sessionmaker(app_engine, expire_on_commit=False)

        # granted: UPDATE driver (the E6 lock itself) succeeds
        async with app_sf() as s, s.begin():
            await s.execute(update(Driver)
                            .where(Driver.id == seeded["driver_id"])
                            .values(lock_reason="APP_ROLE_CAN_LOCK"))

        refused = (
            # evidence UPDATE: refused by MISSING GRANT (layer 2, before the
            # trigger even fires — two independent locks on the same door)
            text("UPDATE compliance_check_log SET result = 'FAILED'"),
            text("DELETE FROM driver"),                       # no DELETE anywhere
            text("TRUNCATE outbox_message"),                  # no TRUNCATE
            text("ALTER TABLE driver ADD COLUMN backdoor int"),  # no DDL
            text("DROP TRIGGER compliance_check_log_immutable "
                 "ON compliance_check_log"),                  # cannot disarm trigger
            text("CREATE TABLE exfil (id int)"),              # schema CREATE revoked
        )
        for stmt in refused:
            async with app_sf() as s:
                with pytest.raises((ProgrammingError, DBAPIError)) as exc:
                    async with s.begin():
                        await s.execute(stmt)
                msg = str(exc.value).lower()
                assert ("permission denied" in msg or "must be owner" in msg
                        or "insufficientprivilege" in msg), stmt.text
