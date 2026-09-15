"""Dashboard — traffic-light list of open obligations, locked drivers pinned
on top with the § 21 banner.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select

from app.dates import days_until, today_berlin
from app.deps import TenantContext, get_tenant_context
from app.models import Deadline, DeadlineKind, DeadlineStatus, Driver, Vehicle
from app.web.rendering import render

router = APIRouter(tags=["dashboard"])

KIND_LABEL = {
    DeadlineKind.HU: "Hauptuntersuchung (HU/AU)",
    DeadlineKind.UVV: "UVV-Prüfung",
    DeadlineKind.FSK: "Führerscheinkontrolle",
}


def classify(days: int) -> tuple[str, str, str]:
    """(traffic-light row class, badge class, badge text) for a day count."""
    if days < 0:
        return "tl-red", "badge-red", f"überfällig ({-days} T.)"
    if days <= 30:
        return "tl-amber", "badge-amber", f"in {days} Tagen"
    return "tl-green", "badge-green", f"in {days} Tagen"


@router.get("/dashboard")
async def dashboard(
    request: Request, ctx: TenantContext = Depends(get_tenant_context)
) -> object:
    session, user = ctx.session, ctx.user

    locked = (
        await session.scalars(
            select(Driver).where(Driver.is_authorized_to_drive.is_(False))
        )
    ).all()

    deadlines = (
        await session.scalars(
            select(Deadline)
            .where(Deadline.status == DeadlineStatus.OPEN)
            .order_by(Deadline.due_on)
        )
    ).all()

    today = today_berlin()
    rows: list[dict[str, object]] = []
    for d in deadlines:
        if d.driver_id is not None:
            driver = await session.get(Driver, d.driver_id)
            name = f"{driver.first_name} {driver.last_name}" if driver else "—"
            link = f"/drivers/{d.driver_id}/history"
        else:
            vehicle = await session.get(Vehicle, d.vehicle_id)
            name = vehicle.kennzeichen if vehicle else "—"
            link = f"/vehicles/{d.vehicle_id}/history"
        days = days_until(d.due_on, today=today)
        tl, badge, text = classify(days)
        rows.append({
            "name": name,
            "kind_label": KIND_LABEL.get(d.kind, d.kind.value),
            "due": d.due_on.strftime("%d.%m.%Y"),
            "days": days,
            "tl_class": tl, "badge_class": badge, "badge_text": text,
            "link": link,
        })
    rows.sort(key=lambda r: int(r["days"]))  # type: ignore[call-overload]

    return render(request, "dashboard.html", user=user, locked=locked, rows=rows)
