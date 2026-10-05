"""JSON API — thin routes that only validate input and call existing services.

No scoring, RAG or business logic lives in this file: every route either
delegates to src/web/jobs.py (which itself only orchestrates the existing
src/agents, src/rag and src/livrables modules) or to a repository/service
under src/web. Lot 50 bis §1: the two dossier-intake routes below construct
the account's configured `LLMClient` (the SAME adapter `jobs.py` uses) and
hand it to `src.web.ao_dossier.intake` — the actual judgment logic still
lives entirely in `src/agents/document_*_agent.py`, this file only wires the
already-existing client through, exactly like it wires a database session.

V3: every route below is gated behind an authenticated, active Starter
user (require_active_starter_user) except /api/contact, which anonymous
prospects use. Analysis-scoped routes additionally verify ownership —
a user can only ever see their own jobs/downloads.
"""
from __future__ import annotations

import csv
import io
import uuid as uuid_module
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.orm import Session

from src.core.logger import get_agent_logger
from src.rag import private_rag_manager
from src.web import analyze_input_service, completion_service, document_rendering_service, examples_service, job_executor, jobs
from src.web.ao_dossier import intake as dossier_intake
from src.web.ao_dossier import limits as dossier_limits
from src.web.ao_dossier import service as dossier_service
from src.web.auth.access_context import AccessContext, get_access_context, require_active_membership, require_permission
from src.web.auth.dependencies import require_active_starter_user
from src.web.database.models import User
from src.web.database.repositories import analyses as analyses_repo
from src.web.database.repositories import analysis_complements as complements_repo
from src.web.database.repositories import ao_dossiers as dossiers_repo
from src.web.database.repositories import contacts as contacts_repo
from src.web.database.repositories import private_capacity as private_capacity_repo
from src.web.database.repositories import scoring_policy as scoring_policy_repo
from src.core import config
from src.web.database.session import get_db
from src.web.security.csrf import require_csrf
from src.web.security.rate_limit import RateLimitExceeded, check_and_record
from src.web.services import history_service
from src.web.services.email_service import notify_new_contact_request
from src.web.storage.service import get_storage_service

router = APIRouter(prefix="/api")
logger = get_agent_logger("web_api")


@router.get("/examples")
def api_list_examples(current_user: User = Depends(require_active_starter_user)):
    return {"examples": examples_service.list_examples()}


@router.get("/examples/{example_id}")
def api_read_example(example_id: str, current_user: User = Depends(require_active_starter_user)):
    text = examples_service.read_example(example_id)
    if text is None:
        raise HTTPException(404, "Exemple introuvable.")
    return {"id": example_id, "text": text}


def _input_error(exc: analyze_input_service.AnalyzeInputError) -> HTTPException:
    """The structured body of every input refusal: error_code + message (+ the piece / category / limit when the
    refusal concerns a dossier)."""
    return HTTPException(exc.http_status, {"error_code": exc.error_code, "message": exc.message, **getattr(exc, "details", {})})


def _require_private_configuration(db: Session, ctx: AccessContext) -> None:
    """B03 / B06-T1: never score against demo/global defaults — an account that has not configured its own capacity
    and an ACTIVE scoring policy is refused up front, before any LLM call (409, in this order)."""
    plan = private_capacity_repo.get_for_owner(db, organization_id=ctx.organization_id, owner_user_id=ctx.user.id)
    if plan is None or plan.status != "configured":
        raise HTTPException(409, {
            "error_code": "CAPACITY_NOT_CONFIGURED",
            "message": "Configurez votre capacité (charge, projets en cours) avant de lancer une analyse.",
        })
    if scoring_policy_repo.get_active(db, organization_id=ctx.organization_id, owner_user_id=ctx.user.id) is None:
        raise HTTPException(409, {
            "error_code": "SCORING_NOT_CONFIGURED",
            "message": "Configurez et activez votre politique de scoring avant de lancer une analyse.",
        })


def _start_job(job: "jobs.Job", content: str, dossier_id=None) -> None:
    """B12-T1: the bounded worker pool; a saturated queue is refused explicitly (429), never accepted onto an
    unbounded backlog. See docs/api/B12_T1_JOB_EXECUTOR_CONTRACT.md."""
    try:
        jobs.start_analysis(job, content, dossier_id=dossier_id)
    except job_executor.JobQueueSaturatedError:
        raise HTTPException(429, {
            "error_code": "JOB_QUEUE_SATURATED",
            "message": "Le service est actuellement saturé. Réessayez dans quelques instants.",
        })


def _start_revision(job: "jobs.Job", spec: "jobs.RevisionSpec") -> None:
    """Lot 49 — same bounded-queue guard as _start_job, for a completion revision."""
    try:
        jobs.start_revision(job, spec)
    except job_executor.JobQueueSaturatedError:
        raise HTTPException(429, {
            "error_code": "JOB_QUEUE_SATURATED",
            "message": "Le service est actuellement saturé. Réessayez dans quelques instants.",
        })


@router.get("/analyze/dossier-limits")
def api_dossier_limits(current_user: User = Depends(require_active_starter_user)):
    """The effective limits of an AO dossier (slots, counts, decimal Mo) — the same values the page shows."""
    return dossier_limits.dossier_limits()


