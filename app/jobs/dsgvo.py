"""Nightly DSGVO purge (03:00 Berlin). Runs as app_jobs (BYPASSRLS), each step
in its own committed transaction so one tenant's failure can't roll back
another's erasure.

Three duties:
  1. Anonymize drivers whose retention window (employment_end +
     company.driver_retention_months) has elapsed.
  2. Prune the notification log (outbox_message) older than 24 months.
  3. Tombstone companies 30 days after a deletion request (full anonymization;
     evidence chain retained — see app/services/dsgvo).
"""
from __future__ import annotations

import logging
from datetime import timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.dates import BERLIN, add_months, instant_months_ago, now_berlin, today_berlin
from app.models import Company, Driver, OutboxMessage
from app.services import dsgvo as svc

logger = logging.getLogger(__name__)

OUTBOX_RETENTION_MONTHS = 24
COMPANY_DELETE_GRACE_DAYS = 30


async def anonymize_expired_drivers(session: AsyncSession) -> int:
    """Anonymize drivers past employment_end + their company's retention."""
    today = today_berlin()
    candidates = (
        await session.scalars(
            select(Driver).where(
                Driver.employment_end.is_not(None),
                Driver.anonymized_at.is_(None),
            )
        )
    ).all()
    count = 0
    for driver in candidates:
        if driver.employment_end is None:  # narrowed for the type checker
            continue
        company = await session.get(Company, driver.company_id)
        months = company.driver_retention_months if company else 12
        cutoff = add_months(driver.employment_end, months)
        if cutoff < today:
            await svc.anonymize_driver(session, driver)
            count += 1
    if count:
        logger.info("anonymized %d expired driver(s)", count)
    return count


async def prune_old_outbox(session: AsyncSession) -> int:
    """Delete notification-log rows older than 24 months (non-evidence)."""
    cutoff = instant_months_ago(OUTBOX_RETENTION_MONTHS)
    result = await session.execute(
        delete(OutboxMessage).where(OutboxMessage.created_at < cutoff))
    n = result.rowcount or 0  # type: ignore[attr-defined]  # CursorResult at runtime
    if n:
        logger.info("pruned %d outbox row(s) older than %d months",
                    n, OUTBOX_RETENTION_MONTHS)
    return n


async def tombstone_deleted_companies(session: AsyncSession) -> int:
    """Anonymize whole tenants 30 days after their deletion request."""
    threshold = now_berlin() - timedelta(days=COMPANY_DELETE_GRACE_DAYS)
    companies = (
        await session.scalars(
            select(Company).where(
                Company.deletion_requested_at.is_not(None),
                Company.deletion_requested_at < threshold,
                Company.deleted_at.is_(None),
            )
        )
    ).all()
    for company in companies:
        await svc.tombstone_company(session, company)
        logger.info("tombstoned company=%s", company.id)
    return len(companies)


async def purge_expired(
    session_factory: async_sessionmaker[AsyncSession],
) -> dict[str, int]:
    """Run all three duties; each in its own transaction."""
    stats: dict[str, int] = {}
    async with session_factory() as s, s.begin():
        stats["drivers_anonymized"] = await anonymize_expired_drivers(s)
    async with session_factory() as s, s.begin():
        stats["outbox_pruned"] = await prune_old_outbox(s)
    async with session_factory() as s, s.begin():
        stats["companies_tombstoned"] = await tombstone_deleted_companies(s)
    logger.info("DSGVO purge complete: %s", stats)
    return stats


def register(
    scheduler: AsyncIOScheduler,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    scheduler.add_job(
        purge_expired,
        CronTrigger(hour=3, minute=0, timezone=BERLIN),
        args=[session_factory],
        id="purge_expired",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
