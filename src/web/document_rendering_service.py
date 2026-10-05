"""B19-T2 — document rendering, decoupled from the analysis result.

Why this module exists (the three confirmed defects):

1. A rendering failure ends the job at status="error"/
   document_generation_failed, while the SQL `analyses` row and its
   `result_data` snapshot are already durably saved (B11-T1 reordered the
   flow precisely so that they are). The analysis therefore SURVIVED — but
   the user had no way whatsoever to obtain the documents afterwards short
   of re-running the whole analysis.
2. There was no per-document status at all: `job.files` is either `{}` or
   carries both keys, so a caller cannot tell "the PDF failed but the DOCX
   is fine" from "both failed", nor "never generated" from "the file was
   lost".
3. (fixed in src/livrables/document_generator.py, not here) free-form
   AO/LLM text reached ReportLab's markup-parsing `Paragraph` unescaped.

What this module is NOT: a second renderer. It calls the SAME
DocumentGenerator the normal job flow calls — it only changes where the
inputs come from (a persisted snapshot instead of a live job) and adds the
authorization, atomic-publish and no-duplicate discipline around it.

Hard rule for a regeneration: NO LLM call, NO re-enrichment, NO rescoring,
ever. The original enrichment prose is already baked into the snapshot's
`ai_content`; `llm=None` is passed so the generator structurally cannot
call a model even if `ai_content` is empty (it falls back to exactly the
same neutral text the ORIGINAL render would have used in that case). A
regenerated document for a historical analysis must mean exactly what it
meant when the analysis ran, even if the account's ScoringPolicy has been
edited or re-activated since.

See docs/api/B19_T2_DOCUMENT_RENDERING_CONTRACT.md.
"""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.core.logger import get_agent_logger
from src.core.models import AOContext, ScoringResult
from src.web.database.models import AnalysisDocument

logger = get_agent_logger("document_rendering")

# The two document kinds and their canonical mime types — the SAME strings
# src/web/jobs.py::_attach_documents_to_database writes, so a regenerated
# document upserts over the original row instead of sitting beside it.
PDF_MIME = "application/pdf"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_MIME_BY_KIND = {"pdf": PDF_MIME, "docx": DOCX_MIME}
DOCUMENT_KINDS = tuple(_MIME_BY_KIND)

AVAILABLE = "available"
UNAVAILABLE = "unavailable"


# ---------------------------------------------------------------------------
# Typed errors. Every one of these is a DELIBERATE refusal — none of them
# ever leaves a half-written file behind, and none of them is ever
# "recovered" by inventing substitute content.
# ---------------------------------------------------------------------------

class DocumentRenderingError(RuntimeError):
    """Base class for every refusal/failure this module raises."""

    error_code = "document_rendering_failed"
    user_message = (
        "La génération du document a échoué. Réessayez ou contactez le support si le problème persiste."
    )


class AnalysisNotFoundError(DocumentRenderingError):
    """No analysis with this id belongs to this user.

    Deliberately ONE error for both "no such analysis" and "it belongs to
    somebody else": a caller must not be able to tell those apart, or the
    error shape itself would confirm the existence of another account's
    analysis. Map it to a plain 404.
    """

    error_code = "analysis_not_found"
    user_message = "Analyse introuvable."


class AnalysisOwnershipMismatchError(DocumentRenderingError):
    """The analysis belongs to this USER but to a different ORGANIZATION
    than the caller's current access context.

    Structurally shouldn't happen (an analysis's organization is fixed at
    creation and the composite FK on analysis_documents enforces the
    match at the database level), so this mirrors the discipline
    upsert_analysis / _attach_documents_to_database already established:
    refuse loudly rather than proceed anyway and write a document row under
    an organization that disagrees with its own analysis. Also map it to a
    404 — it is not a state a legitimate caller can reach.
    """

    error_code = "analysis_organization_mismatch"
    user_message = "Analyse introuvable."


class UnsupportedDocumentKindError(DocumentRenderingError):
    error_code = "unsupported_document_kind"
    user_message = "Type de document inconnu. Les types disponibles sont : pdf, docx."


class SnapshotUnusableError(DocumentRenderingError):
    """The persisted snapshot cannot produce a faithful document.

    Either it lacks `ao`/`result` entirely, or B18-T2's degradation check
    concluded its numeric data is unusable (a non-finite score_global or
    criterion score/poids — the same HISTORICAL_SCORE_UNAVAILABLE case the
    read path in jobs.py already refuses to build a ScoringResult from).

    The ticket is explicit: "Si le snapshot manque d'un élément
    indispensable, retourner une erreur explicite ; ne pas inventer un
    contenu de remplacement." Nothing is fabricated to fill the gap —
    there is no placeholder AO, no placeholder score, no default decision.
    """

    error_code = "analysis_snapshot_unusable"
    user_message = (
        "Cette analyse ne contient plus les données nécessaires pour régénérer le document. "
        "Contactez le support."
    )


