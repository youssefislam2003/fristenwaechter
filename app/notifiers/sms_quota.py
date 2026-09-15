"""Per-company monthly SMS budget guard.

Counts SMS actually delivered this calendar month (Berlin) for a company; once
the cap is reached, further *non-critical* SMS are skipped and a single WARNING
SystemAlert is raised for the company that month. Critical §21 alerts bypass
this entirely — you never silence a criminal-liability notification to save
budget (see SevenNotifier).

Runs as app_jobs (BYPASSRLS): it is a cross-tenant infrastructural count, and
the worker owns it, not a tenant-scoped web request.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.dates import now_berlin
from app.models import AlertSeverity, OutboxChannel, OutboxMessage, SystemAlert

logger = logging.getLogger(__name__)

CAP_ALERT_CODE = "SMS_CAP_EXCEEDED"


def _month_start_instant() -> datetime:
    now = now_berlin()
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


class DbSmsQuota:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        cap: int,
    ) -> None:
        self._sf = session_factory
        self._cap = cap

    async def over_limit(self, company_id: uuid.UUID) -> bool:
        """True when this company has already sent ``cap`` SMS this month."""
        month_start = _month_start_instant()
        async with self._sf() as s:
            sent = await s.scalar(
                select(func.count())
                .select_from(OutboxMessage)
                .where(
                    OutboxMessage.company_id == company_id,
                    OutboxMessage.channel == OutboxChannel.SMS,
                    OutboxMessage.sent_at.is_not(None),
                    OutboxMessage.sent_at >= month_start,
                )
            )
        return (sent or 0) >= self._cap

    async def record_skip(self, company_id: uuid.UUID) -> None:
        """Emit one WARNING alert per company per month, so the customer sees
        that routine SMS are being withheld — without flooding the ledger."""
        month_start = _month_start_instant()
        async with self._sf() as s, s.begin():
            existing = await s.scalar(
                select(func.count())
                .select_from(SystemAlert)
                .where(
                    SystemAlert.company_id == company_id,
                    SystemAlert.code == CAP_ALERT_CODE,
                    SystemAlert.created_at >= month_start,
                )
            )
            if existing:
                return
            s.add(
                SystemAlert(
                    company_id=company_id,
                    severity=AlertSeverity.WARNING,
                    code=CAP_ALERT_CODE,
                    message=(
                        f"Das monatliche SMS-Kontingent ({self._cap}) ist "
                        "erschoepft. Weitere nicht-kritische SMS werden bis "
                        "zum Monatswechsel unterdrueckt; E-Mails werden weiter "
                        "zugestellt. Kritische §21-Warnungen bleiben unberuehrt."
                    ),
                )
            )
        logger.warning("SMS cap reached for company=%s", company_id)
