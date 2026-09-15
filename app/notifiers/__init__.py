"""Notification layer — the ONLY code (besides the relay that drives it) that
talks to Brevo (email) and seven.io (SMS). Everything here is called from the
worker/relay, never from a web request in a DB transaction (I3: no network I/O
inside a transaction).
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.jobs.relay import Notifier, NotifierError
from app.models import OutboxChannel
from app.notifiers.brevo import BrevoNotifier
from app.notifiers.seven import SevenNotifier
from app.notifiers.sms_quota import DbSmsQuota
from app.settings import Settings

__all__ = [
    "NotifierError",
    "BrevoNotifier",
    "SevenNotifier",
    "DbSmsQuota",
    "build_notifiers",
]


def build_notifiers(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> dict[OutboxChannel, Notifier]:
    """Wire the concrete notifiers the relay dispatches on by channel. When a
    (jobs) session factory is supplied, the SMS notifier enforces the
    per-company monthly cap; without one it sends uncapped (fine for tests)."""
    quota = (
        DbSmsQuota(session_factory, cap=settings.SMS_MONTHLY_CAP_PER_COMPANY)
        if session_factory is not None
        else None
    )
    return {
        OutboxChannel.EMAIL: BrevoNotifier(settings),
        OutboxChannel.SMS: SevenNotifier(settings, quota=quota),
    }
