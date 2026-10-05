"""B03 — private reference-documentation CRUD: /api/knowledge/documents/*.

Separate from routes_api.py on purpose (ticket B03 section 5: "éviter les
routes volumineuses") and from /api/analyze's own upload branch — that one
parses an AO to launch an analysis; this one ingests a private reference
document into the caller's own corpus. Two unrelated concerns that happen
to both accept a file upload.

B14-T1 UPDATE: the module docstring above described the PREVIOUS convention
("no per-request CSRF token, relying on SameSite=Lax") — that reasoning was
reviewed and found insufficient on its own for the multipart upload routes
below: a plain cross-origin `<form enctype="multipart/form-data">` submit is
a browser "simple request", sent WITHOUT a CORS preflight and, more to the
point, WITH the browser's own cookies for the target origin regardless of
SameSite=Lax's actual protection scope for a *top-level* cross-site
navigation-triggered form POST in some browser/embedding configurations —
relying on SameSite alone as the sole CSRF defense is exactly what the
ticket that produced this fix forbids ("ne pas remplacer CSRF par
CORS/SameSite seuls"). Every mutating route below now ALSO requires a valid
double-submit CSRF token via the `X-CSRF-Token` header (same convention as
the JSON-body routes in routes_api.py and routes_scoring_policy.py) — see
docs/api/B14_T1_CSRF_RATE_LIMIT_CONTRACT.md.
"""
from __future__ import annotations

import functools
import uuid
from datetime import datetime, timezone

from anyio import CapacityLimiter, to_thread
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from src.agents.knowledge_content_classifier import CATEGORIES as KNOWLEDGE_CONTENT_CATEGORIES
from src.agents.llm_client import ClaudeClient
from src.core import config
from src.core.logger import get_agent_logger
from src.rag import private_rag_manager
from src.web.auth.access_context import AccessContext, get_access_context, require_permission
from src.web.database.repositories import knowledge as knowledge_repo
from src.web.database.session import get_db, get_session_factory
from src.web.knowledge import documents_service, extraction, storage
from src.web.security.csrf import require_csrf

router = APIRouter(prefix="/api/knowledge/documents")
logger = get_agent_logger("web_knowledge_documents")

# Lot 59: ingestion (extraction, local embeddings, DB writes) is synchronous and CPU-bound. Called inline from
# these `async def` routes it blocked Uvicorn's single event loop for the whole upload, /healthz timed out and
# Render restarted the instance mid-import (observed on the demo deployment). It now runs in a worker thread,
# with its own Session, and at most KNOWLEDGE_INGEST_MAX_CONCURRENCY at once: this limiter replaces the global
# thread pool for these calls, and a request waiting for a slot waits asynchronously, holding no thread.
INGEST_LIMITER = CapacityLimiter(max(1, config.KNOWLEDGE_INGEST_MAX_CONCURRENCY))


async def _run_ingestion(work, **kwargs):
    return await to_thread.run_sync(functools.partial(work, **kwargs), limiter=INGEST_LIMITER)


def _ingestion_session(work):
    """Runs `work(db)` in a Session owned by the calling worker thread: rolled back on any error, always closed.
    `work` commits itself, and builds its response BEFORE returning, while the Session is still open."""
    db = get_session_factory()()
    try:
        return work(db)
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def _upload_response(result) -> dict:
    payload = {"document": _document_payload(result.document), "version": _version_summary(result.version)}
    if result.version.extraction_status != "ready":
        raise HTTPException(422, payload)
    return payload


def _ingest_new_document(*, organization_id: uuid.UUID, owner_user_id: uuid.UUID, filename: str, raw: bytes) -> dict:
    def work(db: Session) -> dict:
        try:
            result = documents_service.upload_document(
                db, organization_id=organization_id, owner_user_id=owner_user_id,
                original_filename=filename, raw=raw, llm=ClaudeClient(),
            )
        except extraction.UnsupportedContentError as exc:
            raise HTTPException(422, {"error_code": exc.error_code, "message": str(exc)})
        except documents_service.CorpusFullError as exc:
            raise HTTPException(409, {"error_code": "CORPUS_FULL", "message": str(exc)})
        db.commit()
        return _upload_response(result)
    return _ingestion_session(work)


def _ingest_new_version(
    *, organization_id: uuid.UUID, owner_user_id: uuid.UUID, document_id: uuid.UUID, filename: str, raw: bytes,
) -> dict:
    def work(db: Session) -> dict:
        # Re-read in THIS Session with the same owner scope: the document may have been deleted meanwhile.
        document = knowledge_repo.get_document_for_owner(
            db, document_id=document_id, organization_id=organization_id, owner_user_id=owner_user_id
        )
        if document is None or document.status != "active":
            raise _not_found()
        try:
            result = documents_service.add_version(
                db, organization_id=organization_id, owner_user_id=owner_user_id, document=document,
                original_filename=filename, raw=raw, llm=ClaudeClient(),
            )
        except extraction.UnsupportedContentError as exc:
            raise HTTPException(422, {"error_code": exc.error_code, "message": str(exc)})
        db.commit()
        return _upload_response(result)
    return _ingestion_session(work)


