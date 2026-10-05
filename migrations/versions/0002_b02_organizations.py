"""B02 step 1/2 — organizations, memberships (additive), and a nullable
organization_id on analyses/analysis_documents.

This revision is safe to run against a live database with existing data:
it adds new tables and nullable columns only, changes no existing row, and
drops nothing. Existing analyses/documents get organization_id = NULL here;
scripts/backfill_b02_organizations.py assigns each existing user a private
Organization and backfills their analyses/documents before revision 0003
tightens the constraints (NOT NULL, composite FK). Do not run 0003 before
that backfill has been run and verified — it checks for remaining NULLs and
raises rather than silently truncating data, but the intended order is
0002 -> backfill script -> 0003.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-13
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "organizations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("corpus_access", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('active','suspended')", name="ck_organizations_status"),
    )
    op.create_index("ix_organizations_status", "organizations", ["status"])

    op.create_table(
        "memberships",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("organization_id", sa.Uuid(), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(30), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("role IN ('viewer','analyst','organization_admin')", name="ck_memberships_role"),
        sa.CheckConstraint("status IN ('active','revoked')", name="ck_memberships_status"),
        sa.UniqueConstraint("user_id", "organization_id", name="uq_memberships_user_org"),
    )
    op.create_index("ix_memberships_user_id", "memberships", ["user_id"])
    op.create_index("ix_memberships_organization_id", "memberships", ["organization_id"])
    op.create_index("ix_memberships_status", "memberships", ["status"])

    # Nullable for now — see module docstring. RESTRICT (not CASCADE): an
    # organization with analyses attached should never disappear silently.
    # batch_alter_table: SQLite cannot ALTER a table to add a constraint
    # in-place (only a plain column) — batch mode recreates the table under
    # the hood there, while on Postgres it is a transparent passthrough to
    # a normal ALTER TABLE. Using it here (rather than plain op.add_column /
    # op.create_foreign_key) is what lets this migration run, and be
    # tested, against both dialects.
    with op.batch_alter_table("analyses") as batch_op:
        batch_op.add_column(sa.Column("organization_id", sa.Uuid(), nullable=True))
        batch_op.create_foreign_key(
            "fk_analyses_organization_id", "organizations", ["organization_id"], ["id"], ondelete="RESTRICT"
        )
        batch_op.create_index("ix_analyses_organization_id", ["organization_id"])

    # No FK yet on analysis_documents.organization_id: until backfilled it
    # can't be guaranteed consistent with the parent analysis, which is what
    # revision 0003's composite FK enforces once that's true.
    with op.batch_alter_table("analysis_documents") as batch_op:
        batch_op.add_column(sa.Column("organization_id", sa.Uuid(), nullable=True))
        batch_op.create_index("ix_analysis_documents_organization_id", ["organization_id"])


def downgrade() -> None:
    with op.batch_alter_table("analysis_documents") as batch_op:
        batch_op.drop_index("ix_analysis_documents_organization_id")
        batch_op.drop_column("organization_id")

    with op.batch_alter_table("analyses") as batch_op:
        batch_op.drop_index("ix_analyses_organization_id")
        batch_op.drop_constraint("fk_analyses_organization_id", type_="foreignkey")
        batch_op.drop_column("organization_id")

    op.drop_table("memberships")
    op.drop_table("organizations")
