"""B06-T1 — private ScoringPolicy (versioned) and ProviderProfile.

Purely additive: two new tables only, nothing existing is altered. Safe to
run against a live database with existing B01/B02/B03 data — creates no
rows for any existing account. No ScoringPolicy row means "not configured
yet"; /api/analyze refuses with 409 SCORING_NOT_CONFIGURED until the owner
activates one (see routes_api.py) — no active policy is ever created by
this migration, no global registry (ScoringEngine.mastered/certs_ok,
config.SCORING_THRESHOLD_*) is copied into any row.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-14
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "provider_profiles",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), sa.ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="incomplete"),
        sa.Column("raison_sociale", sa.String(255), nullable=True),
        sa.Column("effectif", sa.String(100), nullable=True),
        sa.Column("competences", JSONB().with_variant(sa.JSON(), "sqlite"), nullable=False),
        sa.Column("certifications", JSONB().with_variant(sa.JSON(), "sqlite"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('incomplete','complete')", name="ck_provider_profiles_status"),
        sa.UniqueConstraint("organization_id", "owner_user_id", name="uq_provider_profiles_org_owner"),
    )
    op.create_index("ix_provider_profiles_organization_id", "provider_profiles", ["organization_id"])
    op.create_index("ix_provider_profiles_owner_user_id", "provider_profiles", ["owner_user_id"])

    op.create_table(
        "scoring_policies",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), sa.ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
        sa.Column("weights", JSONB().with_variant(sa.JSON(), "sqlite"), nullable=False),
        sa.Column("threshold_go", sa.Float(), nullable=True),
        sa.Column("threshold_sous_reserve", sa.Float(), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('draft','active','archived')", name="ck_scoring_policies_status"),
        sa.UniqueConstraint("organization_id", "owner_user_id", "version", name="uq_scoring_policies_org_owner_version"),
    )
    op.create_index("ix_scoring_policies_organization_id", "scoring_policies", ["organization_id"])
    op.create_index("ix_scoring_policies_owner_user_id", "scoring_policies", ["owner_user_id"])
    # Partial unique index: at most one 'active' row per (organization, owner).
    # Supported by both SQLite (3.8+) and PostgreSQL; enforced by the DB
    # itself so a genuine race between two activation requests fails one of
    # them with an IntegrityError rather than leaving two policies active.
    op.create_index(
        "uq_scoring_policies_one_active", "scoring_policies", ["organization_id", "owner_user_id"],
        unique=True, sqlite_where=sa.text("status = 'active'"), postgresql_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    op.drop_index("uq_scoring_policies_one_active", table_name="scoring_policies")
    op.drop_table("scoring_policies")
    op.drop_table("provider_profiles")
