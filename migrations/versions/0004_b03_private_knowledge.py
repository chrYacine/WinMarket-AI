"""B03 — private knowledge (documentation/RAG) and private capacity.

Purely additive: new tables only, nothing existing is altered. Safe to run
against a live database with existing B01/B02 data — creates no rows,
touches no existing table. Does NOT migrate data out of the global demo
corpus (data/reg_docs) or capacity file — those remain a separate,
non-SaaS demo dataset (see docs/architecture/B03_PRIVATE_KNOWLEDGE.md);
every new corpus/capacity row starts empty/unconfigured for its owner.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-13
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_corpora",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), sa.ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('active')", name="ck_knowledge_corpora_status"),
        sa.UniqueConstraint("organization_id", "owner_user_id", name="uq_knowledge_corpora_org_owner"),
        sa.UniqueConstraint("id", "organization_id", "owner_user_id", name="uq_knowledge_corpora_scope"),
    )
    op.create_index("ix_knowledge_corpora_organization_id", "knowledge_corpora", ["organization_id"])
    op.create_index("ix_knowledge_corpora_owner_user_id", "knowledge_corpora", ["owner_user_id"])

    op.create_table(
        "knowledge_documents",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("corpus_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("original_filename", sa.String(255), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("active_version_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('active','deleted')", name="ck_knowledge_documents_status"),
        sa.ForeignKeyConstraint(
            ["corpus_id", "organization_id", "owner_user_id"],
            ["knowledge_corpora.id", "knowledge_corpora.organization_id", "knowledge_corpora.owner_user_id"],
            ondelete="CASCADE", name="fk_knowledge_documents_corpus_scope",
        ),
        sa.UniqueConstraint("id", "organization_id", "owner_user_id", name="uq_knowledge_documents_scope"),
    )
    op.create_index("ix_knowledge_documents_corpus_id", "knowledge_documents", ["corpus_id"])
    op.create_index("ix_knowledge_documents_organization_id", "knowledge_documents", ["organization_id"])
    op.create_index("ix_knowledge_documents_owner_user_id", "knowledge_documents", ["owner_user_id"])
    op.create_index("ix_knowledge_documents_status", "knowledge_documents", ["status"])

    op.create_table(
        "knowledge_document_versions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("storage_key", sa.String(1000), nullable=False),
        sa.Column("content_type_detected", sa.String(100), nullable=True),
        sa.Column("file_size", sa.BigInteger(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("extraction_status", sa.String(20), nullable=False, server_default="received"),
        sa.Column("error_code", sa.String(50), nullable=True),
        sa.Column("extractor_version", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "extraction_status IN ('received','processing','ready','failed')",
            name="ck_knowledge_document_versions_status",
        ),
        sa.ForeignKeyConstraint(
            ["document_id", "organization_id", "owner_user_id"],
            ["knowledge_documents.id", "knowledge_documents.organization_id", "knowledge_documents.owner_user_id"],
            ondelete="CASCADE", name="fk_knowledge_document_versions_document_scope",
        ),
        sa.UniqueConstraint("document_id", "version_number", name="uq_knowledge_document_versions_number"),
        sa.UniqueConstraint("id", "organization_id", "owner_user_id", name="uq_knowledge_document_versions_scope"),
    )
    op.create_index("ix_knowledge_document_versions_document_id", "knowledge_document_versions", ["document_id"])
    op.create_index("ix_knowledge_document_versions_organization_id", "knowledge_document_versions", ["organization_id"])
    op.create_index("ix_knowledge_document_versions_owner_user_id", "knowledge_document_versions", ["owner_user_id"])
    op.create_index("ix_knowledge_document_versions_content_hash", "knowledge_document_versions", ["content_hash"])
    op.create_index("ix_knowledge_document_versions_extraction_status", "knowledge_document_versions", ["extraction_status"])

    # Circular FK (documents.active_version_id -> versions.id, versions.document_id -> documents.id):
    # added after both tables exist, batch mode for SQLite/Postgres portability.
    with op.batch_alter_table("knowledge_documents") as batch_op:
        batch_op.create_foreign_key(
            "fk_knowledge_documents_active_version", "knowledge_document_versions",
            ["active_version_id"], ["id"], ondelete="SET NULL",
        )

    op.create_table(
        "knowledge_chunks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("document_version_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("order_index", sa.Integer(), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column("section", sa.String(255), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_version_id", "organization_id", "owner_user_id"],
            ["knowledge_document_versions.id", "knowledge_document_versions.organization_id", "knowledge_document_versions.owner_user_id"],
            ondelete="CASCADE", name="fk_knowledge_chunks_version_scope",
        ),
    )
    op.create_index("ix_knowledge_chunks_document_version_id", "knowledge_chunks", ["document_version_id"])
    op.create_index("ix_knowledge_chunks_organization_id", "knowledge_chunks", ["organization_id"])
    op.create_index("ix_knowledge_chunks_owner_user_id", "knowledge_chunks", ["owner_user_id"])

    op.create_table(
        "private_capacity_plans",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), sa.ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="unconfigured"),
        sa.Column("charge_globale_pct", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("nombre_projets_en_cours", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("projets_en_cours", JSONB().with_variant(sa.JSON(), "sqlite"), nullable=False),
        sa.Column("capacites_par_pole", JSONB().with_variant(sa.JSON(), "sqlite"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('configured','unconfigured')", name="ck_private_capacity_plans_status"),
        sa.UniqueConstraint("organization_id", "owner_user_id", name="uq_private_capacity_plans_org_owner"),
    )
    op.create_index("ix_private_capacity_plans_organization_id", "private_capacity_plans", ["organization_id"])
    op.create_index("ix_private_capacity_plans_owner_user_id", "private_capacity_plans", ["owner_user_id"])


def downgrade() -> None:
    op.drop_table("private_capacity_plans")
    op.drop_table("knowledge_chunks")
    with op.batch_alter_table("knowledge_documents") as batch_op:
        batch_op.drop_constraint("fk_knowledge_documents_active_version", type_="foreignkey")
    op.drop_table("knowledge_document_versions")
    op.drop_table("knowledge_documents")
    op.drop_table("knowledge_corpora")
