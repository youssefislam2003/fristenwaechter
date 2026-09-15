"""T3 + T4 — authentication flows and row-level tenant isolation, against real
Postgres 16 (RLS is a Postgres feature; there is nothing to test in SQLite).

Everything here runs through ``app_sf`` — the NOBYPASSRLS ``app_user`` role the
web process actually uses — so the policies and the grant matrix are exercised
exactly as in production. The superuser ``admin_sf`` is used only to seed
cross-tenant fixtures (it bypasses RLS, which is the point of a seed helper).
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.models import AccountUser, Company, Driver, Role
from app.services import auth

pytestmark = pytest.mark.asyncio


# ─────────────────────────── seed helpers ───────────────────────────


async def _seed_company(admin_sf, name: str) -> dict:
    """Superuser seed: one company + one driver. Returns their ids."""
    async with admin_sf() as s, s.begin():
        company = Company(name=name)
        s.add(company)
        await s.flush()
        driver = Driver(company_id=company.id, first_name="Fahrer",
                        last_name=name)
        s.add(driver)
        await s.flush()
        return {"company_id": company.id, "driver_id": driver.id}


async def _set_guc(session, company_id: uuid.UUID) -> None:
    await session.execute(
        text("SELECT set_config('app.current_company', :c, true)"),
        {"c": str(company_id)},
    )


# ══════════════════════ §T4 — RLS tenant isolation ══════════════════════


class TestRowLevelSecurity:
    async def test_other_tenant_rows_invisible_even_via_raw_sql(
        self, admin_sf, app_sf
    ) -> None:
        a = await _seed_company(admin_sf, "Alpha GmbH")
        b = await _seed_company(admin_sf, "Beta GmbH")

        async with app_sf() as s, s.begin():
            await _set_guc(s, a["company_id"])
            rows = (await s.execute(text("SELECT id FROM driver"))).all()
            ids = {r.id for r in rows}
        assert ids == {a["driver_id"]}, "company B's driver leaked into A's view"

        async with app_sf() as s, s.begin():
            await _set_guc(s, b["company_id"])
            rows = (await s.execute(text("SELECT id FROM driver"))).all()
            ids = {r.id for r in rows}
        assert ids == {b["driver_id"]}

    async def test_missing_guc_yields_zero_rows_not_error(
        self, admin_sf, app_sf
    ) -> None:
        """A query with no tenant context must return NOTHING — never an error
        that could leak whether rows exist, and never every tenant's data."""
        await _seed_company(admin_sf, "Gamma GmbH")
        async with app_sf() as s, s.begin():
            count = await s.scalar(text("SELECT count(*) FROM driver"))
        assert count == 0

    async def test_write_check_blocks_cross_tenant_insert(
        self, admin_sf, app_sf
    ) -> None:
        """WITH CHECK: while scoped to A, inserting a row tagged for B fails."""
        a = await _seed_company(admin_sf, "Delta GmbH")
        b = await _seed_company(admin_sf, "Epsilon GmbH")
        with pytest.raises(DBAPIError):
            async with app_sf() as s, s.begin():
                await _set_guc(s, a["company_id"])
                await s.execute(
                    text("INSERT INTO driver (company_id, first_name, last_name) "
                         "VALUES (:c, 'X', 'Y')"),
                    {"c": str(b["company_id"])},
                )


# ══════════════════════ §T3 — signup / login / sessions ══════════════════════


class TestSignup:
    async def test_signup_creates_company_and_owner(self, app_sf) -> None:
        async with app_sf() as s, s.begin():
            res = await auth.signup(
                s, company_name="Mustermann Bau",
                owner_email="Chef@Mustermann.DE", password="s3hr-geheim!",
            )
        # Read back under the tenant GUC as app_user.
        async with app_sf() as s, s.begin():
            await _set_guc(s, res.company_id)
            owner = await s.get(AccountUser, res.owner_id)
            assert owner is not None
            assert owner.email == "chef@mustermann.de"   # normalized
            assert owner.role == Role.OWNER.value
            assert owner.password_hash and owner.password_hash != "s3hr-geheim!"

    async def test_authenticate_success_then_wrong_password(self, app_sf) -> None:
        async with app_sf() as s, s.begin():
            await auth.signup(s, company_name="Auth Co",
                              owner_email="a@b.de", password="richtiges-pw")

        res = await auth.login(app_sf, email="a@b.de",
                               password="richtiges-pw", ip="1.2.3.4")
        assert res.credentials.role == Role.OWNER.value
        assert res.token  # a session cookie token was minted

        with pytest.raises(auth.InvalidCredentials):
            await auth.login(app_sf, email="a@b.de", password="falsch",
                             ip="1.2.3.4")

    async def test_unknown_email_is_invalid_not_distinguishable(
        self, app_sf
    ) -> None:
        with pytest.raises(auth.InvalidCredentials):
            await auth.login(app_sf, email="nobody@nowhere.de", password="x",
                             ip="9.9.9.9")


