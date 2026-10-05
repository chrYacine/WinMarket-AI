"""Lot 51 — hybrid RAG scaffolding: passages table + embedding readiness columns, pgvector on PostgreSQL only.

Purely ADDITIVE and dialect-aware, nothing dropped/renamed on any existing column or row:

1. `knowledge_chunks` gets a `UniqueConstraint(id, organization_id, owner_user_id)` (same composite-scope
   pattern already used by every other table in this chain) so the new `knowledge_passages` table can
   reference it with the same "cannot cross accounts by construction" guarantee.
2. `knowledge_document_versions` gets 6 nullable/defaulted columns tracking the readiness of this version's
   VECTOR passages, entirely separate from `extraction_status` (lexical/TF-IDF, unchanged):
   `embedding_status` ('not_applicable' default — every pre-lot-51 row and every SQLite/hybrid-disabled row
   stays exactly there, never silently 'ready'), `embedding_model_id/_revision/_dimension`,
   `embedding_error_code`, `embedding_indexed_at`.
3. `knowledge_passages` (new table, created on BOTH dialects — portable, SQLite just never populates it since
   hybrid mode is structurally impossible there): one embedding-sized window of a chunk's content, with exact
   `start_char`/`end_char` into that chunk's own text and a redundantly-stored `content` slice
   (`content == chunk.content[start_char:end_char]` enforced at the application layer, see
   src/rag/chunking.py). No vector column here — added conditionally below.
4. PostgreSQL ONLY: `CREATE EXTENSION IF NOT EXISTS vector` then `ALTER TABLE knowledge_passages ADD COLUMN
   embedding vector(384)` (384 = the lot 51 default embedding dimension, config.EMBEDDING_DIMENSION at the
   time this migration was written — hardcoded here on purpose: migrations are a frozen historical record,
   never re-read live config; a FUTURE change of embedding dimension needs a NEW additive migration, exactly
   like a model/config change already requires an explicit reindex per `embedding_model_id/_revision`). Never
   attempted on SQLite — `bind.dialect.name` is checked first, so a SQLite target never even sees the
   `vector(...)` type name (which it could not parse). No ANN index is created (ticket: "pas d'index ANN
   obligatoire pour un petit corpus") — plain sequential scan with the `<=>` cosine-distance operator is
   sufficient at this corpus size; a future lot can add an ivfflat/hnsw index without touching this migration.

Downgrade mirrors 0014's policy: refused while any row carries lot-51 embedding data that would be lost.

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-24
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

# Frozen at authoring time — see module docstring point 4. Matches
# config.EMBEDDING_DIMENSION's default in src/core/config.py (lot 51).
_EMBEDDING_DIMENSION = 384


def upgrade() -> None:
    with op.batch_alter_table("knowledge_chunks", schema=None) as batch_op:
        batch_op.create_unique_constraint("uq_knowledge_chunks_scope", ["id", "organization_id", "owner_user_id"])

    with op.batch_alter_table("knowledge_document_versions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("embedding_status", sa.String(length=20), nullable=False, server_default="not_applicable"))
        batch_op.add_column(sa.Column("embedding_model_id", sa.String(length=200), nullable=True))
        batch_op.add_column(sa.Column("embedding_model_revision", sa.String(length=200), nullable=True))
        batch_op.add_column(sa.Column("embedding_dimension", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("embedding_error_code", sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column("embedding_indexed_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.create_check_constraint(
            "ck_knowledge_document_versions_embedding_status",
            "embedding_status IN ('not_applicable','pending','ready','failed')",
        )

    op.create_table(
        "knowledge_passages",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("chunk_id", sa.Uuid(as_uuid=True), nullable=False, index=True),
        sa.Column("document_version_id", sa.Uuid(as_uuid=True), nullable=False, index=True),
        sa.Column("organization_id", sa.Uuid(as_uuid=True), nullable=False, index=True),
        sa.Column("owner_user_id", sa.Uuid(as_uuid=True), nullable=False, index=True),
        sa.Column("start_char", sa.Integer(), nullable=False),
        sa.Column("end_char", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_fingerprint", sa.String(length=64), nullable=False, index=True),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column("section", sa.String(length=255), nullable=True),
        sa.Column("embedding_model_id", sa.String(length=200), nullable=False),
        sa.Column("embedding_model_revision", sa.String(length=200), nullable=False),
        sa.Column("embedding_dimension", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("end_char > start_char", name="ck_knowledge_passages_span"),
        sa.ForeignKeyConstraint(
            ["chunk_id", "organization_id", "owner_user_id"],
            ["knowledge_chunks.id", "knowledge_chunks.organization_id", "knowledge_chunks.owner_user_id"],
            ondelete="CASCADE", name="fk_knowledge_passages_chunk_scope",
        ),
    )

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
        op.execute(f"ALTER TABLE knowledge_passages ADD COLUMN embedding vector({_EMBEDDING_DIMENSION})")


def downgrade() -> None:
    bind = op.get_bind()
    embedded = bind.execute(sa.text(
        "SELECT COUNT(*) FROM knowledge_document_versions WHERE embedding_status <> 'not_applicable'"
    )).scalar()
    passages = bind.execute(sa.text("SELECT COUNT(*) FROM knowledge_passages")).scalar()
    if embedded or passages:
        raise RuntimeError(
            f"Downgrade of 0015 refused: {embedded} knowledge_document_version(s) with a non-default "
            f"embedding_status and {passages} knowledge_passages row(s) exist and would be lost or "
            "unrepresentable. Restore a verified backup taken before 0015 instead."
        )

    if bind.dialect.name == "postgresql":
        op.execute("ALTER TABLE knowledge_passages DROP COLUMN IF EXISTS embedding")

    op.drop_table("knowledge_passages")

    with op.batch_alter_table("knowledge_document_versions", schema=None) as batch_op:
        batch_op.drop_constraint("ck_knowledge_document_versions_embedding_status", type_="check")
        batch_op.drop_column("embedding_indexed_at")
        batch_op.drop_column("embedding_error_code")
        batch_op.drop_column("embedding_dimension")
        batch_op.drop_column("embedding_model_revision")
        batch_op.drop_column("embedding_model_id")
        batch_op.drop_column("embedding_status")

    with op.batch_alter_table("knowledge_chunks", schema=None) as batch_op:
        batch_op.drop_constraint("uq_knowledge_chunks_scope", type_="unique")
