"""Background analysis jobs for the FastAPI UI.

Each pipeline step is called individually (extraction, company lookup,
private RAG search + rerank, capacity, scoring, documents) so the browser
can poll real step-by-step status instead of blocking on one long HTTP
request. Lot 43: the Streamlit demo this sequence used to mirror is gone;
every resource a step consumes (policy, profile, capacity, corpus) is the
one resolved server-side for the job's own organization + owner, and a
missing one stops the job — no demo value is substituted.
"""
from __future__ import annotations

from src.web.auth.manual_access import ManualAccessDenied

import copy
import json
import math
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from src.core.config import DATA_DIR, OUTPUT_DIR
from src.core.content_security import ContentSecurityError
from src.core.logger import get_agent_logger
from src.core.models import AOContext, ScoringResult
from src.core.rag_evidence_validation import InvalidRAGEvidenceError
from src.web.analysis_services import build_analysis_services

logger = get_agent_logger("web_jobs")

# B10-T1 (DEFECT confirmed): a stable, safe-to-expose code for the
# unexpected/generic failure path — added alongside the pre-existing
# invalid_rag_evidence and historical_score_unavailable codes, never
# replacing them.
UNEXPECTED_ERROR_CODE = "unexpected_error"
WORKER_LAUNCH_FAILED_ERROR_CODE = "worker_launch_failed"
DOCUMENT_GENERATION_FAILED_ERROR_CODE = "document_generation_failed"
PERSISTENCE_FAILED_ERROR_CODE = "persistence_failed"
_UNEXPECTED_ERROR_MESSAGE = (
    "Une erreur inattendue est survenue pendant l'analyse. Contactez le support si le problème persiste."
)

# B12-T1: a controlled, terminal state for a job whose durable
# `analysis_jobs` row was found (still 'queued'/'running' after this
# process restarted, or in some other terminal state) but never reached a
# computed result (no `analyses` row exists for it either). Sibling of
# HISTORICAL_SCORE_UNAVAILABLE_ERROR_CODE below — same idiom: a stable,
# safe-to-expose code, surfaced through the exact same
# Job.status="error"/error_code mechanism, never a silent 404 and never an
# automatic retry.
JOB_INTERRUPTED_ERROR_CODE = "job_interrupted"
_JOB_INTERRUPTED_MESSAGE = (
    "Cette analyse a été interrompue avant sa fin (redémarrage du serveur). "
    "Aucune reprise automatique n'est effectuée : relancez une nouvelle analyse si vous en avez besoin."
)

STEPS = [
    "Sécurité et préparation",
    "Lecture intelligente",
    "Recherche client",
    "Analyse sémantique",
    "Scoring enrichi",
    "Disponibilité équipe",
    "Génération documents",
]

ANALYSES_DIR = DATA_DIR / "historique" / "analyses"
# Web-generated PDF/DOCX live under OUTPUT_DIR/<user_id>/<job_id>/<document_id>.ext
# — see _run_analysis below and src/livrables/document_generator.py.
ANALYSIS_FILES_DIR = OUTPUT_DIR


@dataclass
class Job:
    id: str
    user_id: Optional[uuid.UUID] = None
    # B02: the organization this job's analysis/documents will be recorded
    # under, resolved server-side by AccessContext at creation time (see
    # src/web/auth/access_context.py and routes_api.api_analyze) — never
    # inferred later from a company name. None only for jobs created before
    # this field existed; see _persist_to_database and api_download for how
    # that legacy case is handled (never a silent fallback).
    organization_id: Optional[uuid.UUID] = None
    status: str = "running"  # running | done | error
    step_index: int = 0
    step_label: str = STEPS[0]
    message: str = "Initialisation..."
    error: Optional[str] = None
    # B18-T1: a stable, safe-to-expose code for a controlled terminal error
    # (e.g. "invalid_rag_evidence") — None for the generic/unexpected
    # error path, which only ever set `error` (a free-text message).
    error_code: Optional[str] = None
    source_label: str = ""
    ao: Optional[AOContext] = None
    result: Optional[ScoringResult] = None
    files: dict = field(default_factory=dict)
    analysis_id: Optional[uuid.UUID] = None
    created_at: float = field(default_factory=time.time)
    # B06-T1: the exact ScoringPolicy version active when this job ran —
    # pinned at launch time, never re-derived later. Activating a newer
    # policy afterward must never change what an already-run job's result
    # means (ticket section 5): this is what lets a historical analysis
    # keep meaning the numbers that were actually active when it ran.
    scoring_policy_version: Optional[int] = None
    # Lot 49: the job_id this one COMPLETES, if it is a revision produced by "Compléter les informations" —
    # None for every ordinary analysis. Set once, before the job ever runs (see start_revision below), and
    # restored verbatim when a revision is reloaded from SQL/JSON after a restart (see _job_from_snapshot).
    parent_job_id: Optional[str] = None
    # Lot 50 bis §3: the job_id this one EXTENDS, if it is a NEW documentary re-analysis produced by "Ajouter
    # les pièces restantes" — deliberately distinct from `parent_job_id` above (a declarative, frozen-input
    # revision): this one runs the ordinary `_run_analysis` path (fresh extraction/scoring, current policy),
    # never `_run_revision`. None for every ordinary analysis and for every completion revision.
    origin_job_id: Optional[str] = None


_JOBS: dict[str, Job] = {}
_LOCK = threading.Lock()


def create_job(
    source_label: str = "", user_id: Optional[uuid.UUID] = None, organization_id: Optional[uuid.UUID] = None
) -> Job:
    job = Job(id=uuid.uuid4().hex[:12], user_id=user_id, organization_id=organization_id, source_label=source_label)
    with _LOCK:
        _JOBS[job.id] = job
    return job


def get_job(job_id: str) -> Optional[Job]:
    """B11-T1 (DEFECT confirmed): the SQL `analyses` table — which this
    codebase's own docstrings call "the SaaS source of truth (V3)" — used
    to be WRITE-ONLY for this purpose. This function checked `_JOBS` (lost
    on every process restart) and then a LOCAL JSON FILE on this process's
    own disk, and never queried SQL at all, so an analysis whose row was
    sitting right there in the database became unreadable through
    /api/analyze/{job_id}/status, /api/download/... and /app/resultats/...
    as soon as the process restarted or the request landed on another
    instance.

    The fallback chain is now, in order:
      1. `_JOBS` — the in-process cache, fastest, authoritative for a job
         that is still RUNNING (no durable row exists mid-flight).
      2. the SQL `analyses` row — the actual durable reference for a job
         that reached a computed result.
      3. B12-T1: the SQL `analysis_jobs` row — the durable job-queue
         mirror (src/web/job_executor.py) — for a job that has NEITHER of
         the above: never cached in this process (a restart), and never
         reached a computed result (so `analyses` has no row for it
         either, e.g. it crashed mid-flight, or was interrupted). This is
         what lets an abandoned/interrupted job report its own terminal
         state instead of a bare 404 after a restart.
      4. the local JSON file — last-resort artifact ONLY, for a job that
         never had ANY DB row to begin with (user_id=None, which both
         _save_analysis_snapshot and job_executor.submit skip by design);
         it is no longer the primary durable source for anything else.

    No global fallback: if no row exists for this job_id, this returns
    None and the caller's existing 404 path fires, exactly as for a
    genuinely missing job. A row that exists is reconstructed carrying its
    OWN user_id/organization_id, so the callers' unchanged
    `job.user_id != current_user.id` check still refuses another owner's
    analysis — a substitute/plausible-looking job is never synthesized.
    """
    with _LOCK:
        job = _JOBS.get(job_id)
    if job is not None:
        return job
    job = _load_from_database(job_id)
    if job is not None:
        return job
    job = _load_from_job_queue_table(job_id)
    if job is not None:
        return job
    return _load_persisted(job_id)