# ---------------------------------------------------------------------------
# Snapshot reconstruction.
# ---------------------------------------------------------------------------

def _reconstruct_from_snapshot(analysis) -> tuple[AOContext, ScoringResult]:
    """Rebuild (AOContext, ScoringResult) from `analysis.result_data`.

    JUDGMENT CALL, stated plainly: this does NOT call
    jobs._job_from_snapshot, even though that is the one reconstruction
    path for the READ side (B11-T1 section 1). Two reasons: that function
    returns a `Job` and, as a side effect, INSERTS it into the process-wide
    `jobs._JOBS` cache — a regeneration must not mutate the job cache — and
    it encodes "an unusable snapshot becomes an error-shaped Job", whereas
    here an unusable snapshot must become a raised refusal.

    What it does reuse, by import rather than by copy, is the part that
    actually decides MEANING: jobs._sanitize_legacy_result_data, the single
    implementation of the B18-T2 degradation rules. So the "is this
    snapshot usable, and is it degraded?" verdict can never drift between
    the read path and this one. Only the ~8 surrounding lines (construct
    the two models, apply the one-way integrity stamp) are written twice;
    if _job_from_snapshot's own surrounding logic changes, this function
    must be revisited — noted as a real risk in the contract doc.

    `analysis.result_data` is never mutated: a private deep copy is
    sanitized, exactly as _load_from_database does, so reading/regenerating
    never rewrites the stored snapshot.
    """
    import copy

    from src.web import jobs

    data = copy.deepcopy(analysis.result_data or {})
    if not data.get("ao") or not data.get("result"):
        raise SnapshotUnusableError(
            f"analysis_id={analysis.id}: the persisted snapshot has no usable 'ao'/'result' — "
            "refusing to regenerate a document from fabricated content."
        )

    integrity_status, integrity_reason = jobs._sanitize_legacy_result_data(str(analysis.job_id or analysis.id), data["result"])
    if integrity_status == "unavailable":
        # Exactly the read path's refusal, as a raise: no ScoringResult is
        # ever constructed from data carrying a non-finite score.
        raise SnapshotUnusableError(
            f"analysis_id={analysis.id}: {jobs.HISTORICAL_SCORE_UNAVAILABLE_ERROR_CODE} "
            f"({integrity_reason}) — refusing to regenerate a document from unusable numeric data."
        )

    # Degradation is one-way (B11-T1 section 1): a fresh check may never
    # PROMOTE an already-recorded non-"ok" integrity back to "ok".
    stored_integrity = data["result"].get("data_integrity")
    if integrity_status == "ok" and isinstance(stored_integrity, str) and stored_integrity not in ("", "ok"):
        integrity_status = stored_integrity
        integrity_reason = data["result"].get("data_integrity_reason")
    data["result"]["data_integrity"] = integrity_status
    data["result"]["data_integrity_reason"] = integrity_reason

    try:
        return AOContext(**data["ao"]), ScoringResult(**data["result"])
    except Exception as exc:  # pydantic ValidationError, TypeError, ...
        raise SnapshotUnusableError(
            f"analysis_id={analysis.id}: the persisted snapshot could not be reconstructed into "
            f"AOContext/ScoringResult — refusing to substitute placeholder content."
        ) from exc


# ---------------------------------------------------------------------------
# Lookup + authorization.
# ---------------------------------------------------------------------------

def _authorized_analysis(db: Session, *, analysis_id, user_id, organization_id):
    """The ONE lookup used by both public entry points.

    Reuses the existing ownership-checked
    analyses_repo.get_by_id_for_user — never a raw db.get(Analysis, id),
    and never a third lookup of its own.
    """
    from src.web.database.repositories import analyses as analyses_repo

    try:
        analysis = analyses_repo.get_by_id_for_user(db, analysis_id, user_id)
    except Exception as exc:
        # A malformed analysis_id (not a UUID) must read as "not found",
        # never as a server error that distinguishes it from a real miss.
        raise AnalysisNotFoundError(f"analysis_id={analysis_id!r} is not a readable analysis id.") from exc
    if analysis is None:
        raise AnalysisNotFoundError(
            f"analysis_id={analysis_id} does not exist or does not belong to user_id={user_id}."
        )
    if analysis.organization_id != organization_id:
        logger.error(
            "Refusing to act on analysis_id=%s: row organization_id=%s but caller's is %s",
            analysis.id, analysis.organization_id, organization_id,
        )
        raise AnalysisOwnershipMismatchError(
            f"analysis_id={analysis.id} belongs to a different organization than the caller's context."
        )
    return analysis


