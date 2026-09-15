"""Vehicle CRUD. Creating a vehicle auto-spawns its first HU and UVV deadlines
(via dates.first_hu_due / first_uvv_due). Kennzeichen is DIN-validated.
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
    Role,
    Vehicle,
)
from app.services import checks
from app.web.kennzeichen import InvalidKennzeichen, validate_kennzeichen
from app.web.rendering import form_data, render

router = APIRouter(tags=["vehicles"])


def _parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


async def _vehicle_rows(
    session: AsyncSession, company_id: uuid.UUID
) -> list[dict[str, object]]:
    vehicles = (
        await session.scalars(
            select(Vehicle).where(Vehicle.company_id == company_id)
            .order_by(Vehicle.kennzeichen)
        )
    ).all()
    out = []
    for v in vehicles:
        hu = await session.scalar(
            select(Deadline.due_on).where(
                Deadline.vehicle_id == v.id, Deadline.kind == DeadlineKind.HU,
                Deadline.status == DeadlineStatus.OPEN))
        uvv = await session.scalar(
            select(Deadline.due_on).where(
                Deadline.vehicle_id == v.id, Deadline.kind == DeadlineKind.UVV,
                Deadline.status == DeadlineStatus.OPEN))
        out.append({
            "id": v.id, "kennzeichen": v.kennzeichen, "make": v.make,
            "model": v.model,
            "hu_due": hu.strftime("%d.%m.%Y") if hu else None,
            "uvv_due": uvv.strftime("%d.%m.%Y") if uvv else None,
        })
    return out


@router.get("/vehicles")
async def list_vehicles(
    request: Request, ctx: TenantContext = Depends(get_tenant_context)
) -> object:
    rows = await _vehicle_rows(ctx.session, ctx.user.company_id)
    return render(request, "vehicles.html", user=ctx.user, vehicles=rows)


@router.post("/vehicles")
async def create_vehicle(
    request: Request, ctx: TenantContext = Depends(get_tenant_context)
) -> object:
    if ctx.user.role == Role.VIEWER.value:
        raise Forbidden()
    session, user = ctx.session, ctx.user
    data = await form_data(request)

    try:
        kennzeichen = validate_kennzeichen(data.get("kennzeichen", ""))
    except InvalidKennzeichen:
        rows = await _vehicle_rows(session, user.company_id)
        return render(request, "vehicles.html", user=user, vehicles=rows,
                      status_code=400,
                      error="Ungültiges Kennzeichen (z. B. B-AB 1234).")

    first_reg = _parse_date(data.get("first_registration"))
    try:
        interval = int(data.get("hu_interval_months", "24"))
    except ValueError:
        interval = 24

    vehicle = Vehicle(
        company_id=user.company_id, kennzeichen=kennzeichen,
        make=data.get("make") or None, model=data.get("model") or None,
        first_registration=first_reg, hu_interval_months=interval)
    session.add(vehicle)
    await session.flush()

    # Auto-spawn first HU + UVV obligations. Anchor on first registration when
    # known, else today (a used vehicle onboarded mid-life).
    anchor = first_reg or dates.today_berlin()
    session.add(Deadline(
        company_id=user.company_id, kind=DeadlineKind.HU, vehicle_id=vehicle.id,
        due_on=dates.first_hu_due(anchor), status=DeadlineStatus.OPEN))
    session.add(Deadline(
        company_id=user.company_id, kind=DeadlineKind.UVV, vehicle_id=vehicle.id,
        due_on=dates.first_uvv_due(anchor), status=DeadlineStatus.OPEN))

    rows = await _vehicle_rows(session, user.company_id)
    return render(request, "vehicles.html", user=user, vehicles=rows)


@router.get("/vehicles/{vehicle_id}/history")
async def vehicle_history(
    vehicle_id: uuid.UUID, request: Request,
    ctx: TenantContext = Depends(get_tenant_context)
) -> object:
    session = ctx.session
    vehicle = await session.get(Vehicle, vehicle_id)
    if vehicle is None:
        return render(request, "base.html", user=ctx.user, status_code=404)
    # Vehicle checks carry no driver_id; they chain via params->>'vehicle_id'.
    entries = (
        await session.scalars(
            select(ComplianceCheckLog)
            .where(
                ComplianceCheckLog.driver_id.is_(None),
                ComplianceCheckLog.params["vehicle_id"].astext == str(vehicle_id),
            )
            .order_by(ComplianceCheckLog.created_at.desc())
        )
    ).all()
    return render(request, "subject_history.html", user=ctx.user,
                  subject_type="vehicle", subject_id=vehicle_id,
                  subject_name=vehicle.kennzeichen, locked=False,
                  today=dates.today_berlin().isoformat(), entries=entries)


@router.post("/vehicles/{vehicle_id}/checks")
async def record_vehicle_check(
    vehicle_id: uuid.UUID, request: Request,
    ctx: TenantContext = Depends(get_tenant_context)
) -> object:
    if ctx.user.role == Role.VIEWER.value:
        raise Forbidden()
    session, user = ctx.session, ctx.user
    data = await form_data(request)
    kind = data.get("kind", "HU")
    result = data.get("result", checks.RESULT_PASSED)
    performed_on = _parse_date(data.get("performed_on")) or dates.today_berlin()

    kind_enum = DeadlineKind.HU if kind == "HU" else DeadlineKind.UVV
    open_dl = await session.scalar(
        select(Deadline).where(
            Deadline.vehicle_id == vehicle_id, Deadline.kind == kind_enum,
            Deadline.status == DeadlineStatus.OPEN).limit(1))
    if open_dl is not None:
        await checks.complete_deadline(
            session, deadline_id=open_dl.id, company_id=user.company_id,
            result=result, completed_on=performed_on,
            performed_by_id=user.id, method="WERKSTATT")
    else:
        await checks.record_check(
            session, company_id=user.company_id, kind=kind, result=result,
            performed_on=performed_on, vehicle_id=vehicle_id,
            performed_by_id=user.id, method="WERKSTATT")
    return RedirectResponse(url=f"/vehicles/{vehicle_id}/history", status_code=303)
