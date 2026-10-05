"""Pure data-access functions for `analyses` and `analysis_documents`.

Callers (src/web/jobs.py) are responsible for turning AOContext/ScoringResult
into plain fields before calling create_analysis — this module has no
knowledge of the pipeline's business models, only of the SQL rows.

B11-T1: `upsert_analysis` / `upsert_document` are the write entry points the
analysis flow now uses — see docs/api/B11_T1_PERSISTENCE_CONTRACT.md. They
exist because a persistence attempt can legitimately happen twice for the
same job (the analysis snapshot is now saved BEFORE rendering and refreshed
after it, and a future regeneration action will re-attach documents): a
retry must reconcile the existing row, never create a second one and never
touch a row that belongs to someone else.
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.web.database.models import Analysis, AnalysisDocument


class AnalysisOwnershipConflict(RuntimeError):
    """Raised by upsert_analysis when a row already exists for this job_id
    but belongs to a different user/organization than the caller's own.

    Structurally impossible through the normal flow (job_id is minted per
    job by src/web/jobs.py::create_job and `analyses.job_id` is UNIQUE at
    the database level), so this is a defensive refusal, never an expected
    user-facing state: the alternative would be silently overwriting
    another account's analysis. The caller must treat it as a persistence
    FAILURE, never as a successful write."""


def _row_fields(
    *,
    result_data: dict[str, Any],
    title: str | None,
    client_name: str | None,
    sector: str | None,
    score: float | None,
    decision: str | None,
    budget: str | None,
    technologies: list[str] | None,
    summary_data: dict[str, Any] | None,
    parent_job_id: str | None = None,
    origin_job_id: str | None = None,
) -> dict[str, Any]:
    """The single place the column-level normalization (length truncation,
    ""->None) is defined — shared by the INSERT and the UPDATE branch so
    the two can never drift apart (B11-T1 section 4)."""
    return {
        "title": (title or "")[:500] or None,
        "client_name": (client_name or "")[:255] or None,
        "sector": (sector or "")[:100] or None,
        "score": score,
        "decision": (decision or "")[:30] or None,
        "budget": (str(budget) if budget is not None else None),
        "technologies": technologies or None,
        "result_data": result_data,
        "summary_data": summary_data,
        # Lot 49: additive — None for every analysis before this lot and for every ordinary (non-revision)
        # analysis. Set once at creation only; upsert_analysis's UPDATE branch (a re-save of the SAME job,
        # e.g. the post-rendering refresh) must never move an existing row from one parent to another, so it
        # is deliberately excluded from the fields copied there — see upsert_analysis below.
        "parent_job_id": parent_job_id,
        # Lot 50 bis §3: same INSERT-only discipline as `parent_job_id` above, but for a documentary
        # re-analysis (never a declarative revision) — see upsert_analysis below.
        "origin_job_id": origin_job_id,
    }


def create_analysis(
    db: Session,
    *,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    result_data: dict[str, Any],
    job_id: str | None = None,
    title: str | None = None,
    client_name: str | None = None,
    sector: str | None = None,
    score: float | None = None,
    decision: str | None = None,
    budget: str | None = None,
    technologies: list[str] | None = None,
    summary_data: dict[str, Any] | None = None,
) -> Analysis:
    """Plain INSERT. Still used by tooling that knows the row cannot exist
    yet (scripts/migrate_history_to_postgresql.py checks get_by_job_id
    itself first) and by tests building fixtures directly. The analysis
    FLOW uses upsert_analysis instead — see this module's docstring."""
    analysis = Analysis(
        user_id=user_id,
        organization_id=organization_id,
        job_id=job_id,
        **_row_fields(
            result_data=result_data, title=title, client_name=client_name, sector=sector,
            score=score, decision=decision, budget=budget, technologies=technologies,
            summary_data=summary_data,
        ),
    )
    db.add(analysis)
    db.flush()
    return analysis


