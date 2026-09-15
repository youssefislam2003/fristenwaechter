"""Outbox relay — the only process allowed to perform notification I/O.

Delivery semantics: AT-LEAST-ONCE.
  The row is claimed under `FOR UPDATE SKIP LOCKED`, the provider is called,
  and `sent_at` is committed afterwards. A crash between provider-success
  and commit re-sends once on the next cycle. For §21 StVG alerts this bias
  is chosen deliberately: one duplicate SMS is noise; one silent drop is a
  Geschäftsführer unknowingly committing a criminal offense. Providers that
  accept an idempotency key (Brevo does) get `dedup_key` passed through,
  shrinking even that window to near-zero.

Concurrency: SKIP LOCKED makes N concurrent relay instances partition the
queue without coordination — today N=1 (single worker process), but the
query is already horizontal-scale-safe.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Protocol

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.dates import BERLIN, now_berlin
from app.models import AlertSeverity, OutboxChannel, OutboxMessage, SystemAlert

logger = logging.getLogger(__name__)

# attempt n waits BACKOFF[n] seconds before retry; exhausting → dead letter
BACKOFF: tuple[int, ...] = (0, 60, 300, 1_800, 7_200)  # 0s,1m,5m,30m,2h
BATCH_SIZE = 50
RELAY_INTERVAL_SECONDS = 15


class NotifierError(RuntimeError):
    """Any transport-level failure (HTTP 5xx, timeout, provider rejection)."""


class Notifier(Protocol):
    async def send(
        self, *, recipient: str, template: str, payload: dict[str, object], idempotency_key: str
    ) -> str:
        """Deliver one message; returns the provider message id."""
        ...


class DeadManSwitch(Protocol):
    async def ping(self) -> None:
        """Healthchecks.io-style heartbeat; silence pages the operator."""
        ...


async def relay_outbox(
    session_factory: async_sessionmaker[AsyncSession],
    notifiers: dict[OutboxChannel, Notifier],
    heartbeat: DeadManSwitch,
) -> None:
    """One relay cycle. Runs every 15 s; safe to overlap, safe to kill."""
    async with session_factory() as session:
        async with session.begin():
            rows = (
                await session.scalars(
                    select(OutboxMessage)
                    .where(
                        OutboxMessage.sent_at.is_(None),
                        OutboxMessage.dead_lettered_at.is_(None),
                        OutboxMessage.next_attempt_at <= now_berlin(),
                    )
                    # CRITICAL (priority 0) drains before routine reminders,
                    # FIFO within a priority band:
                    .order_by(OutboxMessage.priority, OutboxMessage.created_at)
                    .limit(BATCH_SIZE)
                    .with_for_update(skip_locked=True)
                )
            ).all()

            for row in rows:
                notifier = notifiers[row.channel]
                try:
                    msg_id = await notifier.send(
                        recipient=row.recipient,
                        template=row.template,
                        payload=row.payload,
                        idempotency_key=row.dedup_key,
                    )
                except NotifierError as exc:
                    _record_failure(session, row, exc)
                else:
                    row.sent_at = now_berlin()
                    row.provider_message_id = msg_id
                    logger.info(
                        "outbox delivered id=%s channel=%s attempt=%d",
                        row.id, row.channel, row.attempts + 1,
                    )
            # session.begin() commits here: claims released, states durable

    # heartbeat AFTER a fully successful cycle — its absence is the signal
    await heartbeat.ping()


def _record_failure(
    session: AsyncSession, row: OutboxMessage, exc: NotifierError
) -> None:
    row.attempts += 1
    row.last_error = str(exc)[:500]
    if row.attempts >= len(BACKOFF):
        # Dead letter — and because an undeliverable §21 alert is itself an
        # incident, escalate INSIDE the same transaction (an in-app CRITICAL
        # alert the customer sees at next login) and page the operator via
        # logging/monitoring. The row stays queryable for forensics.
        row.dead_lettered_at = now_berlin()
        session.add(
            SystemAlert(
                company_id=row.company_id,
                severity=AlertSeverity.CRITICAL,
                code="NOTIFICATION_UNDELIVERABLE",
                message=(
                    f"Zustellung fehlgeschlagen ({row.channel} an "
                    f"{row.recipient}) nach {row.attempts} Versuchen. "
                    f"Bitte Kontaktdaten prüfen."
                ),
            )
        )
        logger.critical(
            "outbox DEAD-LETTER id=%s channel=%s recipient=%s err=%s",
            row.id, row.channel, row.recipient, row.last_error,
        )
    else:
        delay = BACKOFF[row.attempts]
        row.next_attempt_at = now_berlin() + timedelta(seconds=delay)
        logger.warning(
            "outbox retry id=%s attempt=%d next=+%ds err=%s",
            row.id, row.attempts, delay, row.last_error,
        )


def register(
    scheduler: AsyncIOScheduler,
    session_factory: async_sessionmaker[AsyncSession],
    notifiers: dict[OutboxChannel, Notifier],
    heartbeat: DeadManSwitch,
) -> None:
    scheduler.add_job(
        relay_outbox,
        IntervalTrigger(seconds=RELAY_INTERVAL_SECONDS, timezone=BERLIN),
        args=[session_factory, notifiers, heartbeat],
        id="outbox_relay",
        max_instances=1,   # belt; SKIP LOCKED is the suspenders
        coalesce=True,     # missed ticks during a slow cycle collapse to one
    )
