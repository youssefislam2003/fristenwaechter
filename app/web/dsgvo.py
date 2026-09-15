"""DSGVO self-service endpoints (OWNER-only): data export (Art. 15/20),
per-driver anonymization (Art. 17), and tenant deletion request.
"""
from __future__ import annotations

import csv
import io
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dates import now_berlin
from app.deps import Forbidden, TenantContext, get_tenant_context
from app.models import (
    AccountUser,
    Company,
    ComplianceCheckLog,
    Deadline,
    Driver,
    EscalationCase,
    Role,
    Vehicle,
)
from app.services import dsgvo as svc

router = APIRouter(tags=["dsgvo"])


def _require_owner(ctx: TenantContext) -> None:
    if ctx.user.role != Role.OWNER.value:
        raise Forbidden()


async def _collect(
    session: AsyncSession, company_id: uuid.UUID
) -> dict[str, object]:
    async def rows(model: Any) -> list[dict[str, object]]:
        cols = [c.name for c in model.__table__.columns]
        objs: list[Any] = list(
            (await session.scalars(
                select(model).where(model.company_id == company_id))).all())
        return [{c: _json_safe(getattr(o, c)) for c in cols} for o in objs]

    company = await session.get(Company, company_id)
    return {
        "company": [{c.name: _json_safe(getattr(company, c.name))
                     for c in Company.__table__.columns}] if company else [],
        "users": await rows(AccountUser),
        "drivers": await rows(Driver),
        "vehicles": await rows(Vehicle),
        "deadlines": await rows(Deadline),
        "escalation_cases": await rows(EscalationCase),
        "check_logs": await rows(ComplianceCheckLog),
    }


def _json_safe(v: object) -> object:
    import datetime as _dt

    if isinstance(v, (uuid.UUID, _dt.date, _dt.datetime)):
        return str(v)
    return v


@router.get("/export")
async def export_json(
    request: Request, ctx: TenantContext = Depends(get_tenant_context)
) -> object:
    _require_owner(ctx)
    data = await _collect(ctx.session, ctx.user.company_id)
    return JSONResponse(
        data,
        headers={"Content-Disposition":
                 'attachment; filename="fristenwaechter-export.json"'})


@router.get("/export.csv")
async def export_csv(
    request: Request, ctx: TenantContext = Depends(get_tenant_context)
) -> object:
    """CSV of the compliance evidence log — the audit trail, most useful flat."""
    _require_owner(ctx)
    logs = (await ctx.session.scalars(
        select(ComplianceCheckLog)
        .where(ComplianceCheckLog.company_id == ctx.user.company_id)
        .order_by(ComplianceCheckLog.created_at))).all()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["created_at", "kind", "result", "method", "driver_id",
                     "performed_on", "prev_hash", "entry_hash"])
    for r in logs:
        writer.writerow([r.created_at.isoformat(), r.kind, r.result, r.method or "",
                         r.driver_id or "", r.performed_on.isoformat(),
                         r.prev_hash or "", r.entry_hash])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition":
                 'attachment; filename="fristenwaechter-nachweise.csv"'})


@router.post("/drivers/{driver_id}/anonymize")
async def anonymize_driver_endpoint(
    driver_id: uuid.UUID, request: Request,
    ctx: TenantContext = Depends(get_tenant_context)
) -> object:
    _require_owner(ctx)
    driver = await svc.get_driver_in_company(
        ctx.session, driver_id, ctx.user.company_id)
    if driver is None:
        return JSONResponse({"detail": "not found"}, status_code=404)
    await svc.anonymize_driver(ctx.session, driver)
    return RedirectResponse(url="/drivers", status_code=303)


@router.post("/company/delete-request")
async def request_company_deletion(
    request: Request, ctx: TenantContext = Depends(get_tenant_context)
) -> object:
    _require_owner(ctx)
    company = await ctx.session.get(Company, ctx.user.company_id)
    if company is not None and company.deletion_requested_at is None:
        company.deletion_requested_at = now_berlin()
    return RedirectResponse(url="/settings", status_code=303)