def _fail(job: Job, *, message: str, error_code: Optional[str]) -> None:
    """The ONE place a job transitions into the terminal "error" state
    (B10-T1, DEFECT confirmed) — refuses to touch a job that is already
    terminal (done/error), so a late/duplicate failure signal can never
    revive a finished job back into "running" or overwrite an earlier,
    already-recorded terminal state (ticket section 2: "une progression
    tardive ne remet pas un job terminal en running"). `message` must
    already be a fixed, safe, user-facing string — this function never
    inspects or embeds raw exception text itself; the caller is
    responsible for logging the real exception server-side separately
    (ticket section 3: never expose document text, keys, prompts, or raw
    exception detail via the API)."""
    if job.status in ("done", "error"):
        return
    job.status = "error"
    job.error = message
    job.error_code = error_code
    # B26-T1: minimal structured event — job_id/step/error_code only, never
    # `message` itself (a fixed, safe string today, but this call site must
    # stay correct even if a future edit made it less so) and never the
    # exception detail the caller already logs separately via
    # logger.exception(...).
    from src.core.logger import log_job_event, WARNING
    log_job_event(logger, WARNING, "job_terminal_error", job_id=job.id, step=job.step_label, error_code=error_code)


@dataclass
class RevisionSpec:
    """Lot 49 — everything `_run_revision` needs to recompute a completion, all of it already resolved and
    validated by src/web/completion_service.py::apply_completion BEFORE this is built. Nothing here triggers
    a re-extraction, an external lookup or a new reference selection: `ao`/`company`/`evidence_pack`/
    `rag_*` are the parent's own frozen values (optionally amended with declared complements on a COPY —
    the parent's own Job/Analysis are never touched), and `policy_version` pins the recompute to the EXACT
    policy that scored the parent, never whatever is active now."""
    parent_job_id: str
    ao: AOContext
    company: object  # CompanyProfile — typed loosely to avoid importing src.agents.company_enrichment here
    capacity: object  # CapacityResult
    evidence_pack: list
    rag_synthesis: str
    rag_selection_status: Optional[str]
    rag_selection_reason: Optional[str]
    policy_version: int
    # Lot 49 bis: the EFFECTIVE provider snapshot to score with — the parent's own frozen
    # `ScoringResult.provider_snapshot`, with ONLY the explicitly-confirmed `declare_prestataire`
    # complements of THIS completion merged in (src/web/completion_service.py::apply_completion). Built
    # once, at submission time — _run_revision never reads ProviderProfile again, so no OTHER, unrelated
    # profile change (a competency, a certification, a different fact) made before the worker actually runs
    # can silently reach this calculation.
    provider_snapshot: dict
    dossier_id: Optional[uuid.UUID] = None
    # Lot 53 (additive; defaults to [] so every existing caller/test that builds a RevisionSpec without this
    # keyword is unaffected): the EXACT `changes` list apply_completion already computed at submission time
    # (before/after per declared/sourced item, plus origin/source_json) — frozen onto
    # `ScoringResult.completion_changes` by _run_revision below so a revision's own result page/PDF/DOCX can
    # show what changed without a second computation or a live re-read of the parent's CURRENT state.
    changes: list = field(default_factory=list)


def start_revision(job: Job, spec: RevisionSpec) -> None:
    """Entry point for a completion revision — the same bounded worker pool, the same error-code idiom and
    the same "never leave a job stuck at running" guarantee as start_analysis, just routed to
    _run_revision instead of _run_analysis (see job_executor.py's _WorkItem/_process)."""
    from src.web import job_executor

    job.parent_job_id = spec.parent_job_id
    try:
        job_executor.submit_revision(job, spec)
    except ManualAccessDenied:
        raise
    except job_executor.JobQueueSaturatedError:
        raise
    except Exception:
        logger.exception("Failed to submit revision job to the worker pool job_id=%s parent_job_id=%s", job.id, spec.parent_job_id)
        _fail(
            job,
            message="Impossible de démarrer le complément d'analyse. Réessayez ou contactez le support si le problème persiste.",
            error_code=WORKER_LAUNCH_FAILED_ERROR_CODE,
        )


def _run_revision(job: Job, spec: RevisionSpec) -> None:
    """Lot 49 / 49 bis: recomputes a completion — same engine, same decision order, same document generator
    as _run_analysis, but with extraction/company-enrichment/RAG-search entirely SKIPPED: `spec.ao`/
    `spec.company`/`spec.evidence_pack`/`spec.rag_*` are reused as-is (a copy already carrying any declared
    complement — see completion_service.apply_completion). The scoring policy is resolved by its EXACT
    historical version — a policy change since the parent ran must never silently substitute itself into a
    revision of that parent. The provider inputs (competences/certifications/declared facts) come ENTIRELY
    from `spec.provider_snapshot`, already built and frozen at submission time: this function never reads
    `ProviderProfile` itself, so an unrelated profile edit made between submission and the worker actually
    running can never silently reach the calculation (see docs/qa/lot_49_bis_20260924/RAPPORT_LOT_49_BIS.md)."""
    def progress(step_idx: int, msg: str) -> None:
        if job.status in ("done", "error"):
            return
        job.step_index = step_idx
        job.step_label = STEPS[step_idx]
        job.message = msg

    try:
        services = build_analysis_services()
        from src.agents.llm_client import ClaudeClient
        from src.core import analysis_service
        from src.web.database.session import session_scope
        from src.web.scoring_context import ScoringConfigurationMissing, resolve_policy_snapshot_from_frozen_provider

        progress(0, "Reprise des données de l'analyse d'origine (sans nouvelle extraction)...")
        try:
            with session_scope() as db:
                policy_snapshot, provider = resolve_policy_snapshot_from_frozen_provider(
                    db, organization_id=job.organization_id, owner_user_id=job.user_id,
                    policy_version=spec.policy_version, provider_snapshot=spec.provider_snapshot,
                )
        except ScoringConfigurationMissing:
            _fail(
                job,
                message="La politique qui a produit l'analyse d'origine n'est plus disponible pour ce compte : "
                        "une nouvelle analyse est nécessaire (celle-ci ne peut pas être complétée de façon fiable).",
                error_code="original_policy_unavailable",
            )
            return

        ao = spec.ao
        job.ao = ao  # preserved even if a later step fails fatally, exactly like _run_analysis

        progress(1, f"Données de l'appel d'offres réutilisées, complétées — {ao.titre[:60]}")
        progress(2, f"Profil client conservé (aucune nouvelle recherche externe) : {ao.client}")
        progress(3, "Références internes déjà retenues, réutilisées telles quelles (aucune nouvelle sélection)")
        llm = ClaudeClient()
        rerank = analysis_service.RerankOutcome(
            evidences=list(spec.evidence_pack), rag_synthesis=spec.rag_synthesis,
            selection_status=spec.rag_selection_status, selection_reason=spec.rag_selection_reason,
        )

        progress(4, "Calcul et enrichissement du score avec les informations complétées...")
        try:
            result = analysis_service.score_and_enrich(
                ao, spec.company, spec.capacity, rerank,
                scoring_engine=services.scoring, llm=llm, policy=policy_snapshot, provider_profile=provider,
            )
        except InvalidRAGEvidenceError as exc:
            _fail(job, message=exc.user_message, error_code=exc.error_code)
            return
        except Exception:
            logger.exception("Scoring failed for revision job_id=%s parent_job_id=%s", job.id, spec.parent_job_id)
            _fail(job, message="Le calcul du score a échoué. Réessayez ou contactez le support.", error_code="scoring_failed")
            return
        job.scoring_policy_version = policy_snapshot.version
        # Lot 49 bis: the revision's own frozen provider inputs — a FURTHER completion of THIS revision
        # (chaining) freezes exactly what was used here, never re-derived from the parent's own snapshot.
        result.provider_snapshot = spec.provider_snapshot
        # Lot 53: the exact before/after changes apply_completion computed at submission time — frozen here,
        # never recomputed later against the parent's (possibly since-changed) current state.
        result.completion_changes = list(spec.changes)
        job.result = result

        snapshot_saved = _save_analysis_snapshot(job)

        progress(5, "Évaluation de la disponibilité de l'équipe...")
        ai_content = services.generator._generate_ai_content(ao, result, llm, provider_profile=provider)
        result.ai_content = ai_content

        progress(6, "Génération de vos nouveaux documents de réponse...")
        try:
            job.files = _generate_documents(job, ao, result)
            rendering_failed = False
        except Exception:
            logger.exception("Document generation failed for revision job_id=%s", job.id)
            job.files = {}
            rendering_failed = True

        try:
            _persist(job)
            json_persistence_failed = False
        except Exception:
            logger.exception("Failed to persist revision JSON job_id=%s", job.id)
            json_persistence_failed = True

        snapshot_refreshed = _save_analysis_snapshot(job)
        documents_attached = _attach_documents_to_database(job)
        db_persistence_failed = not (snapshot_saved and snapshot_refreshed and documents_attached)
        persistence_failed = json_persistence_failed or db_persistence_failed

        if rendering_failed:
            job.status = "error"
            job.error = "Le complément a été calculé avec succès, mais la génération des documents PDF/DOCX a échoué. Contactez le support."
            job.error_code = DOCUMENT_GENERATION_FAILED_ERROR_CODE
        elif persistence_failed:
            job.status = "done"
            job.error = (
                "Le complément a été calculé avec succès, mais son enregistrement durable a échoué. "
                "Le résultat reste consultable tant que le serveur n'est pas redémarré."
            )
            job.error_code = PERSISTENCE_FAILED_ERROR_CODE
        else:
            job.status = "done"
        from src.core.logger import log_job_event, INFO
        log_job_event(logger, INFO, "revision_terminal_done" if job.status == "done" else "revision_terminal_error",
                      job_id=job.id, parent_job_id=spec.parent_job_id, version=job.scoring_policy_version)
    except Exception:  # noqa: BLE001 — surfaced to the user as a clean, safe error card, same as _run_analysis
        logger.exception("Unexpected error during revision job_id=%s parent_job_id=%s step=%s", job.id, spec.parent_job_id, job.step_label)
        _fail(job, message=_UNEXPECTED_ERROR_MESSAGE, error_code=UNEXPECTED_ERROR_CODE)


