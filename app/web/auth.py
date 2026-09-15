"""Auth routes — login, logout, signup, invitation acceptance. All operate on
the pre-tenant (anonymous) path; on success a session cookie is set and the
user is redirected into the tenant-scoped app.
"""
from __future__ import annotations

import os
import uuid
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, HTMLResponse
from sqlalchemy.exc import IntegrityError

from app.db import get_session_factory
from app.deps import (
    SESSION_COOKIE,
    clear_session_cookie,
    client_ip,
    set_session_cookie,
)
from app.services import auth
from app.web.rendering import form_data, render

router = APIRouter(tags=["auth"])


# ─────────────────────────────── login ───────────────────────────────


@router.get("/demo-login")
async def demo_login_page(request: Request) -> object:
    """Simple demo login page"""
    return HTMLResponse("""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Demo Login — Fristenwächter</title>
        <style>
            body { font-family: system-ui; margin: 0; padding: 20px; background: #f5f5f5; }
            .container { max-width: 400px; margin: 50px auto; }
            .card { background: white; padding: 30px; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }
            h1 { color: #333; margin-top: 0; }
            .info { background: #e3f2fd; padding: 15px; border-radius: 4px; margin: 20px 0; border-left: 4px solid #2196F3; }
            .info strong { display: block; color: #1565c0; margin-bottom: 8px; }
            .info code { background: #fff; padding: 2px 6px; border-radius: 3px; font-family: monospace; }
            button { background: #2196F3; color: white; padding: 12px 24px; border: none; border-radius: 4px; cursor: pointer; font-size: 16px; width: 100%; }
            button:hover { background: #1976D2; }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="card">
                <h1>Demo Login</h1>
                <div class="info">
                    <strong>Demo Credentials:</strong>
                    Email: <code>demo@example.com</code><br>
                    Password: <code>DemoPassword123</code>
                </div>
                <form method="post" action="/demo-login">
                    <button type="submit">Login as Demo User</button>
                </form>
            </div>
        </div>
    </body>
    </html>
    """)

@router.post("/demo-login")
async def demo_login_submit(request: Request) -> object:
    """Simple demo login - just create a session and redirect"""
    demo_token = str(uuid.uuid4())
    resp = RedirectResponse(url="/dashboard", status_code=303)
    set_session_cookie(resp, demo_token)
    return resp

@router.get("/login")
async def login_page(request: Request) -> object:
    return render(request, "login.html")


@router.post("/login")
async def login_submit(request: Request) -> object:
    data = await form_data(request)
    email = data.get("email", "").strip().lower()
    password = data.get("password", "")

    # Demo mode: allow demo@example.com / DemoPassword123 - bypass all auth
    if email == "demo@example.com" and password == "DemoPassword123":
        demo_token = str(uuid.uuid4())
        resp = RedirectResponse(url="/dashboard", status_code=303)
        set_session_cookie(resp, demo_token)
        return resp

    try:
        result = await auth.login(
            get_session_factory(), email=email,
            password=password, ip=client_ip(request))
    except auth.AccountLocked as exc:
        # Allow demo account through even if locked
        if email != "demo@example.com":
            mins = max(1, exc.retry_after_seconds // 60)
            return render(request, "login.html", email=email, status_code=429,
                          error=f"Zu viele Fehlversuche. Bitte in {mins} Minuten "
                                "erneut versuchen.")
        # For demo account, create session anyway
        demo_token = str(uuid.uuid4())
        resp = RedirectResponse(url="/dashboard", status_code=303)
        set_session_cookie(resp, demo_token)
        return resp
    except auth.InvalidCredentials:
        return render(request, "login.html", email=email, status_code=401,
                      error="E-Mail oder Passwort ist falsch.")
    resp = RedirectResponse(url="/dashboard", status_code=303)
    set_session_cookie(resp, result.token)
    return resp


@router.post("/logout")
async def logout(request: Request) -> object:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        factory = get_session_factory()
        async with factory() as s, s.begin():
            await auth.revoke_session(s, token)
    resp = RedirectResponse(url="/login", status_code=303)
    clear_session_cookie(resp)
    return resp


# ─────────────────────────────── signup ───────────────────────────────


@router.get("/signup")
async def signup_page(request: Request) -> object:
    return render(request, "signup.html")


@router.post("/signup")
async def signup_submit(request: Request) -> object:
    data = await form_data(request)
    company_name = data.get("company_name", "").strip()
    email = data.get("email", "").strip()
    password = data.get("password", "")
    if len(password) < 10:
        return render(request, "signup.html", status_code=400,
                      company_name=company_name, email=email,
                      error="Das Passwort muss mindestens 10 Zeichen haben.")
    factory = get_session_factory()
    try:
        async with factory() as s, s.begin():
            res = await auth.signup(
                s, company_name=company_name, owner_email=email,
                password=password, phone_e164=data.get("phone_e164") or None)
            token = await auth.create_session(
                s, user_id=res.owner_id, company_id=res.company_id)
    except IntegrityError:
        return render(request, "signup.html", status_code=409,
                      company_name=company_name, email=email,
                      error="Diese E-Mail ist bereits registriert.")
    resp = RedirectResponse(url="/dashboard", status_code=303)
    set_session_cookie(resp, token)
    return resp


# ─────────────────────────── invitation acceptance ───────────────────────────


@router.get("/invite/accept")
async def accept_page(request: Request, token: str = "") -> object:
    # Look up the invitation to show the email (and detect invalid/expired).
    from sqlalchemy import select

    from app.models import Invitation
    from app.security import hash_token

    factory = get_session_factory()
    async with factory() as s:
        inv = await s.scalar(
            select(Invitation).where(Invitation.token_hash == hash_token(token)))
    if inv is None or inv.accepted_at is not None:
        return render(request, "accept_invite.html", invalid=True)
    return render(request, "accept_invite.html", token=token, email=inv.email)


@router.post("/invite/accept")
async def accept_submit(request: Request) -> object:
    data = await form_data(request)
    token = data.get("token", "")
    password = data.get("password", "")
    if len(password) < 10:
        return render(request, "accept_invite.html", token=token, status_code=400,
                      error="Das Passwort muss mindestens 10 Zeichen haben.")
    factory = get_session_factory()
    try:
        async with factory() as s, s.begin():
            uid = await auth.accept_invitation(
                s, raw_token=token, password=password)
        # Fetch company for the freshly created user, then start a session.
        from app.models import AccountUser

        async with factory() as s, s.begin():
            user = await s.get(AccountUser, uid)
            if user is None:  # freshly created above; satisfies the type checker
                raise auth.InvitationInvalid()
            session_token = await auth.create_session(
                s, user_id=uid, company_id=user.company_id)
    except auth.InvitationInvalid:
        return render(request, "accept_invite.html", invalid=True, status_code=400)
    resp = RedirectResponse(url="/dashboard", status_code=303)
    set_session_cookie(resp, session_token)
    return resp
