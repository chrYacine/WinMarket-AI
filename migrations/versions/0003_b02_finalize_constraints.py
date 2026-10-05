"""B02 step 2/2 — tighten organization_id to NOT NULL and add the composite
consistency constraint once the backfill has run.

DO NOT run this before `scripts/backfill_b02_organizations.py` has been run
(without --dry-run) and its output shows 0 remaining orphan rows on the
target database. This revision defensively checks for NULL organization_id
rows itself and raises with a clear message instead of letting a bare
NOT NULL constraint violation (Postgres) fail confusingly mid-DDL — but it
does not run the backfill for you, and it does not know how to attribute an
orphaned row on its own.

Downgrade note: downgrade() only relaxes the constraints back to their 0002
shape. It does NOT restore organization_id to NULL on rows that had it set
— there is no lossy data to "undo" here (the column keeps its values), but
if you are downgrading specifically to re-run a different backfill
strategy, you must clear organization_id yourself first.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-13
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()

    orphan_analyses = connection.execute(
        sa.text("SELECT COUNT(*) FROM analyses WHERE organization_id IS NULL")
    ).scalar_one()
    orphan_documents = connection.execute(
        sa.text("SELECT COUNT(*) FROM analysis_documents WHERE organization_id IS NULL")
    ).scalar_one()
    if orphan_analyses or orphan_documents:
        raise RuntimeError(
            f"Cannot finalize B02 constraints: {orphan_analyses} analyses and "
            f"{orphan_documents} analysis_documents still have organization_id "
            f"IS NULL. Run scripts/backfill_b02_organizations.py (without "
            f"--dry-run) first and confirm it reports zero remaining orphans."
        )

    # batch_alter_table for portability (SQLite recreates the table under
    # the hood; Postgres gets a plain ALTER) — see revision 0002 for why.
    with op.batch_alter_table("analyses") as batch_op:
        batch_op.alter_column("organization_id", nullable=False)
        batch_op.create_unique_constraint("uq_analyses_id_organization_id", ["id", "organization_id"])

    # Composite FK: a document's organization_id must equal its own
    # analysis's organization_id, enforced by the database itself — not
    # just by repository code (see src/web/database/models.py). This is
    # added *alongside* the original single-column analysis_id -> analyses.id
    # FK from revision 0001, not as a replacement: that FK was created
    # anonymously (no explicit name), so its real name is dialect-specific
    # and not safe to guess and drop here. The composite FK below is a
    # strictly stronger constraint anyway (it implies the original one), so
    # leaving both in place is redundant but harmless — safer than a
    # brittle drop-by-guessed-name across Postgres/SQLite.
    with op.batch_alter_table("analysis_documents") as batch_op:
        batch_op.alter_column("organization_id", nullable=False)
        batch_op.create_foreign_key(
            "fk_analysis_documents_analysis_org",
            "analyses",
            ["analysis_id", "organization_id"],
            ["id", "organization_id"],
            ondelete="CASCADE",
        )


def downgrade() -> None:
    with op.batch_alter_table("analysis_documents") as batch_op:
        batch_op.drop_constraint("fk_analysis_documents_analysis_org", type_="foreignkey")
        batch_op.alter_column("organization_id", nullable=True)

    with op.batch_alter_table("analyses") as batch_op:
        batch_op.drop_constraint("uq_analyses_id_organization_id", type_="unique")
        batch_op.alter_column("organization_id", nullable=True)