def start_analysis(job: Job, text: str, dossier_id: Optional[uuid.UUID] = None) -> None:
    """B10-T1 (DEFECT confirmed), superseded by B12-T1: launching the
    worker thread itself can fail (e.g. thread/resource exhaustion) —
    previously this exception propagated straight to the /api/analyze
    caller while the job (already inserted into `_JOBS` with
    status="running" by create_job()) was left behind with no thread ever
    processing it, stuck in "running" forever. Catching it here and
    marking the job terminal makes the failure observable through the SAME
    polling mechanism as any other job failure, rather than requiring the
    caller to special-case "the POST itself failed".

    B12-T1 (DEFECT confirmed): the unbounded
    `threading.Thread(target=_run_analysis, ...).start()` this function
    used to call directly is exactly the defect being fixed — no cap on
    concurrent threads, no queue, no durable trace that a job was even
    ACCEPTED until _run_analysis got far enough to save a result. This now
    delegates to src/web/job_executor.py's bounded worker pool instead.
    `job_executor.JobQueueSaturatedError` is deliberately let through
    uncaught (never turned into the generic WORKER_LAUNCH_FAILED_ERROR_CODE
    path below) — the /api/analyze route is expected to catch it and
    return an explicit 429 rather than accepting a job that was actually
    refused; see docs/api/B12_T1_JOB_EXECUTOR_CONTRACT.md for the exact
    route-side snippet."""
    from src.web import job_executor

    try:
        job_executor.submit(job, text, dossier_id)
    except ManualAccessDenied:
        raise
    except job_executor.JobQueueSaturatedError:
        raise
    except Exception:
        logger.exception("Failed to submit analysis job to the worker pool job_id=%s", job.id)
        _fail(
            job,
            message="Impossible de démarrer l'analyse. Réessayez ou contactez le support si le problème persiste.",
            error_code=WORKER_LAUNCH_FAILED_ERROR_CODE,
        )


