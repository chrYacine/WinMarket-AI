"""Lot 47 bis — the tender dossier (several pieces, one analysis): `ao_dossiers` and `ao_dossier_pieces`.

Purely ADDITIVE: two new tables, nothing dropped, renamed or altered on any existing table, no data
back-filled. Historical analyses are untouched and stay readable exactly as they were (a result stored
before this migration has no dossier: `AOContext.dossier` is simply absent from its `result_data`).

A dossier piece is INPUT to one analysis. It is deliberately NOT stored in `knowledge_*` (the account's own
professional references — the RAG corpus) nor in `analysis_documents` (the generated PDF/DOCX): those
tables have a different role and different lifecycle rules. See src/web/database/models.py::AoDossier.

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-21
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ao_dossiers",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("organization_id", sa.Uuid(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("job_id", sa.String(length=50), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("total_bytes", sa.Integer(), nullable=False),
        sa.Column("piece_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('validated','submitted')", name="ck_ao_dossiers_status"),
        sa.UniqueConstraint("id", "organization_id", "user_id", name="uq_ao_dossiers_scope"),
    )
    op.create_index("ix_ao_dossiers_organization_id", "ao_dossiers", ["organization_id"])
    op.create_index("ix_ao_dossiers_user_id", "ao_dossiers", ["user_id"])
    op.create_index("ix_ao_dossiers_job_id", "ao_dossiers", ["job_id"])

    op.create_table(
        "ao_dossier_pieces",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("dossier_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("organization_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("category", sa.String(length=20), nullable=False),
        sa.Column("display_name", sa.String(length=160), nullable=False),
        sa.Column("file_format", sa.String(length=10), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("storage_key", sa.String(length=1000), nullable=False),
        sa.Column("text_storage_key", sa.String(length=1000), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("char_count", sa.Integer(), nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("duplicate_of_piece_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("category IN ('rc','cctp','ccap','acte_engagement','annexe')", name="ck_ao_dossier_pieces_category"),
        sa.ForeignKeyConstraint(
            ["dossier_id", "organization_id", "user_id"],
            ["ao_dossiers.id", "ao_dossiers.organization_id", "ao_dossiers.user_id"],
            ondelete="CASCADE", name="fk_ao_dossier_pieces_scope",
        ),
    )
    op.create_index("ix_ao_dossier_pieces_dossier_id", "ao_dossier_pieces", ["dossier_id"])
    op.create_index("ix_ao_dossier_pieces_organization_id", "ao_dossier_pieces", ["organization_id"])
    op.create_index("ix_ao_dossier_pieces_user_id", "ao_dossier_pieces", ["user_id"])
    op.create_index("ix_ao_dossier_pieces_content_hash", "ao_dossier_pieces", ["content_hash"])


def downgrade() -> None:
    # Refused while any dossier exists: dropping the tables would lose the tender pieces of past
    # analyses (their bytes stay on disk, but nothing would reference them). Restore a verified
    # backup taken before 0011 instead — same policy as 0010.
    bind = op.get_bind()
    existing = bind.execute(sa.text("SELECT COUNT(*) FROM ao_dossiers")).scalar()
    if existing:
        raise RuntimeError(
            f"Downgrade of 0011 refused: {existing} tender dossier(s) exist and would be lost. "
            "Restore a verified backup taken before 0011 instead."
        )
    for index in ("ix_ao_dossier_pieces_content_hash", "ix_ao_dossier_pieces_user_id",
                  "ix_ao_dossier_pieces_organization_id", "ix_ao_dossier_pieces_dossier_id"):
        op.drop_index(index, table_name="ao_dossier_pieces")
    op.drop_table("ao_dossier_pieces")
    for index in ("ix_ao_dossiers_job_id", "ix_ao_dossiers_user_id", "ix_ao_dossiers_organization_id"):
        op.drop_index(index, table_name="ao_dossiers")
    op.drop_table("ao_dossiers")
