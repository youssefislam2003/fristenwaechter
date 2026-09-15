#!/usr/bin/env bash
# Fristenwächter deploy: build images, bootstrap roles + migrate, bring up
# web + worker. Idempotent — safe to re-run for updates. Requires a populated
# .env (see .env.example).
set -euo pipefail

cd "$(dirname "$0")"

if [[ ! -f .env ]]; then
  echo "ERROR: .env missing. Copy .env.example → .env and set secrets." >&2
  exit 1
fi

echo "==> Building images"
docker compose build

echo "==> Bootstrapping roles + running migrations (one-shot)"
docker compose run --rm migrate

echo "==> Emitting migration SQL for review artifact"
docker compose run --rm --entrypoint sh migrate -c \
  "alembic upgrade head --sql" > migration-review.sql || true

echo "==> Starting web + worker"
docker compose up -d web worker

echo "==> Waiting for web health"
for _ in $(seq 1 20); do
  if curl -fsS http://localhost:8000/healthz >/dev/null 2>&1; then
    echo "web healthy."
    break
  fi
  sleep 2
done

echo "==> Deployed. Web on http://localhost:8000"
docker compose ps