def _run_analysis(job: Job, text: str, dossier_id: Optional[uuid.UUID] = None) -> None:
    def progress(step_idx: int, msg: str) -> None:
        # A terminal job never reports further progress — see _fail's
        # docstring for why this must never revive a finished job.
        if job.status in ("done", "error"):
            return
        job.step_index = step_idx
        job.step_label = STEPS[step_idx]
        job.message = msg

    try:
        # B10-T1 (DEFECT confirmed): service creation used to run OUTSIDE
        # this try block entirely — an exception there (dependency
        # creation, misconfiguration) propagated straight out of this
        # background thread, uncaught, leaving the job stuck at
        # status="running" forever with no visible error. Every step from
        # initialization onward is now inside the SAME protected block.
        services = build_analysis_services()

        dossier_pieces = dossier_summary = None
        if dossier_id is not None:
            # Lot 47 bis: the validated dossier is READ BACK from the database and the private storage — never from a
            # list carried in memory — so the same code serves a fresh job and a job resumed after a restart. The text
            # below is the consolidated text with explicit piece boundaries; each chunk was already normalised at intake.
            from src.agents import dossier_consolidation
            from src.web.ao_dossier import service as dossier_service
            from src.web.database.session import session_scope as _dossier_scope
            try:
                with _dossier_scope() as db:
                    dossier_pieces, dossier_summary = dossier_service.load_texts(db, dossier_id, job.organization_id, job.user_id)
            except Exception:
                logger.exception("Dossier could not be read back job_id=%s", job.id)
                _fail(job, message="Le dossier d'appel d'offres validé n'a pas pu être relu. Renvoyez-le ou contactez le support.",
                      error_code="dossier_unavailable")
                return
            text = dossier_consolidation.consolidated_text(dossier_pieces)

        try:
            if dossier_id is not None:
                # Lot 50 ter §2: relevance/scope was ALREADY decided once, correctly, at admission time
                # (intake or confirm) — informed by each piece's own moderation verdict via
                # `src/web/ao_dossier/scope.py`, never re-derivable from a blunt lexical scan of the final
                # text alone. Re-running the FULL check here would let an obsolete, context-blind rule
                # invalidate a dossier the account already saw admitted. Only injection — an absolute,
                # timeless security concern, cheap to re-verify — is re-checked at run time, as defense in
                # depth against a resumed/tampered dossier that bypassed admission.
                injection_verdict = services.security.check(dossier_consolidation.plain_text(dossier_pieces))
                if "prompt_injection" in injection_verdict.reason_codes:
                    raise ContentSecurityError(["prompt_injection"])
            else:
                services.security.validate(text)
        except ContentSecurityError as exc:
            _fail(job, message=exc.user_message, error_code=None)
            return
        if dossier_id is None:
            text = services.preparer.prepare(text).text

        progress(0, "Lecture du document...")
        from src.agents.ao_extractor import AOExtractor
        from src.agents.llm_client import ClaudeClient
        from src.core import analysis_service
        from src.web.database.session import session_scope
        from src.web.scoring_context import resolve_scoring_context
        llm = ClaudeClient()
        # Lot 44: ONE consistent read of this account's ACTIVE policy,
        # provider profile and private capacity plan — the same function the
        # simulation uses (src/web/scoring_context.py). Everything the
        # extraction, the scoring and the documents use below is a COPY taken
        # here: no second read can contradict it while the calculation runs,
        # and nothing global/demo is ever a fallback.
        #
        # /api/analyze already refuses (409 SCORING_NOT_CONFIGURED /
        # CAPACITY_NOT_CONFIGURED) before a job is ever created — reaching a
        # missing configuration here would be a bug upstream, so it raises and
        # the job ends in the generic controlled failure.
        with session_scope() as db:
            scoring_ctx = resolve_scoring_context(
                db, organization_id=job.organization_id, owner_user_id=job.user_id, source="active",
            )
        requested_facts = scoring_ctx.requested_facts
        profile_row = scoring_ctx.provider
        try:
            if dossier_id is not None:
                # ONE consolidated extraction over the sourced observations of every piece — same extractor, same
                # chain afterwards (RAG of the account's OWN references, scoring by the account's policy).
                ao = dossier_consolidation.extract_dossier_ao(
                    dossier_pieces, AOExtractor(), requested_facts=requested_facts, summary=dossier_summary,
                )
            else:
                ao = analysis_service.extract_ao(text, AOExtractor(), requested_facts=requested_facts)
        except Exception:
            # B10-T1 (DEFECT confirmed): this catches only a genuine BUG in
            # AOExtractor itself — its own LLM-disabled/provider-exception/
            # malformed-response cases are already handled internally
            # (B05-T2) and never raise; this is not a duplicate of that
            # fallback, it is the backstop for something those paths don't
            # cover.
            logger.exception("Extraction failed job_id=%s", job.id)
            _fail(
                job, message="L'extraction de l'appel d'offres a échoué. Réessayez ou contactez le support.",
                error_code="extraction_failed",
            )
            return
        job.ao = ao  # preserved even if a later step fails fatally (ticket section 4)

        progress(1, f"Lecture intelligente du document — {ao.titre[:60]}")
        from src.agents.company_enrichment import CompanyEnrichmentAgent
        # B07-T1: profile_row was already fetched once, above (now shared
        # with the B05-T3 requested_facts read) — reused here for the
        # external lookup's own explicit per-account authorization AND
        # later for scoring/enrichment's real competences/certifications/
        # business_facts — never a second, possibly-stale read.
        progress(2, f"Recherche d'informations sur le client : {ao.client}")
        company = analysis_service.enrich_company(
            ao, CompanyEnrichmentAgent(),
            external_enrichment_enabled=bool(profile_row.external_enrichment_enabled) if profile_row else False,
        )

        progress(3, "Analyse sémantique de vos références internes...")
        # B03: search this account's own private corpus only. semantic_rerank
        # is a stateless LLM call (no dependency on which corpus produced
        # `evidences`) hosted by this job's own reranker — no private data is
        # ever read from or written into it.
        from src.rag import hybrid_search
        from src.web.database.session import session_scope
        try:
            # B18-T2: the underlying lexical search validates every raw
            # similarity BEFORE its own threshold filtering/sorting (see
            # src/core/rag_evidence_validation.py::sanitize_producer_
            # similarity) and can itself raise InvalidRAGEvidenceError —
            # caught here, BEFORE semantic_rerank's LLM call, so an invalid
            # producer value stops the job strictly earlier than a bad
            # value that instead reached ScoringEngine.score() further
            # down (whose own try/except right below stays for the
            # RAGEvidence.model_construct()-bypass case, defense in depth).
            # Lot 51: hybrid_search.search_evidences is a structural no-op
            # (identical to the former direct private_rag_manager.search()
            # call) everywhere PostgreSQL+pgvector+RAG_HYBRID_MODE_ENABLED
            # aren't ALL true — this is the "vrai parcours" the ticket
            # requires actually connecting hybrid mode to, not a demo-only
            # code path.
            with session_scope() as db:
                rerank = analysis_service.search_and_rerank_evidences(
                    ao,
                    search_evidences=lambda q, k, _db=db: hybrid_search.search_evidences(
                        _db, organization_id=job.organization_id, owner_user_id=job.user_id, query=q, top_k=k,
                    ),
                    reranker=services.reranker, llm=llm, top_k=8,
                )
            # Lot 51 bis: a vector nearest-neighbor has no relevance threshold
            # of its own (unlike lexical's existing floor) — when the
            # reranker did NOT actually validate anything for real (LLM
            # disabled/unavailable/failed), an off-topic vector-only
            # candidate must not silently become scoring evidence. A real
            # LLM selection (status "applied") is the genuine decision and
            # is never second-guessed here.
            rerank.evidences = hybrid_search.confirm_evidence_after_rerank(
                rerank.evidences, rerank_status=rerank.selection_status,
            )
        except InvalidRAGEvidenceError as exc:
            _fail(job, message=exc.user_message, error_code=exc.error_code)
            return
        except Exception:
            # B10-T1 (DEFECT confirmed): a NON-InvalidRAGEvidenceError
            # failure here (e.g. a database error from private_rag_manager.
            # search) previously fell through to the generic catch-all
            # with a leaked raw exception message — now a distinguishable,
            # safe code, same as the other named steps in this function.
            logger.exception("RAG search/rerank failed job_id=%s", job.id)
            _fail(
                job, message="La recherche de références internes a échoué. Réessayez ou contactez le support.",
                error_code="rag_failed",
            )
            return

        progress(4, "Calcul et enrichissement du score de réussite...")
        from src.agents.capacity_analyzer import CapacityAnalyzer
        capacity = analysis_service.analyze_capacity(ao, CapacityAnalyzer(), scoring_ctx.capacity_plan)
        policy_snapshot = scoring_ctx.snapshot

        try:
            # B09-T1: score() + rag-outcome attachment + enrich_with_llm(),
            # in the shared order — enrich_with_llm() never raises (it
            # catches its own failures internally into enrichment_status/
            # enrichment_reason, see ScoringEngine.enrich_with_llm), so
            # wrapping both in this one try/except is behaviorally
            # identical to the previous score-only try/except: if score()
            # raises, enrich_with_llm is never reached, exactly as before.
            result = analysis_service.score_and_enrich(
                ao, company, capacity, rerank,
                scoring_engine=services.scoring, llm=llm, policy=policy_snapshot,
                # B19-T1: this account's own declared identity/competences
                # (already fetched above) — never a hardcoded ESN bio.
                provider_profile=profile_row,
            )
        except InvalidRAGEvidenceError as exc:
            # B18-T1 (DEFECT-B04-04): a controlled, terminal data error —
            # same mechanism as ContentSecurityError above. No scoring
            # result is ever published as successful, and no further LLM
            # call happens after this point (enrich_with_llm below is never
            # reached) — the earlier semantic_rerank() LLM call, if any,
            # already happened before this line and is not undone; that is
            # expected, not a violation (ticket section 3).
            _fail(job, message=exc.user_message, error_code=exc.error_code)
            return
        except Exception:
            logger.exception("Scoring failed job_id=%s", job.id)
            _fail(
                job, message="Le calcul du score a échoué. Réessayez ou contactez le support.",
                error_code="scoring_failed",
            )
            return
        job.scoring_policy_version = scoring_ctx.policy_version
        # Lot 49 bis: freezes the EXACT provider inputs this result was scored with — never read again for
        # THIS result; see src/web/completion_service.py for why a later completion needs this instead of
        # re-reading whatever the profile happens to be at completion time.
        from src.web.scoring_context import provider_snapshot_from_identity
        result.provider_snapshot = provider_snapshot_from_identity(profile_row)
        job.result = result  # preserved even if a later step (rendering) fails fatally (ticket section 4)

        # B11-T1 (DEFECT confirmed): the analysis row reaches SQL HERE —
        # right after the result is computed, BEFORE a single document is
        # rendered. It used to be written only after rendering, so a crash
        # anywhere between a successful calculation and the end of
        # rendering meant the computed result never reached the reference
        # storage at all, even though rendering had nothing to do with
        # whether the calculation succeeded. Best-effort, exactly as
        # before: a database problem never blocks a result the user is
        # entitled to see (it is reported afterwards, honestly, via
        # PERSISTENCE_FAILED_ERROR_CODE).
        snapshot_saved = _save_analysis_snapshot(job)

        progress(5, "Évaluation de la disponibilité de l'équipe...")
        ai_content = services.generator._generate_ai_content(ao, result, llm, provider_profile=profile_row)
        result.ai_content = ai_content

        progress(6, "Génération de vos documents de réponse sur mesure...")
        # B10-T1 (DEFECT confirmed): job.ao/job.result are ALREADY set
        # above — a rendering failure must never erase an otherwise valid,
        # already-computed analysis (ticket section 4). Rendering and
        # persistence failures are tracked independently and BOTH are
        # still attempted regardless of the other: a rendering failure
        # must not prevent the already-computed score from at least
        # reaching durable storage (files={} — no file is ever announced
        # as available when none was produced), and a persistence failure
        # must not discard documents that were actually generated.
        try:
            job.files = _generate_documents(job, ao, result)
            rendering_failed = False
        except Exception:
            logger.exception("Document generation failed job_id=%s", job.id)
            job.files = {}
            rendering_failed = True

        try:
            # Lot 43: the legacy global JSON history (a single
            # `historique_ao.json` shared by every account, kept only so the
            # removed Streamlit UI could read it) is no longer written. The
            # existing file is data and is left untouched; the durable
            # per-job JSON below and the `analyses` table are the records.
            _persist(job)
            json_persistence_failed = False
        except Exception:
            logger.exception("Failed to persist analysis JSON job_id=%s", job.id)
            json_persistence_failed = True

        # B11-T1: the analysis row was already written above, before
        # rendering. This second call REFRESHES that same row (an upsert on
        # job_id — one row, never a duplicate) with the two things that
        # only exist now: the generated ai_content carried on `result`, and
        # `files`. Attaching the documents is the separate, later operation
        # — those rows reference files that could not exist any earlier.
        # Both are best-effort and never block a successful analysis.
        snapshot_refreshed = _save_analysis_snapshot(job)
        documents_attached = _attach_documents_to_database(job)
        db_persistence_failed = not (snapshot_saved and snapshot_refreshed and documents_attached)
        persistence_failed = json_persistence_failed or db_persistence_failed

        if rendering_failed:
            # A real deliverable (the documents) was not produced — this IS
            # reported as a failure (never a silent/default success, ticket
            # section 3), while job.ao/job.result/the durable JSON (if
            # persistence itself succeeded) all still hold the real,
            # already-computed analysis for whatever consumer needs it.
            job.status = "error"
            job.error = "L'analyse a été calculée avec succès, mais la génération des documents PDF/DOCX a échoué. Contactez le support."
            job.error_code = DOCUMENT_GENERATION_FAILED_ERROR_CODE
            from src.core.logger import log_job_event, WARNING
            log_job_event(
                logger, WARNING, "job_terminal_error", job_id=job.id, step="rendering",
                error_code=DOCUMENT_GENERATION_FAILED_ERROR_CODE, version=job.scoring_policy_version,
            )
        elif persistence_failed:
            # B11-T1: this branch now covers BOTH durable stores — the
            # legacy JSON file AND the SQL analysis row (at either of its
            # two save points) — under the ONE already-documented public
            # code, PERSISTENCE_FAILED_ERROR_CODE. No new public error code
            # is introduced: from the caller's point of view the fact is
            # identical ("your result was computed but its durable
            # recording failed"), and which store failed is a server-side
            # logging concern, distinguished in the logs by each
            # operation's own message.
            #
            # The analysis itself fully succeeded (job.ao/job.result/job.
            # files are already set) and remains immediately servable for
            # this process's lifetime — only DURABLE recovery across a
            # restart is at risk.
            # status stays "done": a storage hiccup here is not the same
            # class of failure as the analysis itself failing, and must
            # never be reported as one (ticket: "pas de faux succès
            # durable ni de retour en running" — the failure IS surfaced,
            # distinctly, via error/error_code, just without discarding a
            # real result or claiming the job never finished).
            job.status = "done"
            job.error = (
                "L'analyse a été calculée avec succès, mais son enregistrement durable a échoué. "
                "Le résultat reste consultable tant que le serveur n'est pas redémarré."
            )
            job.error_code = PERSISTENCE_FAILED_ERROR_CODE
            from src.core.logger import log_job_event, WARNING
            log_job_event(
                logger, WARNING, "job_terminal_persistence_failed", job_id=job.id,
                error_code=PERSISTENCE_FAILED_ERROR_CODE, version=job.scoring_policy_version,
            )
        else:
            job.status = "done"
            from src.core.logger import log_job_event, INFO
            log_job_event(logger, INFO, "job_terminal_done", job_id=job.id, version=job.scoring_policy_version)
    except Exception:  # noqa: BLE001 — surfaced to the user as a clean, safe error card
        # B10-T1 (DEFECT confirmed): the raw exception used to be embedded
        # directly in job.error (f"...: {exc}"), which is returned VERBATIM
        # by /api/analyze/{job_id}/status — any internal detail the
        # exception happened to carry (a file path, a fragment of document
        # text via a validation error message, ...) leaked straight into
        # the API response. The real exception is now only ever logged
        # server-side; the public message is fixed and generic.
        logger.exception("Unexpected error during analysis job_id=%s step=%s", job.id, job.step_label)
        _fail(job, message=_UNEXPECTED_ERROR_MESSAGE, error_code=UNEXPECTED_ERROR_CODE)


