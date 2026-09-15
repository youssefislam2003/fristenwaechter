"""Server-rendered web layer (Jinja2 + HTMX). Each module exposes a `router`
that app.main includes. Templates and static assets are self-hosted — no
third-party origin is ever referenced (CSP default-src 'self')."""