@router.post("/analyze")
async def api_analyze(
    request: Request,
    mode: str = Form(...),
    example_id: Optional[str] = Form(None),
    text: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
    ctx: AccessContext = Depends(require_permission("analysis:create")),
    db: Session = Depends(get_db),
):
    """One analysis from a pasted text, a stock example, ONE file (historical clients) or — `mode=dossier` (lot 47 bis)
    — an AO DOSSIER: rc / cctp / ccap / acte_engagement (one file each) + annexes (three at most), 100 Mo of files at
    most in total. The two families of fields are never mixed (400 AMBIGUOUS_INPUT); an unknown category or an
    extra file is refused, never ignored. Every piece is validated BEFORE the job exists: a refusal designates the
    piece and no partial analysis is ever started."""
    # B14-T1 (DEFECT confirmed, CSRF gap): a cookie-authenticated mutation reachable via a plain cross-origin multipart
    # <form> submit — checked first, before any other work. See docs/api/B14_T1_CSRF_RATE_LIMIT_CONTRACT.md.
    require_csrf(request, request.headers.get("X-CSRF-Token"))

    # Every part, read once (Starlette caches the parsed form): the route must see the file parts the declared
    # parameters above do not name, or an extra file would be dropped silently.
    items = list((await request.form()).multi_items())
    received = None
    keep_dossier = False
    try:
        try:
            dossier_intake.check_no_stray_files(items)
            if mode == "dossier":
                if dossier_intake.legacy_input_present(items, text=text, example_id=example_id):
                    raise dossier_intake.DossierError(
                        "AMBIGUOUS_INPUT", "Mode dossier : n'envoyez ni texte, ni exemple, ni fichier unique en plus des pièces du dossier.")
                from src.agents.llm_client import ClaudeClient
                received = await dossier_intake.receive_dossier(items, organization_id=ctx.organization_id, user_id=ctx.user.id, llm=ClaudeClient())
                content = ""  # the worker reads the dossier back from the database and the private storage
            else:
                if dossier_intake.dossier_files_present(items):
                    raise dossier_intake.DossierError(
                        "AMBIGUOUS_INPUT", "Les pièces d'un dossier ne s'utilisent qu'avec le mode « dossier » : ne les mélangez pas avec un autre mode.")
                # B13-T1 (DEFECT confirmed): validated BEFORE any costly work — bounded reads, format/content
                # cross-check, bounded pasted/extracted text (docs/api/B13_T1_INPUT_VALIDATION_CONTRACT.md).
                content = await analyze_input_service.resolve_analyze_input(mode=mode, example_id=example_id, text=text, file=file)
        except analyze_input_service.AnalyzeInputError as exc:
            raise _input_error(exc)

        if received is not None:
            source_label = dossier_service.source_label(received.pieces)
        elif mode == "stock":
            source_label = f"Exemple : {(example_id or '').replace('_', ' ')}"
        elif mode == "upload":
            source_label = f"Fichier : {file.filename}"
        else:
            source_label = "Texte collé"

        _require_private_configuration(db, ctx)

        job = jobs.create_job(source_label=source_label, user_id=ctx.user.id, organization_id=ctx.organization_id)
        if received is not None:
            dossier = dossier_service.commit(db, received)
            dossiers_repo.link_job(db, dossier, job.id)
            db.commit()
            keep_dossier = True
        try:
            _start_job(job, content, dossier_id=received.dossier_id if received is not None else None)
        except HTTPException:
            if received is not None:  # a refused (saturated) submission leaves no dossier behind
                keep_dossier = False
                dossiers_repo.delete_dossier(db, dossiers_repo.get_for_owner(
                    db, dossier_id=received.dossier_id, organization_id=ctx.organization_id, user_id=ctx.user.id))
                db.commit()
            raise
        return {"job_id": job.id}
    finally:
        if received is not None and not keep_dossier:
            received.discard()


@router.post("/analyze/dossier-preview")
async def api_dossier_preview(
    request: Request, ctx: AccessContext = Depends(require_permission("analysis:create")), db: Session = Depends(get_db),
):
    """Lot 50 §3 — receives and vets a WHOLE dossier (structure/bytes/format/security/classification/
    relevance) EXACTLY like `POST /api/analyze mode=dossier`, but does not start a job: it persists a
    'staging' dossier (expires after `config.DOSSIER_STAGING_TTL_SECONDS`) and returns the per-file
    admission table for the user to review before `POST .../dossier-preview/{id}/confirm`. A structural
    refusal (wrong slot, too many files, byte budget) behaves exactly as the direct route — nothing is kept."""
    require_csrf(request, request.headers.get("X-CSRF-Token"))
    items = list((await request.form()).multi_items())
    received = None
    staged = False
    try:
        try:
            dossier_intake.check_no_stray_files(items)
            from src.agents.llm_client import ClaudeClient
            received = await dossier_intake.receive_dossier_preview(items, organization_id=ctx.organization_id, user_id=ctx.user.id, llm=ClaudeClient())
        except analyze_input_service.AnalyzeInputError as exc:
            raise _input_error(exc)
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=config.DOSSIER_STAGING_TTL_SECONDS)
        dossier = dossiers_repo.create_staging(
            db, dossier_id=received.dossier_id, organization_id=ctx.organization_id, user_id=ctx.user.id,
            total_bytes=received.total_bytes, pieces=[p.row() for p in received.pieces], expires_at=expires_at,
        )
        db.commit()
        staged = True
        return dossier_service.preview_summary(dossier, rejected=received.rejected)
    finally:
        if received is not None and not staged:
            received.discard()


_STAGING_DECISION_BOOL_DEFAULT = True