class TestThrottle:
    async def test_locks_after_five_failures(self, app_sf) -> None:
        async with app_sf() as s, s.begin():
            await auth.signup(s, company_name="Lock Co",
                              owner_email="lock@co.de", password="correct")

        # Five wrong tries from the same (email, ip) — each records a failure
        # in its own committed transaction (that is the point of login()).
        for _ in range(auth.MAX_FAILS):
            with pytest.raises(auth.InvalidCredentials):
                await auth.login(app_sf, email="lock@co.de", password="nope",
                                 ip="5.5.5.5")

        # Sixth try — even with the CORRECT password — is locked out.
        with pytest.raises(auth.AccountLocked) as exc:
            await auth.login(app_sf, email="lock@co.de", password="correct",
                             ip="5.5.5.5")
        assert exc.value.retry_after_seconds > 0

        # A different IP is unaffected (the key is email+ip).
        res = await auth.login(app_sf, email="lock@co.de", password="correct",
                               ip="6.6.6.6")
        assert res.credentials is not None


class TestSessions:
    async def test_create_resolve_revoke(self, app_sf) -> None:
        async with app_sf() as s, s.begin():
            res = await auth.signup(s, company_name="Sess Co",
                                    owner_email="s@co.de", password="pw12345")
            token = await auth.create_session(
                s, user_id=res.owner_id, company_id=res.company_id)

        async with app_sf() as s, s.begin():
            resolved = await auth.resolve_session(s, token)
            assert resolved is not None
            assert resolved.company_id == res.company_id

        async with app_sf() as s, s.begin():
            await auth.revoke_session(s, token)
        async with app_sf() as s, s.begin():
            assert await auth.resolve_session(s, token) is None

    async def test_garbage_token_resolves_to_none(self, app_sf) -> None:
        async with app_sf() as s, s.begin():
            assert await auth.resolve_session(s, "not-a-real-token") is None


class TestInvitations:
    async def test_invite_and_accept_then_login(self, app_sf) -> None:
        async with app_sf() as s, s.begin():
            owner = await auth.signup(s, company_name="Invite Co",
                                      owner_email="owner@inv.de", password="ownerpw")
            invite = await auth.create_invitation(
                s, company_id=owner.company_id, email="mgr@inv.de",
                role=Role.MANAGER, invited_by_id=owner.owner_id)

        async with app_sf() as s, s.begin():
            new_uid = await auth.accept_invitation(
                s, raw_token=invite.raw_token, password="managerpw")
            assert new_uid is not None

        # The invited manager can now authenticate.
        res = await auth.login(app_sf, email="mgr@inv.de",
                               password="managerpw", ip="7.7.7.7")
        assert res.credentials.role == Role.MANAGER.value

    async def test_accept_is_single_use(self, app_sf) -> None:
        async with app_sf() as s, s.begin():
            owner = await auth.signup(s, company_name="Once Co",
                                      owner_email="once@inv.de", password="ownerpw")
            invite = await auth.create_invitation(
                s, company_id=owner.company_id, email="v@inv.de",
                role=Role.VIEWER, invited_by_id=owner.owner_id)

        async with app_sf() as s, s.begin():
            await auth.accept_invitation(
                s, raw_token=invite.raw_token, password="viewerpw")

        async with app_sf() as s, s.begin():
            with pytest.raises(auth.InvitationInvalid):
                await auth.accept_invitation(
                    s, raw_token=invite.raw_token, password="again")

    async def test_owner_cannot_be_invited(self, app_sf) -> None:
        async with app_sf() as s, s.begin():
            owner = await auth.signup(s, company_name="NoOwner Co",
                                      owner_email="o@x.de", password="pw")
            with pytest.raises(ValueError, match="OWNER"):
                await auth.create_invitation(
                    s, company_id=owner.company_id, email="x@x.de",
                    role=Role.OWNER, invited_by_id=owner.owner_id)
