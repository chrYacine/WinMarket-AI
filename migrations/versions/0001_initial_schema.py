"""Initial SaaS V3 schema — users, subscriptions, analyses, analysis_documents,
contact_requests, password_reset_tokens.

Revision ID: 0001
Revises:
Create Date: 2026-09-09
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("first_name", sa.String(100), nullable=False),
        sa.Column("last_name", sa.String(100), nullable=False),
        sa.Column("company", sa.String(255), nullable=True),
        sa.Column("job_title", sa.String(255), nullable=True),
        sa.Column("phone", sa.String(50), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('pending','active','rejected','disabled')", name="ck_users_status"),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )
    op.create_index("ix_users_email", "users", ["email"])
    op.create_index("ix_users_status", "users", ["status"])

    op.create_table(
        "subscriptions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("plan", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("plan IN ('starter','business','enterprise')", name="ck_subscriptions_plan"),
        sa.CheckConstraint("status IN ('pending','active','cancelled','expired')", name="ck_subscriptions_status"),
    )
    op.create_index("ix_subscriptions_user_id", "subscriptions", ["user_id"])
    op.create_index("ix_subscriptions_status", "subscriptions", ["status"])

    op.create_table(
        "analyses",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("job_id", sa.String(50), nullable=True),
        sa.Column("title", sa.String(500), nullable=True),
        sa.Column("client_name", sa.String(255), nullable=True),
        sa.Column("sector", sa.String(100), nullable=True),
        sa.Column("score", sa.Numeric(5, 2), nullable=True),
        sa.Column("decision", sa.String(30), nullable=True),
        sa.Column("budget", sa.String(50), nullable=True),
        sa.Column("technologies", JSONB().with_variant(sa.JSON(), "sqlite"), nullable=True),
        sa.Column("result_data", JSONB().with_variant(sa.JSON(), "sqlite"), nullable=False),
        sa.Column("summary_data", JSONB().with_variant(sa.JSON(), "sqlite"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("job_id", name="uq_analyses_job_id"),
    )
    op.create_index("ix_analyses_user_id", "analyses", ["user_id"])
    op.create_index("ix_analyses_job_id", "analyses", ["job_id"])
    op.create_index("ix_analyses_created_at", "analyses", ["created_at"])
    op.create_index("ix_analyses_user_id_created_at", "analyses", ["user_id", "created_at"])

    op.create_table(
        "analysis_documents",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("analysis_id", sa.Uuid(), sa.ForeignKey("analyses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("filename", sa.String(255), nullable=True),
        sa.Column("original_filename", sa.String(255), nullable=True),
        sa.Column("storage_path", sa.String(1000), nullable=True),
        sa.Column("mime_type", sa.String(100), nullable=True),
        sa.Column("file_size", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_analysis_documents_analysis_id", "analysis_documents", ["analysis_id"])
    op.create_index("ix_analysis_documents_user_id", "analysis_documents", ["user_id"])

    op.create_table(
        "contact_requests",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("first_name", sa.String(100), nullable=True),
        sa.Column("last_name", sa.String(100), nullable=True),
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column("company", sa.String(255), nullable=True),
        sa.Column("job_title", sa.String(255), nullable=True),
        sa.Column("employee_count", sa.Integer(), nullable=True),
        sa.Column("plan", sa.String(20), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="new"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('new','contacted','closed')", name="ck_contact_requests_status"),
    )

    op.create_table(
        "password_reset_tokens",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(255), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_password_reset_tokens_user_id", "password_reset_tokens", ["user_id"])


def downgrade() -> None:
    op.drop_table("password_reset_tokens")
    op.drop_table("contact_requests")
    op.drop_table("analysis_documents")
    op.drop_table("analyses")
    op.drop_table("subscriptions")
    op.drop_table("users")
