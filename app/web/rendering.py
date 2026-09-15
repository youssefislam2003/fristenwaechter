"""Jinja2 environment, the render helper, and dependency-free form parsing.

Form parsing note: FastAPI's ``Form(...)`` / ``request.form()`` pull in
``python-multipart``, which is not in requirements.txt — and adding a
dependency needs sign-off. Every form in this app is
``application/x-www-form-urlencoded`` (no file uploads), which we parse
ourselves with ``urllib.parse.parse_qs``. Zero new dependencies.
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs

from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.requests import Request
from starlette.responses import HTMLResponse

from app.models import AccountUser
from app.settings import get_settings

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=select_autoescape(["html", "xml"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


def render(
    request: Request,
    template: str,
    *,
    user: AccountUser | None = None,
    status_code: int = 200,
    **context: object,
) -> HTMLResponse:
    """Render a template to an HTMLResponse. ``user`` and ``request`` are always
    available in templates; HX-Request tells a template it is a partial swap."""
    tmpl = _env.get_template(template)
    html = tmpl.render(
        request=request,
        user=user,
        base_url=get_settings().BASE_URL,
        is_htmx=bool(request.headers.get("HX-Request")),
        **context,
    )
    return HTMLResponse(html, status_code=status_code)


async def form_data(request: Request) -> dict[str, str]:
    """Parse a urlencoded form body into a flat str→str dict (last value wins).
    Avoids the python-multipart dependency ``request.form()`` would require."""
    raw = await request.body()
    parsed = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
    return {k: v[-1].strip() for k, v in parsed.items()}