@router.post("/analyze/dossier-preview/{dossier_id}/confirm")
def api_dossier_preview_confirm(
    request: Request, dossier_id: str, payload: dict,
    ctx: AccessContext = Depends(require_permission("analysis:create")), db: Session = Depends(get_db),
):
    """Lot 50 §3 — confirms the final admitted set of a staged dossier and starts the analysis, exactly like
    the direct dossier route from there on. Re-verifies, at THIS instant: the staging dossier still exists
    and has not expired (410), every currently-staged piece is covered by exactly one decision carrying the
    EXACT content hash this preview showed (409 on any mismatch or omission — a stale/tampered preview is
    refused wholesale, no partial write), and a security 'blocked' piece can never be forced admitted
    (server-enforced regardless of what the client sends)."""
    require_csrf(request, request.headers.get("X-CSRF-Token"))
    try:
        dossier_uuid = uuid_module.UUID(dossier_id)
    except ValueError:
        raise HTTPException(404, "Aperçu introuvable.")
    dossier = dossiers_repo.get_staging_for_owner(db, dossier_id=dossier_uuid, organization_id=ctx.organization_id, user_id=ctx.user.id)
    if dossier is None:
        raise HTTPException(404, "Aperçu introuvable.")
    _require_private_configuration(db, ctx)  # checked BEFORE any piece decision is applied, same order as the direct route
    now = datetime.now(timezone.utc)
    expires_at = dossier.staging_expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at is None or now > expires_at:
        dossiers_repo.discard_staging(db, dossier)
        db.commit()
        raise HTTPException(410, {"error_code": "PREVIEW_EXPIRED", "message": "Cet aperçu a expiré : soumettez de nouveau les pièces du dossier."})

    decisions = payload.get("decisions")
    if not isinstance(decisions, list) or not decisions:
        raise HTTPException(422, {"error_code": "INVALID_VALUE", "message": "« decisions » doit être une liste non vide."})
    by_id = {}
    for raw in decisions:
        if not isinstance(raw, dict) or "piece_id" not in raw or "content_hash" not in raw:
            raise HTTPException(422, {"error_code": "INVALID_VALUE", "message": "Chaque décision doit porter {piece_id, content_hash}."})
        by_id[str(raw["piece_id"])] = raw

    current_ids = {str(p.id) for p in dossier.pieces}
    if set(by_id.keys()) != current_ids:
        raise HTTPException(409, {
            "error_code": "STALE_PREVIEW",
            "message": "La sélection ne correspond plus à l'aperçu actuel (pièce ajoutée, manquante ou différente) : "
                        "rechargez l'aperçu avant de confirmer. Rien n'a été écrit.",
        })
    for piece in dossier.pieces:
        decision = by_id[str(piece.id)]
        if str(decision["content_hash"]) != piece.content_hash:
            raise HTTPException(409, {
                "error_code": "STALE_PREVIEW",
                "message": f"La pièce « {piece.display_name} » a changé depuis l'aperçu : rechargez l'aperçu avant de confirmer. Rien n'a été écrit.",
            })

    for piece in dossier.pieces:
        decision = by_id[str(piece.id)]
        include = bool(decision.get("include", _STAGING_DECISION_BOOL_DEFAULT))
        category_final = decision.get("category_final") or piece.category_final or piece.category
        if category_final not in dossier_limits.CATEGORIES:
            raise HTTPException(422, {"error_code": "INVALID_VALUE", "message": f"Catégorie inconnue : {category_final!r}."})
        link_note = decision.get("link_note")
        if link_note is not None and (not isinstance(link_note, str) or len(link_note) > 500):
            raise HTTPException(422, {"error_code": "INVALID_VALUE", "message": "« link_note » doit être un texte de 500 caractères maximum."})
        if piece.security_state == "blocked":
            # A security block is never lifted by a client-sent decision, whatever it says (§3).
            piece.admitted = False
            piece.exclusion_reason = piece.exclusion_reason or piece.security_reason
        else:
            piece.admitted = include
            piece.exclusion_reason = None if include else (decision.get("reason") or piece.exclusion_reason or "Exclue par l'utilisateur.")
        piece.category_final = category_final
        piece.user_link_note = link_note

    usable = [p for p in dossier.pieces if p.admitted and p.duplicate_of_piece_id is None]
    if not usable:
        db.rollback()
        raise HTTPException(422, {
            "error_code": "DOSSIER_NO_USABLE_PIECE",
            "message": "Au moins une pièce sûre et exploitable doit être retenue pour lancer l'analyse.",
        })

    # Lot 50 bis §1 — the SAME whole-dossier scope gate the legacy direct-submit route already enforces at
    # intake (`dossier_intake.receive_dossier`), judged here on the FINAL admitted set: never defer this to
    # the job itself (see dossier_service.check_admitted_scope's own docstring for the exact defect this closes).
    scope_verdict = dossier_service.check_admitted_scope(dossier)
    if not scope_verdict.allowed:
        db.rollback()
        raise HTTPException(422, {
            "error_code": "CONTENT_BLOCKED",
            "message": "Le contenu retenu du dossier n'a pas passé les contrôles de sécurité : aucune pièce retenue n'a été reconnue "
                        "comme liée à un appel d'offres (ni par son vocabulaire, ni par un élément commun avec les autres pièces), "
                        "ou une instruction parasite a été détectée.",
            "reasons": list(scope_verdict.reason_codes),
        })

    categories_missing = sorted(c for c in dossier_limits.MAIN_CATEGORIES if not any(p.category_final == c and p.admitted for p in dossier.pieces))
    dossiers_repo.confirm_staging(
        db, dossier, confirmed_by_user_id=ctx.user.id, categories_missing=categories_missing, scope_limited=bool(categories_missing),
    )
    # Lot 50 bis §3: a staging dossier created by "Ajouter les pièces restantes" carries the job it extends —
    # propagated to the NEW analysis as `origin_job_id` (never `parent_job_id`, reserved for a lot-49-bis
    # declarative revision): a genuinely NEW extraction/scoring, never the frozen inputs of a revision.
    source_label = (
        f"Dossier complété (à partir de {dossier.origin_job_id})" if dossier.origin_job_id
        else dossier_service.source_label(dossier.pieces)
    )
    job = jobs.create_job(source_label=source_label, user_id=ctx.user.id, organization_id=ctx.organization_id)
    if dossier.origin_job_id:
        job.origin_job_id = dossier.origin_job_id
    dossiers_repo.link_job(db, dossier, job.id)
    db.commit()
    _start_job(job, "", dossier_id=dossier.id)
    return {"job_id": job.id, "categories_manquantes": categories_missing, "perimetre_limite": bool(categories_missing)}


