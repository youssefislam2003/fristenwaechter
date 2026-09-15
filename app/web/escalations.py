"""Escalation case view lives on the driver router (/drivers/{id}/escalation),
since a case is always viewed in a driver's context. This module exists so the
app.main router auto-include has a stable target; it currently adds no routes.
"""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["escalations"])
