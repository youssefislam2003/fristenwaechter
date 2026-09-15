"""Core compliance models — E6 lockout, escalation state, transactional outbox.

Invariants encoded here (not in application discipline):
  * Driver mutation is guarded by SQLAlchemy's NATIVE optimistic locking
    (``version_id_col``): any concurrent write between our SELECT and our
    flush raises ``StaleDataError`` at flush time. No hand-rolled
    ``WHERE version = :v`` needed — the mapper emits it on every UPDATE.
  * At most ONE OPEN EscalationCase per driver — a PostgreSQL partial
    unique index makes double-escalation a database impossibility, which
    turns the service layer's race window into a catchable IntegrityError.
  * SystemAlert and the outbox are append-only ledgers; delivery state
    (attempts / next_attempt_at / sent_at) lives on OutboxMessage so a
    relay crash loses nothing that was committed.
"""
from __future__ import annotations

import enum
import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )


def _db_now() -> Mapped[datetime]:
    """timestamptz stamped by the DATABASE clock.

    Evidence ordering must not depend on app-server clock skew; Postgres is
    the single monotonic authority for "when did the system record this".
    """
    return mapped_column(DateTime(timezone=True), server_default=func.now())


# ─────────────────────────── tenancy & users ───────────────────────────


class Company(Base):
    __tablename__ = "company"

    id: Mapped[uuid.UUID] = _uuid_pk()
    created_at: Mapped[datetime] = _db_now()
    name: Mapped[str] = mapped_column(String(200))
    plan: Mapped[str] = mapped_column(String(20), default="trial")

    # ── DSGVO retention (added in migration 0004) ──
    # How long a driver's identifying data is kept after employment_end before
    # the nightly purge anonymizes it (name → "Gelöscht"). The check-log hash
    # chain is never touched — that is the auditable evidence the customer,
    # as Verantwortlicher, must retain.
    driver_retention_months: Mapped[int] = mapped_column(
        SmallInteger, default=12
    )
    # Set when the OWNER requests deletion (Art. 17). 30 days later the purge
    # tombstones the whole tenant (a grace window against mistakes).
    deletion_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    # Stamped once the tombstone (full anonymization) has completed, so the
    # purge never reprocesses it. The PII-free evidence chain is retained —
    # I1 is never weakened. True physical destruction is a documented,
    # superuser-only ops step, never a runtime capability.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Role(enum.StrEnum):
    """Account roles, most→least privileged. OWNER is the Halter/Geschäfts-
    führer who carries the §21 StVG liability; MANAGER runs day-to-day
    compliance; VIEWER is read-only (e.g. an external Datenschutzbeauftragter).
    Stored as the plain string in ``account_user.role`` (String(10))."""

    OWNER = "OWNER"
    MANAGER = "MANAGER"
    VIEWER = "VIEWER"


class AccountUser(Base):
    """Named account_user (not app_user) to avoid colliding with the
    least-privilege runtime DB ROLE `app_user` — a role and a table may
    legally share a name in Postgres, but every GRANT statement becomes a
    reading-comprehension hazard."""

    __tablename__ = "account_user"

    id: Mapped[uuid.UUID] = _uuid_pk()
    created_at: Mapped[datetime] = _db_now()
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id", ondelete="CASCADE"), index=True
    )
    email: Mapped[str] = mapped_column(String(254), unique=True)
    role: Mapped[str] = mapped_column(String(10), default="MANAGER")
    phone_e164: Mapped[str | None] = mapped_column(String(20))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # ── auth (added in migration 0002) ──
    # argon2id hash. NULL until an invited user sets a password on acceptance;
    # the OWNER created at signup gets one immediately. A NULL hash can never
    # authenticate (verify short-circuits) — an invited-but-not-yet-accepted
    # account cannot log in.
    password_hash: Mapped[str | None] = mapped_column(String(200))
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )


# ─────────────────────────────── driver ───────────────────────────────