@router.post("/analyze/{job_id}/add-pieces/preview")
async def api_add_pieces_preview(
    request: Request, job_id: str, ctx: AccessContext = Depends(require_permission("analysis:create")), db: Session = Depends(get_db),
):
    """Lot 50 bis §3 — "Ajouter les pièces restantes": prepares a NEW documentary re-analysis of `job_id`'s
    own dossier — a genuinely fresh extraction/scoring (current policy/profile/capacity), never the frozen
    inputs of a lot-49-bis declarative revision, and never a mutation of `job_id` itself (its result and
    deliverables are untouched either way). Every currently-admitted original piece is re-verified (still on
    disk, same hash) before being offered back — never reconstructed if missing/modified — and combined with
    any newly uploaded pieces through the SAME preview/admission table `POST /api/analyze/dossier-preview`
    itself uses (`POST /api/analyze/dossier-preview/{id}/confirm` also confirms THIS kind of staging dossier)."""
    require_csrf(request, request.headers.get("X-CSRF-Token"))
    origin_job = jobs.get_job(job_id)
    if origin_job is None or origin_job.user_id != ctx.user.id:
        raise HTTPException(404, "Analyse introuvable.")
    require_active_membership(db, user_id=ctx.user.id, organization_id=origin_job.organization_id, not_found_detail="Analyse introuvable.")
    if origin_job.status != "done" or origin_job.result is None:
        raise HTTPException(409, {"error_code": "JOB_NOT_COMPLETE", "message": "Cette analyse n'est pas terminée : rien à compléter."})
    original_dossier = dossiers_repo.get_by_job(db, job_id=job_id, organization_id=ctx.organization_id, user_id=ctx.user.id)
    if original_dossier is None:
        raise HTTPException(404, {"error_code": "NOT_A_DOSSIER_ANALYSIS", "message": "Cette analyse ne provient pas d'un dossier : rien à compléter par pièces."})

    form = await request.form()
    items = list(form.multi_items())
    keep_raw = form.get("keep_piece_ids")
    keep_ids: Optional[set[str]] = None
    if keep_raw:
        import json as _json
        try:
            keep_ids = {str(x) for x in _json.loads(keep_raw)}
        except (ValueError, TypeError):
            raise HTTPException(422, {"error_code": "INVALID_VALUE", "message": "« keep_piece_ids » doit être une liste JSON d'identifiants."})
    original_pieces = [p for p in original_dossier.pieces if keep_ids is None or str(p.id) in keep_ids]
    carried_over, rejected_carry = dossier_intake.reverify_pieces_for_carry_over(original_pieces)

    received = None
    staged = False
    try:
        try:
            dossier_intake.check_no_stray_files(items)
            from src.agents.llm_client import ClaudeClient
            received = await dossier_intake.receive_dossier_preview(
                items, organization_id=ctx.organization_id, user_id=ctx.user.id, llm=ClaudeClient(), carried_over=carried_over,
            )
        except analyze_input_service.AnalyzeInputError as exc:
            raise _input_error(exc)
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=config.DOSSIER_STAGING_TTL_SECONDS)
        dossier = dossiers_repo.create_staging(
            db, dossier_id=received.dossier_id, organization_id=ctx.organization_id, user_id=ctx.user.id,
            total_bytes=received.total_bytes, pieces=[p.row() for p in received.pieces], expires_at=expires_at,
            origin_job_id=job_id,
        )
        db.commit()
        staged = True
        summary = dossier_service.preview_summary(dossier, rejected=received.rejected + rejected_carry)
        summary["origin_job_id"] = job_id
        return summary
    finally:
        if received is not None and not staged:
            received.discard()


@router.get("/analyze/{job_id}/dossier")
def api_analyze_dossier(job_id: str, ctx: AccessContext = Depends(get_access_context), db: Session = Depends(get_db)):
    """The validated dossier an analysis was made from — read from the database, so it is still there after a restart.
    Public description only (categories, display names, formats, sizes, hashes): never a storage path. Another
    account's dossier is indistinguishable from a missing one."""
    dossier = dossiers_repo.get_by_job(db, job_id=job_id, organization_id=ctx.organization_id, user_id=ctx.user.id)
    if dossier is None:
        raise HTTPException(404, "Dossier introuvable.")
    return dossier_service.summary(dossier)


@router.post("/analyze/{job_id}/resume")
def api_analyze_resume(
    request: Request, job_id: str, ctx: AccessContext = Depends(require_permission("analysis:create")), db: Session = Depends(get_db),
):
    """Explicit relaunch of an interrupted / failed analysis ON THE SAME VALIDATED DOSSIER (no re-upload): a NEW job
    reads the dossier back from the database and the private storage. A running or finished job is refused (409)."""
    require_csrf(request, request.headers.get("X-CSRF-Token"))
    dossier = dossiers_repo.get_by_job(db, job_id=job_id, organization_id=ctx.organization_id, user_id=ctx.user.id)
    if dossier is None:
        raise HTTPException(404, "Dossier introuvable.")
    previous = jobs.get_job(job_id)
    if previous is not None and previous.status == "running":
        raise HTTPException(409, {"error_code": "JOB_STILL_RUNNING", "message": "Cette analyse est encore en cours."})
    if previous is not None and previous.status == "done" and previous.result is not None:
        raise HTTPException(409, {"error_code": "JOB_ALREADY_DONE", "message": "Cette analyse est déjà terminée : ouvrez son résultat."})
    _require_private_configuration(db, ctx)
    job = jobs.create_job(source_label=dossier_service.source_label(dossier.pieces), user_id=ctx.user.id, organization_id=ctx.organization_id)
    dossiers_repo.link_job(db, dossier, job.id)
    db.commit()
    _start_job(job, "", dossier_id=dossier.id)
    return {"job_id": job.id, "resumed_from": job_id}


def _job_for_owner(job_id: str, ctx: AccessContext, db: Session) -> "jobs.Job":
    job = jobs.get_job(job_id)
    if job is None or job.user_id != ctx.user.id:
        raise HTTPException(404, "Analyse introuvable.")
    require_active_membership(db, user_id=ctx.user.id, organization_id=job.organization_id, not_found_detail="Analyse introuvable.")
    return job


@router.get("/analyze/{job_id}/completion")
def api_completion_state(job_id: str, ctx: AccessContext = Depends(get_access_context), db: Session = Depends(get_db)):
    """Lot 49 — what would let this analysis be completed: see docs/api/LOT_49_COMPLETION_CONTRACT.md.
    Never itself a scoring result: no note, weight or decision is computed or previewed here."""
    job = _job_for_owner(job_id, ctx, db)
    return completion_service.completion_state(db, job, organization_id=ctx.organization_id, user_id=ctx.user.id)


@router.post("/analyze/{job_id}/completion/search-facts")
def api_completion_search_facts(
    request: Request, job_id: str, payload: dict,
    ctx: AccessContext = Depends(require_permission("analysis:create")), db: Session = Depends(get_db),
):
    """Lot 52 — "Chercher dans mes documents" : an explicit, user-triggered search for a citation-verified
    value for one or more of this analysis's own completion needs (never automatic, never run inside a
    revision job — see src/agents/fact_search.py and docs/api/LOT_52_SOURCED_FACTS_CONTRACT.md). Requires
    CSRF like every other mutation-adjacent call here, since a real provider call has a real cost even
    though nothing is persisted by this route itself — the caller re-submits an accepted proposal (or a
    corrected value) to POST /api/analyze/{job_id}/complete below, which re-verifies it independently."""
    require_csrf(request, request.headers.get("X-CSRF-Token"))
    job = _job_for_owner(job_id, ctx, db)
    need_ids = payload.get("need_ids")
    if not isinstance(need_ids, list) or any(not isinstance(n, str) for n in need_ids):
        raise HTTPException(422, {"error_code": "INVALID_VALUE", "message": "« need_ids » doit être une liste d'identifiants."})
    try:
        results = completion_service.search_facts(
            db, job=job, need_ids=need_ids, organization_id=ctx.organization_id, owner_user_id=ctx.user.id,
        )
    except completion_service.CompletionError as exc:
        raise HTTPException(exc.http_status, {"error_code": exc.error_code, "message": exc.message, **exc.details})
    return {"results": results}


