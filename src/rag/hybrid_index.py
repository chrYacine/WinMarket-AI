"""Lot 51 — computes and persists VECTOR passages for one
KnowledgeDocumentVersion. Called once, synchronously, right after that
version's lexical chunks are extracted (src/web/knowledge/documents_service.
py::_ingest_version) — reuses the existing single-transaction ingestion
flow rather than introducing a second job/queue system (ticket: "réutilise
les jobs/exécuteurs existants... pas de framework d'agents supplémentaire").

Structurally a no-op everywhere hybrid mode isn't BOTH possible (PostgreSQL
+ pgvector) AND explicitly enabled (config.RAG_HYBRID_MODE_ENABLED) — a
SQLite deployment, or a PostgreSQL one that hasn't opted in, never imports
fastembed/pgvector from here and leaves embedding_status='not_applicable'.

Idempotent: `reindex_version` always deletes this version's existing
passages first, then writes a complete, brand-new set — `embedding_status`
only ever flips to 'ready' after EVERY passage of this version has a valid
vector (never a partial index visible to search mid-computation, since this
all happens inside the same DB transaction the caller commits).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from src.core import config
from src.core.reference_identity import compute_content_fingerprint
from src.rag.chunking import window_passages_by_tokens
from src.rag.embeddings import EmbeddingAdapter, EmbeddingUnavailableError, current_embedding_config
from src.web.database.models import KnowledgeChunk, KnowledgeDocumentVersion
from src.web.database.repositories import knowledge as knowledge_repo


# Lot 51 bis — matches migration 0015's hardcoded `vector(384)` column
# EXACTLY (see that migration's own docstring: the dimension is frozen at
# authoring time, a real change needs a NEW additive migration, never a
# silent free-form column). config.EMBEDDING_DIMENSION is presented as an
# operator-configurable value, but nothing previously stopped it from
# drifting away from what the column can actually store — a mismatch
# surfaced as an uncaught PostgreSQL error from deep inside a raw UPDATE,
# swallowed by documents_service's outer best-effort try/except, leaving
# embedding_status silently NOT set to 'failed'. Checked explicitly, before
# any write, so the refusal is clear and embedding_status reflects it.
PGVECTOR_COLUMN_DIMENSION = 384


def dialect_supports_vectors(db: Session) -> bool:
    return db.get_bind().dialect.name == "postgresql"


def hybrid_mode_active(db: Session) -> bool:
    return config.RAG_HYBRID_MODE_ENABLED and dialect_supports_vectors(db)


def version_is_stale(version: KnowledgeDocumentVersion) -> bool:
    """A 'ready' version built with a model/revision/dimension that no
    longer matches the CURRENT config is stale — never silently searched
    as if current (src/rag/hybrid_search.py additionally filters on these
    same three fields at query time, belt-and-suspenders); this is what a
    maintenance/reindex route checks to decide whether retrying is even
    useful."""
    if version.embedding_status != "ready":
        return version.embedding_status in ("pending", "failed")
    current = current_embedding_config()
    return (
        version.embedding_model_id != current.model_id
        or version.embedding_model_revision != current.model_revision
        or version.embedding_dimension != current.dimension
    )


def _set_passage_embedding(db: Session, passage_id: uuid.UUID, vector: list[float], dimension: int) -> None:
    from pgvector.sqlalchemy import Vector

    stmt = text("UPDATE knowledge_passages SET embedding = :vec WHERE id = :id").bindparams(
        bindparam("vec", type_=Vector(dimension))
    )
    db.execute(stmt, {"vec": vector, "id": passage_id})


def index_version(db: Session, *, version: KnowledgeDocumentVersion, chunks: list[KnowledgeChunk]) -> None:
    """The single entry point — called for a NEW version right after its
    lexical chunks are persisted, and by the maintenance reindex route for
    an existing one. Never raises: a provider/dimension failure is recorded
    on the version (embedding_status='failed', embedding_error_code) and
    swallowed here, exactly like the existing content-classification step
    in the same ingestion function — one best-effort enrichment must never
    take down an otherwise-successful upload."""
    if not hybrid_mode_active(db):
        return

    if config.EMBEDDING_DIMENSION != PGVECTOR_COLUMN_DIMENSION:
        knowledge_repo.set_version_embedding_status(
            db, version, "failed", model_id=config.EMBEDDING_MODEL_ID, model_revision=config.EMBEDDING_MODEL_REVISION,
            dimension=config.EMBEDDING_DIMENSION, error_code="dimension_column_mismatch",
            indexed_at=datetime.now(timezone.utc),
        )
        return

    # Old passages (if this version already had a 'ready' index — e.g. a
    # config/model change made it stale, or the maintenance reindex route
    # retries a genuine failure) are deleted ONLY once the new set is fully
    # computed and about to be written, further down — never up front.
    # Deleting first meant a reindex that failed partway (embedding call
    # raises, or is interrupted) left the version with ZERO passages AND
    # embedding_status='failed', destroying a previously-good, searchable
    # index instead of merely failing to refresh it — reproduced this lot
    # by forcing an embed() failure on a version that was already 'ready'.
    adapter = EmbeddingAdapter.shared()
    try:
        # Loads the model (if not already) and reads its REAL effective
        # token budget — done BEFORE building `current`, so the revision
        # stamped on every passage below reflects the concretely resolved
        # artifact (see current_embedding_config's docstring), not merely
        # the configured label. Windowing itself is token-exact: each
        # chunk's FULL text is tokenized once (no truncation), then split
        # into windows that are guaranteed to fit what the model actually
        # sees — never a character count that could silently exceed it.
        max_tokens = adapter.max_content_tokens_per_window()
        windows: list[tuple[KnowledgeChunk, str, int, int]] = []
        for chunk in chunks:
            offsets = adapter.content_token_offsets(chunk.content)
            for span in window_passages_by_tokens(
                offsets, max_tokens=max_tokens, overlap_tokens=min(config.EMBEDDING_CHUNK_OVERLAP_TOKENS, max_tokens - 1)
            ):
                windows.append((chunk, span.content_of(chunk.content), span.start_char, span.end_char))
    except EmbeddingUnavailableError as exc:
        current = current_embedding_config()
        knowledge_repo.set_version_embedding_status(
            db, version, "failed", model_id=current.model_id, model_revision=current.model_revision,
            dimension=current.dimension, error_code=exc.reason, indexed_at=datetime.now(timezone.utc),
        )
        return

    current = current_embedding_config()
    if not windows:
        # A version can legitimately have zero chunks reaching here only in
        # a state this function is never called for (extraction failure
        # already returns earlier) — defensive, not expected in practice.
        # Still clears any OLD passages for consistency with the status
        # ('not_applicable' must never coexist with leftover passage rows).
        knowledge_repo.delete_passages_for_version(db, document_version_id=version.id)
        knowledge_repo.set_version_embedding_status(db, version, "not_applicable")
        return

    try:
        vectors = adapter.embed([w[1] for w in windows])
    except EmbeddingUnavailableError as exc:
        knowledge_repo.set_version_embedding_status(
            db, version, "failed", model_id=current.model_id, model_revision=current.model_revision,
            dimension=current.dimension, error_code=exc.reason, indexed_at=datetime.now(timezone.utc),
        )
        return

    # Every window embedded successfully — ONLY NOW is it safe to replace
    # whatever this version had before (see the module-level comment above
    # for why this must never happen earlier).
    knowledge_repo.delete_passages_for_version(db, document_version_id=version.id)

    created = []
    for (chunk, content, start_char, end_char), _vector in zip(windows, vectors):
        rows = knowledge_repo.create_passages(
            db, chunk=chunk, document_version_id=version.id, organization_id=version.organization_id,
            owner_user_id=version.owner_user_id,
            passages=[{
                "start_char": start_char, "end_char": end_char, "content": content,
                "content_fingerprint": compute_content_fingerprint(content),
                "page_number": chunk.page_number, "section": chunk.section,
                "embedding_model_id": current.model_id, "embedding_model_revision": current.model_revision,
                "embedding_dimension": current.dimension,
            }],
        )
        created.append(rows[0])

    for passage, vector in zip(created, vectors):
        _set_passage_embedding(db, passage.id, vector, current.dimension)

    knowledge_repo.set_version_embedding_status(
        db, version, "ready", model_id=current.model_id, model_revision=current.model_revision,
        dimension=current.dimension, error_code=None, indexed_at=datetime.now(timezone.utc),
    )
