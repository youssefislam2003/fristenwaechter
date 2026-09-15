"""Settings — team (users + invitations), and the SMS cap overview."""
from __future__ import annotations

import uuid
from collections.abc import Sequence

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import HTMLResponse

from app.dates import now_berlin
from app.deps import Forbidden, TenantContext, get_tenant_context
from app.models import (
    AccountUser,
    OutboxChannel,
    OutboxMessage,
    Role,
)
from app.services import auth
from app.settings import get_settings
from app.web.rendering import form_data, render

router = APIRouter(tags=["settings"])


async def _users(
    session: AsyncSession, company_id: uuid.UUID
) -> Sequence[AccountUser]:
    rows = await session.scalars(
        select(AccountUser).where(AccountUser.company_id == company_id)
        .order_by(AccountUser.created_at))
    return rows.all()


async def _sms_used(session: AsyncSession, company_id: uuid.UUID) -> int:
    month_start = now_berlin().replace(day=1, hour=0, minute=0, second=0,
                                       microsecond=0)
    count: int | None = await session.scalar(
        select(func.count()).select_from(OutboxMessage).where(
            OutboxMessage.company_id == company_id,
            OutboxMessage.channel == OutboxChannel.SMS,
            OutboxMessage.sent_at.is_not(None),
            OutboxMessage.sent_at >= month_start))
    return count or 0


async def _render_settings(
    request: Request, ctx: TenantContext, *,
    invite_link: str | None = None, error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    from app.models import Company

    users = await _users(ctx.session, ctx.user.company_id)
    used = await _sms_used(ctx.session, ctx.user.company_id)
    company = await ctx.session.get(Company, ctx.user.company_id)
    return render(request, "settings.html", user=ctx.user, users=users,
                  sms_cap=get_settings().SMS_MONTHLY_CAP_PER_COMPANY,
                  sms_used=used, invite_link=invite_link, error=error,
                  deletion_requested=bool(company and company.deletion_requested_at),
                  status_code=status_code)


@router.get("/settings")
async def settings_page(
    request: Request, ctx: TenantContext = Depends(get_tenant_context)
) -> object:
    return await _render_settings(request, ctx)


@router.post("/settings/invite")
async def invite_user(
    request: Request, ctx: TenantContext = Depends(get_tenant_context)
) -> object:
    if ctx.user.role != Role.OWNER.value:
        raise Forbidden()
    data = await form_data(request)
    email = data.get("email", "").strip()
    role_raw = data.get("role", "MANAGER")
    try:
        role = Role(role_raw)
    except ValueError:
        return await _render_settings(request, ctx, error="Ungültige Rolle.",
                                      status_code=400)
    if role is Role.OWNER:
        return await _render_settings(request, ctx, status_code=400,
                                      error="OWNER kann nicht eingeladen werden.")
    try:
        inv = await auth.create_invitation(
            ctx.session, company_id=ctx.user.company_id, email=email,
            role=role, invited_by_id=ctx.user.id)
    except ValueError as exc:
        return await _render_settings(request, ctx, error=str(exc),
                                      status_code=400)
    link = f"{get_settings().BASE_URL}/invite/accept?token={inv.raw_token}"
    return await _render_settings(request, ctx, invite_link=link)