@router.post("/analyze/{job_id}/complete")
def api_complete_analysis(
    request: Request, job_id: str, payload: dict,
    ctx: AccessContext = Depends(require_permission("analysis:create")), db: Session = Depends(get_db),
):
    """Lot 49 — declare the missing information a completed (typically INCOMPLET) analysis names, and
    recompute it as a NEW, linked revision. The parent analysis, its snapshot and its deliverables are never
    touched; nothing here re-extracts the AO, re-searches references or calls an external company lookup —
    see src/web/jobs.py::_run_revision and docs/api/LOT_49_COMPLETION_CONTRACT.md for the full contract."""
    require_csrf(request, request.headers.get("X-CSRF-Token"))
    job = _job_for_owner(job_id, ctx, db)
    items = payload.get("items")
    if not isinstance(items, list) or any(not isinstance(i, dict) for i in items):
        raise HTTPException(422, {"error_code": "INVALID_VALUE", "message": "« items » doit être une liste d'objets {need_id, value}."})
    expected_profile_version = payload.get("expected_profile_version")
    if expected_profile_version is not None and (isinstance(expected_profile_version, bool) or not isinstance(expected_profile_version, int)):
        raise HTTPException(422, {"error_code": "INVALID_VALUE", "message": "« expected_profile_version » doit être un entier."})
    try:
        outcome = completion_service.apply_completion(
            db, job=job, items=items,
            confirm_profile_write=bool(payload.get("confirm_profile_write", False)),
            apply_current_capacity=bool(payload.get("apply_current_capacity", False)),
            expected_profile_version=expected_profile_version,
        )
    except completion_service.CompletionError as exc:
        raise HTTPException(exc.http_status, {"error_code": exc.error_code, "message": exc.message, **exc.details})

    new_job = jobs.create_job(
        source_label=f"Complément de l'analyse {job_id}", user_id=ctx.user.id, organization_id=ctx.organization_id,
    )
    dossier = dossiers_repo.get_by_job(db, job_id=job_id, organization_id=ctx.organization_id, user_id=ctx.user.id)
    if dossier is not None:
        # Traceability only — the revision does NOT re-read the dossier's pieces (no re-extraction); this
        # only keeps GET /api/analyze/{new_job.id}/dossier resolvable, like every job in this lineage.
        dossiers_repo.link_job(db, dossier, new_job.id)
    if outcome["complement_rows"]:
        complements_repo.record_many(
            db, job_id=new_job.id, organization_id=ctx.organization_id, user_id=ctx.user.id,
            created_by_user_id=ctx.user.id, items=outcome["complement_rows"],
        )
    db.commit()

    spec = jobs.RevisionSpec(
        parent_job_id=job_id, ao=outcome["ao"], company=outcome["company"], capacity=outcome["capacity"],
        evidence_pack=list(job.result.evidence_pack), rag_synthesis=job.result.rag_synthesis,
        rag_selection_status=job.result.rag_selection_status, rag_selection_reason=job.result.rag_selection_reason,
        policy_version=outcome["policy_version"], provider_snapshot=outcome["provider_snapshot"],
        dossier_id=dossier.id if dossier is not None else None, changes=outcome["changes"],
    )
    _start_revision(new_job, spec)
    return {"job_id": new_job.id, "parent_job_id": job_id, "changes": outcome["changes"]}


@router.get("/analyze/{job_id}/status")
def api_analyze_status(
    job_id: str, current_user: User = Depends(require_active_starter_user), db: Session = Depends(get_db)
):
    job = jobs.get_job(job_id)
    if job is None or job.user_id != current_user.id:
        raise HTTPException(404, "Analyse introuvable.")
    require_active_membership(
        db, user_id=current_user.id, organization_id=job.organization_id, not_found_detail="Analyse introuvable."
    )
    return {
        "job_id": job.id,
        "status": job.status,
        "step_index": job.step_index,
        "step_label": job.step_label,
        "message": job.message,
        "total_steps": len(jobs.STEPS),
        "error": job.error,
        # B18-T1: a stable, safe code for a controlled terminal error (e.g.
        # "invalid_rag_evidence") — None for the generic/unexpected path,
        # which has never had a code and still doesn't (only `error`, a
        # free-text message, unaffected in that case).
        "error_code": job.error_code,
        "redirect_url": f"/app/resultats/{job.id}" if job.status == "done" else None,
    }