def _not_found() -> HTTPException:
    # Same generic response for "doesn't exist" and "belongs to someone
    # else" — no distinguishing signal either way (ticket B03 section 4).
    return HTTPException(404, "Document introuvable.")


def _version_summary(version) -> dict:
    return {
        "id": str(version.id), "version_number": version.version_number,
        "status": version.extraction_status, "error_code": version.error_code,
        # Lot 50 bis §2 — a PROPOSAL, never a verified fact ("un document classé « certification » n'est pas
        # une certification vérifiée", ticket verbatim). `content_category_source` names which path produced
        # it (heuristic/llm/a fallback from one/"user" once corrected/"unknown" for a pre-lot-50-bis version).
        "content_category_proposed": version.content_category_proposed,
        "content_category_final": version.content_category_final,
        "content_category_source": version.classification_source,
        "content_category_reason": version.classification_reason,
        # Lot 51 — real state of this version's VECTOR index, entirely
        # separate from `status` above (the lexical extraction state). Never
        # 'ready' for a client to render as "recherche sémantique active"
        # unless it genuinely is — 'not_applicable' for every SQLite
        # deployment and every PostgreSQL one without hybrid mode enabled.
        "embedding_status": version.embedding_status,
        "embedding_error_code": version.embedding_error_code,
        "embedding_model_id": version.embedding_model_id,
    }


def _document_payload(document) -> dict:
    # Lot 45 (additive): `status` only says whether SOME version is active —
    # it is not the state of the latest upload. A document whose only upload
    # failed reads "processing" there; `latest_version` carries the real
    # state (received/processing/ready/failed + error_code) so a client never
    # has to guess, and `active_version_number` names the searchable version.
    latest = max(document.versions, key=lambda v: v.version_number, default=None)
    active = document.active_version
    return {
        "id": str(document.id),
        "original_filename": document.original_filename,
        "status": "ready" if document.active_version_id else "processing",
        "active_version_id": str(document.active_version_id) if document.active_version_id else None,
        "active_version_number": active.version_number if active is not None else None,
        "latest_version": _version_summary(latest) if latest is not None else None,
        "created_at": document.created_at.isoformat(),
        "updated_at": document.updated_at.isoformat(),
    }


@router.get("")
def list_documents(ctx: AccessContext = Depends(get_access_context), db: Session = Depends(get_db)):
    documents = knowledge_repo.list_active_documents(db, organization_id=ctx.organization_id, owner_user_id=ctx.user.id)
    return {"documents": [_document_payload(d) for d in documents]}


@router.post("", status_code=201)
async def upload_document(
    request: Request,
    file: UploadFile = File(...),
    ctx: AccessContext = Depends(require_permission("knowledge:write")),
    db: Session = Depends(get_db),
):
    require_csrf(request, request.headers.get("X-CSRF-Token"))
    if not file.filename:
        raise HTTPException(400, "Aucun fichier fourni.")

    max_bytes = config.KNOWLEDGE_MAX_FILE_SIZE_MB * 1024 * 1024
    try:
        raw = await documents_service.read_upload_with_limit(file, max_bytes)
    except documents_service.DocumentTooLargeError:
        raise HTTPException(413, f"Fichier supérieur à la limite de {config.KNOWLEDGE_MAX_FILE_SIZE_MB} Mio.")

    organization_id, owner_user_id = ctx.organization_id, ctx.user.id
    db.close()  # the ingestion uses its own Session; never hold this request's connection while waiting
    return await _run_ingestion(
        _ingest_new_document, organization_id=organization_id, owner_user_id=owner_user_id,
        filename=file.filename, raw=raw,
    )


@router.get("/{document_id}")
def get_document(document_id: uuid.UUID, ctx: AccessContext = Depends(get_access_context), db: Session = Depends(get_db)):
    document = knowledge_repo.get_document_for_owner(
        db, document_id=document_id, organization_id=ctx.organization_id, owner_user_id=ctx.user.id
    )
    if document is None or document.status != "active":
        raise _not_found()
    return {
        **_document_payload(document),
        "versions": [
            {**_version_summary(v), "created_at": v.created_at.isoformat()}
            for v in sorted(document.versions, key=lambda v: v.version_number)
        ],
    }


@router.get("/{document_id}/download")
def download_document(document_id: uuid.UUID, ctx: AccessContext = Depends(get_access_context), db: Session = Depends(get_db)):
    document = knowledge_repo.get_document_for_owner(
        db, document_id=document_id, organization_id=ctx.organization_id, owner_user_id=ctx.user.id
    )
    if document is None or document.status != "active" or document.active_version is None:
        raise _not_found()
    version = document.active_version
    try:
        path = storage.resolve_private_path(version.storage_key)
    except FileNotFoundError:
        raise HTTPException(404, "Le fichier n'existe plus sur le serveur.")
    # Lot 47: name, extension and Content-Type describe the version really served (not the creation name).
    filename, media_type = documents_service.download_metadata(document, version)
    return FileResponse(path, filename=filename, media_type=media_type, headers={"X-Content-Type-Options": "nosniff"})


