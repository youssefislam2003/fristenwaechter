"""0002 — row-level security, auth infrastructure, login bootstrap.

Revision ID: 0002_rls_sessions
Revises:     0001_initial
Create Date: 2026-07-11

Four things land here, in order:
  1. Auth columns on account_user (password_hash, last_login_at).
  2. Auth-infrastructure tables: user_session, invitation, login_throttle.
     These are DELIBERATELY not under RLS — they are read during the
     pre-tenant authentication phase, before app.current_company exists.
     Their isolation comes from unguessable token hashes / the email+ip key.
  3. The login bootstrap: auth_lookup_credentials(text), a SECURITY DEFINER
     function owned by the BYPASSRLS app_jobs role and EXECUTE-granted only to
     app_user. It is the single, auditable pre-tenant read of account_user
     (which itself IS under RLS). Requires migration_admin ∈ app_jobs — see
     scripts/bootstrap_roles.sql (GRANT app_jobs TO migration_admin).
  4. ENABLE + FORCE row-level security on every tenant table, scoped to
     current_setting('app.current_company'). Using the two-arg form
     (missing_ok=true) means a MISSING GUC yields NULL → zero rows, never an
     error that could leak existence. Plus the grant matrix for the new tables.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "0002_rls_sessions"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


# Tenant tables that carry a company scope and must be RLS-confined. The relay
# and sweeps reach these cross-tenant, but they connect as app_jobs (BYPASSRLS),
# for which policies are simply not evaluated.
#   company is scoped on its own PK (id); everything else on company_id.
RLS_TABLES_BY_COMPANY_ID = (
    "account_user",
    "driver",
    "escalation_case",
    "system_alert",
    "compliance_check_log",
    "outbox_message",
)

# New auth-infra tables and their runtime privileges (SELECT/INSERT/UPDATE —
# never DELETE/TRUNCATE, per the grant matrix invariant).
NEW_RUNTIME_GRANTS = (
    ("user_session", "SELECT, INSERT, UPDATE"),
    ("invitation", "SELECT, INSERT, UPDATE"),
    ("login_throttle", "SELECT, INSERT, UPDATE"),
)
RUNTIME_ROLES = ("app_user", "app_jobs")


def upgrade() -> None:
    # ── 1. auth columns on account_user ──────────────────────────────────
    op.add_column("account_user", sa.Column("password_hash", sa.String(200)))
    op.add_column(
        "account_user",
        sa.Column("last_login_at", sa.DateTime(timezone=True)),
    )

    # ── 2. auth-infrastructure tables (no RLS: pre-tenant) ────────────────
    op.create_table(
        "user_session",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("account_user_id", pg.UUID(as_uuid=True),
                  sa.ForeignKey("account_user.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("company_id", pg.UUID(as_uuid=True),
                  sa.ForeignKey("company.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
    )

    op.create_table(
        "invitation",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("company_id", pg.UUID(as_uuid=True),
                  sa.ForeignKey("company.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("role", sa.String(10), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("invited_by_id", pg.UUID(as_uuid=True),
                  sa.ForeignKey("account_user.id", ondelete="SET NULL")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True)),
    )

    op.create_table(
        "login_throttle",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("ip", sa.String(45), nullable=False),
        sa.Column("fail_count", sa.SmallInteger, nullable=False,
                  server_default="0"),
        sa.Column("locked_until", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index("uq_throttle_email_ip", "login_throttle",
                    ["email", "ip"], unique=True)

    # ── 3. login bootstrap: SECURITY DEFINER credential lookup ────────────
    # Owned by app_jobs (BYPASSRLS) so it can read account_user across tenants;
    # SET search_path pins resolution so the definer context can't be hijacked.
    op.execute("""
        CREATE FUNCTION auth_lookup_credentials(p_email text)
        RETURNS TABLE(
            user_id uuid, company_id uuid,
            password_hash text, role text, is_active boolean
        )
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = public
        AS $$
            SELECT id, company_id, password_hash, role, is_active
            FROM account_user
            WHERE email = lower(p_email)
        $$;
    """)
    # Reassign ownership to the BYPASSRLS role and lock down EXECUTE. Guarded so
    # the migration still runs on dev DBs without the bootstrap roles.
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_jobs') THEN
                -- Reassigning ownership to app_jobs requires that role to hold
                -- CREATE on the function's schema (Postgres checks the NEW
                -- owner's privilege). app_jobs is intentionally CREATE-less at
                -- runtime, so grant it transiently and revoke immediately —
                -- ownership survives the revoke, least privilege is restored.
                GRANT CREATE ON SCHEMA public TO app_jobs;
                ALTER FUNCTION auth_lookup_credentials(text) OWNER TO app_jobs;
                REVOKE CREATE ON SCHEMA public FROM app_jobs;
            END IF;
            REVOKE ALL ON FUNCTION auth_lookup_credentials(text) FROM PUBLIC;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
                GRANT EXECUTE ON FUNCTION auth_lookup_credentials(text) TO app_user;
            END IF;
        END $$;
    """)

    # ── 4a. grant matrix for the new auth tables ──────────────────────────
    grants = "\n".join(
        f"GRANT {privs} ON TABLE {table} TO {role};"
        for role in RUNTIME_ROLES
        for table, privs in NEW_RUNTIME_GRANTS
    )
    op.execute(f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles
                       WHERE rolname IN ('app_user', 'app_jobs')
                       HAVING count(*) = 2) THEN
                {grants}
            END IF;
        END $$;
    """)

    # ── 4b. enable + FORCE RLS on every tenant table ──────────────────────
    # company is scoped on its own primary key; the rest on company_id.
    _enable_rls("company", "id")
    for table in RLS_TABLES_BY_COMPANY_ID:
        _enable_rls(table, "company_id")


def _enable_rls(table: str, column: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    # FORCE so the table OWNER is subject too (defense in depth). Superusers
    # still bypass — which is why the test harness (superuser) sees all rows.
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY {table}_tenant_isolation ON {table}
            USING ({column} = current_setting('app.current_company', true)::uuid)
            WITH CHECK ({column} = current_setting('app.current_company', true)::uuid)
    """)


def downgrade() -> None:
    for table in ("company", *RLS_TABLES_BY_COMPANY_ID):
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    op.execute("DROP FUNCTION IF EXISTS auth_lookup_credentials(text)")

    op.drop_index("uq_throttle_email_ip", table_name="login_throttle")
    op.drop_table("login_throttle")
    op.drop_table("invitation")
    op.drop_table("user_session")
    op.drop_column("account_user", "last_login_at")
    op.drop_column("account_user", "password_hash")
