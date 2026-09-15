"""Resolve who gets notified for a company: its active OWNER and MANAGER
accounts (VIEWERs are read-only and never paged). Shared by the E6 caller and
the sweeps so escalation and reminder notifications target the same people.
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AccountUser, Role
from app.services.e6 import Recipient

_NOTIFIED_ROLES = (Role.OWNER.value, Role.MANAGER.value)


async def resolve_recipients(
    session: AsyncSession, company_id: uuid.UUID
) -> list[Recipient]:
    """Active OWNER/MANAGER users of the company, as notification targets.

    Filters by company_id explicitly so it is correct both in the tenant-scoped
    web session (GUC set) and in the worker's BYPASSRLS session (no GUC)."""
    users = (
        await session.scalars(
            select(AccountUser).where(
                AccountUser.company_id == company_id,
                AccountUser.role.in_(_NOTIFIED_ROLES),
                AccountUser.is_active.is_(True),
            )
        )
    ).all()
    return [Recipient(email=u.email, phone_e164=u.phone_e164) for u in users]
