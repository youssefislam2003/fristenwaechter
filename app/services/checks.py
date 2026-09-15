"""Check recording + deadline roll-forward — the compliance state machine.

Two responsibilities:

  * ``record_check`` appends a hash-chained ComplianceCheckLog row (invariant
    I1). The chain is per-SUBJECT: a driver's FSK entries chain on driver_id;
    a vehicle's HU/UVV entries chain via the vehicle id carried in ``params``
    (the evidence table has no vehicle_id column, and adding one would touch an
    append-only table — off-limits without sign-off). Each entry_hash folds in
    the previous entry_hash, so any retro-edit of history breaks every link
    after it — independent of, and in addition to, the DB append-only trigger.

  * ``complete_deadline`` marks a deadline COMPLETED, records the check, and
    spawns the successor obligation anchored on the ACTUAL completion date via
    the dates.py constructors. HU with defects spawns a 28-day Nachprüfung
    instead of the normal 24-month roll-forward. A PASSED FSK additionally
    resolves any OPEN escalation case and unlocks the driver
    (``_maybe_resolve_open_case`` — the mirror image of E6).

All functions run inside the CALLER's transaction and perform no network I/O.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import dates
from app.models import (
    ComplianceCheckLog,
    Deadline,
    DeadlineKind,
    DeadlineStatus,
    Driver,
    EscalationCase,
    EscalationStatus,
    Vehicle,
)

# Check results. FSK is PASSED/FAILED; HU adds FAILED_DEFECTS ("erhebliche
# Mängel" → Nachprüfung within one month).
RESULT_PASSED = "PASSED"
RESULT_FAILED = "FAILED"
RESULT_FAILED_DEFECTS = "FAILED_DEFECTS"

NACHPRUEFUNG_DAYS = 28


class DeadlineNotFound(LookupError):
    """No such deadline in this tenant → 404 upstream."""


# ─────────────────────────── hash chain ───────────────────────────


def _compute_entry_hash(prev_hash: str | None, fields: dict[str, object]) -> str:
    """SHA-256 over the canonical (sorted-key, compact) JSON of the entry,
    with the previous hash folded in. Deterministic across processes."""
    material = json.dumps(
        {"prev": prev_hash or "", **fields},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


async def _latest_hash_for_driver(
    session: AsyncSession, driver_id: uuid.UUID
) -> str | None:
    row: str | None = await session.scalar(
        select(ComplianceCheckLog.entry_hash)
        .where(ComplianceCheckLog.driver_id == driver_id)
        .order_by(ComplianceCheckLog.created_at.desc(),
                  ComplianceCheckLog.id.desc())
        .limit(1)
    )
    return row


async def _latest_hash_for_vehicle(
    session: AsyncSession, vehicle_id: uuid.UUID
) -> str | None:
    row: str | None = await session.scalar(
        select(ComplianceCheckLog.entry_hash)
        .where(
            ComplianceCheckLog.driver_id.is_(None),
            ComplianceCheckLog.params["vehicle_id"].astext == str(vehicle_id),
        )
        .order_by(ComplianceCheckLog.created_at.desc(),
                  ComplianceCheckLog.id.desc())
        .limit(1)
    )
    return row


async def record_check(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    kind: str,
    result: str,
    performed_on: date,
    driver_id: uuid.UUID | None = None,
    vehicle_id: uuid.UUID | None = None,
    method: str | None = None,
    performed_by_id: uuid.UUID | None = None,
    extra_params: dict[str, object] | None = None,
) -> ComplianceCheckLog:
    """Append one hash-chained evidence row. Exactly one of driver_id /
    vehicle_id must be given. The caller is expected to hold the subject row
    locked (FOR UPDATE) so the chain cannot fork under concurrency — as E6 and
    complete_deadline do."""
    if (driver_id is None) == (vehicle_id is None):
        raise ValueError("record_check needs exactly one of driver_id/vehicle_id")

    params: dict[str, object] = dict(extra_params or {})
    if vehicle_id is not None:
        params["vehicle_id"] = str(vehicle_id)
        prev_hash = await _latest_hash_for_vehicle(session, vehicle_id)
    else:
        assert driver_id is not None
        prev_hash = await _latest_hash_for_driver(session, driver_id)

    entry_hash = _compute_entry_hash(
        prev_hash,
        {
            "company_id": str(company_id),
            "driver_id": str(driver_id) if driver_id else None,
            "vehicle_id": str(vehicle_id) if vehicle_id else None,
            "kind": kind,
            "result": result,
            "performed_on": performed_on.isoformat(),
            "method": method,
            "params": params,
        },
    )
    row = ComplianceCheckLog(
        company_id=company_id,
        driver_id=driver_id,
        kind=kind,
        result=result,
        method=method,
        performed_by_id=performed_by_id,
        performed_on=performed_on,
        params=params,
        prev_hash=prev_hash,
        entry_hash=entry_hash,
    )
    session.add(row)
    await session.flush()
    return row


# ─────────────────────────── deadline roll-forward ───────────────────────────


@dataclass(frozen=True, slots=True)
class CompletionResult:
    completed_deadline_id: uuid.UUID
    successor_deadline_id: uuid.UUID
    check_log_id: uuid.UUID
    resolved_case_id: uuid.UUID | None  # set when a PASSED FSK cleared a lock
    is_nachpruefung: bool


async def _maybe_resolve_open_case(
    session: AsyncSession,
    driver: Driver,
    *,
    resolved_by_id: uuid.UUID | None,
) -> uuid.UUID | None:
    """Mirror image of E6: a PASSED Führerscheinkontrolle resolves the OPEN
    escalation case (if any) and lifts the § 21 hard lock. The driver row must
    already be locked FOR UPDATE by the caller."""
    case = await session.scalar(
        select(EscalationCase).where(
            EscalationCase.driver_id == driver.id,
            EscalationCase.status == EscalationStatus.OPEN,
        )
    )
    # Lift the lock regardless (a PASSED check means the driver is cleared).
    driver.is_authorized_to_drive = True
    driver.lock_reason = None
    driver.locked_at = None

    if case is None:
        return None
    case.status = EscalationStatus.RESOLVED_PASSED
    case.resolved_at = dates.now_berlin()
    case.resolved_by_id = resolved_by_id
    return case.id


async def complete_deadline(
    session: AsyncSession,
    *,
    deadline_id: uuid.UUID,
    company_id: uuid.UUID,
    result: str,
    completed_on: date,
    performed_by_id: uuid.UUID | None = None,
    method: str | None = None,
) -> CompletionResult:
    """Complete a deadline, record the evidence, and spawn the successor.

    Roll-forward rules (anchored on ``completed_on``, never on the due date —
    a late inspection shifts the rhythm, it never back-dates):
      * HU  + FAILED_DEFECTS → 28-day Nachprüfung (is_nachpruefung=True)
      * HU  otherwise        → next_hu_due(completed_on, interval)
      * UVV                  → next_uvv_due(completed_on)
      * FSK + PASSED         → next_fsk_due(completed_on) + resolve/unlock
    """
    deadline = await session.scalar(
        select(Deadline)
        .where(Deadline.id == deadline_id, Deadline.company_id == company_id)
        .with_for_update()
    )
    if deadline is None:
        raise DeadlineNotFound(str(deadline_id))

    deadline.status = DeadlineStatus.COMPLETED
    deadline.completed_on = completed_on

    resolved_case_id: uuid.UUID | None = None
    is_nachpruefung = False

    # ── record evidence + compute successor per kind ──
    if deadline.kind == DeadlineKind.FSK:
        driver = await session.scalar(
            select(Driver).where(Driver.id == deadline.driver_id).with_for_update()
        )
        if driver is None:
            raise DeadlineNotFound(f"driver for deadline {deadline_id}")
        check = await record_check(
            session, company_id=company_id, kind="FSK", result=result,
            performed_on=completed_on, driver_id=driver.id,
            method=method, performed_by_id=performed_by_id,
        )
        if result == RESULT_PASSED:
            resolved_case_id = await _maybe_resolve_open_case(
                session, driver, resolved_by_id=performed_by_id
            )
        next_due = dates.next_fsk_due(completed_on)
        successor = _spawn(deadline, due_on=next_due)

    elif deadline.kind == DeadlineKind.HU:
        vehicle = await session.scalar(
            select(Vehicle).where(Vehicle.id == deadline.vehicle_id).with_for_update()
        )
        if vehicle is None:
            raise DeadlineNotFound(f"vehicle for deadline {deadline_id}")
        check = await record_check(
            session, company_id=company_id, kind="HU", result=result,
            performed_on=completed_on, vehicle_id=vehicle.id,
            method=method, performed_by_id=performed_by_id,
        )
        if result == RESULT_FAILED_DEFECTS:
            is_nachpruefung = True
            next_due = dates.days_forward(completed_on, NACHPRUEFUNG_DAYS)
            successor = _spawn(deadline, due_on=next_due, is_nachpruefung=True)
        else:
            next_due = dates.next_hu_due(completed_on, vehicle.hu_interval_months)
            successor = _spawn(deadline, due_on=next_due)

    elif deadline.kind == DeadlineKind.UVV:
        check = await record_check(
            session, company_id=company_id, kind="UVV", result=result,
            performed_on=completed_on, vehicle_id=deadline.vehicle_id,
            method=method, performed_by_id=performed_by_id,
        )
        next_due = dates.next_uvv_due(completed_on)
        successor = _spawn(deadline, due_on=next_due)

    else:  # pragma: no cover - enum is exhaustive
        raise ValueError(f"unknown deadline kind {deadline.kind!r}")

    session.add(successor)
    await session.flush()
    return CompletionResult(
        completed_deadline_id=deadline.id,
        successor_deadline_id=successor.id,
        check_log_id=check.id,
        resolved_case_id=resolved_case_id,
        is_nachpruefung=is_nachpruefung,
    )


def _spawn(
    parent: Deadline, *, due_on: date, is_nachpruefung: bool = False
) -> Deadline:
    """Build the successor obligation, copying the subject from its parent."""
    return Deadline(
        company_id=parent.company_id,
        kind=parent.kind,
        driver_id=parent.driver_id,
        vehicle_id=parent.vehicle_id,
        due_on=due_on,
        status=DeadlineStatus.OPEN,
        is_nachpruefung=is_nachpruefung,
    )
