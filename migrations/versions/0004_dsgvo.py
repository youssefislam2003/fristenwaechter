"""0004 — DSGVO retention columns + erasure grants.

Revision ID: 0004_dsgvo
Revises:     0003_vehicles_deadlines
Create Date: 2026-07-11

Adds company.driver_retention_months, deletion_requested_at, deleted_at, and —
narrowly — grants DELETE to app_jobs on ONLY outbox_message, so the purge can
prune delivered notification logs older than 24 months.

Tenant "deletion" is implemented as full ANONYMIZATION (tombstone), not a
physical row delete: cascade-deleting a company would hit the append-only
trigger on compliance_check_log / system_alert and fail — and, more to the
point, we never want a runtime path that can erase evidence (invariant I1).
The check logs are PII-free by construction, so retaining the hash chain after
scrubbing all names/contact fully satisfies Art. 17 (no personal data remains)
while keeping the evidence guarantee absolutely inviolable. Physical
destruction, if ever contractually required, is a documented superuser step.

The EVIDENCE tables (compliance_check_log, system_alert) never receive DELETE —
not here, not anywhere.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0004_dsgvo"
down_revision = "0003_vehicles_deadlines"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("company", sa.Column(
        "driver_retention_months", sa.SmallInteger, nullable=False,
        server_default="12"))
    op.add_column("company", sa.Column(
        "deletion_requested_at", sa.DateTime(timezone=True)))
    op.add_column("company", sa.Column(
        "deleted_at", sa.DateTime(timezone=True)))

    # Narrow erasure grant: app_jobs may prune the notification log only.
    # Guarded so dev DBs without the role still migrate.
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_jobs') THEN
                GRANT DELETE ON TABLE outbox_message TO app_jobs;
            END IF;
        END $$;
    """)


def downgrade() -> None:
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_jobs') THEN
                REVOKE DELETE ON TABLE outbox_message FROM app_jobs;
            END IF;
        END $$;
    """)
    op.drop_column("company", "deleted_at")
    op.drop_column("company", "deletion_requested_at")
    op.drop_column("company", "driver_retention_months")
