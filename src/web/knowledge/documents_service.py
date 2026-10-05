"""Orchestrates document ingestion: size/type checks -> private storage ->
extraction -> chunk persistence -> publish. Every function takes an
explicit (organization_id, owner_user_id) scope — never inferred, never
optional (ticket B03 section 4).

Lot 50 bis §2: once a version's text is successfully extracted (still BEFORE it is published/searchable), an
optional content-type classification (référence/certification/présentation/autre/indéterminé) is proposed —
never a verified fact, never a profile/scoring update, and never blocking: a classification failure/disabled
LLM never stops a version from becoming 'ready'/searchable, exactly as before this lot.
"""
from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from fastapi import UploadFile
from sqlalchemy.orm import Session

from src.agents.knowledge_content_classifier import KnowledgeContentClassifierAgent
from src.core import config
from src.rag import hybrid_index
from src.web.database.models import KnowledgeDocument, KnowledgeDocumentVersion
from src.web.database.repositories import knowledge as knowledge_repo
from src.web.knowledge import extraction, storage


_MEDIA_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".md": "text/markdown",
    ".txt": "text/plain",
}
_UNSAFE_NAME_CHARS = re.compile(r'[\x00-\x1f\x7f"<>:*?|\\/]')
_MAX_STEM_CHARS = 120
NEUTRAL_STEM = "document"


def safe_download_stem(name: str | None) -> str:
    """A display-safe stem from the document's creation name — text only, never a path: the last path component
    (either separator), no control character, quote, angle bracket, colon, wildcard or pipe, no leading/trailing dot or
    blank, bounded length. Empty when nothing usable remains (the caller then uses a neutral name)."""
    last = re.split(r"[\\/]", name or "")[-1]
    stem = last.rsplit(".", 1)[0] if "." in last else last
    stem = _UNSAFE_NAME_CHARS.sub("_", stem)
    return re.sub(r"\s+", " ", stem).strip(" .")[:_MAX_STEM_CHARS]


def download_metadata(document: KnowledgeDocument, version: KnowledgeDocumentVersion) -> tuple[str, str]:
    """(filename, media type) of the version that is ACTUALLY served. The extension and media type come from the
    version's own server-side metadata — the format detected at upload (extension + magic bytes, see
    extraction.detect_suffix), cross-checked against the suffix of the file really stored — never from the
    document's creation name (a PDF replaced by a DOCX must not be served as `x.pdf`). When that format is unknown or
    contradicts the stored file (an old or damaged record) no extension is invented: neutral octet-stream. Nothing here
    touches the disk or builds a path: the file on disk keeps its opaque name."""
    from pathlib import PurePosixPath

    stem = safe_download_stem(document.original_filename) or NEUTRAL_STEM
    suffix = (version.content_type_detected or "").lower()
    stored = PurePosixPath(version.storage_key or "").suffix.lower()
    if suffix in extraction.SUPPORTED_SUFFIXES and stored == suffix:
        return stem + suffix, _MEDIA_TYPES[suffix]
    return stem, "application/octet-stream"


class DocumentTooLargeError(Exception):
    pass


class CorpusFullError(Exception):
    pass


@dataclass
class UploadResult:
    document: KnowledgeDocument
    version: KnowledgeDocumentVersion


async def read_upload_with_limit(file: UploadFile, max_bytes: int) -> bytes:
    """Reads in bounded chunks, aborting the moment the limit is exceeded —
    never buffers an arbitrarily large file fully before checking (ticket
    B03 section 6: "Contrôler les limites pendant la réception")."""
    chunks: list[bytes] = []
    total = 0
    chunk_size = 1024 * 256
    while True:
        piece = await file.read(chunk_size)
        if not piece:
            break
        total += len(piece)
        if total > max_bytes:
            raise DocumentTooLargeError(f"Fichier supérieur à la limite de {max_bytes} octets.")
        chunks.append(piece)
    return b"".join(chunks)


def upload_document(
    db: Session, *, organization_id: uuid.UUID, owner_user_id: uuid.UUID, original_filename: str, raw: bytes, llm=None,
) -> UploadResult:
    """The whole ingestion pipeline for one upload, as a single DB
    transaction the caller commits: a version is created 'received', then
    'processing', then either 'ready' (chunks persisted, document's
    active_version_id updated) or 'failed' (error_code set, previous active
    version — if any — left untouched). A failed upload is never
    searchable (ticket B03 section 6). Lot 50 bis §2: `llm` (the account's
    configured adapter, optional) enables a real content-type judgment for
    the classification proposal below — `llm=None` keeps the heuristic-only
    behavior byte-for-byte unchanged."""
    corpus = knowledge_repo.get_or_create_corpus(db, organization_id=organization_id, owner_user_id=owner_user_id)

    if knowledge_repo.count_active_documents(db, corpus.id) >= config.KNOWLEDGE_MAX_ACTIVE_DOCUMENTS_PER_CORPUS:
        raise CorpusFullError(
            f"Limite de {config.KNOWLEDGE_MAX_ACTIVE_DOCUMENTS_PER_CORPUS} documents actifs atteinte pour ce compte."
        )

    content_hash = hashlib.sha256(raw).hexdigest()
    document = knowledge_repo.create_document(
        db, corpus=corpus, organization_id=organization_id, owner_user_id=owner_user_id,
        original_filename=original_filename,
    )
    return _ingest_version(db, corpus=corpus, document=document, organization_id=organization_id, owner_user_id=owner_user_id,
                            original_filename=original_filename, raw=raw, content_hash=content_hash, llm=llm)


