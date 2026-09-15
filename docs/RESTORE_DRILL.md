# Restore Drill — run monthly, log the result

A backup you have never restored is a hope, not a backup. Perform this drill on
the **first business day of each month** and record the outcome (date, restored
snapshot id, row-count spot check, who ran it) in the ops log.

## Prerequisites
- `restic` installed and `RESTIC_REPOSITORY` / `RESTIC_PASSWORD` exported.
- A **scratch** Postgres you can freely clobber (never the production DB).

## Steps

1. **List snapshots** and pick the most recent:
   ```bash
   restic snapshots --tag fristen | tail
   ```

2. **Restore the dump** to a working directory:
   ```bash
   restic restore latest --target /tmp/restore
   # → /tmp/restore/fristen.dump
   ```

3. **Recreate a scratch database** and load it:
   ```bash
   createdb fristen_restore_test
   pg_restore --no-owner --no-privileges --dbname fristen_restore_test \
     /tmp/restore/fristen.dump
   ```

4. **Spot-check integrity** — counts are non-zero and the evidence chain is
   intact (no NULL entry_hash, chain links resolve):
   ```bash
   psql fristen_restore_test -c "SELECT count(*) FROM company;"
   psql fristen_restore_test -c "SELECT count(*) FROM compliance_check_log WHERE entry_hash IS NULL;"  -- expect 0
   psql fristen_restore_test -c "SELECT count(*) FROM compliance_check_log c
     LEFT JOIN compliance_check_log p ON c.prev_hash = p.entry_hash
     WHERE c.prev_hash IS NOT NULL AND p.id IS NULL;"  -- expect 0 (no broken links)
   ```

5. **Tear down** the scratch DB and restore dir:
   ```bash
   dropdb fristen_restore_test && rm -rf /tmp/restore
   ```

6. **Record** in the ops log: date, snapshot id, counts, pass/fail, operator.

## Recovery Time Objective
Target: full restore + verification in **under 30 minutes**. If a drill exceeds
that, treat it as an incident and tune (dump size, network, parallel restore).
