-- scripts/bootstrap_roles.sql
-- Run ONCE per cluster, as the postgres superuser, BEFORE the first
-- `alembic upgrade head`. Roles are cluster-level objects: they do not
-- belong inside migrations (migrations must be re-runnable per database;
-- roles are shared across databases and owned by ops, not by the app).
--
--   psql -U postgres -d fristen -v mig_pw="'…'" -v app_pw="'…'" -v jobs_pw="'…'" \
--        -f scripts/bootstrap_roles.sql

-- ── 1. migration_admin: owns the schema, runs Alembic, nothing else ──────
CREATE ROLE migration_admin
    LOGIN PASSWORD :mig_pw
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;

-- ── 2. app_user: least-privilege FastAPI runtime ─────────────────────────
--    Gets ONLY per-table DML grants from migration 0001. No DDL, no DELETE,
--    no TRUNCATE, anywhere, ever. RLS applies (NOBYPASSRLS).
CREATE ROLE app_user
    LOGIN PASSWORD :app_pw
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;

-- ── 3. app_jobs: the outbox relay / sweeps (legitimately cross-tenant) ───
--    Same DML posture as app_user; BYPASSRLS because the relay drains the
--    outbox across all tenants. The append-only trigger still binds it.
CREATE ROLE app_jobs
    LOGIN PASSWORD :jobs_pw
    NOSUPERUSER NOCREATEDB NOCREATEROLE BYPASSRLS;

-- migration_admin must be a member of app_jobs so migration 0002 can reassign
-- ownership of the SECURITY DEFINER login-lookup function to app_jobs (a
-- function can only be owned by a role the executing user is a member of).
-- migration_admin is DDL-only and already the schema owner, so inheriting
-- app_jobs' BYPASSRLS changes nothing about the runtime security posture.
GRANT app_jobs TO migration_admin;

-- ── 4. Database & schema ownership ───────────────────────────────────────
ALTER DATABASE fristen OWNER TO migration_admin;

\connect fristen

-- public schema: migrations own it; runtime roles may only LOOK UP objects.
ALTER SCHEMA public OWNER TO migration_admin;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;   -- nobody but the owner makes DDL
GRANT USAGE ON SCHEMA public TO app_user, app_jobs;

-- Belt-and-suspenders: even if a future migration forgets explicit grants,
-- newly created tables get NOTHING by default for the runtime roles —
-- every privilege must be granted deliberately in a migration.
ALTER DEFAULT PRIVILEGES FOR ROLE migration_admin IN SCHEMA public
    REVOKE ALL ON TABLES FROM PUBLIC;
