"""Pure data-access functions for the B03 private knowledge chain
(KnowledgeCorpus -> KnowledgeDocument -> KnowledgeDocumentVersion ->
KnowledgeChunk). Every query here takes (organization_id, owner_user_id)
explicitly — there is no "list everything" function, on purpose (ticket B03
section 4: "aucun repository métier public sans scope").
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.web.database.models import KnowledgeChunk, KnowledgeCorpus, KnowledgeDocument, KnowledgeDocumentVersion, KnowledgePassage


def get_or_create_corpus(db: Session, *, organization_id: uuid.UUID, owner_user_id: uuid.UUID) -> KnowledgeCorpus:
    stmt = select(KnowledgeCorpus).where(
        KnowledgeCorpus.organization_id == organization_id, KnowledgeCorpus.owner_user_id == owner_user_id
    )
    corpus = db.execute(stmt).scalar_one_or_none()
    if corpus is not None:
        return corpus
    corpus = KnowledgeCorpus(organization_id=organization_id, owner_user_id=owner_user_id)
    db.add(corpus)
    db.flush()
    return corpus


def bump_generation(db: Session, corpus: KnowledgeCorpus) -> int:
    """The one function every write to this account's knowledge must call.
    Persisted in the DB (not a process-local counter) so a second instance
    of the service can detect the change too — see
    src/rag/private_rag_manager.py."""
    corpus.generation = corpus.generation + 1
    db.flush()
    return corpus.generation


def count_active_documents(db: Session, corpus_id: uuid.UUID) -> int:
    stmt = select(func.count()).select_from(KnowledgeDocument).where(
        KnowledgeDocument.corpus_id == corpus_id, KnowledgeDocument.status == "active"
    )
    return db.execute(stmt).scalar_one()


def create_document(
    db: Session, *, corpus: KnowledgeCorpus, organization_id: uuid.UUID, owner_user_id: uuid.UUID, original_filename: str
) -> KnowledgeDocument:
    doc = KnowledgeDocument(
        corpus_id=corpus.id, organization_id=organization_id, owner_user_id=owner_user_id,
        original_filename=original_filename[:255], status="active",
    )
    db.add(doc)
    db.flush()
    return doc


def get_document_for_owner(
    db: Session, *, document_id: uuid.UUID, organization_id: uuid.UUID, owner_user_id: uuid.UUID
) -> KnowledgeDocument | None:
    """Ownership-checked — never returns another account's document, even
    within the same organization."""
    stmt = select(KnowledgeDocument).where(
        KnowledgeDocument.id == document_id,
        KnowledgeDocument.organization_id == organization_id,
        KnowledgeDocument.owner_user_id == owner_user_id,
    )
    return db.execute(stmt).scalar_one_or_none()


def list_active_documents(db: Session, *, organization_id: uuid.UUID, owner_user_id: uuid.UUID) -> list[KnowledgeDocument]:
    stmt = (
        select(KnowledgeDocument)
        .where(
            KnowledgeDocument.organization_id == organization_id,
            KnowledgeDocument.owner_user_id == owner_user_id,
            KnowledgeDocument.status == "active",
        )
        .order_by(KnowledgeDocument.created_at.desc())
    )
    return list(db.execute(stmt).scalars().all())


def next_version_number(db: Session, document_id: uuid.UUID) -> int:
    stmt = select(func.coalesce(func.max(KnowledgeDocumentVersion.version_number), 0)).where(
        KnowledgeDocumentVersion.document_id == document_id
    )
    return db.execute(stmt).scalar_one() + 1


def create_version(
    db: Session, *, document: KnowledgeDocument, organization_id: uuid.UUID, owner_user_id: uuid.UUID,
    storage_key: str, content_type_detected: str | None, file_size: int, content_hash: str, extractor_version: str,
) -> KnowledgeDocumentVersion:
    version = KnowledgeDocumentVersion(
        document_id=document.id, organization_id=organization_id, owner_user_id=owner_user_id,
        version_number=next_version_number(db, document.id),
        storage_key=storage_key, content_type_detected=content_type_detected, file_size=file_size,
        content_hash=content_hash, extraction_status="received", extractor_version=extractor_version,
    )
    db.add(version)
    db.flush()
    return version


def set_version_status(db: Session, version: KnowledgeDocumentVersion, status: str, *, error_code: str | None = None) -> None:
    version.extraction_status = status
    version.error_code = error_code
    db.flush()


def add_chunks(
    db: Session, *, version: KnowledgeDocumentVersion, organization_id: uuid.UUID, owner_user_id: uuid.UUID,
    chunks: list[dict[str, Any]],
) -> list[KnowledgeChunk]:
    rows: list[KnowledgeChunk] = []
    for i, chunk in enumerate(chunks):
        row = KnowledgeChunk(
            document_version_id=version.id, organization_id=organization_id, owner_user_id=owner_user_id,
            order_index=i, page_number=chunk.get("page_number"), section=chunk.get("section"),
            content=chunk["content"],
        )
        db.add(row)
        rows.append(row)
    db.flush()
    return rows


def publish_version(db: Session, *, document: KnowledgeDocument, version: KnowledgeDocumentVersion) -> None:
    """Only called after extraction succeeds and chunks are written — this
    is the single moment a version becomes the searchable one."""
    document.active_version_id = version.id
    db.flush()


def find_by_content_hash(
    db: Session, *, organization_id: uuid.UUID, owner_user_id: uuid.UUID, content_hash: str
) -> KnowledgeDocumentVersion | None:
    """Dedup scoped to one account only — never checked against another
    account's hashes, and never reveals whether one exists elsewhere."""
    stmt = (
        select(KnowledgeDocumentVersion)
        .where(
            KnowledgeDocumentVersion.organization_id == organization_id,
            KnowledgeDocumentVersion.owner_user_id == owner_user_id,
            KnowledgeDocumentVersion.content_hash == content_hash,
            KnowledgeDocumentVersion.extraction_status == "ready",
        )
        .order_by(KnowledgeDocumentVersion.created_at.desc())
    )
    return db.execute(stmt).scalars().first()