@router.get("/download/{job_id}/{kind}")
def api_download(
    job_id: str,
    kind: str,
    current_user: User = Depends(require_active_starter_user),
    db: Session = Depends(get_db),
):
    """Resolve a download through the full ownership chain: authenticated
    user -> job/analysis they own -> document belonging to that analysis ->
    a storage reference confined to the storage root. A document that
    belongs to someone else is indistinguishable from one that doesn't
    exist — see src/web/storage/service.py:resolve_for_download.
    """
    if kind not in ("pdf", "docx"):
        raise HTTPException(400, "Type de document invalide.")
    job = jobs.get_job(job_id)
    if job is None or job.user_id != current_user.id:
        raise HTTPException(404, "Document introuvable.")
    # B02: confirms the caller's membership in *this job's* organization is
    # still active right now — this is what makes a revoked membership (or
    # a suspended organization) cut off a download that already succeeded
    # once, using the same cookie, without needing a new login. A job with
    # no recorded organization (pre-B02) is refused here rather than
    # falling back to anything — see require_active_membership's docstring.
    require_active_membership(db, user_id=current_user.id, organization_id=job.organization_id)

    mime_type = "application/pdf" if kind == "pdf" else (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    storage = get_storage_service()
    download_name = None
    storage_ref = None

    analysis = analyses_repo.get_by_job_id_for_user(db, job_id, current_user.id)
    if analysis is not None:
        document = analyses_repo.get_document_for_analysis(
            db, analysis_id=analysis.id, user_id=current_user.id,
            organization_id=job.organization_id, mime_type=mime_type,
        )
        if document is not None and document.storage_path:
            storage_ref = document.storage_path
            download_name = document.original_filename or document.filename

    if storage_ref is None:
        # Legacy fallback: analyses persisted before this fix (or a request
        # made while the DB write in _persist_to_database hadn't landed yet)
        # have no AnalysisDocument row — fall back to the in-memory/JSON job
        # record, which is itself only ever set by this same server process,
        # never by client input. Membership was already re-verified above,
        # so this fallback can never be used to bypass a permission refusal
        # or an incoherent DB row — only to serve a job that legitimately
        # has no AnalysisDocument row yet.
        storage_ref = job.files.get(kind)
        if not storage_ref:
            raise HTTPException(404, "Document introuvable.")

    try:
        path = storage.resolve_for_download(storage_ref)
    except FileNotFoundError:
        raise HTTPException(404, "Le fichier n'existe plus sur le serveur.")

    return FileResponse(path, media_type=mime_type, filename=download_name or path.name)


# B19-T2: HTTP status a document_rendering_service refusal maps to — the
# two ownership errors share one code deliberately (see that module's own
# docstrings): a caller must not be able to tell "doesn't exist" from
# "belongs to someone else" apart from the response shape.
_DOCUMENT_ERROR_STATUS = {
    document_rendering_service.AnalysisNotFoundError: 404,
    document_rendering_service.AnalysisOwnershipMismatchError: 404,
    document_rendering_service.UnsupportedDocumentKindError: 400,
    document_rendering_service.SnapshotUnusableError: 409,
}


def _raise_for_document_error(exc: document_rendering_service.DocumentRenderingError) -> None:
    status = _DOCUMENT_ERROR_STATUS.get(type(exc), 500)
    raise HTTPException(status, {"error_code": exc.error_code, "message": exc.user_message})


def _resolve_analysis_for_job(job_id: str, current_user: User, db: Session):
    """Same ownership chain api_download already established: an
    authenticated user's own job -> the SQL analysis it produced. Reused
    here so the two new document-lifecycle routes below never diverge from
    the download route's own notion of ownership."""
    job = jobs.get_job(job_id)
    if job is None or job.user_id != current_user.id:
        raise HTTPException(404, "Analyse introuvable.")
    require_active_membership(db, user_id=current_user.id, organization_id=job.organization_id)
    analysis = analyses_repo.get_by_job_id_for_user(db, job_id, current_user.id)
    if analysis is None:
        raise HTTPException(404, "Analyse introuvable.")
    return job, analysis


@router.get("/analyze/{job_id}/documents/status")
def api_document_status(
    job_id: str, current_user: User = Depends(require_active_starter_user), db: Session = Depends(get_db),
):
    """B19-T2: per-document availability — never a single all-or-nothing
    flag. "available" only ever means a real, resolvable file exists;
    everything else ("never generated" and "generated then lost" are
    deliberately indistinguishable here) is "unavailable", meaning
    "regenerate it" — see docs/api/B19_T2_DOCUMENT_RENDERING_CONTRACT.md."""
    # Integration fix (reviewer-caught): pass the JOB's own organization_id
    # (the context this request was already verified against via
    # require_active_membership above) — not the analysis row's own
    # organization_id, which would make document_rendering_service's
    # cross-organization guard tautological (always true, since a value
    # can never differ from itself), never actually exercised.
    job, analysis = _resolve_analysis_for_job(job_id, current_user, db)
    try:
        status = document_rendering_service.get_document_status(
            db, analysis_id=analysis.id, user_id=current_user.id, organization_id=job.organization_id,
        )
    except document_rendering_service.DocumentRenderingError as exc:
        _raise_for_document_error(exc)
    return {"job_id": job_id, "analysis_id": str(analysis.id), **status}


@router.post("/analyze/{job_id}/documents/{kind}/regenerate")
def api_regenerate_document(
    request: Request,
    job_id: str, kind: str, current_user: User = Depends(require_active_starter_user), db: Session = Depends(get_db),
):
    """B19-T2: rebuild one document from the persisted analysis snapshot
    alone — no LLM call, no rescoring, score/decision/INCOMPLET preserved
    exactly as originally computed. Never available for another account's
    analysis (same ownership chain as the download route).

    B14-T1: CSRF-checked (same X-CSRF-Token header convention as every
    other JSON/multipart mutation in this file) and rate-limited as the
    "costly action" this ticket requires at least one of — regeneration
    re-renders a PDF/DOCX from a persisted snapshot, a real per-request
    cost. Keyed by the authenticated user's id (a more precise identity
    than an IP for an already-logged-in action). Both checks run before
    the kind/ownership validation below, so an abusive or CSRF-less
    request never reaches document_rendering_service at all.
    """
    require_csrf(request, request.headers.get("X-CSRF-Token"))
    try:
        check_and_record(
            "regenerate_document", str(current_user.id),
            max_attempts=config.RATE_LIMIT_REGENERATE_DOCUMENT_MAX_ATTEMPTS,
            window_seconds=config.RATE_LIMIT_REGENERATE_DOCUMENT_WINDOW_SECONDS,
        )
    except RateLimitExceeded as exc:
        raise HTTPException(
            429,
            {"error_code": "RATE_LIMITED", "message": "Trop de régénérations. Réessayez plus tard."},
            headers={"Retry-After": str(exc.retry_after_seconds)},
        )
    if kind not in document_rendering_service.DOCUMENT_KINDS:
        raise HTTPException(400, "Type de document invalide.")
    # Same integration fix as api_document_status above: pass the JOB's
    # verified organization_id, not the analysis row's own.
    job, analysis = _resolve_analysis_for_job(job_id, current_user, db)
    try:
        document = document_rendering_service.regenerate_document(
            db, analysis_id=analysis.id, user_id=current_user.id, organization_id=job.organization_id, kind=kind,
        )
    except document_rendering_service.DocumentRenderingError as exc:
        db.rollback()
        _raise_for_document_error(exc)
    db.commit()
    return {
        "job_id": job_id, "analysis_id": str(analysis.id), "kind": kind,
        "filename": document.original_filename or document.filename,
    }


_HISTORY_CSV_FIELDNAMES = ["ao_id", "titre", "client", "secteur", "decision", "score", "budget", "techs", "date", "resultat"]

# B22-T1: the listing endpoint below is a DISPLAY page-browser, not a bulk
# extraction path — capped so a caller can never turn "list a page" into an
# implicit full-corpus export by simply asking for an absurd page_size.
# The dedicated, complete export is /api/history/export.csv below, which
# has no such cap because completeness IS its whole point.
_MAX_HISTORY_PAGE_SIZE = 100


@router.get("/history")
def api_list_history(
    page: int = 1,
    page_size: int = 50,
    ctx: AccessContext = Depends(get_access_context),
    db: Session = Depends(get_db),
):
    """B22-T1: real SQL pagination over this user's FULL history — the
    previous only listing (history_service.list_for_user) silently capped
    at 200 rows with no way to reach anything beyond that. `total` always
    reflects the true row count (a real COUNT(*)), never the size of
    whatever page happened to load, so a caller can page through
    deterministically (order: created_at DESC, id DESC — a stable
    tiebreaker, so no row is ever skipped or duplicated across pages even
    when two analyses share the same timestamp).

    B22-T2: scoped to ctx.organization_id — resolved server-side by
    get_access_context from the caller's own active memberships (a
    `?organization_id=` query param is only ever a SELECTION among those,
    checked against them, never a grant by itself). An absent/ambiguous
    selection follows get_access_context's own contract (403/409), never a
    silent fallback to "every organization"."""
    page = max(1, page)
    page_size = max(1, min(page_size, _MAX_HISTORY_PAGE_SIZE))
    items, total = history_service.list_for_user_page(
        db, ctx.user.id, ctx.organization_id, page=page, page_size=page_size
    )
    return {
        "items": items,
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": max(1, -(-total // page_size)),  # ceil division, no float rounding
    }


@router.get("/history/export.csv")
def api_export_history_csv(
    ctx: AccessContext = Depends(get_access_context),
    db: Session = Depends(get_db),
):
    """B22-T1 (DEFECT confirmed): this used to call history_service.
    list_for_user, silently capped at 200 rows — an account with more
    analyses got a CSV that LOOKED complete (no error, no truncation
    notice) but was missing everything past the 200 most recent. Now
    streams every authorized row via history_service.iter_for_export, in
    bounded batches, never materializing the full export in memory at
    once and never capped.

    B22-T2: scoped to ctx.organization_id — see api_list_history above for
    the resolution contract (never a client-trusted id, never a fallback
    across every organization).

    Each string cell is passed through _neutralize_csv_cell before being
    written — a title/client/technology value starting with =/+/-/@ is a
    documented spreadsheet-formula-injection vector (e.g.
    '=HYPERLINK("http://evil","click")' or "=cmd|'/c calc'!A1") that
    Excel/LibreOffice/Sheets would execute on open; prefixing it with a
    literal apostrophe forces "treat as text" in every one of them. This
    ONLY affects the exported CSV cell — the stored `analyses` row is never
    modified by an export."""
    def _generate():
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=_HISTORY_CSV_FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        yield buffer.getvalue()
        for record in history_service.iter_for_export(db, ctx.user.id, ctx.organization_id):
            buffer = io.StringIO()
            row = dict(record)
            row["techs"] = ", ".join(row.get("techs", []) or [])
            for key in ("titre", "client", "secteur", "decision", "techs", "resultat"):
                row[key] = history_service.neutralize_csv_cell(str(row.get(key, "") or ""))
            writer = csv.DictWriter(buffer, fieldnames=_HISTORY_CSV_FIELDNAMES, extrasaction="ignore")
            writer.writerow(row)
            yield buffer.getvalue()

    return StreamingResponse(
        _generate(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=historique_ao.csv"},
    )


@router.get("/capacity")
def api_get_capacity(ctx: AccessContext = Depends(get_access_context), db: Session = Depends(get_db)):
    """B03: this account's own private capacity — there is no global demo
    capacity file any more (removed in lot 43)."""
    plan = private_capacity_repo.get_for_owner(db, organization_id=ctx.organization_id, owner_user_id=ctx.user.id)
    if plan is None:
        return {
            "status": "unconfigured", "charge_globale_pct": 0, "disponibilite_pct": 100,
            "nombre_projets_en_cours": 0, "projets_en_cours": [], "capacites_par_pole": {},
            "disponibilite_minimum_pct": 10,
        }
    return {
        "status": plan.status,
        "charge_globale_pct": plan.charge_globale_pct,
        "disponibilite_pct": max(0, 100 - plan.charge_globale_pct),
        "nombre_projets_en_cours": plan.nombre_projets_en_cours,
        "projets_en_cours": plan.projets_en_cours,
        "capacites_par_pole": plan.capacites_par_pole,
        # B08-T1: the explicit private threshold that decides
        # `equipe_disponible` — see src/agents/capacity_analyzer.py.
        "disponibilite_minimum_pct": plan.disponibilite_minimum_pct,
    }


@router.post("/capacity")
async def api_save_capacity(
    request: Request,
    payload: dict, ctx: AccessContext = Depends(require_permission("capacity:configure")), db: Session = Depends(get_db),
):
    """B02-C1 fix: a permission is now actually enforced (a viewer gets 403
    from require_permission before this body ever runs). B06-T1: narrowed
    from org:configure (organization_admin only) to capacity:configure
    (analyst AND organization_admin) — see access_context.py's
    ROLE_PERMISSIONS comment for why this is safe: save_for_owner always
    writes ctx.user.id's own row, never a colleague's, whatever the role.
    B03: writes this account's own private CapacityPlan only — never the
    global file (see docs/architecture/B03_PRIVATE_KNOWLEDGE.md).

    B14-T1: CSRF-checked via the X-CSRF-Token header (JSON body, so a body
    field would be more invasive than reusing a header) — see
    docs/api/B14_T1_CSRF_RATE_LIMIT_CONTRACT.md."""
    require_csrf(request, request.headers.get("X-CSRF-Token"))
    raw_threshold = payload.get("disponibilite_minimum_pct")
    plan = private_capacity_repo.save_for_owner(
        db, organization_id=ctx.organization_id, owner_user_id=ctx.user.id,
        charge_globale_pct=int(payload.get("charge_globale_pct", 0)),
        nombre_projets_en_cours=int(payload.get("nombre_projets_en_cours", 0)),
        projets_en_cours=[str(x) for x in payload.get("projets_en_cours", [])],
        capacites_par_pole={k: int(v) for k, v in payload.get("capacites_par_pole", {}).items()},
        disponibilite_minimum_pct=(int(raw_threshold) if raw_threshold is not None else None),
    )
    db.commit()
    return api_get_capacity(ctx, db)


@router.get("/knowledge")
def api_knowledge(ctx: AccessContext = Depends(get_access_context), db: Session = Depends(get_db)):
    """B02-C2 fix (in its B03 form): this reads this account's OWN private
    corpus only. There is no longer a shared corpus for MULTI_CLIENT_MODE/
    corpus_access to gate on this route — see src/core/config.py."""
    summary = _private_knowledge_summary(db, ctx)
    return summary


@router.post("/knowledge/reload")
def api_knowledge_reload(
    request: Request, ctx: AccessContext = Depends(require_permission("knowledge:write")), db: Session = Depends(get_db)
):
    # B14-T1: CSRF-checked via the X-CSRF-Token header — see
    # docs/api/B14_T1_CSRF_RATE_LIMIT_CONTRACT.md.
    require_csrf(request, request.headers.get("X-CSRF-Token"))
    private_rag_manager.invalidate(organization_id=ctx.organization_id, owner_user_id=ctx.user.id)
    return _private_knowledge_summary(db, ctx)


@router.get("/knowledge/search")
def api_knowledge_search(q: str = "", ctx: AccessContext = Depends(get_access_context), db: Session = Depends(get_db)):
    if not q.strip():
        return {"query": q, "results": [], "mode": "empty_query", "degraded_reason": None, "corpus_empty": private_rag_manager.corpus_is_empty(
            db, organization_id=ctx.organization_id, owner_user_id=ctx.user.id
        )}
    from src.rag import hybrid_search
    outcome = hybrid_search.search(db, organization_id=ctx.organization_id, owner_user_id=ctx.user.id, query=q, top_k=5)
    return {
        "query": q,
        # Lot 51 §4 — the mode ACTUALLY executed for this call, never a
        # technical distance presented as metier confidence: 'empty_corpus',
        # 'lexical' (declared, SQLite or hybrid disabled), 'hybrid', or
        # 'hybrid_degraded_vector_unavailable' with a safe reason — never a
        # plain "zero results" standing in for a controlled failure.
        "mode": outcome.mode,
        "degraded_reason": outcome.degraded_reason,
        "corpus_empty": outcome.mode == "empty_corpus",
        "results": [
            {
                "source": ev.source,
                "score": ev.score,
                "relevance_pct": min(int(ev.score * 400), 100),
                "excerpt": ev.content[:800],
                "document_version_id": ev.document_version_id,
                # Lot 51 bis: a vector nearest-neighbor has no relevance floor of its own — this
                # is a purely INFORMATIONAL distinction for the search UI (never a filter here,
                # never applied to scoring evidence, which uses hybrid_search.
                # confirm_evidence_after_rerank instead): a candidate at/under the same lexical
                # floor `private_rag_manager.search()` already uses to accept a result was never
                # independently confirmed by anything besides "nearest available".
                "lexically_confirmed": ev.score > private_rag_manager.LEXICAL_RELEVANCE_FLOOR,
            }
            for ev in outcome.evidences
        ],
    }


def _private_knowledge_summary(db: Session, ctx: AccessContext) -> dict:
    """Same response shape as the pre-B03 global-corpus /api/knowledge
    (total_documents/total_kb/groups) — stabilizing this existing contract
    for the current frontend (static/js/knowledge.js) rather than changing
    it, per ticket B03 section 1. The new per-document detail (status,
    versions, ids) lives in the new /api/knowledge/documents* endpoints —
    see docs/api/B03_KNOWLEDGE_CONTRACT.md."""
    from src.web.database.repositories import knowledge as knowledge_repo

    documents = knowledge_repo.list_active_documents(db, organization_id=ctx.organization_id, owner_user_id=ctx.user.id)
    ready = [d for d in documents if d.active_version and d.active_version.extraction_status == "ready"]
    return {
        "total_documents": len(ready),
        "total_kb": sum(d.active_version.file_size for d in ready) // 1000,
        "groups": [{"folder": "Mes documents", "documents": [
            {"source": d.original_filename, "chars": d.active_version.file_size} for d in ready
        ]}] if ready else [],
        "corpus_empty": not ready,
    }


@router.post("/contact")
async def api_contact(payload: dict, request: Request, db: Session = Depends(get_db)):
    """Business/Enterprise prospect requests — not a Starter signup (that's
    /register, which creates a real User + Subscription instead)."""
    require_csrf(request, payload.get("csrf_token"))

    first_name = (payload.get("first_name") or "").strip()
    last_name = (payload.get("last_name") or "").strip()
    email = (payload.get("email") or "").strip()
    company = (payload.get("company") or "").strip()
    job_title = (payload.get("job_title") or "").strip()
    plan = (payload.get("plan") or "").strip()
    message = (payload.get("message") or "").strip()
    raw_employee_count = (payload.get("employee_count") or "").strip() if isinstance(payload.get("employee_count"), str) else payload.get("employee_count")

    if not first_name or not last_name or not email or not message:
        raise HTTPException(400, "Merci de renseigner votre prénom, votre nom, votre email et votre message.")
    if "@" not in email or "." not in email.rsplit("@", 1)[-1]:
        raise HTTPException(400, "Adresse email invalide.")

    employee_count = None
    if raw_employee_count not in (None, ""):
        try:
            employee_count = int(raw_employee_count)
        except (TypeError, ValueError):
            raise HTTPException(400, "Nombre d'utilisateurs invalide.")

    contact_request = contacts_repo.create_contact_request(
        db,
        first_name=first_name,
        last_name=last_name,
        email=email,
        company=company or None,
        job_title=job_title or None,
        employee_count=employee_count,
        plan=plan or None,
        message=message,
    )
    db.commit()

    try:
        notify_new_contact_request(contact_request)
    except Exception:
        logger.exception("Failed to send contact-request admin notification")

    return {"status": "ok"}