class Driver(Base):
    __tablename__ = "driver"

    id: Mapped[uuid.UUID] = _uuid_pk()
    created_at: Mapped[datetime] = _db_now()
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id", ondelete="CASCADE"), index=True
    )

    first_name: Mapped[str] = mapped_column(String(80))
    last_name: Mapped[str] = mapped_column(String(80))

    # ── employment window (added in migration 0003) ──
    # employment_start anchors the first Führerscheinkontrolle; employment_end
    # starts the DSGVO retention clock (T8). No birthday/address/licence number
    # ever — that is invariant I2, enforced here by the ABSENCE of such columns.
    employment_start: Mapped[date | None]
    employment_end: Mapped[date | None]
    # Stamped by the DSGVO anonymizer (T8): name → "Gelöscht", set here. The
    # check-log hash chain is deliberately left intact — that is the point.
    anonymized_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    # ── E6 hard lock ──
    is_authorized_to_drive: Mapped[bool] = mapped_column(Boolean, default=True)
    lock_reason: Mapped[str | None] = mapped_column(String(50))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # ── optimistic locking guard ──
    # SQLAlchemy's version_id_col: every UPDATE the mapper emits becomes
    #   UPDATE driver SET ..., version_id = :new
    #   WHERE id = :id AND version_id = :expected
    # rowcount 0 → orm.exc.StaleDataError at flush. Works identically under
    # AsyncSession (the check happens in the sync core beneath greenlets).
    version_id: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __mapper_args__ = {"version_id_col": version_id}

    __table_args__ = (
        Index("ix_driver_company_locked", "company_id", "is_authorized_to_drive"),
    )


# ─────────────────────────────── vehicle ───────────────────────────────


class Vehicle(Base):
    """A fleet vehicle. Its HU (§29 StVZO) and UVV (DGUV V70) obligations are
    tracked as Deadline rows spawned on creation (see T7 / complete_deadline)."""

    __tablename__ = "vehicle"

    id: Mapped[uuid.UUID] = _uuid_pk()
    created_at: Mapped[datetime] = _db_now()
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id", ondelete="CASCADE"), index=True
    )
    # Amtliches Kennzeichen (DIN 1451 pattern validated at the API boundary).
    kennzeichen: Mapped[str] = mapped_column(String(15))
    make: Mapped[str | None] = mapped_column(String(40))
    model: Mapped[str | None] = mapped_column(String(40))
    first_registration: Mapped[date | None]
    # PKW = 24; the FIRST HU for a new PKW is 36 months out (see first_hu_due).
    hu_interval_months: Mapped[int] = mapped_column(SmallInteger, default=24)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    __table_args__ = (
        Index("ix_vehicle_company", "company_id", "is_active"),
    )


# ─────────────────────────────── deadline ───────────────────────────────


class DeadlineKind(enum.StrEnum):
    HU = "HU"    # Hauptuntersuchung (§29 StVZO) — vehicle
    UVV = "UVV"  # DGUV Vorschrift 70 §57 — vehicle
    FSK = "FSK"  # Führerscheinkontrolle — driver


class DeadlineStatus(enum.StrEnum):
    OPEN = "OPEN"
    COMPLETED = "COMPLETED"
    SUPERSEDED = "SUPERSEDED"  # replaced by a rolled-forward successor


class Deadline(Base):
    """A single dated obligation for exactly one subject (a driver XOR a
    vehicle). The reminder sweep watches OPEN rows; completing one via
    complete_deadline stamps it COMPLETED and spawns its successor."""

    __tablename__ = "deadline"

    id: Mapped[uuid.UUID] = _uuid_pk()
    created_at: Mapped[datetime] = _db_now()
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[DeadlineKind] = mapped_column(
        Enum(DeadlineKind, native_enum=False)
    )
    driver_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("driver.id", ondelete="CASCADE")
    )
    vehicle_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("vehicle.id", ondelete="CASCADE")
    )
    # Berlin civil OBLIGATION date (never a timezone-carrying instant).
    due_on: Mapped[date]
    status: Mapped[DeadlineStatus] = mapped_column(
        Enum(DeadlineStatus, native_enum=False),
        default=DeadlineStatus.OPEN,
    )
    completed_on: Mapped[date | None]
    # True for a 28-day Nachprüfung spawned after an HU with defects.
    is_nachpruefung: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (
        # Exactly one subject — a deadline is either a driver's or a vehicle's.
        CheckConstraint(
            "(driver_id IS NOT NULL)::int + (vehicle_id IS NOT NULL)::int = 1",
            name="ck_deadline_one_subject",
        ),
        # The sweep's hot path: OPEN rows ordered by due date within a tenant.
        Index("ix_deadline_sweep", "status", "due_on"),
        Index("ix_deadline_driver", "driver_id"),
        Index("ix_deadline_vehicle", "vehicle_id"),
    )


