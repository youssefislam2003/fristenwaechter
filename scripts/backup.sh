#!/usr/bin/env bash
# Nightly encrypted backup: pg_dump piped straight into restic (no plaintext
# dump ever touches disk). Intended to run from cron on the DB host, e.g.:
#   15 2 * * *  /srv/fristen/scripts/backup.sh >> /var/log/fristen-backup.log 2>&1
#
# Requires: restic, pg_dump, and env RESTIC_REPOSITORY, RESTIC_PASSWORD,
# and a superuser/owner PGDATABASE connection (PGHOST/PGUSER/PGPASSWORD or a
# .pgpass). EU-resident restic backend only (Hetzner Storage Box, etc.).
set -euo pipefail

: "${RESTIC_REPOSITORY:?set RESTIC_REPOSITORY}"
: "${RESTIC_PASSWORD:?set RESTIC_PASSWORD}"
PGDATABASE="${PGDATABASE:-fristen}"
TAG="fristen-$(date -u +%Y%m%dT%H%M%SZ)"

echo "[$(date -u)] starting backup ${TAG}"

# Init repo on first run (no-op if it already exists).
restic snapshots >/dev/null 2>&1 || restic init

# Stream a consistent custom-format dump into restic via stdin.
pg_dump --format=custom --no-owner --no-privileges "${PGDATABASE}" \
  | restic backup --stdin --stdin-filename "${PGDATABASE}.dump" --tag "${TAG}"

# Retention: 7 daily, 4 weekly, 6 monthly.
restic forget --prune --keep-daily 7 --keep-weekly 4 --keep-monthly 6

# Prove the repo is intact (fast structural check).
restic check --read-data-subset=1%

echo "[$(date -u)] backup ${TAG} complete"
