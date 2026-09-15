"""T5 — DB-backed SMS monthly cap, against real Postgres.

Counts only SMS delivered this month, and raises exactly one WARNING alert per
company per month. Uses admin_sf (superuser) as the quota's session factory —
in production this is the BYPASSRLS app_jobs role; both see across tenants,
which is what the cross-tenant count needs.
"""
from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.dates import now_berlin
from app.models import (
    AlertSeverity,
    Company,
    OutboxChannel,
    OutboxMessage,
    SystemAlert,
)
from app.notifiers.sms_quota import CAP_ALERT_CODE, DbSmsQuota

pytestmark = pytest.mark.asyncio


async def _seed_company(admin_sf, name: str = "Cap Co"):
    async with admin_sf() as s, s.begin():
        c = Company(name=name)
        s.add(c)
        await s.flush()
        return c.id


async def _add_sent_sms(admin_sf, company_id, n: int) -> None:
    now = now_berlin()
    async with admin_sf() as s, s.begin():
        for i in range(n):
            s.add(OutboxMessage(
                company_id=company_id, channel=OutboxChannel.SMS,
                recipient=f"+4915100000{i:03d}", template="deadline_reminder",
                payload={}, dedup_key=f"sent-sms:{company_id}:{i}",
                sent_at=now))


class TestSmsQuota:
    async def test_under_cap_is_not_over_limit(self, admin_sf) -> None:
        cid = await _seed_company(admin_sf)
        await _add_sent_sms(admin_sf, cid, 3)
        quota = DbSmsQuota(admin_sf, cap=5)
        assert await quota.over_limit(cid) is False

    async def test_at_cap_is_over_limit(self, admin_sf) -> None:
        cid = await _seed_company(admin_sf)
        await _add_sent_sms(admin_sf, cid, 5)
        quota = DbSmsQuota(admin_sf, cap=5)
        assert await quota.over_limit(cid) is True

    async def test_record_skip_writes_one_warning_per_month(self, admin_sf) -> None:
        cid = await _seed_company(admin_sf)
        quota = DbSmsQuota(admin_sf, cap=1)

        await quota.record_skip(cid)
        await quota.record_skip(cid)   # idempotent within the month

        async with admin_sf() as s:
            count = await s.scalar(
                select(func.count()).select_from(SystemAlert).where(
                    SystemAlert.company_id == cid,
                    SystemAlert.code == CAP_ALERT_CODE))
            alert = await s.scalar(
                select(SystemAlert).where(SystemAlert.company_id == cid))
        assert count == 1
        assert alert.severity == AlertSeverity.WARNING

    async def test_cap_is_per_company(self, admin_sf) -> None:
        a = await _seed_company(admin_sf, "A")
        b = await _seed_company(admin_sf, "B")
        await _add_sent_sms(admin_sf, a, 5)
        quota = DbSmsQuota(admin_sf, cap=5)
        assert await quota.over_limit(a) is True
        assert await quota.over_limit(b) is False   # B has its own budget
