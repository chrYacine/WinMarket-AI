"""B06-T4 / B07-T1 / B08-T1 — private business-rule columns.

Purely additive: three new nullable-free columns with safe, inert
defaults on EXISTING tables — nothing removed, nothing renamed. Safe to
run against a live database with existing B06-T1 rows:

- scoring_policies.business_rules defaults to '{}' (empty dict) on every
  existing row — an existing active policy is NOT back-filled with the
  old hardcoded engine constants (50000 EUR / 95% / 4 techs / 20 points).
  ScoringEngine.score() treats an absent key as "not configured", never
  as the old constant, for any policy row — old or new.
- provider_profiles.external_enrichment_enabled defaults to false on
  every existing row — no account is silently opted into external
  company lookups by this migration.
- private_capacity_plans.disponibilite_minimum_pct defaults to 10 on
  every existing row — the exact threshold src/agents/capacity_analyzer.py
  already hardcoded before this ticket, made an explicit, editable
  per-account parameter instead (existing accounts keep behaving exactly
  as before until they choose to change it).

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-16
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "scoring_policies",
        sa.Column("business_rules", JSONB().with_variant(sa.JSON(), "sqlite"), nullable=False, server_default="{}"),
    )
    op.add_column(
        "provider_profiles",
        sa.Column("external_enrichment_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "private_capacity_plans",
        sa.Column("disponibilite_minimum_pct", sa.Integer(), nullable=False, server_default="10"),
    )


def downgrade() -> None:
    op.drop_column("private_capacity_plans", "disponibilite_minimum_pct")
    op.drop_column("provider_profiles", "external_enrichment_enabled")
    op.drop_column("scoring_policies", "business_rules")
