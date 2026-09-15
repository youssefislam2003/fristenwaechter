"""Static legal pages — public (no auth). Impressum (§5 TMG),
Datenschutzerklärung, and an AVV (Auftragsverarbeitungsvertrag, Art. 28 DSGVO)
placeholder. All copy is marked LAWYER-REVIEW-REQUIRED: this encodes the
structure, not legal advice.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

from app.web.rendering import render

router = APIRouter(tags=["legal"])


@router.get("/impressum")
async def impressum(request: Request) -> object:
    return render(request, "legal_impressum.html")


@router.get("/datenschutz")
async def datenschutz(request: Request) -> object:
    return render(request, "legal_datenschutz.html")


@router.get("/avv")
async def avv(request: Request) -> object:
    """AVV download placeholder. A real deployment serves the signed PDF; here
    it is a clearly-marked stub so the link exists and the gap is visible."""
    body = (
        "AUFTRAGSVERARBEITUNGSVERTRAG (AVV) — Art. 28 DSGVO\n"
        "===================================================\n\n"
        "*** PLATZHALTER — LAWYER-REVIEW-REQUIRED ***\n\n"
        "Dieses Dokument ist ein Platzhalter. Vor Produktivbetrieb ist ein\n"
        "durch eine Fachanwältin/einen Fachanwalt fuer IT-/Datenschutzrecht\n"
        "gepruefter AVV bereitzustellen. Fristenwaechter handelt als\n"
        "Auftragsverarbeiter; der Kunde ist Verantwortlicher.\n\n"
        "Subunternehmer (EU): Hetzner (DE), Brevo (EU), seven.io (DE).\n"
    )
    return PlainTextResponse(
        body, headers={"Content-Disposition":
                       'attachment; filename="AVV-PLATZHALTER.txt"'})