def upsert_analysis(
    db: Session,
    *,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    job_id: str,
    result_data: dict[str, Any],
    title: str | None = None,
    client_name: str | None = None,
    sector: str | None = None,
    score: float | None = None,
    decision: str | None = None,
    budget: str | None = None,
    technologies: list[str] | None = None,
    summary_data: dict[str, Any] | None = None,
    parent_job_id: str | None = None,
    origin_job_id: str | None = None,
) -> Analysis:
    """Insert the analysis row for `job_id`, or update it in place if it
    already exists. Idempotent by job_id: calling this twice for the same
    job produces exactly ONE row, carrying the SECOND call's values.

    `job_id` is required here (unlike create_analysis, where it is
    optional): it IS the idempotency key, and there is nothing to reconcile
    against without it.

    Raises AnalysisOwnershipConflict if the existing row's user_id or
    organization_id differs from the caller's — never silently attaches to
    or overwrites another owner's analysis (B11-T1 section 4). Ownership
    columns of an existing row are never rewritten by this function.

    Lot 49: `parent_job_id` is set ONLY on the INSERT branch (the row's
    lineage, fixed at creation) — the UPDATE branch (the post-rendering
    refresh of the SAME job, see src/web/jobs.py::_save_analysis_snapshot's
    two calls) never touches it, so a second upsert for the same job_id can
    never move an already-created revision away from its parent, whatever
    `parent_job_id` the caller happens to pass (normally the same value, or
    left at its default None by a caller that doesn't track revisions).
    Lot 50 bis §3: `origin_job_id` follows the exact same INSERT-only rule.
    """
    if not job_id:
        raise ValueError("upsert_analysis requires a non-empty job_id — it is the idempotency key.")

    fields = _row_fields(
        result_data=result_data, title=title, client_name=client_name, sector=sector,
        score=score, decision=decision, budget=budget, technologies=technologies,
        summary_data=summary_data, parent_job_id=parent_job_id, origin_job_id=origin_job_id,
    )
    existing = get_by_job_id(db, job_id)
    if existing is None:
        analysis = Analysis(user_id=user_id, organization_id=organization_id, job_id=job_id, **fields)
        db.add(analysis)
        db.flush()
        return analysis

    if existing.user_id != user_id or existing.organization_id != organization_id:
        raise AnalysisOwnershipConflict(
            f"analyses.job_id={job_id} already belongs to another owner — refusing to overwrite it."
        )
    fields.pop("parent_job_id", None)
    fields.pop("origin_job_id", None)
    for key, value in fields.items():
        setattr(existing, key, value)
    db.flush()
    return existing


def get_by_parent_job_id(db: Session, parent_job_id: str) -> Analysis | None:
    """Lot 49: the revision (if any) that already completes `parent_job_id` — used to refuse a second,
    concurrent/stale completion attempt on the same analysis (409) instead of creating a second revision."""
    stmt = select(Analysis).where(Analysis.parent_job_id == parent_job_id)
    return db.execute(stmt).scalar_one_or_none()


def list_by_origin_job_id(db: Session, origin_job_id: str) -> list[Analysis]:
    """Lot 50 bis §3 — every documentary re-analysis ("Ajouter les pièces restantes") built from
    `origin_job_id`, most recent first. Deliberately a LIST, not a single row (unlike `get_by_parent_job_id`'s
    at-most-one revision): `origin_job_id` is not unique, an account may add pieces more than once."""
    stmt = select(Analysis).where(Analysis.origin_job_id == origin_job_id).order_by(Analysis.created_at.desc())
    return list(db.execute(stmt).scalars().all())


def get_by_job_id(db: Session, job_id: str) -> Analysis | None:
    stmt = select(Analysis).where(Analysis.job_id == job_id)
    return db.execute(stmt).scalar_one_or_none()


def get_by_id_for_user(db: Session, analysis_id: uuid.UUID | str, user_id: uuid.UUID) -> Analysis | None:
    """Ownership-checked lookup — never returns another user's analysis."""
    stmt = select(Analysis).where(Analysis.id == analysis_id, Analysis.user_id == user_id)
    return db.execute(stmt).scalar_one_or_none()


