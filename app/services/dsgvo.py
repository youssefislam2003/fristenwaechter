"""DSGVO erasure primitives — anonymization that PRESERVES the evidence chain.

The governing idea: personal data must be erasable (Art. 17), compliance
evidence must stay immutable (invariant I1). These are reconciled by scrubbing
identifying fields (driver names, user email/phone, company name) while leaving
the PII-free compliance_check_log / system_alert rows — which reference only
UUIDs, kinds, results and hashes — completely untouched. After anonymization no
personal data remains, so retention of the hash chain is lawful.

Nothing here deletes an evidence row or a company row; a physical purge, if a
DPA ever demands it, is a documented superuser-only step.
"""
from __future__ import annotations

import uuid

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.dates import now_berlin
from app.models import (
    AccountUser,
    Company,
    Driver,
    Invitation,
    OutboxMessage,
    UserSession,
)

ANON_NAME = "Gelöscht"


async def anonymize_driver(session: AsyncSession, driver: Driver) -> None:
    """Scrub a single driver's identity. Idempotent (anonymized_at guards it).
    The driver row itself stays so the check-log FKs remain valid."""
    if driver.anonymized_at is not None:
        return
    driver.first_name = ANON_NAME
    driver.last_name = ANON_NAME
    driver.anonymized_at = now_berlin()


async def anonymize_account_user(session: AsyncSession, user: AccountUser) -> None:
    """Neutralize a user: unique non-routable email, no phone, deactivated,
    password cleared so it can never authenticate again."""
    user.email = f"geloescht+{user.id}@invalid.local"
    user.phone_e164 = None
    user.is_active = False
    user.password_hash = None


async def tombstone_company(session: AsyncSession, company: Company) -> None:
    """Full-tenant anonymization: scrub the company, all its users, and all its
    drivers; revoke sessions; drop pending invitations and the notification
    log. The evidence chain is retained (PII-free). Sets deleted_at so the
    purge never revisits it."""
    now = now_berlin()

    company.name = ANON_NAME
    company.deleted_at = now

    users = (await session.scalars(
        select(AccountUser).where(AccountUser.company_id == company.id))).all()
    for u in users:
        await anonymize_account_user(session, u)

    drivers = (await session.scalars(
        select(Driver).where(Driver.company_id == company.id))).all()
    for d in drivers:
        d.first_name = ANON_NAME
        d.last_name = ANON_NAME
        if d.anonymized_at is None:
            d.anonymized_at = now

    # Revoke live sessions (UPDATE — no DELETE grant needed on user_session).
    await session.execute(
        update(UserSession)
        .where(UserSession.company_id == company.id,
               UserSession.revoked_at.is_(None))
        .values(revoked_at=now))
    # Drop pending invitations and the notification log for this tenant.
    await session.execute(
        delete(OutboxMessage).where(OutboxMessage.company_id == company.id))
    await session.execute(
        update(Invitation)
        .where(Invitation.company_id == company.id,
               Invitation.accepted_at.is_(None))
        .values(accepted_at=now))


async def get_driver_in_company(
    session: AsyncSession, driver_id: uuid.UUID, company_id: uuid.UUID
) -> Driver | None:
    row: Driver | None = await session.scalar(
        select(Driver).where(Driver.id == driver_id,
                             Driver.company_id == company_id))
    return row
