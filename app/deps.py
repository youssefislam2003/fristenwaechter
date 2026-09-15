"""FastAPI dependency layer — request-scoped DB sessions, authentication, and
tenant scoping. This is where I5 (tenancy) is mechanically enforced: the
tenant is ALWAYS ``current_user.company_id``, resolved server-side from the
session cookie, and pinned as the ``app.current_company`` GUC so RLS scopes
every subsequent query on the same connection.

Two request shapes:
  * ANONYMOUS  (login, signup, accept-invite): ``anon_session`` — a
    transaction with NO tenant GUC, for the pre-tenant auth flow.
  * TENANT     (everything else): ``get_tenant_context`` resolves the cookie,
    sets the GUC, loads the user, and yields a scoped session. ``current_user``
    and ``tenant_session`` are thin views over that one shared context so a
    single request opens exactly one transaction.
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from fastapi import Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session_factory
from app.models import AccountUser, Role
from app.services import auth
from app.settings import get_settings

SESSION_COOKIE = "fw_session"


class NotAuthenticated(Exception):
    """No valid session. A handler in main.py turns this into a redirect to
    /login for browser requests (or 401 for API/HTMX callers)."""


class Forbidden(Exception):
    """Authenticated but lacks the required role → 403."""


# ─────────────────────────── anonymous session ───────────────────────────


async def anon_session() -> AsyncIterator[AsyncSession]:
    """A committing transaction with NO tenant GUC — for login/signup/accept.
    Commits on clean exit, rolls back on exception."""
    factory = get_session_factory()
    async with factory() as session:
        async with session.begin():
            yield session


# ─────────────────────────── tenant context ───────────────────────────


@dataclass(slots=True)
class TenantContext:
    session: AsyncSession
    user: AccountUser


async def get_tenant_context(request: Request) -> AsyncIterator[TenantContext]:
    """Resolve the session cookie, pin the tenant GUC, load the user, and yield
    a tenant-scoped transaction. The single source of per-request tenancy."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise NotAuthenticated()

    factory = get_session_factory()
    async with factory() as session:
        async with session.begin():
            user_session = await auth.resolve_session(session, token)
            if user_session is None:
                raise NotAuthenticated()

            # Pin tenancy BEFORE any RLS-protected read. From here every query
            # on this connection is confined to the user's company.
            await auth.set_tenant_guc(session, user_session.company_id)

            user = await session.get(AccountUser, user_session.account_user_id)
            if user is None or not user.is_active:
                raise NotAuthenticated()

            yield TenantContext(session=session, user=user)


async def tenant_session(
    ctx: TenantContext = Depends(get_tenant_context),
) -> AsyncSession:
    """The tenant-scoped DB session for route handlers (GUC already set)."""
    return ctx.session


async def current_user(
    ctx: TenantContext = Depends(get_tenant_context),
) -> AccountUser:
    """The authenticated account user (tenant resolved server-side)."""
    return ctx.user


def require_role(
    *allowed: Role,
) -> Callable[..., Coroutine[Any, Any, AccountUser]]:
    """Dependency factory: gate a route to one or more roles. OWNER is always
    permitted implicitly where MANAGER is (it is strictly more privileged)."""
    allowed_values = {r.value for r in allowed}
    # OWNER supersedes MANAGER: any MANAGER-gated route also admits OWNER.
    if Role.MANAGER.value in allowed_values:
        allowed_values.add(Role.OWNER.value)

    async def _guard(user: AccountUser = Depends(current_user)) -> AccountUser:
        if user.role not in allowed_values:
            raise Forbidden()
        return user

    return _guard


# ─────────────────────────── cookie helpers ───────────────────────────


def _cookie_secure() -> bool:
    # Secure flag only over HTTPS; local http dev would otherwise never see the
    # cookie set. Derived from the configured public origin.
    return get_settings().BASE_URL.startswith("https://")


def set_session_cookie(response: Response, raw_token: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE,
        value=raw_token,
        max_age=int(auth.SESSION_TTL.total_seconds()),
        httponly=True,
        secure=_cookie_secure(),
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


def client_ip(request: Request) -> str:
    """Best-effort client IP for throttling. Honors a single X-Forwarded-For
    hop (the reverse proxy we control); falls back to the socket peer."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