def get_by_job_id_for_user(db: Session, job_id: str, user_id: uuid.UUID) -> Analysis | None:
    """Ownership-checked lookup by job_id — never returns another user's analysis."""
    stmt = select(Analysis).where(Analysis.job_id == job_id, Analysis.user_id == user_id)
    return db.execute(stmt).scalar_one_or_none()


def list_for_user(db: Session, user_id: uuid.UUID, organization_id: uuid.UUID, limit: int = 200) -> list[Analysis]:
    """Kept for callers that only ever want "the N most recent" (the
    /app/analyser and /app/resultats sidebar panels) — a genuine bound on
    what a SIDEBAR needs to show, not a substitute for real pagination or
    for statistics over the full authorized set. See list_for_user_page /
    count_for_user / decision_counts_for_user below for those (B22-T1).

    B22-T2 (DEFECT confirmed): this used to filter by user_id ALONE — the
    same person's analyses under every organization they belong to were
    aggregated together, with no way to see only the currently selected
    organization's own history. `organization_id` is now REQUIRED (not
    optional) so no caller can silently forget to scope it; it must come
    from a server-resolved, membership-checked value (src.web.auth.
    access_context.get_access_context), never a client-supplied id trusted
    as-is."""
    stmt = (
        select(Analysis)
        .where(Analysis.user_id == user_id, Analysis.organization_id == organization_id)
        .order_by(Analysis.created_at.desc())
        .limit(limit)
    )
    return list(db.execute(stmt).scalars().all())


