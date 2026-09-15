# Fristenwächter — Compliance Engine for German Commercial Fleets

**B2B Micro-SaaS · Berlin · HU/AU (TÜV) · UVV-Prüfung · Führerscheinkontrolle**

Small German fleet operators (Handwerksbetriebe, 5–50 vehicles) carry personal
criminal liability — *Halterhaftung*, § 21 Abs. 1 Nr. 2 StVG — if an employee
drives a company vehicle without a valid license, and face fines and insurance
exposure for missed HU or UVV inspections. This system is automated insurance
against that: it tracks every deadline, proves every check happened, and
escalates hard when one fails.

**Two product-defining commitments, enforced in code:**

1. **No document retention.** No license scans, no license numbers — there is
   no column where they could live, and the Pydantic layer (`extra="forbid"`,
   `strict=True`) rejects them at the API boundary. We store the legally
   useful artifact only: a hash-chained, timestamped, append-only log of
   *who checked whom, when, how, with what result*. We act strictly as
   Auftragsverarbeiter (Art. 28 DSGVO); the customer is the Verantwortlicher.
2. **Zero-unhandled-I/O-failure on critical alerts.** A failed license check
   triggers an atomic transition (driver hard-lock + immutable alert + 7-day
   escalation case + notification intent) in ONE database transaction with no
   network I/O. Delivery is a separate at-least-once relay. There is no state
   in which the driver is locked but the Geschäftsführer was never told.

---

## Repository layout

```
app/
├── dates.py               Calendar authority. Instants (aware datetime) vs
│                          Obligations (Berlin civil date). Clamping
│                          add_months, month-granular HU dates, the 7-day
│                          end-of-civil-day escalation window.  [VERIFIED]
├── models.py              SQLAlchemy 2.0 async models: Company, AccountUser,
│                          Driver (native version_id optimistic locking),
│                          EscalationCase (partial unique: <=1 OPEN per
│                          driver), SystemAlert & ComplianceCheckLog
│                          (append-only), OutboxMessage (delivery FSM)
├── services/e6.py         run_e6 -> handle_failed_license_check: the atomic
│                          § 21 StVG pipeline. SAVEPOINT-wrapped idempotent
│                          replay; StaleDataError -> 409
└── jobs/relay.py          15 s outbox relay: FOR UPDATE SKIP LOCKED,
                           priority-first, exponential backoff (0s->2h),
                           dead-letter => CRITICAL SystemAlert + operator
                           page, dead-man heartbeat

migrations/
├── env.py                 Async Alembic env (ALEMBIC_DATABASE_URL override)
└── versions/0001_initial.py
                           Schema + partial unique index + forbid_mutation()
                           triggers on both evidence tables + explicit
                           dual-role grant matrix (no DELETE/TRUNCATE/DDL for
                           runtime roles) + downgrade guard on evidence

scripts/bootstrap_roles.sql
                           Cluster-level, run once as superuser:
                           migration_admin / app_user / app_jobs (BYPASSRLS)

tests/
├── test_dates.py          Clamping table, leap days, DST spring-forward
│                          proof, naive-datetime rejection  [ASSERTIONS RUN]
└── test_integration.py    Real Postgres 16 via testcontainers: E6 race,
                           savepoint replay, version guard, atomicity,
                           SKIP LOCKED partitioning (2 relays x 5 msgs),
                           backoff->dead-letter, DST timestamptz round-trip,
                           trigger + role privilege boundaries  [UNRUN]

docs/presentation.html     Self-contained stakeholder presentation
CLAUDE_CODE_PROMPT.md      Hand this to Claude Code to build the rest
```

## The four core guarantees

| # | Guarantee | Mechanism | Where |
|---|---|---|---|
| G1 | Evidence can never be rewritten | Per-driver SHA-256 hash chain + `BEFORE UPDATE OR DELETE` trigger + no UPDATE/DELETE grants for runtime roles (two independent locks) | models, migration |
| G2 | Lock and alert are atomic | Single transaction, zero network I/O; transactional outbox carries the notification intent | services/e6.py |
| G3 | Critical alerts survive crashes | At-least-once relay: SKIP LOCKED claim -> send -> commit; backoff; dead-letter escalates as its own CRITICAL alert; heartbeat monitors relay *silence* | jobs/relay.py |
| G4 | Legal dates are Berlin-calendar-correct | Instant/Obligation type split; clamping month math; deadline = end of 7th Berlin civil day, DST-proof | dates.py |

## Status — COMPLETE (T0–T9 built and verified)

All tasks from `CLAUDE_CODE_PROMPT.md` are implemented and green. See
`BUILD_COMPLETE.md` for the full run-through.

**Verified:**
- `pytest tests/` → **105 passed** (unit + real-Postgres-16 integration via
  testcontainers), incl. the full flow: signup → vehicle/driver →
  FAILED FSK (lock + case + email/SMS staged) → PASSED (unlock + resolve) →
  hash-chained history, and cross-tenant RLS isolation.
- `ruff check app/` clean · `mypy --strict app/` clean (36 files).
- `docker compose up` brings up db + web + worker; signup→dashboard works,
  the relay drains every 15 s, and all three containers report healthy.

**What exists now (beyond the original scaffold):** settings/db layer,
FastAPI + worker entrypoints, session-cookie auth + argon2id + throttling +
invitations, migrations 0002–0004 (RLS/FORCE + sessions, vehicles/deadlines,
DSGVO), Brevo/seven.io notifiers + SMS cap, escalation & reminder sweeps +
deadline roll-forward (incl. 28-day Nachprüfung), HTMX dashboard + CRUD +
check-recording + history timeline, DSGVO purge/export/anonymize/deletion +
legal pages, Dockerfile/compose/deploy/backup + restore drill + healthchecks +
GitHub Actions CI.

### Quickstart (Docker)
```bash
cp .env.example .env          # set real secrets
docker compose up -d          # db + migrate + web + worker
# → http://localhost:8000  (signup, add vehicle/driver, record checks)
```

## Quickstart (test environment)

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements-test.txt
# Docker must be running:
pytest tests/ -v          # spins up postgres:16-alpine, runs the REAL
                          # migration, executes the full suite
```

Production bring-up (once the app layer exists): run
`scripts/bootstrap_roles.sql` as superuser -> `alembic upgrade head` as
migration_admin -> app connects as app_user, worker as app_jobs.

## Compliance notes (read before selling)

- AVV, § 21 StVG alert wording, ToS liability clause, and retention defaults
  need review by a German lawyer (Verkehrsrecht + Datenschutz). This
  repository encodes the rules as engineering constraints, not legal advice.
- EU-only subprocessors by design: Hetzner (DE), Brevo (EU), seven.io (DE).
- `downgrade()` on the initial migration refuses to drop evidence tables
  without `ALLOW_EVIDENCE_DOWNGRADE=yes` — leave that unset everywhere real.