def soft_delete_document(db: Session, document: KnowledgeDocument) -> None:
    from datetime import datetime, timezone
    document.status = "deleted"
    document.deleted_at = datetime.now(timezone.utc)
    db.flush()


def active_chunks_for_corpus(db: Session, *, organization_id: uuid.UUID, owner_user_id: uuid.UUID) -> list[tuple[KnowledgeChunk, str]]:
    """Every chunk that is currently allowed to be searched: its version
    must be the document's *active* version, and the document must not be
    deleted. Returns (chunk, source_label) pairs — this is the single query
    the RAG snapshot builder and the belt-and-suspenders re-check before
    returning search results both call, so there is exactly one definition
    of "authorized to search" in the codebase."""
    stmt = (
        select(KnowledgeChunk, KnowledgeDocument.original_filename)
        .join(KnowledgeDocumentVersion, KnowledgeChunk.document_version_id == KnowledgeDocumentVersion.id)
        .join(KnowledgeDocument, KnowledgeDocumentVersion.document_id == KnowledgeDocument.id)
        .where(
            KnowledgeChunk.organization_id == organization_id,
            KnowledgeChunk.owner_user_id == owner_user_id,
            KnowledgeDocument.status == "active",
            KnowledgeDocument.active_version_id == KnowledgeDocumentVersion.id,
        )
        .order_by(KnowledgeDocument.original_filename, KnowledgeChunk.order_index)
    )
    return [(chunk, filename) for chunk, filename in db.execute(stmt).all()]


def get_active_chunk_by_id(
    db: Session, *, chunk_id: uuid.UUID, organization_id: uuid.UUID, owner_user_id: uuid.UUID,
) -> tuple[KnowledgeChunk, str] | None:
    """Lot 52 — same authorization/active-version rule as `active_chunks_for_corpus` above, scoped to ONE
    chunk id: used to RE-VERIFY a sourced fact proposal's provenance at completion-submission time
    (src/web/completion_service.py). Returns None if the chunk does not exist, belongs to a different
    account, or its document/version is no longer the active one (deleted, superseded by a reindex or a new
    version) — the caller must treat that as an explicit "source no longer available", never a silent
    reattribution to a different, coincidentally-similar chunk."""
    stmt = (
        select(KnowledgeChunk, KnowledgeDocument.original_filename)
        .join(KnowledgeDocumentVersion, KnowledgeChunk.document_version_id == KnowledgeDocumentVersion.id)
        .join(KnowledgeDocument, KnowledgeDocumentVersion.document_id == KnowledgeDocument.id)
        .where(
            KnowledgeChunk.id == chunk_id,
            KnowledgeChunk.organization_id == organization_id,
            KnowledgeChunk.owner_user_id == owner_user_id,
            KnowledgeDocument.status == "active",
            KnowledgeDocument.active_version_id == KnowledgeDocumentVersion.id,
        )
    )
    row = db.execute(stmt).first()
    return (row[0], row[1]) if row else None


def set_version_embedding_status(
    db: Session, version: KnowledgeDocumentVersion, status: str, *,
    model_id: str | None = None, model_revision: str | None = None, dimension: int | None = None,
    error_code: str | None = None, indexed_at=None,
) -> None:
    """Lot 51 — the ONE place `embedding_status` (and the config it was
    computed with) is ever written. Portable across dialects: never touches
    the `embedding` vector column itself (postgres-only, managed by raw SQL
    in src/rag/hybrid_index.py) — only this version-level readiness flag."""
    version.embedding_status = status
    version.embedding_model_id = model_id
    version.embedding_model_revision = model_revision
    version.embedding_dimension = dimension
    version.embedding_error_code = error_code
    version.embedding_indexed_at = indexed_at
    db.flush()


