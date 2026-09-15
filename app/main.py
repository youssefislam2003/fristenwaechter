"""FastAPI entrypoint — the WEB process only.

Hard rule (T2): the web process must NOT start the APScheduler. The relay and
sweeps run exclusively in app/worker.py. If the web app also scheduled them,
N web replicas would each run their own relay — SKIP LOCKED would keep it
correct, but it muddies ownership and doubles provider calls under autoscale.
One writer of side-effects, one place it lives.

Security posture (German courts have fined sites for leaking visitor IPs to
US CDNs via Google Fonts): a strict Content-Security-Policy of
``default-src 'self'`` with NO third-party origins anywhere. Every asset —
CSS, htmx, fonts — is self-hosted and served from this origin. The middleware
below is the enforcement point.
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.staticfiles import StaticFiles

from app.db import dispose_engines

_STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"

logger = logging.getLogger(__name__)


# ─────────────────────────── security headers ───────────────────────────

# Self-only CSP. 'unsafe-inline' is permitted for style/script because the
# HTMX dashboard uses small inline handlers and a few inline styles; scripts
# and styles still cannot be loaded from any foreign origin. No connect-src to
# third parties, no font-src to Google — everything is same-origin.
_CSP = (
    "default-src 'self'; "
    "img-src 'self' data:; "
    "style-src 'self' 'unsafe-inline'; "
    "script-src 'self' 'unsafe-inline'; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'"
)

_SECURITY_HEADERS = {
    "Content-Security-Policy": _CSP,
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
    # HSTS is safe to send; browsers ignore it over plain HTTP (local dev).
    "Strict-Transport-Security": "max-age=63072000; includeSubDomains",
    # No FLoC / Topics interest-cohort participation.
    "Permissions-Policy": "browser=(), geolocation=(), microphone=(), camera=()",
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        for header, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        return response


# ───────────────────────────── lifespan ─────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logger.info("web starting up (scheduler NOT started here — see worker.py)")
    yield
    await dispose_engines()
    logger.info("web shut down; DB pools disposed")


# ───────────────────────────── app factory ─────────────────────────────


def create_app() -> FastAPI:
    app = FastAPI(
        title="Fristenwächter",
        description="Compliance-Engine für gewerbliche Fuhrparks (HU/AU, UVV, FSK).",
        lifespan=lifespan,
        docs_url=None,      # no interactive API docs on a server-rendered app
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(SecurityHeadersMiddleware)
    _register_exception_handlers(app)

    # Self-hosted assets only (CSS, vendored htmx) — no third-party origin.
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # Routers are included as their tasks land (T3 auth, T7 dashboard, T8
    # DSGVO). Import locally to keep this module importable before they exist.
    _include_routers(app)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> JSONResponse:
        """Liveness for the web process. Deliberately does NOT touch the DB:
        it answers 'is this uvicorn worker accepting requests', which is what
        a load balancer needs. DB-dependent readiness is a separate concern."""
        return JSONResponse({"status": "ok"})

    @app.get("/", include_in_schema=False)
    async def index() -> RedirectResponse:
        # Landing → dashboard (auth redirects to /login when unauthenticated,
        # once T3/T7 are wired). 307 preserves method; harmless for GET.
        return RedirectResponse(url="/dashboard", status_code=307)

    return app


def _register_exception_handlers(app: FastAPI) -> None:
    """Auth/authorization failures become browser-appropriate responses.

    An unauthenticated *browser* navigation should land on /login, but an
    unauthenticated HTMX/fetch/API call wants a clean 401 (HTMX can react to
    the status) — so we branch on whether the client asked for HTML.
    """
    from app.deps import Forbidden, NotAuthenticated

    def _wants_html(request: Request) -> bool:
        # HTMX requests carry HX-Request; treat those as API-style even though
        # they accept HTML, so the login redirect doesn't get swallowed into a
        # partial swap.
        if request.headers.get("HX-Request"):
            return False
        return "text/html" in request.headers.get("accept", "")

    @app.exception_handler(NotAuthenticated)
    async def _on_unauthenticated(
        request: Request, exc: NotAuthenticated
    ) -> Response:
        if _wants_html(request):
            return RedirectResponse(url="/login", status_code=303)
        resp = JSONResponse({"detail": "not authenticated"}, status_code=401)
        # Let HTMX trigger a full-page redirect to the login screen.
        resp.headers["HX-Redirect"] = "/login"
        return resp

    @app.exception_handler(Forbidden)
    async def _on_forbidden(request: Request, exc: Forbidden) -> Response:
        return JSONResponse({"detail": "forbidden"}, status_code=403)


def _include_routers(app: FastAPI) -> None:
    """Wire feature routers. Guarded so partial builds still boot: a router
    module that doesn't exist yet is simply skipped with a log line, not an
    ImportError that takes the whole app down."""
    for module_path, attr in (
        ("app.web.auth", "router"),
        ("app.web.dashboard", "router"),
        ("app.web.vehicles", "router"),
        ("app.web.drivers", "router"),
        ("app.web.escalations", "router"),
        ("app.web.settings_page", "router"),
        ("app.web.dsgvo", "router"),
        ("app.web.legal", "router"),
    ):
        try:
            module = __import__(module_path, fromlist=[attr])
        except ModuleNotFoundError:
            logger.debug("router %s not present yet — skipping", module_path)
            continue
        app.include_router(getattr(module, attr))


app = create_app()
