"""B12-T1 — durable job queue/state table (analysis_jobs).

Purely additive: one new table, nothing dropped/renamed/altered on any
existing table. Tested (see tests/test_b12_t1_job_executor.py) against
both a fresh empty SQLite db and one seeded with rows from migrations
0001-0006 applied first.

analysis_jobs mirrors src/web/jobs.py's in-process Job/_JOBS state machine
durably, so a job that is merely queued/running (and therefore has no
`analyses` row yet — see that table's own NOT NULL result_data) is not
lost across a process restart. See src/web/database/models.py::AnalysisJob
for the full column-by-column rationale (heartbeat-based abandonment
detection in particular).

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-16
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "analysis_jobs",
        sa.Column("id", sa.String(length=50), primary_key=True),
        sa.Column("user_id", sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "organization_id", sa.Uuid(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="queued"),
        sa.Column("source_label", sa.String(length=255), nullable=True),
        sa.Column("worker_instance_id", sa.String(length=64), nullable=True),
        sa.Column("error_code", sa.String(length=50), nullable=True),
        sa.Column("analysis_id", sa.Uuid(as_uuid=True), sa.ForeignKey("analyses.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued','running','done','error','interrupted')",
            name="ck_analysis_jobs_status",
        ),
    )
    op.create_index("ix_analysis_jobs_user_id", "analysis_jobs", ["user_id"])
    op.create_index("ix_analysis_jobs_organization_id", "analysis_jobs", ["organization_id"])
    op.create_index("ix_analysis_jobs_status", "analysis_jobs", ["status"])
    op.create_index("ix_analysis_jobs_created_at", "analysis_jobs", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_analysis_jobs_created_at", table_name="analysis_jobs")
    op.drop_index("ix_analysis_jobs_status", table_name="analysis_jobs")
    op.drop_index("ix_analysis_jobs_organization_id", table_name="analysis_jobs")
    op.drop_index("ix_analysis_jobs_user_id", table_name="analysis_jobs")
    op.drop_table("analysis_jobs")