def list_for_user_page(db: Session, user_id: uuid.UUID, organization_id: uuid.UUID, *, limit: int, offset: int) -> list[Analysis]:
    """B22-T1 (DEFECT confirmed): the previous only listing available
    (list_for_user above) hardcoded `limit: int = 200` with NO offset —
    an account's 201st analysis onward was silently unreachable from the
    history page, the export, and the sidebar stats, which all called it.

    Deterministic, bounded pagination: ordered by (created_at DESC, id DESC)
    — the id tiebreaker is required for determinism (and therefore for "no
    duplicate/missing row across pages") whenever two rows share the exact
    same created_at, which a synthetic/bulk-seeded or fast-arriving-real
    dataset can genuinely produce; created_at alone does not guarantee a
    stable order in that case.

    B22-T2: `organization_id` scopes every page to ONE organization — see
    list_for_user's own docstring above for why this is required, not
    optional."""
    stmt = (
        select(Analysis)
        .where(Analysis.user_id == user_id, Analysis.organization_id == organization_id)
        .order_by(Analysis.created_at.desc(), Analysis.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(db.execute(stmt).scalars().all())


def count_for_user(db: Session, user_id: uuid.UUID, organization_id: uuid.UUID) -> int:
    """A real SQL COUNT(*) — never derived from a page/slice of rows
    already loaded into Python (B22-T1's core fix: the previous sidebar
    stats counted from a `limit=200`-capped list, silently wrong beyond
    200 analyses). B22-T2: scoped to one organization, same as
    list_for_user_page above."""
    stmt = (
        select(func.count()).select_from(Analysis)
        .where(Analysis.user_id == user_id, Analysis.organization_id == organization_id)
    )
    return int(db.execute(stmt).scalar_one())


def decision_counts_for_user(db: Session, user_id: uuid.UUID, organization_id: uuid.UUID) -> dict[str, int]:
    """SQL GROUP BY aggregate over the user's ENTIRE authorized set — the
    sidebar's GO/RESERVE/NO-GO counters no longer depend on how many rows
    happened to fit in whatever page/limit was loaded for display. Keys are
    the raw stored `decision` strings (including `None`/"" for a job with
    no computed decision yet) — the caller (history_service.sidebar_stats)
    already knows how to classify "RESERVE" as a substring match, so the
    raw counts are handed over unclassified rather than this repository
    guessing at business categories. B22-T2: scoped to one organization,
    same as list_for_user_page above."""
    stmt = (
        select(Analysis.decision, func.count())
        .where(Analysis.user_id == user_id, Analysis.organization_id == organization_id)
        .group_by(Analysis.decision)
    )
    return {(decision or ""): count for decision, count in db.execute(stmt).all()}


def add_document(
    db: Session,
    *,
    analysis_id: uuid.UUID,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    filename: str | None,
    original_filename: str | None,
    storage_path: str,
    mime_type: str | None,
    file_size: int | None,
) -> AnalysisDocument:
    """`organization_id` must equal the parent analysis's own organization_id
    — the composite FK on analysis_documents (see models.py) rejects the
    insert at the database level otherwise, it isn't just a convention."""
    doc = AnalysisDocument(
        analysis_id=analysis_id,
        user_id=user_id,
        organization_id=organization_id,
        filename=filename,
        original_filename=original_filename,
        storage_path=storage_path,
        mime_type=mime_type,
        file_size=file_size,
    )
    db.add(doc)
    db.flush()
    return doc


def get_document_for_analysis(
    db: Session, *, analysis_id: uuid.UUID, user_id: uuid.UUID, organization_id: uuid.UUID, mime_type: str
) -> AnalysisDocument | None:
    """Ownership-checked lookup of the most recent document of a given type
    for an analysis — never returns a document belonging to another user or
    a stale/mismatched organization, even if `analysis_id` were somehow
    guessed correctly."""
    stmt = (
        select(AnalysisDocument)
        .where(
            AnalysisDocument.analysis_id == analysis_id,
            AnalysisDocument.user_id == user_id,
            AnalysisDocument.organization_id == organization_id,
            AnalysisDocument.mime_type == mime_type,
        )
        .order_by(AnalysisDocument.created_at.desc())
    )
    return db.execute(stmt).scalars().first()


def upsert_document(
    db: Session,
    *,
    analysis_id: uuid.UUID,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    filename: str | None,
    original_filename: str | None,
    storage_path: str,
    mime_type: str | None,
    file_size: int | None,
) -> AnalysisDocument:
    """Attach a document to an analysis, replacing in place the existing
    document of the SAME mime_type for that analysis if there is one.
    Idempotent per (analysis_id, mime_type): a re-rendered PDF updates the
    existing row rather than adding a second one (B11-T1 section 4).

    This is the function a regeneration action (B19-T2) should call.

    Deliberately an APPLICATION-level reconciliation, with NO new database
    constraint: the existing ownership-scoped get_document_for_analysis
    lookup already answers "is there one of these?", the composite FK on
    (analysis_id, organization_id) already makes a cross-organization
    document impossible at the DB level, and every writer of this table is
    serialized per analysis (one worker thread per job; a user-initiated
    regeneration is one request). A UniqueConstraint("analysis_id",
    "mime_type") would only additionally close a genuinely CONCURRENT
    double-attach of the same analysis, which the application never
    produces today — see docs/api/B11_T1_PERSISTENCE_CONTRACT.md for the
    condition under which that constraint should be added.

    `mime_type=None` cannot be reconciled (there is no key to match on) and
    is always inserted — never guessed to be "the same document".
    """
    existing = (
        get_document_for_analysis(
            db, analysis_id=analysis_id, user_id=user_id,
            organization_id=organization_id, mime_type=mime_type,
        )
        if mime_type
        else None
    )
    if existing is None:
        return add_document(
            db, analysis_id=analysis_id, user_id=user_id, organization_id=organization_id,
            filename=filename, original_filename=original_filename, storage_path=storage_path,
            mime_type=mime_type, file_size=file_size,
        )
    existing.filename = filename
    existing.original_filename = original_filename
    existing.storage_path = storage_path
    existing.file_size = file_size
    db.flush()
    return existing
