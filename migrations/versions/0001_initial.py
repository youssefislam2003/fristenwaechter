"""0001 — initial schema, evidence hardening, dual-role grant matrix.

Revision ID: 0001_initial
Revises:     None
Create Date: 2026-07-07

Three layers established here, in order:
  1. Schema  — tables + the partial unique index guaranteeing at most one
               OPEN EscalationCase per driver.
  2. Triggers — forbid_mutation() bound BEFORE UPDATE OR DELETE on the two
               evidence tables. Binds to EVERY role including app_jobs
               (BYPASSRLS bypasses row security, never triggers).
  3. Grants  — explicit per-table DML for app_user / app_jobs. Evidence
               tables get SELECT+INSERT only, so even if a trigger were
               dropped by a rogue migration, the runtime roles STILL cannot
               mutate history. Two independent locks on the same door.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


# ── grant matrix: the single source of truth for runtime privileges ──────
# (table, privileges). No DELETE and no TRUNCATE appear anywhere: rows are
# ended by status columns and retention jobs, never destroyed by the app.
RUNTIME_GRANTS: list[tuple[str, str]] = [
    ("company",              "SELECT, INSERT, UPDATE"),
    ("account_user",         "SELECT, INSERT, UPDATE"),
    ("driver",               "SELECT, INSERT, UPDATE"),
    ("escalation_case",      "SELECT, INSERT, UPDATE"),
    ("outbox_message",       "SELECT, INSERT, UPDATE"),  # relay mutates delivery FSM
    ("system_alert",         "SELECT, INSERT"),           # append-only ledger
    ("compliance_check_log", "SELECT, INSERT"),           # append-only evidence
]
RUNTIME_ROLES = ("app_user", "app_jobs")


def upgrade() -> None:
    # ───────────────────────── 1. schema ─────────────────────────
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")  # gen_random_uuid on PG<13 images

    op.create_table(
        "company",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("plan", sa.String(20), nullable=False,
                  server_default="trial"),
    )

    op.create_table(
        "account_user",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("company_id", pg.UUID(as_uuid=True),
                  sa.ForeignKey("company.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("email", sa.String(254), nullable=False, unique=True),
        sa.Column("role", sa.String(10), nullable=False,
                  server_default="MANAGER"),
        sa.Column("phone_e164", sa.String(20)),
        sa.Column("is_active", sa.Boolean, nullable=False,
                  server_default=sa.true()),
    )

    op.create_table(
        "driver",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("company_id", pg.UUID(as_uuid=True),
                  sa.ForeignKey("company.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("first_name", sa.String(80), nullable=False),
        sa.Column("last_name", sa.String(80), nullable=False),
        sa.Column("is_authorized_to_drive", sa.Boolean, nullable=False,
                  server_default=sa.true()),
        sa.Column("lock_reason", sa.String(50)),
        sa.Column("locked_at", sa.DateTime(timezone=True)),
        sa.Column("version_id", sa.Integer, nullable=False,
                  server_default="1"),
    )
    op.create_index("ix_driver_company_locked", "driver",
                    ["company_id", "is_authorized_to_drive"])

    op.create_table(
        "escalation_case",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("company_id", pg.UUID(as_uuid=True),
                  sa.ForeignKey("company.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("driver_id", pg.UUID(as_uuid=True),
                  sa.ForeignKey("driver.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("status", sa.String(20), nullable=False,
                  server_default="OPEN"),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("resolved_by_id", pg.UUID(as_uuid=True),
                  sa.ForeignKey("account_user.id", ondelete="SET NULL")),
        sa.Column("last_nag_on", sa.Date),
    )
    # THE invariant: at most one OPEN case per driver — database-guaranteed.
    op.create_index(
        "uq_escalation_open_per_driver",
        "escalation_case",
        ["driver_id"],
        unique=True,
        postgresql_where=sa.text("status = 'OPEN'"),
    )

    op.create_table(
        "system_alert",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("company_id", pg.UUID(as_uuid=True),
                  sa.ForeignKey("company.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("severity", sa.String(10), nullable=False),
        sa.Column("code", sa.String(40), nullable=False),
        sa.Column("driver_id", pg.UUID(as_uuid=True),
                  sa.ForeignKey("driver.id", ondelete="CASCADE")),
        sa.Column("escalation_case_id", pg.UUID(as_uuid=True),
                  sa.ForeignKey("escalation_case.id", ondelete="SET NULL")),
        sa.Column("message", sa.Text, nullable=False),
    )

    op.create_table(
        "outbox_message",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("company_id", pg.UUID(as_uuid=True),
                  sa.ForeignKey("company.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("channel", sa.String(6), nullable=False),
        sa.Column("recipient", sa.String(254), nullable=False),
        sa.Column("template", sa.String(40), nullable=False),
        sa.Column("payload", pg.JSONB, nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("priority", sa.SmallInteger, nullable=False,
                  server_default="100"),
        sa.Column("dedup_key", sa.String(120), nullable=False, unique=True),
        sa.Column("attempts", sa.SmallInteger, nullable=False,
                  server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("dead_lettered_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text),
        sa.Column("provider_message_id", sa.String(100)),
        sa.CheckConstraint("attempts >= 0", name="ck_outbox_attempts_nonneg"),
    )
    op.create_index(
        "ix_outbox_pending", "outbox_message",
        ["next_attempt_at", "priority"],
        postgresql_where=sa.text(
            "sent_at IS NULL AND dead_lettered_at IS NULL"),
    )

    op.create_table(
        "compliance_check_log",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("company_id", pg.UUID(as_uuid=True),
                  sa.ForeignKey("company.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("driver_id", pg.UUID(as_uuid=True),
                  sa.ForeignKey("driver.id", ondelete="CASCADE")),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("result", sa.String(20), nullable=False),
        sa.Column("method", sa.String(20)),
        sa.Column("performed_by_id", pg.UUID(as_uuid=True),
                  sa.ForeignKey("account_user.id", ondelete="SET NULL")),
        sa.Column("performed_on", sa.Date, nullable=False),
        sa.Column("params", pg.JSONB, nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("prev_hash", sa.String(64)),
        sa.Column("entry_hash", sa.String(64), nullable=False, unique=True),
    )
    op.create_index("ix_check_chain_driver", "compliance_check_log",
                    ["driver_id", "created_at"])

    # ──────────────────── 2. append-only triggers ────────────────────
    op.execute("""
        CREATE OR REPLACE FUNCTION forbid_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION
                '% is append-only compliance evidence (op=% blocked)',
                TG_TABLE_NAME, TG_OP
                USING ERRCODE = 'raise_exception';
        END;
        $$;
    """)
    for table in ("compliance_check_log", "system_alert"):
        op.execute(f"""
            CREATE TRIGGER {table}_immutable
                BEFORE UPDATE OR DELETE ON {table}
                FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
        """)

    # ───────────────────── 3. dual-role grant matrix ─────────────────────
    # Explicit, table-by-table, no wildcards: `GRANT ALL ON ALL TABLES` is
    # exactly the pattern this migration exists to forbid. Wrapped in a
    # role-existence guard so the migration also runs on dev machines where
    # bootstrap_roles.sql hasn't been applied (grants simply skip).
    grants = "\n".join(
        f"GRANT {privs} ON TABLE {table} TO {role};"
        for role in RUNTIME_ROLES
        for table, privs in RUNTIME_GRANTS
    )
    op.execute(f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles
                       WHERE rolname IN ('app_user', 'app_jobs')
                       HAVING count(*) = 2) THEN
                {grants}
            ELSE
                RAISE NOTICE 'runtime roles absent - skipping grants '
                             '(run scripts/bootstrap_roles.sql)';
            END IF;
        END $$;
    """)
    # Note what is deliberately ABSENT for runtime roles:
    #   * no CREATE/ALTER/DROP (schema CREATE was revoked in bootstrap)
    #   * no DELETE, no TRUNCATE on any table
    #   * no UPDATE on compliance_check_log / system_alert
    #   * no TRIGGER privilege → runtime roles cannot drop forbid_mutation


def downgrade() -> None:
    # Downgrading destroys evidence tables. Permitted for dev/test databases
    # only — refuse anywhere that looks like production.
    import os
    if os.environ.get("ALLOW_EVIDENCE_DOWNGRADE") != "yes":
        raise RuntimeError(
            "Refusing to drop append-only evidence tables. "
            "Set ALLOW_EVIDENCE_DOWNGRADE=yes only on disposable databases."
        )
    for table in ("compliance_check_log", "system_alert"):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_immutable ON {table}")
    op.execute("DROP FUNCTION IF EXISTS forbid_mutation()")
    for table in ("compliance_check_log", "outbox_message", "system_alert",
                  "escalation_case", "driver", "account_user", "company"):
        op.drop_table(table)