@router.post("/{document_id}/versions")
async def upload_version(
    request: Request,
    document_id: uuid.UUID, file: UploadFile = File(...),
    ctx: AccessContext = Depends(require_permission("knowledge:write")),
    db: Session = Depends(get_db),
):
    require_csrf(request, request.headers.get("X-CSRF-Token"))
    document = knowledge_repo.get_document_for_owner(
        db, document_id=document_id, organization_id=ctx.organization_id, owner_user_id=ctx.user.id
    )
    if document is None or document.status != "active":
        raise _not_found()
    if not file.filename:
        raise HTTPException(400, "Aucun fichier fourni.")

    max_bytes = config.KNOWLEDGE_MAX_FILE_SIZE_MB * 1024 * 1024
    try:
        raw = await documents_service.read_upload_with_limit(file, max_bytes)
    except documents_service.DocumentTooLargeError:
        raise HTTPException(413, f"Fichier supérieur à la limite de {config.KNOWLEDGE_MAX_FILE_SIZE_MB} Mio.")

    organization_id, owner_user_id = ctx.organization_id, ctx.user.id
    db.close()  # the ingestion uses its own Session; never hold this request's connection while waiting
    return await _run_ingestion(
        _ingest_new_version, organization_id=organization_id, owner_user_id=owner_user_id,
        document_id=document_id, filename=file.filename, raw=raw,
    )


@router.post("/{document_id}/versions/{version_id}/category")
def correct_version_category(
    request: Request, document_id: uuid.UUID, version_id: uuid.UUID, payload: dict,
    ctx: AccessContext = Depends(require_permission("knowledge:write")), db: Session = Depends(get_db),
):
    """Lot 50 bis §2 — a traceable human correction of a version's PROPOSED content type. Never re-runs
    classification, never touches the account's profile/scoring, never changes anything else about the
    version (extraction status, active-ness, searchability). `category=null` clears the correction back to
    "indéterminé" — a legitimate, honest state, not an error."""
    require_csrf(request, request.headers.get("X-CSRF-Token"))
    document = knowledge_repo.get_document_for_owner(
        db, document_id=document_id, organization_id=ctx.organization_id, owner_user_id=ctx.user.id
    )
    if document is None or document.status != "active":
        raise _not_found()
    version = next((v for v in document.versions if v.id == version_id), None)
    if version is None:
        raise _not_found()
    category = payload.get("category")
    if category is not None and category not in KNOWLEDGE_CONTENT_CATEGORIES:
        raise HTTPException(422, {"error_code": "INVALID_VALUE", "message": f"Catégorie inconnue : {category!r}."})
    version.content_category_final = category
    version.classification_source = "user"
    version.classification_reason = "Corrigée manuellement par l'utilisateur."
    version.classified_by_user_id = ctx.user.id
    version.classified_at = datetime.now(timezone.utc)
    db.commit()
    return _version_summary(version)


@router.post("/{document_id}/versions/{version_id}/reindex")
def reindex_version(
    request: Request, document_id: uuid.UUID, version_id: uuid.UUID,
    ctx: AccessContext = Depends(require_permission("knowledge:write")), db: Session = Depends(get_db),
):
    """Lot 51 — retries this version's vector indexing (a genuine provider
    failure, or a version that predates hybrid mode / a since-changed
    embedding model). A no-op (embedding_status stays whatever it already
    is) when hybrid mode is structurally inactive — never a fake 'ready'.
    Never touches extraction/lexical state, never re-runs classification."""
    require_csrf(request, request.headers.get("X-CSRF-Token"))
    document = knowledge_repo.get_document_for_owner(
        db, document_id=document_id, organization_id=ctx.organization_id, owner_user_id=ctx.user.id
    )
    if document is None or document.status != "active":
        raise _not_found()
    version = next((v for v in document.versions if v.id == version_id), None)
    if version is None or version.extraction_status != "ready":
        raise _not_found()
    from src.rag import hybrid_index
    hybrid_index.index_version(db, version=version, chunks=list(version.chunks))
    db.commit()
    return _version_summary(version)


@router.delete("/{document_id}")
def delete_document(
    request: Request,
    document_id: uuid.UUID, ctx: AccessContext = Depends(require_permission("knowledge:write")), db: Session = Depends(get_db),
):
    require_csrf(request, request.headers.get("X-CSRF-Token"))
    document = knowledge_repo.get_document_for_owner(
        db, document_id=document_id, organization_id=ctx.organization_id, owner_user_id=ctx.user.id
    )
    if document is None or document.status != "active":
        raise _not_found()
    physically_cleaned = documents_service.delete_document(
        db, organization_id=ctx.organization_id, owner_user_id=ctx.user.id, document=document
    )
    db.commit()
    if not physically_cleaned:
        logger.warning("Physical delete incomplete for knowledge document_id=%s — metadata deleted, retry needed", document_id)
    return {"status": "deleted", "physically_cleaned": physically_cleaned}
