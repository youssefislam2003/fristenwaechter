"""0003 — vehicles, deadlines, driver employment/retention columns.

Revision ID: 0003_vehicles_deadlines
Revises:     0002_rls_sessions
Create Date: 2026-07-11

Adds the obligation-tracking core: vehicle + deadline tables (both tenant
data → company_id + ENABLE/FORCE RLS + grant matrix), and the driver columns
that anchor the first FSK (employment_start) and the DSGVO retention clock
(employment_end, anonymized_at). No append-only evidence table is touched.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "0003_vehicles_deadlines"
down_revision = "0002_rls_sessions"
branch_labels = None
depends_on = None

NEW_RUNTIME_GRANTS = (
    ("vehicle", "SELECT, INSERT, UPDATE"),
    ("deadline", "SELECT, INSERT, UPDATE"),
)
RUNTIME_ROLES = ("app_user", "app_jobs")


def upgrade() -> None:
    # ── driver: employment window + anonymization stamp ──────────────────
    op.add_column("driver", sa.Column("employment_start", sa.Date))
    op.add_column("driver", sa.Column("employment_end", sa.Date))
    op.add_column("driver",
                  sa.Column("anonymized_at", sa.DateTime(timezone=True)))

    # ── vehicle ──────────────────────────────────────────────────────────
    op.create_table(
        "vehicle",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("company_id", pg.UUID(as_uuid=True),
                  sa.ForeignKey("company.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("kennzeichen", sa.String(15), nullable=False),
        sa.Column("make", sa.String(40)),
        sa.Column("model", sa.String(40)),
        sa.Column("first_registration", sa.Date),
        sa.Column("hu_interval_months", sa.SmallInteger, nullable=False,
                  server_default="24"),
        sa.Column("is_active", sa.Boolean, nullable=False,
                  server_default=sa.true()),
    )
    op.create_index("ix_vehicle_company", "vehicle",
                    ["company_id", "is_active"])

    # ── deadline ─────────────────────────────────────────────────────────
    op.create_table(
        "deadline",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("company_id", pg.UUID(as_uuid=True),
                  sa.ForeignKey("company.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("driver_id", pg.UUID(as_uuid=True),
                  sa.ForeignKey("driver.id", ondelete="CASCADE")),
        sa.Column("vehicle_id", pg.UUID(as_uuid=True),
                  sa.ForeignKey("vehicle.id", ondelete="CASCADE")),
        sa.Column("due_on", sa.Date, nullable=False),
        sa.Column("status", sa.String(12), nullable=False,
                  server_default="OPEN"),
        sa.Column("completed_on", sa.Date),
        sa.Column("is_nachpruefung", sa.Boolean, nullable=False,
                  server_default=sa.false()),
        sa.CheckConstraint(
            "(driver_id IS NOT NULL)::int + (vehicle_id IS NOT NULL)::int = 1",
            name="ck_deadline_one_subject"),
    )
    op.create_index("ix_deadline_sweep", "deadline", ["status", "due_on"])
    op.create_index("ix_deadline_driver", "deadline", ["driver_id"])
    op.create_index("ix_deadline_vehicle", "deadline", ["vehicle_id"])

    # ── grant matrix for the two new tenant tables ───────────────────────
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

    # ── ENABLE + FORCE RLS, scoped to app.current_company ────────────────
    for table in ("vehicle", "deadline"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""
            CREATE POLICY {table}_tenant_isolation ON {table}
                USING (company_id = current_setting('app.current_company', true)::uuid)
                WITH CHECK (company_id = current_setting('app.current_company', true)::uuid)
        """)


def downgrade() -> None:
    for table in ("deadline", "vehicle"):
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.drop_table("deadline")
    op.drop_table("vehicle")
    op.drop_column("driver", "anonymized_at")
    op.drop_column("driver", "employment_end")
    op.drop_column("driver", "employment_start")
