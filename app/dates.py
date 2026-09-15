"""Calendar authority for the compliance engine. The ONLY module that does
date math.

Type doctrine — enforced by signatures, checked by mypy:

  INSTANT     = aware ``datetime`` (tz-attached; persisted as timestamptz).
                "When did the system do something." Compared on the UTC line.
  OBLIGATION  = plain ``datetime.date`` interpreted on the Europe/Berlin
                civil calendar. "By which German calendar day must the
                Verantwortliche have acted." Never carries a timezone,
                because a legal deadline day has none.

The two never mix implicitly: converting an instant to an obligation goes
through ``today_berlin()`` / ``.astimezone(BERLIN).date()``, and converting
an obligation to an instant goes through ``escalation_deadline``-style
constructors that pin an explicit Berlin wall-clock time. Naive datetimes
are rejected at the boundary — a naive timestamp is a bug, not a value.
"""
from __future__ import annotations

import calendar
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo("Europe/Berlin")


# ───────────────────────────── clock seam ─────────────────────────────
# The only two functions that read the wall clock. Tests monkeypatch here.


def now_berlin() -> datetime:
    """Aware INSTANT in Europe/Berlin."""
    return datetime.now(tz=BERLIN)


def today_berlin() -> date:
    """The compliance 'today' — the current German civil calendar day.

    NEVER use ``date.today()`` or ``datetime.utcnow().date()`` in this
    codebase: between 23:00 and 00:00 UTC in winter (22:00–00:00 in summer)
    they disagree with German calendar reality, and a §21 StVG deadline
    evaluated one day early or late is a correctness bug with legal weight.
    """
    return now_berlin().date()


def require_aware(dt: datetime) -> datetime:
    """Boundary guard: naive datetimes are refused, loudly and early."""
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError(f"naive datetime rejected: {dt!r} — instants must be aware")
    return dt


# ─────────────────── month arithmetic (clamping) ───────────────────────


def add_months(source_date: date, months: int) -> date:
    """Shift by whole months, clamping to the target month's last valid day.

    date(2023, 8, 31) + 6  → date(2024, 2, 29)   (clamped INTO a leap day)
    date(2024, 2, 29) + 12 → date(2025, 2, 28)   (clamped FROM a leap day)
    date(2026, 1, 31) + 1  → date(2026, 2, 28)   (never spills into March)
    Negative ``months`` walks backwards with identical clamping.
    """
    y, m0 = divmod(source_date.month - 1 + months, 12)
    year, month = source_date.year + y, m0 + 1
    return date(year, month, min(source_date.day, calendar.monthrange(year, month)[1]))


def last_day_of_month(d: date) -> date:
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


# ──────────────── obligation constructors (German rules) ────────────────


def next_fsk_due(last_check: date, interval_months: int = 6) -> date:
    """Führerscheinkontrolle cadence, anchored on the ACTUAL check date.
    A late check shifts the rhythm; it never compresses the next window."""
    return add_months(last_check, interval_months)


def next_uvv_due(last_inspection: date) -> date:
    """DGUV Vorschrift 70 §57 — at least annually, anchored on inspection."""
    return add_months(last_inspection, 12)


def next_hu_due(inspected_on: date, interval_months: int) -> date:
    """§29 StVZO — the Plakette is month-granular, so the obligation is the
    LAST day of the target month. Anchored on the actual inspection date
    (no back-dating for overdue HUs, post-2012 rule)."""
    return last_day_of_month(add_months(inspected_on, interval_months))


# ─────────────── first-obligation constructors (on registration) ──────────────
# Used when a vehicle/driver is first created and its inaugural deadline must be
# spawned before any inspection has happened. Distinct from the next_* family,
# which anchors on an ACTUAL completed check.


def first_hu_due(first_registration: date, first_interval_months: int = 36) -> date:
    """§29 StVZO — a brand-new PKW's FIRST Hauptuntersuchung falls 36 months
    after initial registration (then every 24; see next_hu_due). Month-granular
    like every HU date."""
    return last_day_of_month(add_months(first_registration, first_interval_months))


def first_uvv_due(in_service_on: date) -> date:
    """DGUV V70 §57 — the first UVV inspection is due within a year of the
    vehicle entering service."""
    return add_months(in_service_on, 12)


def first_fsk_due(hired_on: date, interval_months: int = 6) -> date:
    """First Führerscheinkontrolle window for a newly onboarded driver —
    same 6-month cadence as the recurring check, anchored on the hire date."""
    return add_months(hired_on, interval_months)


# ───────────────────── E6: the 7-day escalation window ─────────────────────


def escalation_deadline(opened_at: datetime, days: int = 7) -> datetime:
    """Resolve the E6 window to the END of the ``days``-th Berlin calendar
    day after opening — 23:59:59 local wall time — matching how a German
    Geschäftsführer reads "Frist: 7 Tage".

    Case opened Wed 25.03. 14:00 → deadline Wed 01.04. 23:59:59 (Berlin).

    DST safety: the calendar-day walk happens on ``date`` objects (immune to
    the 02:00→03:00 spring-forward gap — 23:59:59 exists on every civil
    day), and the result is materialized through ``zoneinfo`` so its UTC
    offset is whatever is in force ON THE DEADLINE DAY, even when the
    window straddles the CET→CEST transition. ``fold`` ambiguity (the
    autumn repeated hour) cannot occur at 23:59:59 either.
    """
    local_open = require_aware(opened_at).astimezone(BERLIN)
    end_day = local_open.date() + timedelta(days=days)
    return datetime.combine(end_day, time(23, 59, 59), tzinfo=BERLIN)


def instant_months_ago(months: int) -> datetime:
    """Start (00:00 Berlin) of the civil day ``months`` calendar months before
    today — an aware INSTANT for 'older than N months' comparisons against
    timestamptz columns (e.g. the 24-month notification-log retention)."""
    cutoff_day = add_months(today_berlin(), -months)
    return datetime.combine(cutoff_day, time(0, 0, 0), tzinfo=BERLIN)


def days_forward(anchor: date, days: int) -> date:
    """A civil date ``days`` calendar days after ``anchor``. Used for the
    fixed-length HU Nachprüfung window (28 days) — a pure day count, not a
    month-granular obligation, so no clamping applies."""
    return anchor + timedelta(days=days)


def days_until(due: date, *, today: date | None = None) -> int:
    """Whole Berlin calendar days until an obligation; negative = overdue.
    ``today`` is injectable so a long sweep evaluates every row against ONE
    frozen day (and so tests never depend on the wall clock)."""
    return (due - (today or today_berlin())).days
