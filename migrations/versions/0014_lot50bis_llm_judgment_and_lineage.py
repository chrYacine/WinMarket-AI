"""Lot 50 bis — real LLM judgment traceability, private-document content classification, documentary lineage.

Purely ADDITIVE, nothing dropped/renamed on any existing column or row:

1. `ao_dossier_pieces`: three new columns tracking WHICH path actually produced the classification/moderation/
   security reasoning — `classification_source`, `moderation_source`, `security_review_source`, each one of
   `'heuristic'` (default — every pre-lot-50-bis row was, by construction, heuristic-only: lot 50 never called
   an LLM), `'llm'`, `'heuristic_llm_unavailable'` or `'heuristic_llm_invalid'`. Never claims an LLM
   participated when it did not (§1: "préciser si heuristique, LLM ou confirmation utilisateur a réellement
   participé").
2. `analyses`: `origin_job_id` (nullable, NOT unique — deliberately different from `parent_job_id`'s UNIQUE
   index) — the job a NEW documentary re-analysis (§3 "Ajouter les pièces restantes") extends. NULL for every
   analysis before this lot and for every ordinary/completion-revision analysis. `ao_dossiers` gets the same
   `origin_job_id` (nullable) — set on a STAGING dossier created by that same flow, propagated to the new
   Analysis row only once confirmed.
3. `knowledge_document_versions`: business-content classification for the private knowledge base (§2) —
   `content_category_proposed`/`content_category_final` (`'reference'`/`'certification'`/`'presentation'`/
   `'autre'`/`'indetermine'`), `classification_source` (adds `'user'` — a human correction — and `'unknown'` to
   the four values above), `classification_reason`, `classified_by_user_id`, `classified_at`. Every version
   that existed BEFORE this migration is explicitly backfilled to `classification_source='unknown'` (a
   `server_default`, so any future raw insert that omits the column also gets it) — lot 50 bis never
   retroactively claims a classification (heuristic OR LLM) was attempted for a document classified before this
   feature existed (ticket, verbatim: "jamais «validés par IA» rétroactivement"). New rows created through the
   application default to `'heuristic'` (the Python-side ORM default in `src/web/database/models.py`) since
   the classifier always at least attempts its heuristic reading.

SQLite cannot ALTER a CHECK constraint in place: all three tables are rebuilt via `batch_alter_table`
(Alembic's standard SQLite-safe recreate-table strategy) — a plain `ALTER TABLE ADD COLUMN`/constraint change
on PostgreSQL.

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-26
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

_SOURCE_VALUES = "'heuristic','llm','heuristic_llm_unavailable','heuristic_llm_invalid'"


def upgrade() -> None:
    with op.batch_alter_table("ao_dossier_pieces", schema=None) as batch_op:
        batch_op.add_column(sa.Column("classification_source", sa.String(length=30), nullable=False, server_default="heuristic"))
        batch_op.add_column(sa.Column("moderation_source", sa.String(length=30), nullable=False, server_default="heuristic"))
        batch_op.add_column(sa.Column("security_review_source", sa.String(length=30), nullable=False, server_default="heuristic"))
        batch_op.create_check_constraint("ck_ao_dossier_pieces_classification_source", f"classification_source IN ({_SOURCE_VALUES})")
        batch_op.create_check_constraint("ck_ao_dossier_pieces_moderation_source", f"moderation_source IN ({_SOURCE_VALUES})")
        batch_op.create_check_constraint("ck_ao_dossier_pieces_security_review_source", f"security_review_source IN ({_SOURCE_VALUES})")

    with op.batch_alter_table("analyses", schema=None) as batch_op:
        batch_op.add_column(sa.Column("origin_job_id", sa.String(length=50), nullable=True))
        batch_op.create_index("ix_analyses_origin_job_id", ["origin_job_id"])

    with op.batch_alter_table("ao_dossiers", schema=None) as batch_op:
        batch_op.add_column(sa.Column("origin_job_id", sa.String(length=50), nullable=True))
        batch_op.create_index("ix_ao_dossiers_origin_job_id", ["origin_job_id"])

    with op.batch_alter_table("knowledge_document_versions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("content_category_proposed", sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column("content_category_final", sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column("classification_source", sa.String(length=30), nullable=False, server_default="unknown"))
        batch_op.add_column(sa.Column("classification_reason", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("classified_by_user_id", sa.Uuid(as_uuid=True), nullable=True))
        batch_op.add_column(sa.Column("classified_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.create_check_constraint(
            "ck_knowledge_document_versions_category_proposed",
            "content_category_proposed IS NULL OR content_category_proposed IN ('reference','certification','presentation','autre','indetermine')",
        )
        batch_op.create_check_constraint(
            "ck_knowledge_document_versions_category_final",
            "content_category_final IS NULL OR content_category_final IN ('reference','certification','presentation','autre','indetermine')",
        )
        batch_op.create_check_constraint(
            "ck_knowledge_document_versions_classification_source",
            f"classification_source IN ({_SOURCE_VALUES},'user','unknown')",
        )
        batch_op.create_foreign_key(
            "fk_knowledge_document_versions_classified_by_user_id", "users", ["classified_by_user_id"], ["id"], ondelete="SET NULL",
        )

    # One-time repair, not a business back-fill: every version that existed before this migration never had
    # ANY classification attempted (the feature did not exist) — the server_default above already gives it
    # 'unknown' for the ADD COLUMN itself, this UPDATE is belt-and-suspenders for backends (or an already-open
    # transaction) where the DEFAULT might not have been applied retroactively to rows written before the
    # column existed.
    bind = op.get_bind()
    bind.execute(sa.text("UPDATE knowledge_document_versions SET classification_source = 'unknown' WHERE classification_source IS NULL"))


def downgrade() -> None:
    # Refused while any row carries lot-50-bis LLM/classification/lineage data that would be lost or
    # unrepresentable in the pre-0014 schema — same policy as every migration since 0010: restore a verified
    # backup taken before 0014 instead.
    bind = op.get_bind()
    llm_pieces = bind.execute(sa.text(
        "SELECT COUNT(*) FROM ao_dossier_pieces WHERE classification_source <> 'heuristic' "
        "OR moderation_source <> 'heuristic' OR security_review_source <> 'heuristic'"
    )).scalar()
    lineage = bind.execute(sa.text("SELECT COUNT(*) FROM analyses WHERE origin_job_id IS NOT NULL")).scalar()
    staging_lineage = bind.execute(sa.text("SELECT COUNT(*) FROM ao_dossiers WHERE origin_job_id IS NOT NULL")).scalar()
    classified_docs = bind.execute(sa.text(
        "SELECT COUNT(*) FROM knowledge_document_versions WHERE content_category_proposed IS NOT NULL "
        "OR content_category_final IS NOT NULL OR classification_source NOT IN ('unknown')"
    )).scalar()
    if llm_pieces or lineage or staging_lineage or classified_docs:
        raise RuntimeError(
            f"Downgrade of 0014 refused: {llm_pieces} dossier piece(s) with real LLM judgment data, "
            f"{lineage} documentary-lineage analysis(es), {staging_lineage} documentary-lineage dossier(s) and "
            f"{classified_docs} classified knowledge document version(s) exist and would be lost or unrepresentable. "
            "Restore a verified backup taken before 0014 instead."
        )

    with op.batch_alter_table("knowledge_document_versions", schema=None) as batch_op:
        batch_op.drop_constraint("fk_knowledge_document_versions_classified_by_user_id", type_="foreignkey")
        batch_op.drop_constraint("ck_knowledge_document_versions_classification_source", type_="check")
        batch_op.drop_constraint("ck_knowledge_document_versions_category_final", type_="check")
        batch_op.drop_constraint("ck_knowledge_document_versions_category_proposed", type_="check")
        batch_op.drop_column("classified_at")
        batch_op.drop_column("classified_by_user_id")
        batch_op.drop_column("classification_reason")
        batch_op.drop_column("classification_source")
        batch_op.drop_column("content_category_final")
        batch_op.drop_column("content_category_proposed")

    with op.batch_alter_table("ao_dossiers", schema=None) as batch_op:
        batch_op.drop_index("ix_ao_dossiers_origin_job_id")
        batch_op.drop_column("origin_job_id")

    with op.batch_alter_table("analyses", schema=None) as batch_op:
        batch_op.drop_index("ix_analyses_origin_job_id")
        batch_op.drop_column("origin_job_id")

    with op.batch_alter_table("ao_dossier_pieces", schema=None) as batch_op:
        batch_op.drop_constraint("ck_ao_dossier_pieces_security_review_source", type_="check")
        batch_op.drop_constraint("ck_ao_dossier_pieces_moderation_source", type_="check")
        batch_op.drop_constraint("ck_ao_dossier_pieces_classification_source", type_="check")
        batch_op.drop_column("security_review_source")
        batch_op.drop_column("moderation_source")
        batch_op.drop_column("classification_source")