def _kind_to_mime(kind: str) -> str:
    mime = _MIME_BY_KIND.get(str(kind).strip().lower())
    if mime is None:
        raise UnsupportedDocumentKindError(
            f"kind={kind!r} is not a supported document kind (expected one of {', '.join(DOCUMENT_KINDS)})."
        )
    return mime


# ---------------------------------------------------------------------------
# A. Per-document status.
# ---------------------------------------------------------------------------

def get_document_status(db: Session, *, analysis_id, user_id, organization_id) -> dict[str, str]:
    """Per-document availability for one analysis.

    Returns exactly {"pdf": <status>, "docx": <status>} where each status
    is "available" or "unavailable" — both keys always present, no other
    keys, no None.

    "available" means, and only means: an AnalysisDocument row of that mime
    type exists for this (analysis, user, organization) AND the storage
    service resolved its storage_path to a real file inside the configured
    storage roots. The check goes through
    StorageService.resolve_for_download — the very call the download route
    makes — so an "available" answer cannot be a link that 404s. Anything
    less certain (no row, empty storage_path, file deleted, path escaping
    the storage roots, storage backend erroring) is reported
    "unavailable". There is deliberately no third value: "never generated"
    and "generated then lost" are indistinguishable to a caller here, and
    both mean the same actionable thing — regenerate it.

    Raises AnalysisNotFoundError / AnalysisOwnershipMismatchError under the
    same rules as regenerate_document, so a route can map both entry points
    identically.
    """
    analysis = _authorized_analysis(db, analysis_id=analysis_id, user_id=user_id, organization_id=organization_id)

    rows = db.execute(
        select(AnalysisDocument).where(
            AnalysisDocument.analysis_id == analysis.id,
            AnalysisDocument.user_id == user_id,
            AnalysisDocument.organization_id == organization_id,
        ).order_by(AnalysisDocument.created_at.desc())
    ).scalars().all()

    by_mime: dict[str, AnalysisDocument] = {}
    for row in rows:
        if row.mime_type and row.mime_type not in by_mime:
            by_mime[row.mime_type] = row

    return {kind: (AVAILABLE if _file_is_really_there(by_mime.get(mime)) else UNAVAILABLE)
            for kind, mime in _MIME_BY_KIND.items()}


def _file_is_really_there(row: AnalysisDocument | None) -> bool:
    if row is None or not row.storage_path:
        return False
    try:
        from src.web.storage.service import get_storage_service

        get_storage_service().resolve_for_download(row.storage_path)
        return True
    except Exception:
        # FileNotFoundError (gone, or outside every storage root) and any
        # backend error alike: when in doubt, never claim "available".
        return False


# ---------------------------------------------------------------------------
# B. Regeneration.
# ---------------------------------------------------------------------------