# ─────────────────────────── escalation case ───────────────────────────


class EscalationStatus(enum.StrEnum):
    OPEN = "OPEN"
    RESOLVED_PASSED = "RESOLVED_PASSED"
    RESOLVED_REMOVED = "RESOLVED_REMOVED"
    BREACHED = "BREACHED"


class EscalationCase(Base):
    __tablename__ = "escalation_case"

    id: Mapped[uuid.UUID] = _uuid_pk()
    created_at: Mapped[datetime] = _db_now()
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id", ondelete="CASCADE"), index=True
    )
    driver_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("driver.id", ondelete="CASCADE")
    )

    status: Mapped[EscalationStatus] = mapped_column(
        Enum(EscalationStatus, native_enum=False),
        default=EscalationStatus.OPEN,
    )
    # End of the 7th calendar day, Europe/Berlin (see dates.escalation_deadline)
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("account_user.id", ondelete="SET NULL")
    )
    last_nag_on: Mapped[date | None]  # calendar-date idempotency key for sweeps

    __table_args__ = (
        # THE uniqueness guarantee: at most one OPEN case per driver, ever.
        # Partial index ⇒ resolved/breached history stacks freely.
        Index(
            "uq_escalation_open_per_driver",
            "driver_id",
            unique=True,
            postgresql_where=text("status = 'OPEN'"),
        ),
    )


# ──────────────────────────── system alert ────────────────────────────


class AlertSeverity(enum.StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class SystemAlert(Base):
    """Immutable in-app alert ledger.

    Pair with the standard append-only trigger:
        CREATE TRIGGER system_alert_immutable
          BEFORE UPDATE OR DELETE ON system_alert
          FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
    """

    __tablename__ = "system_alert"

    id: Mapped[uuid.UUID] = _uuid_pk()
    created_at: Mapped[datetime] = _db_now()
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id", ondelete="CASCADE"), index=True
    )
    severity: Mapped[AlertSeverity] = mapped_column(
        Enum(AlertSeverity, native_enum=False)
    )
    code: Mapped[str] = mapped_column(String(40))  # e.g. 'FSK_FAILED_LOCK'
    driver_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("driver.id", ondelete="CASCADE")
    )
    escalation_case_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("escalation_case.id", ondelete="SET NULL")
    )
    message: Mapped[str] = mapped_column(Text)


# ────────────────────────── transactional outbox ──────────────────────────


class OutboxChannel(enum.StrEnum):
    EMAIL = "EMAIL"
    SMS = "SMS"


