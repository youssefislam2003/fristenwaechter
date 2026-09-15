"""Pure-logic tests for the deadline layer — reminder notch selection and the
first-obligation date constructors. No DB, no Docker."""
from __future__ import annotations

from datetime import date

import pytest

from app.dates import days_forward, first_fsk_due, first_hu_due, first_uvv_due
from app.jobs.sweeps import reminder_notch


@pytest.mark.parametrize(
    ("due", "today", "expected"),
    [
        (date(2026, 7, 1), date(2026, 7, 1), ("0", 0)),      # due today
        (date(2026, 7, 8), date(2026, 7, 1), ("7", 7)),      # 7 days out
        (date(2026, 7, 6), date(2026, 7, 1), ("7", 7)),      # 5 days → 7 notch
        (date(2026, 7, 31), date(2026, 7, 1), ("30", 30)),   # 30 days
        (date(2026, 8, 20), date(2026, 7, 1), ("90", 90)),   # 50 days → 90 notch
        (date(2027, 1, 1), date(2026, 7, 1), None),          # >90 days: silent
    ],
)
def test_reminder_notch_pre_due(due, today, expected) -> None:
    assert reminder_notch(due, today) == expected


def test_reminder_notch_overdue_is_weekly() -> None:
    token, display = reminder_notch(date(2026, 6, 1), date(2026, 7, 1))
    assert display == -1
    assert token.startswith("ov")
    # Same ISO week → same token (idempotent); next week → different token.
    t2, _ = reminder_notch(date(2026, 6, 1), date(2026, 7, 8))
    assert t2 != token


def test_first_obligation_constructors() -> None:
    # New PKW: first HU 36 months out, month-granular (last day of month).
    assert first_hu_due(date(2026, 3, 15)) == date(2029, 3, 31)
    assert first_uvv_due(date(2026, 3, 15)) == date(2027, 3, 15)
    assert first_fsk_due(date(2026, 1, 10)) == date(2026, 7, 10)


def test_days_forward_is_plain_day_count() -> None:
    assert days_forward(date(2026, 7, 1), 28) == date(2026, 7, 29)
    # 28-day Nachprüfung across a month boundary, no clamping.
    assert days_forward(date(2026, 2, 15), 28) == date(2026, 3, 15)