def add_version(
    db: Session, *, organization_id: uuid.UUID, owner_user_id: uuid.UUID, document: KnowledgeDocument,
    original_filename: str, raw: bytes, llm=None,
) -> UploadResult:
    """A new version of an existing document. The previous active version
    stays active (searchable) until this one succeeds and is published —
    ticket B03 section 6: "Une nouvelle version n'efface pas la version
    active avant validation"."""
    corpus = knowledge_repo.get_or_create_corpus(db, organization_id=organization_id, owner_user_id=owner_user_id)
    content_hash = hashlib.sha256(raw).hexdigest()
    return _ingest_version(db, corpus=corpus, document=document, organization_id=organization_id, owner_user_id=owner_user_id,
                            original_filename=original_filename, raw=raw, content_hash=content_hash, llm=llm)


def _ingest_version(
    db: Session, *, corpus, document: KnowledgeDocument, organization_id: uuid.UUID, owner_user_id: uuid.UUID,
    original_filename: str, raw: bytes, content_hash: str, llm=None,
) -> UploadResult:
    try:
        suffix = extraction.detect_suffix(original_filename, raw)
    except extraction.UnsupportedContentError as exc:
        # Nothing to persist as a version at all — reject before writing
        # anything to storage or the DB, per the "refuse avant tout
        # traitement coûteux" requirement.
        raise

    version = knowledge_repo.create_version(
        db, document=document, organization_id=organization_id, owner_user_id=owner_user_id,
        storage_key="", content_type_detected=suffix, file_size=len(raw), content_hash=content_hash,
        extractor_version=config.KNOWLEDGE_EXTRACTOR_VERSION,
    )
    storage_key = storage.write_upload(
        organization_id=organization_id, owner_user_id=owner_user_id, document_id=document.id, version_id=version.id,
        suffix=suffix, content=raw,
    )
    version.storage_key = storage_key
    knowledge_repo.set_version_status(db, version, "processing")

    try:
        chunks = extraction.extract_chunks(suffix, raw)
    except extraction.UnsupportedContentError as exc:
        knowledge_repo.set_version_status(db, version, "failed", error_code=exc.error_code)
        return UploadResult(document=document, version=version)

    chunk_rows = knowledge_repo.add_chunks(
        db, version=version, organization_id=organization_id, owner_user_id=owner_user_id,
        chunks=[{"content": c.content, "page_number": c.page_number, "section": c.section} for c in chunks],
    )
    # Lot 51 — vector passages, best-effort exactly like the classification
    # step below: a no-op unless hybrid mode is structurally possible
    # (PostgreSQL+pgvector) AND explicitly enabled; any provider/dimension
    # failure is recorded as embedding_status='failed' on the version and
    # never stops it from becoming 'ready'/lexically searchable.
    try:
        hybrid_index.index_version(db, version=version, chunks=chunk_rows)
    except Exception:
        from src.core.logger import get_agent_logger
        get_agent_logger("knowledge_documents_service").exception("Hybrid indexing failed version_id=%s", version.id)
    # Lot 50 bis §2 — a PROPOSAL only, never blocking: any failure here (a bug in the classifier itself, which
    # should never happen but must never take down an otherwise-successful upload) is caught and leaves the
    # version's classification fields at their column defaults, exactly like a version from before this lot.
    try:
        joined = "\n\n".join(c.content for c in chunks)
        result = KnowledgeContentClassifierAgent().classify(joined, filename=original_filename, llm=llm)
        version.content_category_proposed = result.category_proposed
        version.content_category_final = result.category_proposed
        version.classification_source = result.source
        version.classification_reason = " ".join(result.reasons) if result.reasons else None
    except Exception:
        from src.core.logger import get_agent_logger
        get_agent_logger("knowledge_documents_service").exception("Content classification failed version_id=%s", version.id)

    knowledge_repo.set_version_status(db, version, "ready")
    knowledge_repo.publish_version(db, document=document, version=version)
    knowledge_repo.bump_generation(db, corpus)
    return UploadResult(document=document, version=version)


def delete_document(db: Session, *, organization_id: uuid.UUID, owner_user_id: uuid.UUID, document: KnowledgeDocument) -> bool:
    """Metadata-first delete: the document is marked 'deleted' and the
    corpus generation bumped (excluding it from every subsequent search,
    including any snapshot rebuilt after this call) BEFORE the physical
    file is touched. A failed physical unlink is reported (return False)
    but never re-activates the document to "hide" the error (ticket B03
    section 8)."""
    corpus = knowledge_repo.get_or_create_corpus(db, organization_id=organization_id, owner_user_id=owner_user_id)
    versions = document.versions
    knowledge_repo.soft_delete_document(db, document)
    knowledge_repo.bump_generation(db, corpus)
    all_deleted = True
    for version in versions:
        if version.storage_key and not storage.delete_private_path(version.storage_key):
            all_deleted = False
    return all_deleted
