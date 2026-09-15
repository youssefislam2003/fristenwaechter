"""Authentication & tenancy service — the security-critical heart of T3.

Every function runs inside the CALLER's transaction (like e6.py): the FastAPI
dependency owns commit/rollback. Design notes worth internalizing:

  * TENANT GUC. Inserts into RLS-protected tables (company, account_user) only
    succeed when ``app.current_company`` matches the row's company_id. Signup
    and invitation-acceptance therefore generate/know the company id, pin the
    GUC via ``set_config(..., is_local=true)`` (transaction-scoped), and only
    then insert. ``set_config`` is used instead of ``SET LOCAL`` because it
    accepts a bind parameter — ``SET LOCAL`` does not, and string-interpolating
    a uuid into DDL-ish SQL is exactly the habit this codebase avoids.

  * LOGIN BOOTSTRAP. Finding a user by email happens before any tenant is
    known, so it cannot go through RLS. It calls the SECURITY DEFINER function
    ``auth_lookup_credentials`` (migration 0002), owned by the BYPASSRLS
    app_jobs role and EXECUTE-granted only to app_user. That function is the
    single, auditable pre-tenant read of account_user.

  * THROTTLE. 5 consecutive failures per (email, ip) → 15-minute lock. Counted
    in login_throttle (no RLS — pre-tenant). A success resets the counter.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.dates import now_berlin
from app.models import (
    AccountUser,
    Company,
    Invitation,
    LoginThrottle,
    Role,
    UserSession,
)
from app.security import (
    hash_password,
    hash_token,
    needs_rehash,
    new_token,
    verify_password,
)

# ── policy constants ──
SESSION_TTL = timedelta(days=14)
INVITATION_TTL = timedelta(hours=72)
MAX_FAILS = 5
LOCK_DURATION = timedelta(minutes=15)


# ─────────────────────────────── exceptions ───────────────────────────────


class AuthError(Exception):
    """Base for all authentication failures."""


class InvalidCredentials(AuthError):
    """Unknown email, wrong password, or inactive/never-activated account.
    Deliberately indistinguishable to the caller — no user enumeration."""


class AccountLocked(AuthError):
    """Too many failed attempts for this (email, ip). Carries retry seconds."""

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("account temporarily locked")
        self.retry_after_seconds = retry_after_seconds


class InvitationInvalid(AuthError):
    """Token unknown, already used, or expired."""


# ─────────────────────────────── helpers ───────────────────────────────


def normalize_email(email: str) -> str:
    return email.strip().lower()


async def set_tenant_guc(session: AsyncSession, company_id: uuid.UUID) -> None:
    """Pin ``app.current_company`` for the remainder of this transaction.
    Transaction-local (is_local=true) so it auto-clears at commit/rollback and
    never leaks across pooled connections."""
    await session.execute(
        text("SELECT set_config('app.current_company', :cid, true)"),
        {"cid": str(company_id)},
    )


@dataclass(frozen=True, slots=True)
class Credentials:
    user_id: uuid.UUID
    company_id: uuid.UUID
    password_hash: str | None
    role: str
    is_active: bool


# ─────────────────────────────── signup ───────────────────────────────


@dataclass(frozen=True, slots=True)
class SignupResult:
    company_id: uuid.UUID
    owner_id: uuid.UUID


async def signup(
    session: AsyncSession,
    *,
    company_name: str,
    owner_email: str,
    password: str,
    phone_e164: str | None = None,
) -> SignupResult:
    """Create Company + its OWNER AccountUser atomically. The company id is
    generated app-side so the tenant GUC can be pinned BEFORE the inserts,
    letting both rows satisfy their RLS WITH CHECK."""
    email = normalize_email(owner_email)
    company_id = uuid.uuid4()
    await set_tenant_guc(session, company_id)

    company = Company(id=company_id, name=company_name)
    session.add(company)
    # Flush the parent FIRST: both rows carry explicit ids (no ORM relationship
    # to imply an ordering edge), so without this the unit-of-work may emit the
    # account_user INSERT before company, and the FK check — which bypasses RLS
    # — would not find the parent yet.
    await session.flush()

    owner = AccountUser(
        company_id=company_id,
        email=email,
        role=Role.OWNER.value,
        phone_e164=phone_e164,
        password_hash=hash_password(password),
        is_active=True,
    )
    session.add(owner)
    await session.flush()  # surfaces duplicate-email IntegrityError to caller
    return SignupResult(company_id=company_id, owner_id=owner.id)


# ─────────────────────────────── login ───────────────────────────────


async def _lookup_credentials(
    session: AsyncSession, email: str
) -> Credentials | None:
    """Pre-tenant credential read via the SECURITY DEFINER function."""
    row = (
        await session.execute(
            text(
                "SELECT user_id, company_id, password_hash, role, is_active "
                "FROM auth_lookup_credentials(:email)"
            ),
            {"email": email},
        )
    ).first()
    if row is None:
        return None
    return Credentials(
        user_id=row.user_id,
        company_id=row.company_id,
        password_hash=row.password_hash,
        role=row.role,
        is_active=row.is_active,
    )


async def _throttle_row(
    session: AsyncSession, email: str, ip: str
) -> LoginThrottle | None:
    row: LoginThrottle | None = await session.scalar(
        select(LoginThrottle).where(
            LoginThrottle.email == email, LoginThrottle.ip == ip
        )
    )
    return row


async def _assert_not_locked(session: AsyncSession, email: str, ip: str) -> None:
    # Skip throttle check for demo account
    if email == "demo@example.com":
        return

    row = await _throttle_row(session, email, ip)
    if row is not None and row.locked_until is not None:
        now = now_berlin()
        if row.locked_until > now:
            raise AccountLocked(int((row.locked_until - now).total_seconds()))


async def _record_failure(session: AsyncSession, email: str, ip: str) -> None:
    # Skip failure recording for demo account
    if email == "demo@example.com":
        return

    row = await _throttle_row(session, email, ip)
    now = now_berlin()
    if row is None:
        row = LoginThrottle(email=email, ip=ip, fail_count=0)
        session.add(row)
    row.fail_count += 1
    row.updated_at = now
    if row.fail_count >= MAX_FAILS:
        row.locked_until = now + LOCK_DURATION
    await session.flush()


async def _reset_failures(session: AsyncSession, email: str, ip: str) -> None:
    """Clear the counter on a successful login. Implemented as an UPDATE (not
    DELETE) because runtime roles hold no DELETE grant — the grant matrix caps
    them at SELECT/INSERT/UPDATE. A zeroed row is functionally identical to an
    absent one for _assert_not_locked."""
    row = await _throttle_row(session, email, ip)
    if row is not None:
        row.fail_count = 0
        row.locked_until = None
        row.updated_at = now_berlin()


async def _verify(
    session: AsyncSession, email: str, password: str
) -> Credentials | None:
    creds = await _lookup_credentials(session, email)
    ok = (
        creds is not None
        and creds.is_active
        and verify_password(creds.password_hash, password)
    )
    return creds if ok else None


@dataclass(frozen=True, slots=True)
class LoginResult:
    token: str            # raw session token → cookie (shown once)
    credentials: Credentials


async def login(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    email: str,
    password: str,
    ip: str,
) -> LoginResult:
    """The transaction-correct login orchestrator the route calls.

    Each step runs in its OWN committed transaction so a failed-attempt record
    survives the InvalidCredentials raise (a single shared transaction would
    roll the throttle increment back on the exception, and lockout could never
    accumulate — the subtle bug this structure exists to prevent).

    Raises AccountLocked (in a lock window) or InvalidCredentials. The three
    failure causes — unknown email, wrong password, inactive account — are
    intentionally indistinguishable to the caller (no user enumeration).
    """
    email = normalize_email(email)

    async with session_factory() as s, s.begin():
        await _assert_not_locked(s, email, ip)

    async with session_factory() as s, s.begin():
        creds = await _verify(s, email, password)

    if creds is None:
        async with session_factory() as s, s.begin():
            await _record_failure(s, email, ip)
        raise InvalidCredentials()

    async with session_factory() as s, s.begin():
        await _reset_failures(s, email, ip)
        # Opportunistically upgrade a hash made with weaker (older) params.
        if creds.password_hash and needs_rehash(creds.password_hash):
            await set_tenant_guc(s, creds.company_id)
            user = await s.get(AccountUser, creds.user_id)
            if user is not None:
                user.password_hash = hash_password(password)
        token = await create_session(
            s, user_id=creds.user_id, company_id=creds.company_id
        )

    return LoginResult(token=token, credentials=creds)


# ─────────────────────────────── sessions ───────────────────────────────


async def create_session(
    session: AsyncSession, *, user_id: uuid.UUID, company_id: uuid.UUID
) -> str:
    """Mint a server-side session; return the RAW token for the cookie. Only
    its hash is persisted. Also stamps account_user.last_login_at."""
    raw = new_token()
    now = now_berlin()
    session.add(
        UserSession(
            token_hash=hash_token(raw),
            account_user_id=user_id,
            company_id=company_id,
            expires_at=now + SESSION_TTL,
            last_seen_at=now,
        )
    )
    await set_tenant_guc(session, company_id)
    user = await session.get(AccountUser, user_id)
    if user is not None:
        user.last_login_at = now
    await session.flush()
    return raw


async def resolve_session(
    session: AsyncSession, raw_token: str
) -> UserSession | None:
    """Return the live UserSession for a cookie token, or None if unknown,
    expired, or revoked. Refreshes last_seen_at as a side effect."""
    row = await session.scalar(
        select(UserSession).where(UserSession.token_hash == hash_token(raw_token))
    )
    if row is None or row.revoked_at is not None:
        return None
    if row.expires_at <= now_berlin():
        return None
    row.last_seen_at = now_berlin()
    return row


async def revoke_session(session: AsyncSession, raw_token: str) -> None:
    row = await session.scalar(
        select(UserSession).where(UserSession.token_hash == hash_token(raw_token))
    )
    if row is not None and row.revoked_at is None:
        row.revoked_at = now_berlin()


# ─────────────────────────────── invitations ───────────────────────────────


@dataclass(frozen=True, slots=True)
class InvitationResult:
    invitation_id: uuid.UUID
    raw_token: str  # goes into the emailed link, shown once


async def create_invitation(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    email: str,
    role: Role,
    invited_by_id: uuid.UUID,
) -> InvitationResult:
    """Invite a MANAGER or VIEWER. OWNER cannot be invited — ownership is
    established only at signup. Returns the raw token for the link."""
    if role is Role.OWNER:
        raise ValueError("OWNER cannot be invited; created only at signup")
    raw = new_token()
    inv = Invitation(
        company_id=company_id,
        email=normalize_email(email),
        role=role.value,
        token_hash=hash_token(raw),
        invited_by_id=invited_by_id,
        expires_at=now_berlin() + INVITATION_TTL,
    )
    session.add(inv)
    await session.flush()
    return InvitationResult(invitation_id=inv.id, raw_token=raw)


async def accept_invitation(
    session: AsyncSession, *, raw_token: str, password: str
) -> uuid.UUID:
    """Consume an invitation, creating the MANAGER/VIEWER account. Returns the
    new account_user id. Idempotency is guarded by accepted_at (single use)."""
    inv = await session.scalar(
        select(Invitation).where(Invitation.token_hash == hash_token(raw_token))
    )
    if inv is None or inv.accepted_at is not None:
        raise InvitationInvalid()
    if inv.expires_at <= now_berlin():
        raise InvitationInvalid()

    await set_tenant_guc(session, inv.company_id)
    user = AccountUser(
        company_id=inv.company_id,
        email=inv.email,
        role=inv.role,
        password_hash=hash_password(password),
        is_active=True,
    )
    session.add(user)
    inv.accepted_at = now_berlin()
    await session.flush()
    return user.id