def regenerate_document(db: Session, *, analysis_id, user_id, organization_id, kind: str) -> AnalysisDocument:
    """Rebuild one document (kind="pdf"|"docx") from the persisted snapshot
    alone and attach it to the analysis, returning the AnalysisDocument row.

    No LLM call, no RAG, no rescoring, no read of the account's CURRENT
    ScoringPolicy/ProviderProfile/capacity: every value the document shows
    — score, decision (INCOMPLET included, verbatim), scoring_completeness/
    scoring_missing, enrichment_status, data_integrity,
    rag_selection_status, the retained RAG evidence, the pinned
    scoring_policy_version — comes out of `analysis.result_data` exactly as
    it was written when the analysis ran.

    The caller owns the transaction: this function flushes but never
    commits, so a route/session_scope decides when the change becomes
    durable. Note the one ordering consequence in the contract doc: the
    PREVIOUS physical file is deleted after the row is updated, so a caller
    that rolls back afterwards loses the old file while the row reverts to
    pointing at it — the honest outcome is then "unavailable", i.e.
    regenerate again, never a partial or wrong document.
    """
    from src.web.database.repositories import analyses as analyses_repo
    from src.web.storage.service import get_storage_service

    mime_type = _kind_to_mime(kind)
    kind = str(kind).strip().lower()
    analysis = _authorized_analysis(db, analysis_id=analysis_id, user_id=user_id, organization_id=organization_id)
    ao, result = _reconstruct_from_snapshot(analysis)

    previous = analyses_repo.get_document_for_analysis(
        db, analysis_id=analysis.id, user_id=user_id, organization_id=organization_id, mime_type=mime_type,
    )
    previous_storage_path = previous.storage_path if previous is not None else None

    target = _new_document_path(analysis, user_id=user_id, kind=kind)
    written = _render(ao, result, kind=kind, target=target)

    storage = get_storage_service()
    from src.livrables.document_generator import _safe_name

    readable_stem = _safe_name(ao.titre)
    document = analyses_repo.upsert_document(
        db,
        analysis_id=analysis.id,
        user_id=user_id,
        organization_id=organization_id,
        filename=written.name,
        original_filename=(f"rapport_decision_{readable_stem}.pdf" if kind == "pdf"
                           else f"candidature_{readable_stem}.docx"),
        storage_path=storage.save(written),
        mime_type=mime_type,
        file_size=written.stat().st_size,
    )

    # The row now points at the NEW file — the old one is unreachable
    # through the application and must not linger on disk where a stale
    # reference could still serve it.
    _delete_superseded_file(storage, previous_storage_path, new_storage_path=document.storage_path)
    logger.info(
        "Regenerated %s for analysis_id=%s (user_id=%s) from its persisted snapshot — no LLM call",
        kind, analysis.id, user_id,
    )
    return document


def _new_document_path(analysis, *, user_id, kind: str) -> Path:
    """The SAME private, account-scoped layout the normal job flow uses
    (src/web/jobs.py::_generate_documents): OUTPUT_DIR/<user_id>/<job
    scope>/<random document id>.<ext>.

    `jobs.ANALYSIS_FILES_DIR` is read at CALL time, not captured at import,
    so this follows the one root the rest of the app (and the test suite's
    isolation fixtures) point at. Nothing in the path derives from
    user-controlled input: the two directory segments are a UUID from the
    session and the job/analysis id minted server-side, and the basename is
    a fresh random hex — never the AO title, never a client-supplied name.

    A fresh random basename per call is also what makes the atomic publish
    below safe: `_prepare_target` REFUSES to overwrite an existing file, so
    a regeneration never competes with, or half-overwrites, the file the
    previous one published.
    """
    from src.web import jobs

    job_scope = str(analysis.job_id) if analysis.job_id else f"analysis-{analysis.id}"
    return Path(jobs.ANALYSIS_FILES_DIR) / str(user_id) / job_scope / f"{uuid.uuid4().hex}.{kind}"


def _render(ao: AOContext, result: ScoringResult, *, kind: str, target: Path) -> Path:
    """Call the ONE existing renderer, with llm=None.

    DocumentGenerator.generate_pdf/generate_docx only ever reach for a
    model when `llm` is truthy AND `result.ai_content` is falsy; passing
    None makes an LLM call structurally impossible whatever the snapshot
    contains. Both already publish atomically — they write to
    `<target>.tmp` and os.replace() it onto `target` only after the render
    fully succeeded (_prepare_target/_finalize) — so a reader can never
    observe a partially-written document, and a failed render leaves
    nothing at `target` for a download route to serve.
    """
    from src.livrables.document_generator import DocumentGenerator

    generator = DocumentGenerator()
    try:
        if kind == "pdf":
            return Path(generator.generate_pdf(ao, result, llm=None, output_path=target))
        return Path(generator.generate_docx(ao, result, llm=None, output_path=target))
    except Exception as exc:
        logger.exception("Regeneration render failed kind=%s target=%s", kind, target)
        _remove_quietly(target.with_name(target.name + ".tmp"))
        _remove_quietly(target)
        raise DocumentRenderingError(f"Rendering the {kind} document failed.") from exc


def _delete_superseded_file(storage, previous_storage_path: Any, *, new_storage_path: Any) -> None:
    """Remove the physical file the PREVIOUS row pointed at, once the row
    points somewhere else. Never raises: an orphaned file is a cleanup
    nuisance, not a reason to fail a regeneration the user already has."""
    if not previous_storage_path or previous_storage_path == new_storage_path:
        return
    try:
        storage.delete(previous_storage_path)
    except Exception:
        logger.warning(
            "Could not delete the superseded document file %s — the database already points at the new one",
            previous_storage_path,
        )


def _remove_quietly(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError:
        logger.warning("Could not clean up the partial render artifact %s", path)