def _generate_documents(job: Job, ao: AOContext, result: ScoringResult) -> dict[str, str]:
    """Write this job's PDF and DOCX and return {"pdf": path, "docx": path}.

    job.id is already unique at this point (minted in create_job(), before
    the pipeline runs). B11-T1 note: the SQL analysis row now DOES exist by
    the time this runs (it is saved before rendering), but the layout stays
    keyed on job.id deliberately — the path must not depend on a durable
    row whose write is best-effort and may legitimately have been skipped
    (no organization, revoked membership) or failed. Scoping the directory
    by user + job guarantees two analyses never write
    to the same path, whatever their title or timestamp, and each document
    additionally gets its own random id so a regenerated document is a new
    file rather than an overwrite of the previous one.
    """
    from src.livrables.document_generator import DocumentGenerator
    dg = DocumentGenerator()
    user_scope = str(job.user_id) if job.user_id else "anonymous"
    analysis_dir = ANALYSIS_FILES_DIR / user_scope / job.id
    document_ids = {"pdf": uuid.uuid4().hex, "docx": uuid.uuid4().hex}
    return {
        "pdf": str(dg.generate_pdf(ao, result, output_path=analysis_dir / f"{document_ids['pdf']}.pdf")),
        "docx": str(dg.generate_docx(ao, result, output_path=analysis_dir / f"{document_ids['docx']}.docx")),
    }