class OutboxMessage(Base):
    """Transactional outbox row.

    Written in the SAME transaction as the state change it announces, so the
    intent to notify is exactly as durable as the lockout itself. The relay
    (jobs/relay.py) owns delivery with at-least-once semantics.

    Payload policy (DSGVO / data minimization): metadata only — driver
    display name, deadline string, deep-link path. Never license numbers,
    never scans; the Pydantic layer upstream cannot even represent them.
    """

    __tablename__ = "outbox_message"

    id: Mapped[uuid.UUID] = _uuid_pk()
    created_at: Mapped[datetime] = _db_now()
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id", ondelete="CASCADE"), index=True
    )

    channel: Mapped[OutboxChannel] = mapped_column(
        Enum(OutboxChannel, native_enum=False)
    )
    recipient: Mapped[str] = mapped_column(String(254))
    template: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict)

    # 0 = CRITICAL (§21 StVG broadcasts) … 100 = routine reminders
    priority: Mapped[int] = mapped_column(SmallInteger, default=100)

    # logical-event dedup: enqueueing the same event twice is a no-op
    dedup_key: Mapped[str] = mapped_column(String(120), unique=True)

    # ── delivery state machine (mutated ONLY by the relay) ──
    attempts: Mapped[int] = mapped_column(SmallInteger, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dead_lettered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    provider_message_id: Mapped[str | None] = mapped_column(String(100))

    __table_args__ = (
        CheckConstraint("attempts >= 0", name="ck_outbox_attempts_nonneg"),
        # hot-path index for the relay's polling query: only undelivered,
        # non-dead rows, pre-sorted the way the relay reads them
        Index(
            "ix_outbox_pending",
            "next_attempt_at",
            "priority",
            postgresql_where=text(
                "sent_at IS NULL AND dead_lettered_at IS NULL"
            ),
        ),
    )


# ───────────────────── compliance evidence (append-only) ─────────────────────


class ComplianceCheckLog(Base):
    """Hash-chained, PII-free audit artifact. APPEND-ONLY — enforced by the
    forbid_mutation() trigger installed in migration 0001, PLUS the absence
    of UPDATE/DELETE grants for the runtime role. Two independent layers."""

    __tablename__ = "compliance_check_log"

    id: Mapped[uuid.UUID] = _uuid_pk()
    created_at: Mapped[datetime] = _db_now()
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id", ondelete="CASCADE"), index=True
    )
    driver_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("driver.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(String(10))       # FSK | UVV | HU | SP
    result: Mapped[str] = mapped_column(String(20))     # PASSED | FAILED | …
    method: Mapped[str | None] = mapped_column(String(20))
    performed_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("account_user.id", ondelete="SET NULL")
    )
    performed_on: Mapped[date]
    params: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict)  # whitelisted keys only
    prev_hash: Mapped[str | None] = mapped_column(String(64))
    entry_hash: Mapped[str] = mapped_column(String(64), unique=True)

    __table_args__ = (
        Index("ix_check_chain_driver", "driver_id", "created_at"),
    )


# ────────────────────── auth infrastructure (migration 0002) ──────────────────
# These three tables are DELIBERATELY not under row-level security: they must
# be read during the PRE-tenant authentication phase, before any
# `app.current_company` GUC exists. Isolation for them is provided by
# unguessable secret tokens (sessions, invitations) or by the email+IP key
# (throttle), not by RLS. account_user itself IS under RLS; the one pre-tenant
# read it needs (login by email) goes through the SECURITY DEFINER function
# `auth_lookup_credentials` created in migration 0002.


class UserSession(Base):
    """Server-side session. The cookie carries an opaque random token; only
    its SHA-256 hash is stored here, so a database disclosure cannot be
    replayed into live sessions. Carries company_id so session resolution can
    set the tenant GUC without a second query."""

    __tablename__ = "user_session"

    id: Mapped[uuid.UUID] = _uuid_pk()
    created_at: Mapped[datetime] = _db_now()
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    account_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("account_user.id", ondelete="CASCADE"), index=True
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id", ondelete="CASCADE"), index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    # Set on logout / forced revocation; a revoked session never authenticates.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Invitation(Base):
    """Email-token invitation for MANAGER/VIEWER accounts. Raw token is mailed
    inside a link; only its hash is stored. 72-hour expiry; single-use
    (accepted_at stamps it closed)."""

    __tablename__ = "invitation"

    id: Mapped[uuid.UUID] = _uuid_pk()
    created_at: Mapped[datetime] = _db_now()
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id", ondelete="CASCADE"), index=True
    )
    email: Mapped[str] = mapped_column(String(254))
    role: Mapped[str] = mapped_column(String(10))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    invited_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("account_user.id", ondelete="SET NULL")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LoginThrottle(Base):
    """Brute-force guard, keyed by (email, ip). After 5 consecutive failures
    the pair is locked for 15 minutes; a success resets the counter. Not
    tenant-scoped — it operates before the tenant is known."""

    __tablename__ = "login_throttle"

    id: Mapped[uuid.UUID] = _uuid_pk()
    created_at: Mapped[datetime] = _db_now()
    email: Mapped[str] = mapped_column(String(254))
    ip: Mapped[str] = mapped_column(String(45))  # IPv6-max textual length
    fail_count: Mapped[int] = mapped_column(SmallInteger, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        Index("uq_throttle_email_ip", "email", "ip", unique=True),
    )
