"""Scheduled compliance sweeps — escalation nag/breach and deadline reminders.

Both sweeps ENQUEUE OutboxMessage rows and commit; the relay performs the
actual sending (I3: no network I/O in a state-changing transaction). Both run
as app_jobs (BYPASSRLS) so they see every tenant, filter by company_id
explicitly, ping the heartbeat only after a fully successful pass, and are
scheduled with a misfire_grace_time so a worker that was briefly down still
runs the sweep on recovery (catch-up).

Idempotency is the crux:
  * escalations — nag at most once per calendar day (EscalationCase.last_nag_on)
    and breach at most once (the OPEN→BREACHED flip removes it from the query);
  * reminders — one outbox row per (deadline, notch, channel, recipient), keyed
    by dedup_key ``rem:{deadline_id}:{notch}:{channel}:{recipient}``. After
    downtime that skipped several notches, only the MOST URGENT missed notch is
    sent (reminder_notch returns a single applicable notch), not a backlog.
"""
from __future__ import annotations

import logging
import uuid
from datetime import date
from typing import Protocol

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.dates import BERLIN, days_until, now_berlin, today_berlin
from app.models import (
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
from app.services.e6 import Recipient
from app.services.recipients import resolve_recipients

logger = logging.getLogger(__name__)

REMINDER_NOTCHES = (0, 7, 30, 90)  # most-urgent first; see reminder_notch
_KIND_LABEL = {
    DeadlineKind.HU: "Hauptuntersuchung (HU/AU)",
    DeadlineKind.UVV: "UVV-Pruefung",
    DeadlineKind.FSK: "Fuehrerscheinkontrolle",
}


class DeadManSwitch(Protocol):
    async def ping(self) -> None: ...


# ─────────────────────────── enqueue helper ───────────────────────────


async def _enqueue_unique(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    channel: OutboxChannel,
    recipient: str,
    template: str,
    payload: dict[str, object],
    priority: int,
    dedup_key: str,
) -> bool:
    """Insert an outbox row unless its dedup_key already exists. Safe to
    existence-check-then-insert because sweeps run single-instance
    (max_instances=1) and the relay never inserts outbox rows."""
    exists = await session.scalar(
        select(OutboxMessage.id).where(OutboxMessage.dedup_key == dedup_key)
    )
    if exists is not None:
        return False
    session.add(
        OutboxMessage(
            company_id=company_id,
            channel=channel,
            recipient=recipient,
            template=template,
            payload=payload,
            priority=priority,
            dedup_key=dedup_key,
        )
    )
    return True


def _channels(recipient: Recipient) -> list[tuple[OutboxChannel, str]]:
    out: list[tuple[OutboxChannel, str]] = []
    if recipient.email:
        out.append((OutboxChannel.EMAIL, recipient.email))
    if recipient.phone_e164:
        out.append((OutboxChannel.SMS, recipient.phone_e164))
    return out


# ═══════════════════════ escalation sweep ═══════════════════════


async def sweep_escalations(
    session_factory: async_sessionmaker[AsyncSession],
    heartbeat: DeadManSwitch,
) -> None:
    """Nag OPEN cases once per day; breach those past their deadline."""
    now = now_berlin()
    today = today_berlin()
    async with session_factory() as s, s.begin():
        cases = (
            await s.scalars(
                select(EscalationCase)
                .where(EscalationCase.status == EscalationStatus.OPEN)
                .with_for_update(skip_locked=True)
            )
        ).all()

        for case in cases:
            driver = await s.get(Driver, case.driver_id)
            name = f"{driver.first_name} {driver.last_name}" if driver else "—"
            recipients = await resolve_recipients(s, case.company_id)
            ctx: dict[str, object] = {
                "driver_name": name,
                "deadline": f"{case.deadline_at.astimezone(BERLIN):%d.%m.%Y}",
                "case_path": f"/drivers/{case.driver_id}/escalation",
                "company_id": str(case.company_id),
            }

            if now > case.deadline_at:
                case.status = EscalationStatus.BREACHED
                for r in recipients:
                    for channel, addr in _channels(r):
                        await _enqueue_unique(
                            s, company_id=case.company_id, channel=channel,
                            recipient=addr, template="escalation_breached",
                            payload=ctx, priority=0,
                            dedup_key=f"esc:{case.id}:breached:{channel}:{addr}")
                logger.info("escalation BREACHED case=%s", case.id)
            elif case.last_nag_on != today:
                case.last_nag_on = today
                for r in recipients:
                    for channel, addr in _channels(r):
                        await _enqueue_unique(
                            s, company_id=case.company_id, channel=channel,
                            recipient=addr, template="escalation_nag",
                            payload=ctx, priority=50,
                            dedup_key=f"esc:{case.id}:nag:{today}:{channel}:{addr}")

    await heartbeat.ping()


# ═══════════════════════ reminder sweep ═══════════════════════


def reminder_notch(due_on: date, today: date) -> tuple[str, int] | None:
    """Return (dedup_token, display_notch) for the single applicable reminder,
    or None if nothing is due yet.

    Pre-due: the smallest crossed threshold (most urgent) — d=5 → ("7", 7).
    Overdue: a weekly token so exactly one overdue reminder goes out per ISO
    week — display_notch -1 tells the template to say "ueberfaellig"."""
    d = days_until(due_on, today=today)
    if d >= 0:
        for n in REMINDER_NOTCHES:      # 0, 7, 30, 90
            if d <= n:
                return (str(n), n)
        return None                      # more than 90 days out
    iso = today.isocalendar()
    return (f"ov{iso.year}W{iso.week:02d}", -1)


async def _subject_context(session: AsyncSession, d: Deadline) -> dict[str, object]:
    if d.driver_id is not None:
        driver = await session.get(Driver, d.driver_id)
        name = f"{driver.first_name} {driver.last_name}" if driver else "—"
        deep_link = f"/drivers/{d.driver_id}/history"
    else:
        vehicle = await session.get(Vehicle, d.vehicle_id)
        name = vehicle.kennzeichen if vehicle else "—"
        deep_link = f"/vehicles/{d.vehicle_id}/history"
    return {"subject_name": name, "deep_link": deep_link}


async def send_reminders(
    session_factory: async_sessionmaker[AsyncSession],
    heartbeat: DeadManSwitch,
) -> None:
    """Threshold sweep over OPEN deadlines at 90/30/7/0 + weekly overdue."""
    today = today_berlin()
    async with session_factory() as s, s.begin():
        deadlines = (
            await s.scalars(
                select(Deadline).where(Deadline.status == DeadlineStatus.OPEN)
            )
        ).all()

        for d in deadlines:
            notch = reminder_notch(d.due_on, today)
            if notch is None:
                continue
            token, display = notch
            subject = await _subject_context(s, d)
            recipients = await resolve_recipients(s, d.company_id)
            payload = {
                "company_id": str(d.company_id),
                "kind": d.kind.value,
                "kind_label": _KIND_LABEL.get(d.kind, d.kind.value),
                "due_date": f"{d.due_on:%d.%m.%Y}",
                "notch": display,
                **subject,
            }
            for r in recipients:
                for channel, addr in _channels(r):
                    await _enqueue_unique(
                        s, company_id=d.company_id, channel=channel,
                        recipient=addr, template="deadline_reminder",
                        payload=payload, priority=100,
                        dedup_key=f"rem:{d.id}:{token}:{channel}:{addr}")

    await heartbeat.ping()


# ─────────────────────────── registration ───────────────────────────


def register(
    scheduler: AsyncIOScheduler,
    session_factory: async_sessionmaker[AsyncSession],
    heartbeat: DeadManSwitch,
) -> None:
    """Schedule both sweeps. misfire_grace_time lets a sweep still fire after a
    brief worker outage (catch-up); coalesce collapses a backlog of missed
    ticks into a single run."""
    scheduler.add_job(
        sweep_escalations,
        CronTrigger(hour=6, minute=0, timezone=BERLIN),
        args=[session_factory, heartbeat],
        id="sweep_escalations",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        send_reminders,
        CronTrigger(hour=6, minute=30, timezone=BERLIN),
        args=[session_factory, heartbeat],
        id="send_reminders",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
