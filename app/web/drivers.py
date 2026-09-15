"""Driver CRUD, per-driver history timeline, escalation page, and FSK check
recording — the heart of the § 21 flow.

Recording a FAILED Führerscheinkontrolle writes the evidence row and then runs
E6 (hard lock + case + outbox). A PASSED check completes the open FSK deadline,
which rolls the obligation forward AND resolves any open case + unlocks.
"""
from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import dates
from app.deps import Forbidden, TenantContext, get_tenant_context
from app.models import (
    ComplianceCheckLog,
    Deadline,
    DeadlineKind,
    DeadlineStatus,
    Driver,
    EscalationCase,
    Role,
    SystemAlert,
)
from app.services import checks
from app.services.e6 import run_e6
from app.services.recipients import resolve_recipients
from app.web.rendering import form_data, render

router = APIRouter(tags=["drivers"])

_STATUS_LABEL = {
    "OPEN": "offen", "RESOLVED_PASSED": "erledigt (bestanden)",
    "RESOLVED_REMOVED": "erledigt (Fahrer entfernt)", "BREACHED": "Frist überschritten",
}
_STATUS_BADGE = {
    "OPEN": "badge-red", "RESOLVED_PASSED": "badge-green",
    "RESOLVED_REMOVED": "badge-green", "BREACHED": "badge-red",
}


def _parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


async def _driver_rows(
    session: AsyncSession, company_id: uuid.UUID
) -> list[dict[str, object]]:
    drivers = (
        await session.scalars(
            select(Driver).where(Driver.company_id == company_id)
            .order_by(Driver.last_name, Driver.first_name)
        )
    ).all()
    out = []
    for d in drivers:
        fsk = await session.scalar(
            select(Deadline.due_on).where(
                Deadline.driver_id == d.id, Deadline.kind == DeadlineKind.FSK,
                Deadline.status == DeadlineStatus.OPEN))
        out.append({
            "id": d.id, "first_name": d.first_name, "last_name": d.last_name,
            "is_authorized_to_drive": d.is_authorized_to_drive,
            "fsk_due": fsk.strftime("%d.%m.%Y") if fsk else None,
        })
    return out


@router.get("/drivers")
async def list_drivers(
    request: Request, ctx: TenantContext = Depends(get_tenant_context)
) -> object:
    rows = await _driver_rows(ctx.session, ctx.user.company_id)
    return render(request, "drivers.html", user=ctx.user, drivers=rows)


@router.post("/drivers")
async def create_driver(
    request: Request, ctx: TenantContext = Depends(get_tenant_context)
) -> object:
    if ctx.user.role == Role.VIEWER.value:
        raise Forbidden()
    session, user = ctx.session, ctx.user
    data = await form_data(request)
    first = data.get("first_name", "").strip()
    last = data.get("last_name", "").strip()
    if not first or not last:
        rows = await _driver_rows(session, user.company_id)
        return render(request, "drivers.html", user=user, drivers=rows,
                      status_code=400, error="Vor- und Nachname sind erforderlich.")
    start = _parse_date(data.get("employment_start"))
    driver = Driver(company_id=user.company_id, first_name=first, last_name=last,
                    employment_start=start)
    session.add(driver)
    await session.flush()

    anchor = start or dates.today_berlin()
    session.add(Deadline(
        company_id=user.company_id, kind=DeadlineKind.FSK, driver_id=driver.id,
        due_on=dates.first_fsk_due(anchor), status=DeadlineStatus.OPEN))

    rows = await _driver_rows(session, user.company_id)
    return render(request, "drivers.html", user=user, drivers=rows)


@router.get("/drivers/{driver_id}/history")
async def driver_history(
    driver_id: uuid.UUID, request: Request,
    ctx: TenantContext = Depends(get_tenant_context)
) -> object:
    session = ctx.session
    driver = await session.get(Driver, driver_id)
    if driver is None:  # RLS already scopes; None ⇒ cross-tenant or missing
        return render(request, "base.html", user=ctx.user, status_code=404)
    entries = (
        await session.scalars(
            select(ComplianceCheckLog)
            .where(ComplianceCheckLog.driver_id == driver_id)
            .order_by(ComplianceCheckLog.created_at.desc())
        )
    ).all()
    return render(request, "subject_history.html", user=ctx.user,
                  subject_type="driver", subject_id=driver_id,
                  subject_name=f"{driver.first_name} {driver.last_name}",
                  locked=not driver.is_authorized_to_drive,
                  today=dates.today_berlin().isoformat(), entries=entries)


@router.post("/drivers/{driver_id}/checks")
async def record_driver_check(
    driver_id: uuid.UUID, request: Request,
    ctx: TenantContext = Depends(get_tenant_context)
) -> object:
    if ctx.user.role == Role.VIEWER.value:
        raise Forbidden()
    session, user = ctx.session, ctx.user
    data = await form_data(request)
    result = data.get("result", "")
    performed_on = _parse_date(data.get("performed_on")) or dates.today_berlin()

    driver = await session.get(Driver, driver_id)
    if driver is None:
        return render(request, "base.html", user=user, status_code=404)

    if result == checks.RESULT_FAILED:
        # Evidence first, then the atomic E6 lock+case+outbox.
        await checks.record_check(
            session, company_id=user.company_id, kind="FSK", result="FAILED",
            performed_on=performed_on, driver_id=driver_id,
            performed_by_id=user.id, method="SICHT")
        recipients = await resolve_recipients(session, user.company_id)
        await run_e6(session, driver_id, company_id=user.company_id,
                     trigger_check_id=uuid.uuid4(), recipients=recipients)
    else:  # PASSED
        open_fsk = await session.scalar(
            select(Deadline).where(
                Deadline.driver_id == driver_id,
                Deadline.kind == DeadlineKind.FSK,
                Deadline.status == DeadlineStatus.OPEN).limit(1))
        if open_fsk is not None:
            await checks.complete_deadline(
                session, deadline_id=open_fsk.id, company_id=user.company_id,
                result=checks.RESULT_PASSED, completed_on=performed_on,
                performed_by_id=user.id, method="SICHT")
        else:
            # No open deadline (ad-hoc pass): record + clear any lock.
            await checks.record_check(
                session, company_id=user.company_id, kind="FSK",
                result="PASSED", performed_on=performed_on, driver_id=driver_id,
                performed_by_id=user.id, method="SICHT")
            await checks._maybe_resolve_open_case(
                session, driver, resolved_by_id=user.id)

    return RedirectResponse(url=f"/drivers/{driver_id}/history", status_code=303)


@router.get("/drivers/{driver_id}/escalation")
async def escalation_page(
    driver_id: uuid.UUID, request: Request,
    ctx: TenantContext = Depends(get_tenant_context)
) -> object:
    session = ctx.session
    driver = await session.get(Driver, driver_id)
    if driver is None:
        return render(request, "base.html", user=ctx.user, status_code=404)
    case = await session.scalar(
        select(EscalationCase).where(EscalationCase.driver_id == driver_id)
        .order_by(EscalationCase.created_at.desc()).limit(1))
    alerts = (
        await session.scalars(
            select(SystemAlert).where(SystemAlert.driver_id == driver_id)
            .order_by(SystemAlert.created_at.desc())
        )
    ).all()
    status = case.status.value if case else ""
    return render(request, "escalation.html", user=ctx.user, driver=driver,
                  case=case, alerts=alerts, berlin=dates.BERLIN,
                  status_label=_STATUS_LABEL.get(status, status),
                  badge_class=_STATUS_BADGE.get(status, "badge-amber"))