def delete_passages_for_version(db: Session, *, document_version_id: uuid.UUID) -> None:
    """Idempotent-reindex prerequisite: a version's OLD passages (built with
    a since-changed embedding config, or a previous failed attempt) are
    always cleared before new ones are computed — never left mixed with a
    fresh batch (ticket: "réindexation idempotente")."""
    stmt = select(KnowledgePassage).where(KnowledgePassage.document_version_id == document_version_id)
    for passage in db.execute(stmt).scalars().all():
        db.delete(passage)
    db.flush()


def create_passages(
    db: Session, *, chunk: KnowledgeChunk, document_version_id: uuid.UUID, organization_id: uuid.UUID,
    owner_user_id: uuid.UUID, passages: list[dict[str, Any]],
) -> list[KnowledgePassage]:
    """Creates the PORTABLE columns only (no vector value here — see
    src/rag/hybrid_index.py, which flushes this first to obtain real ids,
    then sets each row's `embedding` via a dialect-checked raw SQL UPDATE).
    `passages` items: start_char, end_char, content, content_fingerprint,
    page_number, section, embedding_model_id, embedding_model_revision,
    embedding_dimension."""
    rows: list[KnowledgePassage] = []
    for p in passages:
        row = KnowledgePassage(
            chunk_id=chunk.id, document_version_id=document_version_id, organization_id=organization_id,
            owner_user_id=owner_user_id, start_char=p["start_char"], end_char=p["end_char"], content=p["content"],
            content_fingerprint=p["content_fingerprint"], page_number=p.get("page_number"), section=p.get("section"),
            embedding_model_id=p["embedding_model_id"], embedding_model_revision=p["embedding_model_revision"],
            embedding_dimension=p["embedding_dimension"],
        )
        db.add(row)
        rows.append(row)
    db.flush()
    return rows


def corpus_fully_vector_ready(db: Session, *, organization_id: uuid.UUID, owner_user_id: uuid.UUID, model_id: str, model_revision: str, dimension: int) -> bool:
    """Lot 51 bis — True only if EVERY active document's active version has
    a 'ready' embedding_status AND matches the CURRENT embedding config
    exactly (a 'ready' version built under a now-superseded model/revision/
    dimension is STALE — src/rag/hybrid_index.py::version_is_stale — and is
    just as invisible to the vector signal as a 'failed' one, filtered out
    by the same three fields in hybrid_search._vector_candidates). Used by
    src/rag/hybrid_search.py to decide whether to report the honest,
    distinct 'hybrid_partial' mode instead of a plain 'hybrid' that would
    otherwise silently overclaim full-corpus vector coverage (a genuinely
    failed/stale/not-yet-reindexed document is never invisible to LEXICAL
    search — only to the vector signal specifically). A pure read, no side
    effect (see active_chunk_ids_for_corpus's own docstring for why that
    matters near an AO-dossier-only code path)."""
    stmt = (
        select(func.count())
        .select_from(KnowledgeDocument)
        .join(KnowledgeDocumentVersion, KnowledgeDocument.active_version_id == KnowledgeDocumentVersion.id)
        .where(
            KnowledgeDocument.organization_id == organization_id,
            KnowledgeDocument.owner_user_id == owner_user_id,
            KnowledgeDocument.status == "active",
            (
                (KnowledgeDocumentVersion.embedding_status != "ready")
                | (KnowledgeDocumentVersion.embedding_model_id != model_id)
                | (KnowledgeDocumentVersion.embedding_model_revision != model_revision)
                | (KnowledgeDocumentVersion.embedding_dimension != dimension)
            ),
        )
    )
    return db.execute(stmt).scalar_one() == 0


def active_chunk_ids_for_corpus(db: Session, *, organization_id: uuid.UUID, owner_user_id: uuid.UUID) -> set[uuid.UUID]:
    stmt = (
        select(KnowledgeChunk.id)
        .join(KnowledgeDocumentVersion, KnowledgeChunk.document_version_id == KnowledgeDocumentVersion.id)
        .join(KnowledgeDocument, KnowledgeDocumentVersion.document_id == KnowledgeDocument.id)
        .where(
            KnowledgeChunk.organization_id == organization_id,
            KnowledgeChunk.owner_user_id == owner_user_id,
            KnowledgeDocument.status == "active",
            KnowledgeDocument.active_version_id == KnowledgeDocumentVersion.id,
        )
    )
    return set(db.execute(stmt).scalars().all())
