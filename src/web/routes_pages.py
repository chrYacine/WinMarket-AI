"""HTML page routes — server-rendered Jinja2 templates.

Every route here only gathers data from existing services and renders a
template; no scoring, RAG, LLM or extraction logic is implemented here.

V3: every /app/* route is gated by resolve_app_access() (auth + active
Starter subscription check, re-verified against PostgreSQL on every
request). Personal history/results come from PostgreSQL via
src/web/services/history_service.py (the legacy global JSON historique and
its Streamlit reader were removed in lot 43; the existing file is data and
is left in place).
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from src.core import config
from src.core.logger import get_agent_logger
from src.web import examples_service, jobs
from src.web.auth.access_context import AccessContext, get_access_context
from src.web.auth.dependencies import resolve_app_access
from src.web.database.repositories import knowledge as knowledge_repo
from src.web.database.repositories import memberships as memberships_repo
from src.web.database.repositories import organizations as organizations_repo
from src.web.database.session import get_db
from src.web.knowledge import extraction
from src.web.security.csrf import attach_csrf_cookie, get_or_create_csrf_token
from src.web.services import history_service
from src.web.templating import templates

router = APIRouter()
_logger = get_agent_logger("web_pages")


def _resolve_ctx(request: Request, user, db: Session) -> tuple[AccessContext | None, HTMLResponse | None]:
    """B22-T2: every /app/* page's history/stats must be scoped to ONE
    resolved, membership-checked organization — never an aggregate across
    every organization the signed-in user belongs to (the previous
    behavior, confirmed as a defect during campaign 36). Reuses the exact
    same get_access_context resolution the JSON /api/* routes use (see its
    own docstring for the contract: single membership → automatic,
    several → an explicit ?organization_id= that must match one of them,
    zero → refused outright) — never a second, page-only notion of
    "current organization". A malformed organization_id in the query
    string is treated as absent (falls back to the normal resolution/
    ambiguity rules below) rather than raising here.

    Returns (ctx, None) on success or (None, an already-rendered error
    page) on failure — mirrors resolve_app_access's own (value, redirect)
    convention so callers can `if error: return error` the same way."""
    org_param = request.query_params.get("organization_id")
    organization_id: uuid.UUID | None = None
    if org_param:
        try:
            organization_id = uuid.UUID(org_param)
        except ValueError:
            organization_id = None
    try:
        ctx = get_access_context(request=request, user=user, db=db, organization_id=organization_id)
    except HTTPException as exc:
        if exc.status_code == 409:
            # Ambiguous selection (several active organizations, none
            # chosen): a page that lets the user choose — without it a
            # multi-organization account could never reach any /app page,
            # since the switcher lives in the shell of a page that loaded.
            # The choice is only ever a SELECTION among the caller's own
            # active memberships (revalidated server-side on every request).
            organizations = _organizations_for_selector(db, user.id)
            if len(organizations) > 1:
                return None, templates.TemplateResponse(
                    request, "org_choice.html", {"organizations": organizations}, status_code=409,
                )
        return None, templates.TemplateResponse(
            request, "error.html", {"status_code": exc.status_code, "detail": exc.detail}, status_code=exc.status_code,
        )
    return ctx, None


def _organizations_for_selector(db: Session, user_id: uuid.UUID) -> list[dict]:
    """B27-T1: the sidebar's organization switcher (templates/app_shell.html)
    needs every active organization this account belongs to, by name — the
    template itself decides to hide the selector entirely when there is
    only one (the overwhelming majority of accounts)."""
    memberships = memberships_repo.list_active_for_user(db, user_id)
    orgs = []
    for m in memberships:
        org = organizations_repo.get_by_id(db, m.organization_id)
        if org is not None and org.status == "active":
            orgs.append({"id": str(org.id), "name": org.name})
    return orgs


def _render(
    request: Request, template_name: str, context: dict, status_code: int = 200,
    ctx: AccessContext | None = None, db: Session | None = None,
):
    """Every /app/* page needs a CSRF token (sidebar logout form) — resolve
    it here so each route doesn't repeat the same lines. Reuses the
    existing cookie's token when valid (see get_or_create_csrf_token) so
    that having several /app/* pages open at once doesn't invalidate each
    other's forms.

    B27-T1: passing `ctx` (and `db`) also injects the organization
    switcher's own data (`organizations`, `current_organization_id`) — every
    route that has already resolved an AccessContext should pass both."""
    token, is_new = get_or_create_csrf_token(request)
    extra = {}
    if ctx is not None and db is not None:
        extra = {
            "organizations": _organizations_for_selector(db, ctx.user.id),
            "current_organization_id": str(ctx.organization_id),
        }
    resp = templates.TemplateResponse(
        request, template_name, {**context, **extra, "csrf_token": token}, status_code=status_code,
    )
    if is_new:
        attach_csrf_cookie(resp, token)
    return resp


@router.get("/healthz")
def healthz():
    """B26-T1: liveness only — the process is up and can serve HTTP.
    Deliberately checks NOTHING else (no DB, no LLM, no filesystem) — an
    orchestrator uses this to decide "should this instance be killed and
    restarted", which a slow-but-recoverable dependency must never trigger.
    See /readyz for "are this instance's actual dependencies available"."""
    return {"status": "ok"}


@router.get("/readyz")
def readyz():
    """B26-T1: readiness — this instance's REQUIRED dependencies are
    actually reachable, distinct from /healthz's "process is alive". Checks
    the database with the cheapest possible real round-trip (`SELECT 1`,
    never a table scan, never business data) — no LLM/Pappers/email call
    ever happens here (per the ticket: a degraded/absent external provider
    is a supported, non-blocking mode — see src/core/config.py::
    validate_config — not a readiness failure). No database configured at
    all (e.g. a pure-demo deployment) is reported as ready — there is
    nothing to be unready about in that case. On a real connectivity
    failure, the response is a plain safe boolean — NEVER the connection
    string, table name, or raw exception text (which could contain a
    hostname/credential fragment from the driver's own error message)."""
    from src.web.database.session import is_database_configured, session_scope
    from sqlalchemy import text

    checks = {}
    if is_database_configured():
        try:
            with session_scope() as db:
                db.execute(text("SELECT 1"))
            checks["database"] = "ok"
        except Exception:
            _logger.exception("readyz: database connectivity check failed")
            checks["database"] = "unreachable"
    else:
        checks["database"] = "not_configured"

    ready = checks.get("database") != "unreachable"
    return JSONResponse(status_code=200 if ready else 503, content={"status": "ok" if ready else "degraded", "checks": checks})


@router.get("/", response_class=HTMLResponse)
def landing(request: Request):
    return templates.TemplateResponse(request, "landing.html", {})


@router.get("/pricing", response_class=HTMLResponse)
def pricing(request: Request):
    return templates.TemplateResponse(request, "pricing.html", {})


@router.get("/contact", response_class=HTMLResponse)
def contact(request: Request, plan: str = ""):
    token, is_new = get_or_create_csrf_token(request)
    resp = templates.TemplateResponse(request, "contact.html", {"default_plan": plan, "csrf_token": token})
    if is_new:
        attach_csrf_cookie(resp, token)
    return resp


@router.get("/app")
def app_root():
    return RedirectResponse(url="/app/analyser")


@router.get("/app/analyser", response_class=HTMLResponse)
def app_analyser(request: Request, db: Session = Depends(get_db)):
    user, redirect = resolve_app_access(request, db)
    if redirect:
        return redirect
    ctx, error = _resolve_ctx(request, user, db)
    if error:
        return error
    from src.web.ao_dossier import limits as dossier_limits

    dossier = dossier_limits.dossier_limits()
    words = {1: "un", 2: "deux", 3: "trois", 4: "quatre", 5: "cinq"}
    return _render(request, "app_analyze.html", {
        "active_nav": "analyser",
        "user": user,
        "sidebar_stats": history_service.sidebar_stats(db, user.id, ctx.organization_id),
        "examples": examples_service.list_examples(),
        "steps": jobs.STEPS,
        # Lot 47 bis: the effective dossier limits, from the server's own configuration (the page never invents them).
        "dossier": dossier,
        "annex_words": words.get(dossier["max_annexes"], str(dossier["max_annexes"])),
    }, ctx=ctx, db=db)


@router.get("/app/resultats/{job_id}", response_class=HTMLResponse)
def app_resultats(request: Request, job_id: str, db: Session = Depends(get_db)):
    user, redirect = resolve_app_access(request, db)
    if redirect:
        return redirect
    ctx, error = _resolve_ctx(request, user, db)
    if error:
        return error

    ctx_base = {"active_nav": "analyser", "user": user, "sidebar_stats": history_service.sidebar_stats(db, user.id, ctx.organization_id)}
    job = jobs.get_job(job_id)

    # Ownership check: a job that exists but belongs to someone else must
    # look exactly like a job that doesn't exist — never leak its presence.
    if job is None or job.user_id != user.id:
        return _render(request, "app_error.html", {
            **ctx_base,
            "title": "Analyse introuvable",
            "heading": "Cette analyse n'existe pas ou plus.",
            "message": "Le lien utilisé est invalide, ou le résultat a été supprimé du serveur.",
        }, status_code=404, ctx=ctx, db=db)

    if job.status == "error":
        return _render(request, "app_error.html", {
            **ctx_base,
            "title": "Échec de l'analyse",
            "heading": "L'analyse n'a pas pu être finalisée.",
            "message": job.error or "Erreur inconnue.",
        }, ctx=ctx, db=db)

    if job.status == "running":
        return _render(request, "app_result_pending.html", {
            **ctx_base,
            "job": job,
            "steps": jobs.STEPS,
        }, ctx=ctx, db=db)

    history_recent = history_service.list_for_user(db, user.id, ctx.organization_id)[:10]

    # Lot 49: whether this analysis IS a completion revision (its own parent, if any — the parent's own
    # source_label is shown, never re-fetching its full result), and whether it has ALREADY been completed
    # by a later revision (offered as "see the completed result" instead of a second "Compléter" action).
    from src.web.database.repositories import analyses as analyses_repo
    from src.web.database.repositories import analysis_complements as complements_repo

    parent_label = None
    parent_result = None
    if job.parent_job_id:
        parent_job = jobs.get_job(job.parent_job_id)
        parent_label = parent_job.source_label if parent_job is not None else None
        parent_result = parent_job.result if parent_job is not None else None
    revision = analyses_repo.get_by_parent_job_id(db, job.id)
    complements = complements_repo.list_for_job(db, job_id=job.id, organization_id=ctx.organization_id, user_id=user.id)

    from src.web.result_presentation import build_result_view
    result_view = build_result_view(
        job.result, complements, scoring_policy_version=job.scoring_policy_version, parent_result=parent_result,
    )

    # Lot 50 bis §3 — the SAME lineage display as parent_job_id/revision_job_id above, but for a documentary
    # re-analysis ("Ajouter les pièces restantes") — deliberately a DIFFERENT pair of fields so the template
    # (and the history/export views) can never conflate a frozen declarative revision with a genuinely new,
    # freshly-scored re-analysis of an expanded dossier.
    from src.web.ao_dossier import service as dossier_service

    origin_label = None
    origin_piece_change = None
    if job.origin_job_id:
        origin_job = jobs.get_job(job.origin_job_id)
        origin_label = origin_job.source_label if origin_job is not None else None
        # Lot 54 §1 (DEFECT confirmed): this banner used to say "en ajoutant des pièces" unconditionally —
        # a re-analysis that only REMOVED a piece (no addition) got the same wording, which never happened.
        # Compared by content hash of admitted pieces only, never a filename or a guess.
        before = dossier_service.admitted_piece_hashes(db, job_id=job.origin_job_id, organization_id=ctx.organization_id, user_id=user.id)
        after = dossier_service.admitted_piece_hashes(db, job_id=job.id, organization_id=ctx.organization_id, user_id=user.id)
        if before is not None and after is not None:
            origin_piece_change = dossier_service.describe_piece_change(before, after)
    extensions = analyses_repo.list_by_origin_job_id(db, job.id)
    extension_views = []
    if extensions:
        this_hashes = dossier_service.admitted_piece_hashes(db, job_id=job.id, organization_id=ctx.organization_id, user_id=user.id)
        for ext in extensions:
            ext_after = dossier_service.admitted_piece_hashes(db, job_id=ext.job_id, organization_id=ctx.organization_id, user_id=user.id)
            piece_change = dossier_service.describe_piece_change(this_hashes, ext_after) if (this_hashes is not None and ext_after is not None) else None
            extension_views.append({"job_id": ext.job_id, "piece_change": piece_change})

    return _render(request, "app_result.html", {
        **ctx_base,
        "job_id": job.id,
        "ao": job.ao,
        "result": job.result,
        "files": job.files,
        "history_recent": history_recent,
        "parent_job_id": job.parent_job_id,
        "parent_label": parent_label,
        "revision_job_id": revision.job_id if revision is not None else None,
        "complements": complements,
        "result_view": result_view,
        "origin_job_id": job.origin_job_id,
        "origin_piece_change": origin_piece_change,
        "extension_views": extension_views,
        "origin_label": origin_label,
    }, ctx=ctx, db=db)


@router.get("/app/historique", response_class=HTMLResponse)
def app_historique(request: Request, db: Session = Depends(get_db)):
    """B27-T1: the page itself no longer embeds any history rows
    server-side — static/js/history.js fetches the real paginated
    GET /api/history (organization-scoped since B22-T2) instead of a
    (pre-B22-T1, 200-row-capped) full dump into window.WM_HISTORY. Nothing
    left for this route to query beyond the sidebar stats every /app/* page
    already needs."""
    user, redirect = resolve_app_access(request, db)
    if redirect:
        return redirect
    ctx, error = _resolve_ctx(request, user, db)
    if error:
        return error
    return _render(request, "app_history.html", {
        "active_nav": "historique",
        "user": user,
        "sidebar_stats": history_service.sidebar_stats(db, user.id, ctx.organization_id),
    }, ctx=ctx, db=db)


@router.get("/app/base-connaissances", response_class=HTMLResponse)
def app_base_connaissances(request: Request, db: Session = Depends(get_db)):
    """B02-C2 fix (B03 form): resolves the SAME AccessContext the JSON
    /api/knowledge routes use, and reads only this account's own private
    corpus — there is no longer a shared corpus this page (or any flag
    combination) can reach. _resolve_ctx wraps get_access_context so this
    HTML route reuses its exact resolution logic and turns a refusal into a
    normal page instead of a raw JSON error."""
    user, redirect = resolve_app_access(request, db)
    if redirect:
        return redirect
    ctx, error = _resolve_ctx(request, user, db)
    if error:
        return error
    documents = knowledge_repo.list_active_documents(db, organization_id=ctx.organization_id, owner_user_id=ctx.user.id)
    ready = [d for d in documents if d.active_version and d.active_version.extraction_status == "ready"]
    # Lot 45: the page manages this account's own documents from the JSON
    # routes (list / upload / versions / download / delete / search — see
    # static/js/knowledge.js). The server only decides what the page may
    # OFFER (write permission, configured limits); every route re-checks the
    # role, the scope and the CSRF token itself. The counters below are the
    # first paint, refreshed by the script.
    return _render(request, "app_knowledge.html", {
        "active_nav": "connaissances",
        "user": user,
        "sidebar_stats": history_service.sidebar_stats(db, user.id, ctx.organization_id),
        "knowledge": {
            "total_documents": len(ready),
            "total_kb": sum(d.active_version.file_size for d in ready) // 1000,
        },
        "can_write": "knowledge:write" in ctx.permissions,
        "knowledge_limits": {
            "max_file_mb": config.KNOWLEDGE_MAX_FILE_SIZE_MB,
            "max_documents": config.KNOWLEDGE_MAX_ACTIVE_DOCUMENTS_PER_CORPUS,
            "formats": sorted(extraction.SUPPORTED_SUFFIXES),
        },
    }, ctx=ctx, db=db)


@router.get("/app/parametres", response_class=HTMLResponse)
def app_parametres(request: Request, db: Session = Depends(get_db)):
    """B27-T1 (DEFECT confirmed): no screen existed anywhere for configuring
    a ProviderProfile/ScoringPolicy — only the JSON API (routes_scoring_
    policy.py) did. Without it, /api/analyze's existing 409
    SCORING_NOT_CONFIGURED gate meant a brand-new real account could never
    get past onboarding through the web UI at all — only a capacity modal
    (templates/app_analyze.html) had a screen.

    Reuses routes_scoring_policy.py's own payload-shaping helpers (never a
    second, drifting copy of "what a profile/policy looks like to a
    client") — this route only gathers the SAME data GET /api/scoring-config
    already returns and renders it server-side; every mutation (save
    profile/draft, validate, activate) goes through that same JSON API from
    the page's own JS, identically to how templates/app_analyze.html's
    capacity modal already calls POST /api/capacity."""
    user, redirect = resolve_app_access(request, db)
    if redirect:
        return redirect
    ctx, error = _resolve_ctx(request, user, db)
    if error:
        return error

    from src.web.database.repositories import provider_profile as provider_profile_repo
    from src.web.database.repositories import scoring_policy as scoring_policy_repo
    from src.agents import criteria_catalogue
    from src.web.routes_scoring_policy import (
        BUSINESS_FACTS_CATALOGUE, _full_validation, _policy_payload, _profile_payload,
    )

    profile = provider_profile_repo.get_for_owner(db, organization_id=ctx.organization_id, owner_user_id=ctx.user.id)
    active = scoring_policy_repo.get_active(db, organization_id=ctx.organization_id, owner_user_id=ctx.user.id)
    draft = scoring_policy_repo.get_draft(db, organization_id=ctx.organization_id, owner_user_id=ctx.user.id)

    if active is not None:
        state = "analyses"
    elif draft is not None:
        state = "brouillon"
    else:
        state = "a_configurer"

    draft_validation = None
    if draft is not None:
        errors = _full_validation(db, ctx, draft, profile)
        draft_validation = {"valid": not errors, "errors": errors}

    return _render(request, "app_parametres.html", {
        "active_nav": "parametres",
        "user": user,
        "sidebar_stats": history_service.sidebar_stats(db, user.id, ctx.organization_id),
        "state": state,
        "profile": _profile_payload(profile),
        "active_policy": _policy_payload(active),
        "draft_policy": _policy_payload(draft),
        "draft_validation": draft_validation,
        "business_facts_catalogue": BUSINESS_FACTS_CATALOGUE,
        # Lot 44: the closed evaluator catalogue and the proposed drafts (never active).
        "criteria_catalogue": criteria_catalogue.catalogue_payload(),
        "criteria_templates": criteria_catalogue.proposed_templates(),
        "can_configure": "scoring:configure" in ctx.permissions,
    }, ctx=ctx, db=db)
