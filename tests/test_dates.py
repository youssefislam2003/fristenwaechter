"""dates.py must be bulletproof before anything else exists — these tests
are the codebase's most valuable asset. Pure functions, zero fixtures."""
from datetime import date, datetime, timedelta

import pytest

from app.dates import (
    BERLIN,
    add_months,
    days_until,
    escalation_deadline,
    last_day_of_month,
    next_fsk_due,
    next_hu_due,
    require_aware,
)

# ───────────────────────── add_months: clamping table ─────────────────────


@pytest.mark.parametrize(
    ("source", "months", "expected"),
    [
        # the canonical leap-day case from the spec:
        (date(2023, 8, 31), 6, date(2024, 2, 29)),   # clamp INTO leap day
        (date(2026, 8, 31), 6, date(2027, 2, 28)),   # same shift, non-leap year
        (date(2024, 2, 29), 12, date(2025, 2, 28)),  # clamp FROM leap day
        (date(2024, 2, 29), 48, date(2028, 2, 29)),  # leap → leap survives
        (date(2026, 1, 31), 1, date(2026, 2, 28)),   # Jan31 → Feb28, no Mar spill
        (date(2026, 3, 31), 1, date(2026, 4, 30)),   # 31 → 30-day month
        (date(2026, 12, 15), 6, date(2027, 6, 15)),  # plain year rollover
        (date(2026, 7, 15), 0, date(2026, 7, 15)),   # identity
        (date(2026, 3, 31), -1, date(2026, 2, 28)),  # negative months clamp too
        (date(2026, 1, 10), -13, date(2024, 12, 10)),  # negative across years
    ],
)
def test_add_months_clamps(source: date, months: int, expected: date) -> None:
    assert add_months(source, months) == expected


def test_fsk_six_month_cadence_anchors_on_actual_check() -> None:
    # checked late on Aug 31 → next window ends Feb 29 (leap), not Feb 31-ish
    assert next_fsk_due(date(2023, 8, 31)) == date(2024, 2, 29)


def test_hu_is_month_granular() -> None:
    # inspected 2026-02-10, 24-month PKW interval → due "Feb 2028" = 2028-02-29
    assert next_hu_due(date(2026, 2, 10), 24) == date(2028, 2, 29)
    assert last_day_of_month(date(2028, 2, 1)) == date(2028, 2, 29)


# ──────────────── escalation_deadline: Berlin-day semantics ────────────────


def test_deadline_is_end_of_seventh_berlin_day() -> None:
    opened = datetime(2026, 7, 1, 14, 30, tzinfo=BERLIN)  # Wed afternoon
    dl = escalation_deadline(opened, days=7)
    assert dl == datetime(2026, 7, 8, 23, 59, 59, tzinfo=BERLIN)
    assert dl.tzinfo is BERLIN


def test_deadline_computed_from_utc_instant_lands_on_berlin_calendar() -> None:
    # 23:30 UTC on July 1 is ALREADY July 2, 01:30 in Berlin (CEST) —
    # the window must count from the German day, not the UTC day.
    from datetime import timezone

    opened_utc = datetime(2026, 7, 1, 23, 30, tzinfo=timezone.utc)
    dl = escalation_deadline(opened_utc, days=7)
    assert dl.date() == date(2026, 7, 9)  # Jul 2 + 7, not Jul 1 + 7


def test_spring_forward_gap_is_safe() -> None:
    """DST transition 2026: Sunday 29.03., 02:00 CET → 03:00 CEST.

    A case opened Wed 25.03. (CET, +01:00) has its 7-day window straddle the
    gap. Assertions:
      * the deadline lands on the correct civil day (01.04.),
      * it carries the POST-transition offset (+02:00 CEST) — zoneinfo
        resolved the rules for the deadline day, not the opening day,
      * the elapsed real time is 7×24h MINUS the lost hour — proof the
        computation is calendar-driven, not naive timedelta addition,
      * 23:59:59 exists on every civil day, so no ValueError from the
        nonexistent 02:00–03:00 wall-clock range is even reachable.
    """
    opened = datetime(2026, 3, 25, 14, 0, tzinfo=BERLIN)
    assert opened.utcoffset() == timedelta(hours=1)  # CET before the jump

    dl = escalation_deadline(opened, days=7)

    assert dl.date() == date(2026, 4, 1)
    assert dl.utcoffset() == timedelta(hours=2)  # CEST after the jump
    real_elapsed = dl.astimezone(BERLIN).timestamp() - opened.timestamp()
    week_seconds = 7 * 24 * 3600
    lost_hour = 3600
    expected = week_seconds - lost_hour + (23 * 3600 + 59 * 60 + 59) - (14 * 3600)
    assert real_elapsed == expected


def test_naive_datetime_is_rejected_at_the_boundary() -> None:
    with pytest.raises(ValueError, match="naive datetime rejected"):
        escalation_deadline(datetime(2026, 3, 25, 14, 0))  # no tzinfo
    with pytest.raises(ValueError):
        require_aware(datetime(2026, 1, 1))


# ─────────────────────────── days_until seam ───────────────────────────


def test_days_until_is_injectable_and_signed() -> None:
    frozen = date(2026, 7, 7)
    assert days_until(date(2026, 7, 14), today=frozen) == 7
    assert days_until(date(2026, 7, 7), today=frozen) == 0
    assert days_until(date(2026, 7, 1), today=frozen) == -6  # overdue
