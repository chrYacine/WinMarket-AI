"""Lot 49 — completing an INCOMPLET analysis without inventing data or losing history.

Purely ADDITIVE, three changes, nothing dropped/renamed/altered on any existing column, no data
back-filled beyond a deliberate one-time repair (below):

1. `analyses.parent_job_id` (nullable, UNIQUE): the job_id of the analysis a revision COMPLETES. NULL for
   every analysis before this lot and for every non-revision analysis. UNIQUE (SQL allows any number of
   NULLs) enforces "at most one revision per parent" at the database level, not just in application code.
2. `ao_dossier_job_links`: fixes the lot 47 bis reserve where `AoDossier.job_id` (a single column,
   overwritten by every `resume`/completion) made an earlier job lose its own dossier link. Every job that
   is ever associated with a dossier — the original submission, every `resume`, every completion revision —
   gets its own row here; `AoDossier.job_id` itself is untouched (it keeps naming the MOST RECENT job, for
   display/status). Backfilled from the existing `ao_dossiers.job_id` column so dossiers already submitted
   before this migration are not silently orphaned from their one recorded job.
3. `analysis_complements`: durable record of what a user DECLARED (subject, field, value, unit, author,
   date) to complete a revision — distinct from any extracted/observed value, never backfilled (nothing to
   backfill: this concept did not exist before).

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-23
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("analyses", sa.Column("parent_job_id", sa.String(length=50), nullable=True))
    op.create_index("ix_analyses_parent_job_id", "analyses", ["parent_job_id"], unique=True)

    op.create_table(
        "ao_dossier_job_links",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("dossier_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("organization_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("job_id", sa.String(length=50), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("job_id", name="uq_ao_dossier_job_links_job_id"),
        sa.ForeignKeyConstraint(
            ["dossier_id", "organization_id", "user_id"],
            ["ao_dossiers.id", "ao_dossiers.organization_id", "ao_dossiers.user_id"],
            ondelete="CASCADE", name="fk_ao_dossier_job_links_scope",
        ),
    )
    op.create_index("ix_ao_dossier_job_links_dossier_id", "ao_dossier_job_links", ["dossier_id"])
    op.create_index("ix_ao_dossier_job_links_organization_id", "ao_dossier_job_links", ["organization_id"])
    op.create_index("ix_ao_dossier_job_links_user_id", "ao_dossier_job_links", ["user_id"])
    op.create_index("ix_ao_dossier_job_links_job_id", "ao_dossier_job_links", ["job_id"])

    # One-time repair, not a business backfill: every dossier already carrying a job_id gets the ONE link
    # row it would already have if this table had existed from the start — no association is invented,
    # `ao_dossiers.job_id` is the only source, and it already named a real job.
    bind = op.get_bind()
    import uuid
    from datetime import datetime, timezone
    rows = bind.execute(sa.text("SELECT id, organization_id, user_id, job_id FROM ao_dossiers WHERE job_id IS NOT NULL")).fetchall()
    for dossier_id, organization_id, user_id, job_id in rows:
        bind.execute(
            sa.text(
                "INSERT INTO ao_dossier_job_links (id, dossier_id, organization_id, user_id, job_id, created_at) "
                "VALUES (:id, :dossier_id, :organization_id, :user_id, :job_id, :created_at)"
            ),
            {"id": uuid.uuid4(), "dossier_id": dossier_id, "organization_id": organization_id,
             "user_id": user_id, "job_id": job_id, "created_at": datetime.now(timezone.utc)},
        )

    op.create_table(
        "analysis_complements",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("job_id", sa.String(length=50), nullable=False),
        sa.Column("organization_id", sa.Uuid(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("need_id", sa.String(length=120), nullable=False),
        sa.Column("subject", sa.String(length=20), nullable=False),
        sa.Column("field_key", sa.String(length=80), nullable=False),
        sa.Column("field_label", sa.String(length=200), nullable=False),
        sa.Column("value_json", sa.JSON(), nullable=False),
        sa.Column("unit", sa.String(length=50), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("subject IN ('ao','acheteur','prestataire')", name="ck_analysis_complements_subject"),
    )
    op.create_index("ix_analysis_complements_job_id", "analysis_complements", ["job_id"])
    op.create_index("ix_analysis_complements_organization_id", "analysis_complements", ["organization_id"])
    op.create_index("ix_analysis_complements_user_id", "analysis_complements", ["user_id"])


def downgrade() -> None:
    # Refused while any complement or revision exists — same policy as 0010/0011: restore a verified
    # backup taken before 0012 instead of losing declared complements or revision lineage.
    bind = op.get_bind()
    complements = bind.execute(sa.text("SELECT COUNT(*) FROM analysis_complements")).scalar()
    revisions = bind.execute(sa.text("SELECT COUNT(*) FROM analyses WHERE parent_job_id IS NOT NULL")).scalar()
    if complements or revisions:
        raise RuntimeError(
            f"Downgrade of 0012 refused: {complements} declared complement(s) and {revisions} revision(s) "
            "exist and would be lost. Restore a verified backup taken before 0012 instead."
        )
    op.drop_index("ix_analysis_complements_user_id", table_name="analysis_complements")
    op.drop_index("ix_analysis_complements_organization_id", table_name="analysis_complements")
    op.drop_index("ix_analysis_complements_job_id", table_name="analysis_complements")
    op.drop_table("analysis_complements")

    for index in ("ix_ao_dossier_job_links_job_id", "ix_ao_dossier_job_links_user_id",
                  "ix_ao_dossier_job_links_organization_id", "ix_ao_dossier_job_links_dossier_id"):
        op.drop_index(index, table_name="ao_dossier_job_links")
    op.drop_table("ao_dossier_job_links")

    op.drop_index("ix_analyses_parent_job_id", table_name="analyses")
    op.drop_column("analyses", "parent_job_id")