def _persist(job: Job) -> None:
    ANALYSES_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "id": job.id,
        "user_id": str(job.user_id) if job.user_id else None,
        "organization_id": str(job.organization_id) if job.organization_id else None,
        "created_at": job.created_at,
        "source_label": job.source_label,
        "ao": job.ao.model_dump() if job.ao else None,
        "result": job.result.model_dump() if job.result else None,
        "files": job.files,
        "scoring_policy_version": job.scoring_policy_version,
        "parent_job_id": job.parent_job_id,
        "origin_job_id": job.origin_job_id,
    }
    (ANALYSES_DIR / f"{job.id}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _result_data_snapshot(job: Job) -> dict:
    """The exact `result_data` shape this table has always stored — the
    same keys as the legacy JSON payload in _persist(), minus user_id/
    organization_id, which are real columns on the row rather than blob
    content. No new field is invented here: ScoringResult.model_dump()
    already carries enrichment_status/data_integrity/rag_selection_status/
    scoring_completeness/scoring_missing, and they round-trip verbatim."""
    return {
        "id": job.id,
        "created_at": job.created_at,
        "source_label": job.source_label,
        "ao": job.ao.model_dump() if job.ao else None,
        "result": job.result.model_dump() if job.result else None,
        "files": job.files,
        "scoring_policy_version": job.scoring_policy_version,
        "parent_job_id": job.parent_job_id,
        "origin_job_id": job.origin_job_id,
    }


def _database_write_precondition(job: Job, db, *, operation: str) -> bool:
    """Shared gate for both database operations below: an organization must
    have been resolved at creation time, and the membership must STILL be
    active at write time.

    Re-checking membership right before writing (rather than trusting the
    context resolved when the job started) is what stops a long-running
    job whose membership was revoked mid-flight from recording an analysis
    under an organization the user no longer belongs to (B02 section 5).
    A refusal here is a deliberate skip, not a storage failure — see each
    caller's return contract."""
    from src.web.database.repositories import memberships as memberships_repo

    if job.organization_id is None:
        # Created before B02, or by a caller that didn't resolve an
        # AccessContext — refuse to guess an organization after the fact
        # (B02 section 5: no post-hoc attribution from a company name or
        # any other heuristic).
        logger.warning("Skipping PostgreSQL %s: job_id=%s has no organization_id", operation, job.id)
        return False
    if memberships_repo.get_active(db, user_id=job.user_id, organization_id=job.organization_id) is None:
        logger.warning(
            "Skipping PostgreSQL %s: membership revoked during job_id=%s (user_id=%s, organization_id=%s)",
            operation, job.id, job.user_id, job.organization_id,
        )
        return False
    return True


def _save_analysis_snapshot(job: Job) -> bool:
    """Operation (a) of B11-T1's reordered save sequence: write the ANALYSIS
    ROW — AO + result + scoring_policy_version + statuses — to SQL.

    Called EARLY, immediately after `job.result` is set and BEFORE any
    document is rendered, then called again after rendering to refresh the
    same row with the parts that only exist by then (ai_content, files).
    Both calls are safe because the write is an upsert keyed on job_id:
    exactly one row, carrying the latest values (B11-T1 section 4).

    Why the early call matters (the core architectural fix): rendering has
    nothing to do with whether the calculation succeeded, so a crash
    between "the score is computed" and "the PDF is written" must not
    prevent the computed result from ever reaching the reference storage.

    Returns False ONLY if a write was attempted and failed — the caller
    turns that into the honest PERSISTENCE_FAILED_ERROR_CODE signal.
    Returns True when the row was written AND when the write was
    deliberately skipped (no user_id — an anonymous job has no owner to
    file the row under, and the legacy JSON artifact remains its only
    durable trace; no organization_id; membership revoked mid-flight):
    a deliberate refusal to write is a pre-existing, documented policy,
    not a storage failure to report to the user.

    Never raises: a user must still get their result even if the database
    is briefly unavailable.
    """
    if job.user_id is None:
        return True
    try:
        from src.web.database.repositories import analyses as analyses_repo
        from src.web.database.session import session_scope

        # One session_scope per operation, and NEVER one spanning the
        # file-rendering step — that step is disk I/O, not DB work, and
        # holding a transaction open across it would be a transaction held
        # open for the length of a PDF render.
        with session_scope() as db:
            if not _database_write_precondition(job, db, operation="analysis snapshot"):
                return True
            analysis = analyses_repo.upsert_analysis(
                db,
                user_id=job.user_id,
                organization_id=job.organization_id,
                job_id=job.id,
                title=job.ao.titre if job.ao else None,
                client_name=job.ao.client if job.ao else None,
                sector=job.ao.secteur if job.ao else None,
                score=job.result.score_global if job.result else None,
                decision=job.result.decision if job.result else None,
                budget=job.ao.budget_estime if job.ao else None,
                technologies=(job.ao.technologies_demandees[:4] if job.ao else None),
                result_data=_result_data_snapshot(job),
                parent_job_id=job.parent_job_id,
                origin_job_id=job.origin_job_id,
            )
            job.analysis_id = analysis.id
        return True
    except Exception:
        logger.exception("Failed to save the analysis snapshot to PostgreSQL job_id=%s", job.id)
        return False


def _attach_documents_to_database(job: Job) -> bool:
    """Operation (b) of B11-T1's reordered save sequence: attach the
    rendered PDF/DOCX as AnalysisDocument rows.

    Deliberately separate from, and later than, _save_analysis_snapshot:
    these rows reference files that do not exist until rendering has
    succeeded, so unlike the analysis row they genuinely cannot be written
    early. Each document is upserted per mime_type, so a re-render updates
    its row instead of adding a duplicate.

    Returns False only on an attempted-and-failed write (including "the
    analysis row this job's documents belong to isn't there", which means
    the snapshot save failed earlier — the documents have nowhere to
    attach). Returns True when there is simply nothing to attach.
    """
    if job.user_id is None or not job.files:
        return True
    try:
        from src.livrables.document_generator import _safe_name
        from src.web.database.repositories import analyses as analyses_repo
        from src.web.database.session import session_scope
        from src.web.storage.service import get_storage_service

        with session_scope() as db:
            if not _database_write_precondition(job, db, operation="document attachment"):
                return True

            analysis = analyses_repo.get_by_job_id(db, job.id)
            if analysis is None:
                logger.warning(
                    "Cannot attach documents: no analysis row for job_id=%s (the snapshot save failed earlier)", job.id,
                )
                return False
            if analysis.user_id != job.user_id or analysis.organization_id != job.organization_id:
                # Structurally impossible (job_id is UNIQUE and minted per
                # job) — refused rather than written, never attached to
                # another owner's analysis.
                logger.error("Refusing to attach documents: analysis row for job_id=%s has a different owner", job.id)
                return False
            job.analysis_id = analysis.id

            storage = get_storage_service()
            readable_stem = _safe_name(job.ao.titre) if job.ao else "ao"
            original_names = {
                "pdf": f"rapport_decision_{readable_stem}.pdf",
                "docx": f"candidature_{readable_stem}.docx",
            }
            for kind, path_str in job.files.items():
                path = Path(path_str)
                if not path.exists():
                    continue
                analyses_repo.upsert_document(
                    db,
                    analysis_id=analysis.id,
                    user_id=job.user_id,
                    organization_id=job.organization_id,
                    filename=path.name,
                    original_filename=original_names.get(kind, path.name),
                    storage_path=storage.save(path),
                    mime_type=("application/pdf" if kind == "pdf" else
                               "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
                    file_size=path.stat().st_size,
                )
        return True
    except Exception:
        logger.exception("Failed to attach analysis documents to PostgreSQL job_id=%s", job.id)
        return False


def _persist_to_database(job: Job) -> bool:
    """Both database operations, in order, for a caller that has a fully
    finished job in hand (a replay/repair path, and the existing
    membership-revocation test in tests/test_b02_qa_validation.py).

    _run_analysis does NOT call this — it calls the two operations at the
    two distinct points of the flow where each becomes possible, which is
    the whole point of B11-T1 section 3. Kept as one entry point so there
    is no second, divergent "write everything" implementation."""
    snapshot_ok = _save_analysis_snapshot(job)
    documents_ok = _attach_documents_to_database(job)
    return snapshot_ok and documents_ok


HISTORICAL_SCORE_UNAVAILABLE_ERROR_CODE = "historical_score_unavailable"
_HISTORICAL_SCORE_UNAVAILABLE_MESSAGE = (
    "Cette analyse historique contient une valeur numérique invalide et ne peut plus être "
    "affichée de façon fiable. Contactez le support si vous avez besoin de la retrouver."
)


def _is_finite_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _sanitize_legacy_result_data(job_id: str, result_data: dict) -> tuple[str, Optional[str]]:
    """B18-T2 (closing a B18-T1 gap): a result persisted before the RAG
    evidence fix could contain (a) an invalid evidence inside
    `evidence_pack`, and/or (b) a `score_global`/criterion `score`/`poids`
    that the old, unbounded bug had already contaminated (DEFECT-B04-04 —
    "vérifier également les scores de critères et le score global").
    Reconstructing either as-is would raise a Pydantic ValidationError
    (evidence) or silently carry a NaN/Infinity into a value this process
    treats as an ordinary float (score) — neither may reach a caller.

    Returns ("ok", None) if nothing needed fixing (verified fresh on every
    call, never assumed from a previous read). Returns
    ("degraded", "invalid_evidence_removed") after dropping only the
    offending evidence_pack entries in place — `decision`/`score_global`/
    criteria are never touched or recomputed (ticket: "ne pas réécrire les
    scores déjà stockés"). Returns ("unavailable", "non_finite_score")
    when `score_global` or any criterion's `score`/`poids` is itself
    non-finite/wrong-typed — the caller (_load_persisted) must NOT
    construct a ScoringResult in that case at all; it surfaces a
    controlled, per-analysis error instead, using the exact same
    job.status="error"/error_code mechanism as InvalidRAGEvidenceError,
    so this ONE historical analysis fails cleanly without affecting any
    other job/analysis a caller might read next."""
    from src.core.rag_evidence_validation import validate_similarity_score

    if not _is_finite_number(result_data.get("score_global")):
        return "unavailable", "non_finite_score"
    for criterion in result_data.get("criteres") or []:
        if not _is_finite_number(criterion.get("score")) or not _is_finite_number(criterion.get("poids")):
            return "unavailable", "non_finite_score"

    evidence_pack = result_data.get("evidence_pack") or []
    sane, dropped = [], 0
    for entry in evidence_pack:
        try:
            validate_similarity_score(entry.get("score"))
        except (ValueError, TypeError, AttributeError):
            dropped += 1
            continue
        sane.append(entry)
    if dropped:
        from src.core.logger import get_agent_logger
        get_agent_logger("web_jobs").warning(
            "Dropped %d pre-B18-T1 invalid RAG evidence entr%s while reloading job_id=%s "
            "(historical result kept, decision/score_global unchanged)",
            dropped, "y" if dropped == 1 else "ies", job_id,
        )
        result_data["evidence_pack"] = sane
        return "degraded", "invalid_evidence_removed"

    return "ok", None


def _job_from_snapshot(
    job_id: str,
    data: dict,
    *,
    user_id: Optional[uuid.UUID],
    organization_id: Optional[uuid.UUID],
) -> Optional[Job]:
    """Rebuild a servable Job from a `result_data`-shaped snapshot.

    B11-T1: this is the ONE reconstruction path, shared by the SQL read
    (_load_from_database) and the legacy JSON read (_load_persisted) — a
    result_data blob is a result_data blob regardless of which storage
    produced it, so the B18-T2 degradation logic
    (_sanitize_legacy_result_data / HISTORICAL_SCORE_UNAVAILABLE_ERROR_CODE)
    applies identically to a corrupted blob coming out of Postgres and to
    one coming out of a JSON file, rather than existing in two copies that
    could drift.

    `data` is MUTATED in place (evidence sanitization, data_integrity
    stamping) — callers must hand over a private copy, never a live ORM
    attribute (see _load_from_database's deepcopy).

    Nothing here is recomputed against the account's CURRENT configuration:
    decision/score_global/criteria/scoring_completeness/scoring_missing and
    the pinned scoring_policy_version are re-READ exactly as they were
    stored, so a historical analysis keeps meaning what it meant when it
    ran even after the account activates a different ScoringPolicy
    (B11-T1 section 5).
    """
    if not data.get("ao") or not data.get("result"):
        return None
    common_fields = dict(
        id=data.get("id") or job_id,
        user_id=user_id,
        organization_id=organization_id,
        source_label=data.get("source_label", "") or "",
        files=data.get("files") or {},
        created_at=data.get("created_at") or time.time(),
        scoring_policy_version=data.get("scoring_policy_version"),
        # Lot 49 (additive; absent from every snapshot stored before it, so this is simply None for those —
        # never fabricated): restores which analysis this one completes, after a restart or on any reload.
        parent_job_id=data.get("parent_job_id"),
        # Lot 50 bis §3 (same additive discipline): restores which job this documentary re-analysis extends.
        origin_job_id=data.get("origin_job_id"),
    )

    integrity_status, integrity_reason = _sanitize_legacy_result_data(job_id, data["result"])
    if integrity_status == "unavailable":
        # B18-T2: a controlled, per-analysis terminal error — reuses the
        # exact same Job.status="error"/error_code mechanism as
        # InvalidRAGEvidenceError. No ScoringResult is ever constructed
        # from data containing a non-finite score_global/criterion — every
        # OTHER historical job remains independently readable, since this
        # only affects the return value for THIS one job_id.
        job = Job(
            **common_fields,
            status="error",
            step_index=len(STEPS) - 1,
            step_label=STEPS[-1],
            message="Analyse terminée.",
            error=_HISTORICAL_SCORE_UNAVAILABLE_MESSAGE,
            error_code=HISTORICAL_SCORE_UNAVAILABLE_ERROR_CODE,
            ao=AOContext(**data["ao"]),
            result=None,
        )
        with _LOCK:
            _JOBS[job.id] = job
        return job

    # The fresh verification wins (B18-T2: "verified fresh on every call,
    # never assumed from a previous read") — with one exception added by
    # B11-T1 section 5: it may never PROMOTE an already-recorded honest
    # "not fully trustworthy" state back to "ok". A snapshot that was
    # already marked degraded has had its offending entries removed, so a
    # re-check of it now legitimately finds nothing wrong; overwriting the
    # stored "degraded" with that "ok" would erase the only trace that
    # something was dropped. Degradation is one-way.
    stored_integrity = data["result"].get("data_integrity")
    if integrity_status == "ok" and isinstance(stored_integrity, str) and stored_integrity not in ("", "ok"):
        integrity_status = stored_integrity
        integrity_reason = data["result"].get("data_integrity_reason")
    data["result"]["data_integrity"] = integrity_status
    data["result"]["data_integrity_reason"] = integrity_reason
    job = Job(
        **common_fields,
        status="done",
        step_index=len(STEPS) - 1,
        step_label=STEPS[-1],
        message="Analyse terminée.",
        ao=AOContext(**data["ao"]),
        result=ScoringResult(**data["result"]),
    )
    with _LOCK:
        _JOBS[job.id] = job
    return job


def _load_from_database(job_id: str) -> Optional[Job]:
    """B11-T1: read the durable `analyses` row — the actual reference — and
    rebuild a servable Job from its `result_data` snapshot.

    The row's OWN user_id/organization_id are carried onto the Job, never
    the caller's: that is what keeps the routes' unchanged
    `job.user_id != current_user.id` ownership check meaningful for a job
    reconstructed from SQL. No filtering by requester happens here (the
    callers own that decision, as they always have), and no row is ever
    substituted for a missing one.

    A database that is unconfigured/unreachable/erroring is NOT an error
    for this function: it returns None so the legacy JSON fallback can
    still be tried, and the caller's 404 fires if that fails too — the
    read path never raises into a route because of a storage problem.
    """
    try:
        from src.web.database.session import is_database_configured, session_scope

        if not is_database_configured():
            return None

        from src.web.database.repositories import analyses as analyses_repo

        with session_scope() as db:
            row = analyses_repo.get_by_job_id(db, job_id)
            if row is None:
                return None
            # A private deep copy: _job_from_snapshot mutates what it is
            # given, and mutating a live ORM attribute would risk writing
            # the sanitized copy back over the stored snapshot on commit.
            # A read must never rewrite what it read (B18-T2: "ne pas
            # réécrire les scores déjà stockés").
            snapshot = copy.deepcopy(row.result_data or {})
            user_id = row.user_id
            organization_id = row.organization_id
    except Exception:
        logger.exception("Failed to read analysis from the database job_id=%s", job_id)
        return None

    return _job_from_snapshot(job_id, snapshot, user_id=user_id, organization_id=organization_id)


def _load_from_job_queue_table(job_id: str) -> Optional[Job]:
    """B12-T1: consulted only when neither `_JOBS` nor the `analyses` table
    (both checked above, in that order, by get_job) has anything for this
    job_id — i.e. a job that never reached a computed result AND is not
    cached in this process. Reads the durable `analysis_jobs` row
    (src/web/database/repositories/analysis_jobs.py, written by
    src/web/job_executor.py) and reconstructs whatever it can honestly
    report:

    - status == 'interrupted' (job_executor.reconcile_on_startup already
      ran and determined this row was abandoned): a controlled terminal
      error, same idiom as HISTORICAL_SCORE_UNAVAILABLE_ERROR_CODE below —
      cached into `_JOBS` (it is genuinely terminal, it can only ever mean
      this from here on).
    - status == 'error' (the pipeline failed before ever reaching
      _save_analysis_snapshot — e.g. extraction/RAG/scoring failed, or the
      durable row itself failed to write at submit time): also terminal,
      cached the same way. The ORIGINAL free-text error is never
      reconstructed here (this table only durably stores a queue-level
      error_code, not jobs.py's rich per-analysis error text) — the
      message is the same safe, generic one UNEXPECTED_ERROR_MESSAGE uses.
    - status in ('queued', 'running'): reconciliation has not run yet, or
      has not reached this row — reported as still in progress, and
      DELIBERATELY NOT cached into `_JOBS`, so a later call re-reads fresh
      state instead of a snapshot that would otherwise mask a
      reconciliation that runs afterward (this function's own scan does
      not itself decide abandonment — see job_executor.reconcile_on_startup
      for why that is a startup-time scan, not a per-read staleness
      check).
    - status == 'done' with NO corresponding `analyses` row (this function
      only runs after _load_from_database already looked and found
      nothing): this is exactly the existing, pre-B12-T1
      PERSISTENCE_FAILED_ERROR_CODE scenario — job.py's own
      _save_analysis_snapshot failed, so job.status still ended up "done"
      in memory (a storage hiccup is deliberately not treated as an
      analysis failure — see _run_analysis's own comment on that branch),
      but nothing durable ever captured the actual computed result. B11-T1
      established that once `_JOBS` is lost, this is UNRECOVERABLE — the
      caller's 404 must fire, exactly as if no row existed at all — so
      this returns None here rather than fabricating a placeholder,
      falling through to the legacy JSON check and, if that also has
      nothing (the common case), to get_job()'s own None/404.
      tests/test_b11_t1_persistence.py::
      test_sql_write_failure_reports_persistence_failed_and_claims_no_row
      is the regression test for this exact branch.

    A database that is unconfigured/unreachable/erroring is NOT an error
    for this function, exactly like _load_from_database: it returns None
    so the legacy JSON fallback can still be tried.
    """
    try:
        from src.web.database.session import is_database_configured, session_scope

        if not is_database_configured():
            return None

        from src.web.database.repositories import analysis_jobs as analysis_jobs_repo

        with session_scope() as db:
            row = analysis_jobs_repo.get_by_id(db, job_id)
            if row is None:
                return None
            user_id = row.user_id
            organization_id = row.organization_id
            status = row.status
            error_code = row.error_code
            source_label = row.source_label or ""
            created_ts = row.created_at.timestamp() if row.created_at else time.time()
    except Exception:
        logger.exception("Failed to read the durable job queue row job_id=%s", job_id)
        return None

    common_fields = dict(
        id=job_id, user_id=user_id, organization_id=organization_id, source_label=source_label,
        created_at=created_ts,
    )

    if status == "interrupted":
        job = Job(
            **common_fields, status="error", step_index=0, step_label=STEPS[0],
            message="Analyse interrompue.", error=_JOB_INTERRUPTED_MESSAGE, error_code=JOB_INTERRUPTED_ERROR_CODE,
        )
        with _LOCK:
            _JOBS[job.id] = job
        return job

    if status == "error":
        job = Job(
            **common_fields, status="error", step_index=0, step_label=STEPS[0],
            message="Analyse en erreur.", error=_UNEXPECTED_ERROR_MESSAGE, error_code=error_code or UNEXPECTED_ERROR_CODE,
        )
        with _LOCK:
            _JOBS[job.id] = job
        return job

    if status in ("queued", "running"):
        # Reconciliation has not run yet, or has not reached this row —
        # reported as in-progress, never cached (see docstring above).
        return Job(
            **common_fields, status="running", step_index=0, step_label=STEPS[0], message="Analyse en cours...",
        )

    # status == 'done' with no analyses row (see docstring) — never
    # fabricate a placeholder for this; let the caller fall through.
    return None


def _load_persisted(job_id: str) -> Optional[Job]:
    """Legacy local-JSON read — LAST resort only (B11-T1).

    Kept, unchanged in behavior, because it is the only durable trace of a
    job that never had a database row at all: _save_analysis_snapshot
    skips any job with user_id=None (an anonymous/pre-account job), so for
    those this file is all there is. It is no longer the primary durable
    source for anything else — see get_job's fallback chain.
    """
    path = ANALYSES_DIR / f"{job_id}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    raw_user_id = data.get("user_id")
    raw_org_id = data.get("organization_id")
    try:
        user_id = uuid.UUID(raw_user_id) if raw_user_id else None
        organization_id = uuid.UUID(raw_org_id) if raw_org_id else None
    except (ValueError, AttributeError, TypeError):
        return None
    return _job_from_snapshot(job_id, data, user_id=user_id, organization_id=organization_id)
