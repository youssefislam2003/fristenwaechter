# syntax=docker/dockerfile:1
# Multi-stage, non-root. One image runs BOTH the web and the worker; the
# process is chosen by the container's command (see docker-compose.yml).

# ── build stage: compile wheels into a venv ──────────────────────────────
FROM python:3.11-slim AS build

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
RUN python -m venv /venv
ENV PATH="/venv/bin:$PATH"

# Install runtime deps first for layer caching.
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# ── runtime stage: slim, non-root ────────────────────────────────────────
FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/venv/bin:$PATH" \
    WORKER_HEARTBEAT_FILE=/tmp/fristen-worker.alive

# Non-root user; owns /app and /tmp for the heartbeat file.
RUN groupadd --system app && useradd --system --gid app --create-home app

COPY --from=build /venv /venv
WORKDIR /app
COPY --chown=app:app app ./app
COPY --chown=app:app migrations ./migrations
COPY --chown=app:app alembic.ini ./alembic.ini
COPY --chown=app:app scripts ./scripts

USER app
EXPOSE 8000

# Default command = web. The worker service overrides this in compose.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
