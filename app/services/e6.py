"""E6 — atomic lockout & escalation for a failed Führerscheinkontrolle.

Contract of ``handle_failed_license_check``:

  * Runs entirely inside the CALLER's transaction. It never commits, never
    rolls back, and performs ZERO network I/O. The FastAPI dependency that
    yields the session owns the transaction boundary; the outbox relay owns
    delivery. This separation is what makes "the alert was lost" impossible:
    either the whole transition commits (lock + alert + case + outbox rows)
    or none of it exists.
  * Idempotent under retry and under concurrency:
      - double-submit of the same failed check → the partial unique index on
        (driver_id) WHERE status='OPEN' rejects a second case; we convert
        that into returning the existing case (no duplicate broadcast,
        thanks to outbox dedup_key).
      - a racing write to the Driver row → StaleDataError from the mapper's
        version_id guard, surfaced as StaleDriverState → HTTP 409 upstream.
  * Payload is metadata-only by construction (names, dates, URL path).
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from app.dates import escalation_deadline, now_berlin
from app.models import (
    AlertSeverity,
    Driver,
    EscalationCase,
    EscalationStatus,
    OutboxChannel,
    OutboxMessage,
    SystemAlert,
)

logger = logging.getLogger(__name__)

ESCALATION_DAYS = 7
LOCK_REASON_FSK = "FSK_FAILED"


class DriverNotFound(LookupError):
    """No such driver in this tenant → 404 upstream."""


class StaleDriverState(RuntimeError):
    """Concurrent modification detected by version_id guard → 409 upstream."""


@dataclass(frozen=True, slots=True)
class Recipient:
    """Pre-resolved notification target (resolved by the caller from the
    tenant's OWNER/MANAGER users — keeps this service free of user-model
    coupling and trivially unit-testable)."""

    email: str | None
    phone_e164: str | None


@dataclass(frozen=True, slots=True)
class E6Result:
    case_id: uuid.UUID
    alert_id: uuid.UUID | None  # None ⇒ idempotent replay, nothing new written
    outbox_message_ids: tuple[uuid.UUID, ...]
    already_open: bool


async def handle_failed_license_check(
    db_session: AsyncSession,
    driver_id: uuid.UUID,
    *,
    company_id: uuid.UUID,
    trigger_check_id: uuid.UUID,
    recipients: list[Recipient],
) -> E6Result:
    """Atomically lock the driver and stage the §21 StVG escalation.

    Effects (all-or-nothing within the enclosing transaction):
      1. Driver.is_authorized_to_drive → False  (optimistic version guard)
      2. EscalationCase(OPEN)                    (unique-per-driver, 7-day window)
      3. SystemAlert(CRITICAL)                   (immutable ledger)
      4. OutboxMessage × recipient × channel     (transactional outbox)
    """
    driver = await db_session.scalar(
        select(Driver)
        .where(Driver.id == driver_id, Driver.company_id == company_id)
        .with_for_update()  # serialize E6 vs. resolve on the same driver row
    )
    if driver is None:
        raise DriverNotFound(str(driver_id))

    now = now_berlin()
    deadline = escalation_deadline(now, days=ESCALATION_DAYS)

    # ── 1) hard lock ────────────────────────────────────────────────────
    # Plain attribute mutation: the mapper's version_id_col turns the flush
    # into `UPDATE … WHERE id=:id AND version_id=:expected`. If anything
    # else touched this row since our SELECT, flush raises StaleDataError.
    driver.is_authorized_to_drive = False
    driver.lock_reason = LOCK_REASON_FSK
    driver.locked_at = now

    # ── 2) unique OPEN escalation case ──────────────────────────────────
    case = EscalationCase(
        company_id=company_id,
        driver_id=driver.id,
        deadline_at=deadline,
        status=EscalationStatus.OPEN,
    )
    db_session.add(case)

    try:
        # Force the INSERT + versioned UPDATE to hit the DB now, inside the
        # caller's transaction, so both guard rails fire HERE — not at some
        # distant commit where we can no longer map the error to a driver.
        await db_session.flush()
    except IntegrityError:
        # Almost certainly uq_escalation_open_per_driver (a case is already
        # OPEN for this driver — double-submit/retry). An IntegrityError
        # inside flush poisons the enclosing (sub)transaction, so recovery
        # CANNOT happen here: we re-raise and let `run_e6`'s SAVEPOINT
        # wrapper roll back cleanly and translate this into an idempotent
        # replay. Any other integrity violation propagates from there too.
        raise
    except StaleDataError as exc:
        raise StaleDriverState(str(driver_id)) from exc

    # ── 3) immutable CRITICAL alert ─────────────────────────────────────
    alert = SystemAlert(
        company_id=company_id,
        severity=AlertSeverity.CRITICAL,
        code="FSK_FAILED_LOCK",
        driver_id=driver.id,
        escalation_case_id=case.id,
        message=(
            f"Führerscheinkontrolle FEHLGESCHLAGEN: {driver.first_name} "
            f"{driver.last_name}. Fahrer mit sofortiger Wirkung gesperrt. "
            f"Weiterer Fahrzeugeinsatz begründet das Risiko einer Strafbarkeit "
            f"nach § 21 Abs. 1 Nr. 2 StVG (Halterverantwortung). "
            f"Frist zur Klärung: {deadline:%d.%m.%Y}."
        ),
    )
    db_session.add(alert)

    # ── 4) transactional outbox — metadata only, CRITICAL priority ──────
    ctx = {
        "driver_name": f"{driver.first_name} {driver.last_name}",
        "deadline": f"{deadline:%d.%m.%Y}",
        "case_path": f"/drivers/{driver.id}/escalation",
    }
    messages: list[OutboxMessage] = []
    for r in recipients:
        if r.email:
            messages.append(
                OutboxMessage(
                    company_id=company_id,
                    channel=OutboxChannel.EMAIL,
                    recipient=r.email,
                    template="fsk_failed_critical",
                    payload=ctx,
                    priority=0,
                    dedup_key=f"e6:{trigger_check_id}:EMAIL:{r.email}",
                )
            )
        if r.phone_e164:
            messages.append(
                OutboxMessage(
                    company_id=company_id,
                    channel=OutboxChannel.SMS,
                    recipient=r.phone_e164,
                    template="fsk_failed_critical",
                    payload=ctx,
                    priority=0,
                    dedup_key=f"e6:{trigger_check_id}:SMS:{r.phone_e164}",
                )
            )
    db_session.add_all(messages)
    await db_session.flush()  # surfaces dedup_key collisions as IntegrityError

    logger.info(
        "E6 staged: driver=%s case=%s outbox=%d", driver.id, case.id, len(messages)
    )
    return E6Result(
        case_id=case.id,
        alert_id=alert.id,
        outbox_message_ids=tuple(m.id for m in messages),
        already_open=False,
    )


async def run_e6(
    db_session: AsyncSession,
    driver_id: uuid.UUID,
    *,
    company_id: uuid.UUID,
    trigger_check_id: uuid.UUID,
    recipients: list[Recipient],
) -> E6Result:
    """Public entry point: wraps the pipeline in a SAVEPOINT so the
    duplicate-open-case IntegrityError degrades to an idempotent success
    instead of poisoning the caller's outer transaction.
    """
    try:
        async with db_session.begin_nested():  # SAVEPOINT
            return await handle_failed_license_check(
                db_session,
                driver_id,
                company_id=company_id,
                trigger_check_id=trigger_check_id,
                recipients=recipients,
            )
    except IntegrityError:
        # SAVEPOINT rolled back; outer transaction is intact. Fetch the
        # already-open case and report the replay.
        existing = await db_session.scalar(
            select(EscalationCase).where(
                EscalationCase.driver_id == driver_id,
                EscalationCase.status == EscalationStatus.OPEN,
            )
        )
        if existing is None:  # not the duplicate-case race after all
            raise
        logger.info("E6 replay: driver=%s existing case=%s", driver_id, existing.id)
        return E6Result(
            case_id=existing.id,
            alert_id=None,
            outbox_message_ids=(),
            already_open=True,
        )
