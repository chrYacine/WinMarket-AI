"""B06-T5 — private business-fact columns (sector-neutral scoring).

Purely additive, same idiom as migration 0006: two new nullable-free
columns with safe, inert defaults on EXISTING tables — nothing removed,
nothing renamed, nothing back-filled with invented data. Safe to run
against a live database with existing B06-T1/B06-T4 rows:

- provider_profiles.business_facts defaults to '{}' (empty dict) on every
  existing row — no account is silently given a declared business fact it
  never entered. src/agents/business_facts.py owns the fixed catalogue of
  fact types/operators this data is validated against.
- scoring_policies.custom_criteria defaults to '[]' (empty list) on every
  existing row — an existing active policy keeps scoring with EXACTLY the
  same fixed 12 IT-specific criteria it always used; nothing is added to
  it by this migration. ScoringEngine.score() treats an empty list
  identically to "this account configured no custom criterion" for a
  brand-new account too.

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-18
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "provider_profiles",
        sa.Column("business_facts", JSONB().with_variant(sa.JSON(), "sqlite"), nullable=False, server_default="{}"),
    )
    op.add_column(
        "scoring_policies",
        sa.Column("custom_criteria", JSONB().with_variant(sa.JSON(), "sqlite"), nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    op.drop_column("scoring_policies", "custom_criteria")
    op.drop_column("provider_profiles", "business_facts")
