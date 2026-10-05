"""B06-T1 — private scoring configuration API: /api/scoring-config/*.

Separate from routes_api.py on purpose (same convention as
routes_knowledge_documents.py — ticket B03 section 5, "éviter les routes
volumineuses"). B14-T1: every mutating route below (PUT /profile, PUT
/policy, POST /policy/validate, POST /policy/activate, POST /simulate) now
requires a valid double-submit CSRF token via the `X-CSRF-Token` header —
see routes_knowledge_documents.py's module docstring for why relying on
SameSite=Lax alone was found insufficient, and
docs/api/B14_T1_CSRF_RATE_LIMIT_CONTRACT.md for the full contract.

Every route resolves AccessContext (organization_id + role, server-side,
never client-supplied) and every write is scoped to
(ctx.organization_id, ctx.user.id) by the repository layer itself — an
analyst or organization_admin configuring "their own" scoring can
structurally never reach a colleague's row (see access_context.py's
ROLE_PERMISSIONS comment on capacity:configure/scoring:configure for why
widening WHO may call these routes doesn't widen WHAT any call can touch).
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from sqlalchemy.orm import Session
from typing import Optional

from src.agents import business_facts, criteria_catalogue
from src.agents.scoring_engine import ScoringEngine, ScoringPolicySnapshot
from src.agents.scoring_policy_validation import validate_for_activation
from src.core.rag_evidence_validation import InvalidRAGEvidenceError
from src.web.auth.access_context import AccessContext, get_access_context, require_permission
from src.web.database.models import ProviderProfile, ScoringPolicy
from src.web.database.repositories import provider_profile as provider_profile_repo
from src.web.database.repositories import scoring_policy as scoring_policy_repo
from src.web.database.session import get_db
from src.web.security.csrf import require_csrf

router = APIRouter(prefix="/api/scoring-config")

# Lot 48: `_IT_WEIGHTS_TEMPLATE` / `AVAILABLE_CRITERIA` (a "pondérations informatiques par défaut" template served
# as `available_criteria`) were confirmed dead code — lot 44 had already flagged them as unused by the screen
# (RAPPORT_LOT_44.md §"Restes"); grep across static/js, templates and tests found zero reader. The real,
# used-by-the-form proposed draft is `proposed_templates()` below (`criteria_templates`, loaded only on an
# explicit button click). Removed here, not just moved: `ScoringEngine.labels`/`label_display` themselves stay —
# `scoring_policy_validation.CRITERIA_KEYS` still needs them to validate a LEGACY-format save.

# B06-T5: the fixed, technical catalogue a settings screen needs to let an
# account build its OWN sector-neutral criteria — never itself
# configurable, reused as-is from src.agents.business_facts (never
# redefined here — one source of truth).
AVAILABLE_FACT_TYPES = sorted(business_facts.FACT_TYPES)
AVAILABLE_OPERATORS = sorted(business_facts.OPERATORS)
AVAILABLE_NUMERIC_COMPARISONS = sorted(business_facts.NUMERIC_COMPARISONS)
BUSINESS_FACTS_CATALOGUE = {
    "fact_types": AVAILABLE_FACT_TYPES,
    "operators": AVAILABLE_OPERATORS,
    "numeric_comparisons": AVAILABLE_NUMERIC_COMPARISONS,
    # Guidance for a form (which operator fits which fact type, which types
    # take a unit) — never an authority: the server re-validates everything.
    "operator_fact_types": business_facts.OPERATOR_FACT_TYPES,
    "unit_fact_types": business_facts.UNIT_FACT_TYPES,
}


def _json_safe_deep(value):
    """Lot 41 (D3-03): standard JSON has no NaN/Infinity, but Python's JSON
    parser accepts them in a request body — and PostgreSQL's JSONB refuses
    to store them, and Starlette refuses to serialize them back. A
    non-finite float anywhere inside business_facts/custom_criteria is
    replaced by its TEXT ("inf", "nan"): the draft stays saveable, and
    /validate then reports a precise structured error (a number field
    holding text) — never a 500, and never a silently favorable number."""
    import math
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _json_safe_deep(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe_deep(v) for v in value]
    return value


def _profile_payload(profile: ProviderProfile | None) -> dict:
    if profile is None:
        return {
            "status": "incomplete", "raison_sociale": None, "effectif": None,
            "competences": [], "certifications": [],
            # B07-T1: off until the owner explicitly turns it on — a
            # server-wide Pappers key is never, by itself, an
            # authorization (src/agents/company_enrichment.py).
            "external_enrichment_enabled": False,
            "business_facts": {},
        }
    return {
        "status": profile.status, "raison_sociale": profile.raison_sociale, "effectif": profile.effectif,
        "competences": list(profile.competences or []), "certifications": list(profile.certifications or []),
        "external_enrichment_enabled": bool(profile.external_enrichment_enabled),
        # B06-T5: {key: {"key","label","type","unit","value"}} — this
        # account's own private, sector-neutral facts (never the fixed
        # competences/certifications above, which stay IT-specific).
        "business_facts": _json_safe_deep(profile.business_facts) if isinstance(profile.business_facts, dict) else {},
    }


def _validate_certification_proofs(db: Session, ctx: AccessContext, profile: ProviderProfile | None) -> list[str]:
    """A `preuve_reference` must name a document THIS owner actually
    uploaded — never a colleague's, another organization's, or a
    fabricated id (ticket section 6, scenario 4: "preuve étrangère :
    validation refusée"). A certification with no proof reference at all
    ('declaree', not 'verifiee') is not an error — the field is optional,
    per ticket section 4."""
    if profile is None:
        return []
    from src.web.database.repositories import knowledge as knowledge_repo

    errors: list[str] = []
    for cert in profile.certifications or []:
        ref = cert.get("preuve_reference")
        if not ref:
            continue
        try:
            document_id = uuid.UUID(str(ref))
        except (ValueError, AttributeError):
            errors.append(f"Référence de preuve invalide pour la certification « {cert.get('nom', '?')} ».")
            continue
        document = knowledge_repo.get_document_for_owner(
            db, document_id=document_id, organization_id=ctx.organization_id, owner_user_id=ctx.user.id,
        )
        if document is None:
            errors.append(
                f"La preuve référencée pour la certification « {cert.get('nom', '?')} » n'appartient pas à votre compte."
            )
    return errors


def _full_validation(db: Session, ctx: AccessContext, draft: ScoringPolicy, profile: ProviderProfile | None) -> dict[str, list[str]]:
    """validate_for_activation (pure) + the one check that needs DB access
    (certification proof ownership) — combined so /validate and /activate
    can never drift into checking different things.

    B06-T5: `draft.custom_criteria` is validated against `profile.
    business_facts` by validate_for_activation itself — nothing extra
    needed here beyond passing it through."""
    errors = validate_for_activation(
        weights=draft.weights, threshold_go=draft.threshold_go,
        threshold_sous_reserve=draft.threshold_sous_reserve, profile=profile,
        business_rules=draft.business_rules, custom_criteria=draft.custom_criteria,
        # Lot 44: a policy authored in the explicit-criteria format is
        # validated on its criteria/settings; a historical one on its legacy
        # fields (which stay authoritative for it).
        origin=draft.origin, criteria=draft.criteria, settings=draft.settings,
    )
    proof_errors = _validate_certification_proofs(db, ctx, profile)
    if proof_errors:
        errors.setdefault("profile", []).extend(proof_errors)
    return errors


def _json_safe_number(value):
    """Standard JSON has no NaN/Infinity token — a draft is allowed to
    (temporarily) hold a non-finite weight/threshold (only /validate and
    /activate refuse it, see module docstring), but echoing it back
    verbatim would crash Starlette's JSONResponse encoder. Represented as
    null in the response instead — /validate's error message is what tells
    the caller a value is invalid, not this echo."""
    import math
    if isinstance(value, (int, float)) and not isinstance(value, bool) and not math.isfinite(value):
        return None
    return value


def _policy_payload(policy: ScoringPolicy | None) -> dict | None:
    if policy is None:
        return None
    return {
        "id": str(policy.id), "version": policy.version, "status": policy.status,
        "weights": {k: _json_safe_number(v) for k, v in dict(policy.weights or {}).items()},
        "threshold_go": _json_safe_number(policy.threshold_go),
        "threshold_sous_reserve": _json_safe_number(policy.threshold_sous_reserve),
        # B06-T4: an absent key here means "not configured" — the frontend
        # form must treat a missing key as an empty field, never as 0 or
        # any other implicit value (see docs/api/B06_T4_BUSINESS_RULES_CONTRACT.md).
        "business_rules": {k: _json_safe_number(v) for k, v in dict(policy.business_rules or {}).items()},
        # B06-T5: additive, sector-neutral criteria — see ScoringPolicy.
        # custom_criteria's own docstring for the exact shape of each entry.
        "custom_criteria": _json_safe_deep(policy.custom_criteria) if isinstance(policy.custom_criteria, list) else [],
        # Lot 44: the explicit criteria the engine evaluates, their schema
        # version (distinct from `version`), the policy settings, and the
        # representation the policy is authored in ("legacy" = a historical
        # policy migrated to explicit criteria: its values are compatibility
        # values, not a choice the account made; "user" = authored in the
        # criteria format).
        "criteria": _json_safe_deep(policy.criteria) if isinstance(policy.criteria, list) else [],
        "settings": _json_safe_deep(policy.settings) if isinstance(policy.settings, dict) else {},
        "criteria_version": policy.criteria_version, "origin": policy.origin,
        "origin_label": "Politique historique migrée" if policy.origin == "legacy" else "Nouvelle politique",
        "created_at": policy.created_at.isoformat(), "updated_at": policy.updated_at.isoformat(),
        "activated_at": policy.activated_at.isoformat() if policy.activated_at else None,
    }


@router.get("")
def get_scoring_config(ctx: AccessContext = Depends(get_access_context), db: Session = Depends(get_db)):
    """Everything the frontend needs to render the form and decide which
    step of À CONFIGURER -> BROUILLON -> VALIDATION -> ACTIVATION ->
    ANALYSES the account is on. `state` collapses to the 3 states this
    backend actually persists (a_configurer/brouillon/analyses) —
    VALIDATION and ACTIVATION are actions (POST .../validate,
    .../activate), not separate persisted states; `draft_validation`
    reports what a POST .../validate on the current draft would return,
    so the frontend can show readiness without an extra round-trip.
    `score_global: null` (always, on this status endpoint — never a
    real/stale score) matches the ticket's own example contract for the
    pre-configuration case."""
    profile = provider_profile_repo.get_for_owner(db, organization_id=ctx.organization_id, owner_user_id=ctx.user.id)
    active = scoring_policy_repo.get_active(db, organization_id=ctx.organization_id, owner_user_id=ctx.user.id)
    draft = scoring_policy_repo.get_draft(db, organization_id=ctx.organization_id, owner_user_id=ctx.user.id)

    if active is not None:
        state = "analyses"
    elif draft is not None:
        state = "brouillon"
    else:
        state = "a_configurer"

    draft_validation_errors = None
    if draft is not None:
        draft_validation_errors = _full_validation(db, ctx, draft, profile)

    return {
        "status": "configuration_required" if active is None else "configured",
        "state": state,
        "score_global": None,
        "profile": _profile_payload(profile),
        "policy": {"active": _policy_payload(active), "draft": _policy_payload(draft)},
        # B06-T5 (B27-T2 backend contract): the fixed, technical catalogue a
        # settings screen needs to let this account build its OWN
        # sector-neutral criteria on top of its declared business_facts —
        # never itself configurable.
        "business_facts_catalogue": BUSINESS_FACTS_CATALOGUE,
        # Lot 44: the closed catalogue of evaluators (with their parameter
        # schemas and required data), what cannot be computed, and the
        # proposed drafts (never active — loaded explicitly by the user).
        "criteria_catalogue": criteria_catalogue.catalogue_payload(),
        "criteria_templates": criteria_catalogue.proposed_templates(),
        "draft_validation": (
            None if draft is None else {"valid": not draft_validation_errors, "errors": draft_validation_errors}
        ),
        "can_configure": "scoring:configure" in ctx.permissions,
    }


@router.get("/policy/versions")
def list_policy_versions(ctx: AccessContext = Depends(get_access_context), db: Session = Depends(get_db)):
    versions = scoring_policy_repo.list_versions(db, organization_id=ctx.organization_id, owner_user_id=ctx.user.id)
    return {"versions": [_policy_payload(v) for v in versions]}


@router.put("/profile")
async def save_profile(
    request: Request,
    payload: dict, ctx: AccessContext = Depends(require_permission("scoring:configure")), db: Session = Depends(get_db),
):
    """A field the user already provided elsewhere is never re-fabricated
    here — only what this payload explicitly sends is stored; the frontend
    is responsible for pre-filling from GET /api/scoring-config's `profile`
    if it wants to let the user reuse a previous value (ticket section 2:
    "les données déjà renseignées peuvent être réutilisées avec leur
    provenance ; aucune valeur métier n'est inventée").

    B14-T1: CSRF-checked via the X-CSRF-Token header."""
    require_csrf(request, request.headers.get("X-CSRF-Token"))
    profile = provider_profile_repo.save_for_owner(
        db, organization_id=ctx.organization_id, owner_user_id=ctx.user.id,
        raison_sociale=(payload.get("raison_sociale") or None),
        effectif=(payload.get("effectif") or None),
        competences=[str(c) for c in payload.get("competences", [])],
        certifications=[
            {
                "nom": str(c.get("nom", "")).strip(),
                "statut": c.get("statut") if c.get("statut") in ("declaree", "verifiee") else "declaree",
                "preuve_reference": c.get("preuve_reference"),
            }
            for c in payload.get("certifications", []) if str(c.get("nom", "")).strip()
        ],
        # B07-T1: explicit, defaults to False — a field simply absent from
        # an older client's payload must never be silently interpreted as
        # "leave enabled" (unlike the additive-only fields elsewhere in
        # this lot, this one is a genuine authorization toggle: erring
        # toward OFF on ambiguity is the only safe default).
        external_enrichment_enabled=bool(payload.get("external_enrichment_enabled", False)),
        # B06-T5: `business_facts` absent from the payload entirely means
        # "leave untouched" (repo idiom) — an EXPLICIT `{}` clears it. Each
        # entry's own `key` is forced to match its dict key server-side (a
        # client sending a mismatched one would otherwise corrupt every
        # criterion referencing it) — full type/operator validation happens
        # at /validate and /activate (src.agents.business_facts), never
        # here: a profile save is allowed to hold an incomplete/invalid
        # fact, same as a policy draft is allowed to hold incomplete
        # weights.
        business_facts=(
            # A non-object entry is stored as given (never silently
            # dropped): /validate reports it as a structured error.
            {str(k): ({**_json_safe_deep(v), "key": str(k)} if isinstance(v, dict) else _json_safe_deep(v))
             for k, v in payload["business_facts"].items()}
            if isinstance(payload.get("business_facts"), dict) else None
        ),
    )
    db.commit()
    return _profile_payload(profile)


@router.put("/policy")
async def save_policy_draft(
    request: Request,
    payload: dict, ctx: AccessContext = Depends(require_permission("scoring:configure")), db: Session = Depends(get_db),
):
    """Create-or-resume the single draft — never touches an active/archived
    row (src/web/database/repositories/scoring_policy.py::save_draft).
    Accepts partial/invalid weights and thresholds without refusing the
    save itself (a brouillon is allowed to be incomplete — only /validate
    and /activate enforce the rules); values are coerced to float where
    possible, left as-is otherwise so /validate can report a precise
    per-field error rather than a generic 400.

    B14-T1: CSRF-checked via the X-CSRF-Token header."""
    require_csrf(request, request.headers.get("X-CSRF-Token"))

    def _coerce_float(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return value

    if "criteria" in payload:
        # Lot 44: the explicit-criteria format. A new policy starts EMPTY —
        # nothing is filled in here; the draft is stored as sent (an incomplete
        # or invalid draft is saveable, /validate and /activate enforce).
        settings = _json_safe_deep(payload["settings"]) if isinstance(payload.get("settings"), dict) else criteria_catalogue.default_settings()
        draft = scoring_policy_repo.save_draft_criteria(
            db, organization_id=ctx.organization_id, owner_user_id=ctx.user.id, created_by_user_id=ctx.user.id,
            criteria=_json_safe_deep(payload["criteria"]), settings=settings,
            threshold_go=_coerce_float(payload.get("threshold_go")) if payload.get("threshold_go") is not None else None,
            threshold_sous_reserve=(_coerce_float(payload.get("threshold_sous_reserve"))
                                    if payload.get("threshold_sous_reserve") is not None else None),
        )
        db.commit()
        return _policy_payload(draft)

    raw_weights = payload.get("weights", {})
    weights = {str(k): _coerce_float(v) for k, v in raw_weights.items()} if isinstance(raw_weights, dict) else raw_weights
    threshold_go = _coerce_float(payload.get("threshold_go")) if payload.get("threshold_go") is not None else None
    threshold_sous_reserve = (
        _coerce_float(payload.get("threshold_sous_reserve")) if payload.get("threshold_sous_reserve") is not None else None
    )
    # B06-T4: same "accept partial/invalid without refusing the save"
    # idiom as weights/thresholds above — only /validate and /activate
    # enforce type/range rules; a draft may hold an incomplete or even
    # malformed business_rules dict. `None` (key absent from the payload)
    # means "leave the draft's current business_rules untouched" — see
    # scoring_policy_repo.save_draft.
    raw_business_rules = payload.get("business_rules")
    business_rules = (
        {str(k): _coerce_float(v) for k, v in raw_business_rules.items()}
        if isinstance(raw_business_rules, dict) else raw_business_rules
    )

    # B06-T5: same "accept partial/invalid without refusing the save" idiom
    # — a draft's custom_criteria may reference a fact not (yet) declared,
    # or an incompatible operator; only /validate and /activate enforce
    # this (via src.agents.business_facts, see _full_validation above).
    # `None` (key absent from the payload) means "leave untouched".
    raw_custom_criteria = payload.get("custom_criteria")
    custom_criteria = _json_safe_deep(raw_custom_criteria) if raw_custom_criteria is not None else None

    try:
        draft = scoring_policy_repo.save_draft(
            db, organization_id=ctx.organization_id, owner_user_id=ctx.user.id, created_by_user_id=ctx.user.id,
            weights=weights, threshold_go=threshold_go, threshold_sous_reserve=threshold_sous_reserve,
            business_rules=business_rules, custom_criteria=custom_criteria,
        )
    except scoring_policy_repo.DraftFormatConflict:
        db.rollback()
        raise HTTPException(409, {
            "error_code": "DRAFT_FORMAT_CONFLICT",
            "message": "Le brouillon actuel est au format « critères explicites » : un enregistrement au format historique "
                       "(weights / business_rules / custom_criteria) l'écraserait. Envoyez « criteria ».",
        })
    db.commit()
    return _policy_payload(draft)


@router.post("/policy/validate")
def validate_policy_draft(
    request: Request, ctx: AccessContext = Depends(require_permission("scoring:configure")), db: Session = Depends(get_db)
):
    """Dry-run — never persists, never activates. Uses the SAME validation
    function /activate uses (src/agents/scoring_policy_validation.py), so
    a green /validate is a reliable predictor of a successful /activate.

    B14-T1: CSRF-checked via the X-CSRF-Token header, same as every other
    mutating-shaped route in this file — this one never writes, but the
    ticket lists it explicitly (it's POST, and the double-submit check is
    cheap defense in depth)."""
    require_csrf(request, request.headers.get("X-CSRF-Token"))
    draft = scoring_policy_repo.get_draft(db, organization_id=ctx.organization_id, owner_user_id=ctx.user.id)
    if draft is None:
        raise HTTPException(404, "Aucun brouillon à valider — enregistrez d'abord une configuration (PUT /api/scoring-config/policy).")
    profile = provider_profile_repo.get_for_owner(db, organization_id=ctx.organization_id, owner_user_id=ctx.user.id)
    errors = _full_validation(db, ctx, draft, profile)
    return {"valid": not errors, "errors": errors}


@router.post("/policy/activate")
def activate_policy(
    request: Request,
    payload: dict, ctx: AccessContext = Depends(require_permission("scoring:configure")), db: Session = Depends(get_db),
):
    """Validates (identically to /validate) BEFORE persisting anything — a
    failed validation leaves the draft untouched, activates nothing (ticket
    section 4: "une validation échouée conserve le brouillon et n'active
    rien"). `expected_active_version` (int or null) implements the
    optimistic-concurrency check described in
    scoring_policy_repo.activate_draft — pass the `version` of the
    `policy.active` your last GET /api/scoring-config returned (or null if
    it was null), never omit it silently as "don't care".

    B14-T1: CSRF-checked via the X-CSRF-Token header, before any of the
    validation/persistence below."""
    require_csrf(request, request.headers.get("X-CSRF-Token"))
    draft = scoring_policy_repo.get_draft(db, organization_id=ctx.organization_id, owner_user_id=ctx.user.id)
    if draft is None:
        raise HTTPException(404, "Aucun brouillon à activer.")
    profile = provider_profile_repo.get_for_owner(db, organization_id=ctx.organization_id, owner_user_id=ctx.user.id)
    errors = _full_validation(db, ctx, draft, profile)
    if errors:
        raise HTTPException(422, {"error_code": "SCORING_POLICY_INVALID", "errors": errors})

    if "expected_active_version" not in payload:
        raise HTTPException(400, "expected_active_version est requis (version actuellement active connue du client, ou null).")

    try:
        activated = scoring_policy_repo.activate_draft(
            db, organization_id=ctx.organization_id, owner_user_id=ctx.user.id,
            expected_active_version=payload.get("expected_active_version"),
        )
    except scoring_policy_repo.ActivationConflict as exc:
        db.rollback()
        raise HTTPException(409, {
            "error_code": "SCORING_POLICY_ACTIVATION_CONFLICT",
            "message": "La politique active a changé depuis votre dernière lecture.",
            "current_active_version": exc.current_active_version,
        })
    db.commit()
    return _policy_payload(activated)


@router.post("/simulate")
async def simulate_policy(
    request: Request,
    mode: str = Form(...),
    example_id: Optional[str] = Form(None),
    text: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
    ctx: AccessContext = Depends(require_permission("scoring:configure")),
    db: Session = Depends(get_db),
):
    """Runs the REAL ScoringEngine against the caller's private capacity +
    provider profile + the CURRENT DRAFT (never the active policy, never
    activated by this call) — same input modes as /api/analyze for
    consistency. Requires a draft that passes the same validation as
    /activate (ticket section 5: "exiger un brouillon complet et
    validé"). Deliberately skips semantic RAG reranking and LLM
    enrichment — reuses only deterministic, already-available structured
    inputs (ticket section 5: "réutiliser des entrées structurées
    disponibles pour éviter des appels LLM inutiles"); the result is
    marked `"simulation": true` and carries the draft's version so it can
    never be confused with a real, activated analysis.

    Lot 51 bis: RAG search here stays on `private_rag_manager.search`
    directly, NEVER `hybrid_search` — even when RAG_HYBRID_MODE_ENABLED is
    globally on. hybrid_search's first real call loads (and, uncached,
    downloads) the embedding model, a real network dependency this route's
    own "strictement locale" contract explicitly rules out — this stays
    deterministic and side-effect-free regardless of the deployment's
    hybrid-mode setting, exactly like it already skips the reranker and any
    LLM/Pappers enrichment.

    B14-T1: CSRF-checked via the X-CSRF-Token header, before any of the
    input-resolution/scoring work below."""
    require_csrf(request, request.headers.get("X-CSRF-Token"))
    from src.agents.ao_extractor import AOExtractor
    from src.agents.capacity_analyzer import CapacityAnalyzer
    from src.agents.company_enrichment import CompanyEnrichmentAgent
    from src.rag import private_rag_manager
    from src.web import analyze_input_service
    from src.web.scoring_context import ScoringConfigurationMissing, resolve_scoring_context

    # B13-T1 (DEFECT confirmed): same validation gap as /api/analyze — an
    # unbounded `await file.read()` with no size/format check, duplicated
    # here as a second inline copy. Both routes now share the single
    # validated entry point (see docs/api/B13_T1_INPUT_VALIDATION_CONTRACT.md).
    # Called BEFORE the draft/capacity 409 gates below, matching
    # /api/analyze's own ordering — an oversized or malformed body is
    # refused as early as possible, before any other DB lookup runs.
    try:
        content = await analyze_input_service.resolve_analyze_input(
            mode=mode, example_id=example_id, text=text, file=file,
        )
    except analyze_input_service.AnalyzeInputError as exc:
        raise HTTPException(exc.http_status, {"error_code": exc.error_code, "message": exc.message})

    draft = scoring_policy_repo.get_draft(db, organization_id=ctx.organization_id, owner_user_id=ctx.user.id)
    if draft is None:
        raise HTTPException(404, "Aucun brouillon à simuler — enregistrez d'abord une configuration.")
    profile = provider_profile_repo.get_for_owner(db, organization_id=ctx.organization_id, owner_user_id=ctx.user.id)
    errors = _full_validation(db, ctx, draft, profile)
    if errors:
        raise HTTPException(422, {"error_code": "SCORING_POLICY_INVALID", "errors": errors})

    # Lot 44: the SAME resolution function as the real analysis job
    # (src/web/scoring_context.py), on the DRAFT: one consistent read of
    # draft + profile + private capacity for this organization + owner, copied
    # into plain data — no second, contradictory read below. Lot 41 (D3-01):
    # the same business computation as a real analysis, but with no external
    # call at all: `allow_llm=False` keeps extraction on the deterministic
    # local path even when a provider is configured (a simulation never calls
    # Claude), and the external company lookup is never enabled here. An
    # insufficient local extraction therefore yields INCOMPLET through the
    # engine's normal completeness contract — the criterion is never ignored.
    try:
        scoring_ctx = resolve_scoring_context(db, organization_id=ctx.organization_id, owner_user_id=ctx.user.id, source="draft")
    except ScoringConfigurationMissing as exc:
        if exc.kind == "capacity_plan":
            raise HTTPException(409, {
                "error_code": "CAPACITY_NOT_CONFIGURED",
                "message": "Configurez votre capacité avant de simuler une analyse.",
            })
        raise HTTPException(404, "Aucun brouillon à simuler — enregistrez d'abord une configuration.")
    requested_facts = scoring_ctx.requested_facts
    ao = AOExtractor().extract(content, requested_facts=requested_facts, allow_llm=False)
    company = CompanyEnrichmentAgent().enrich(ao.client, external_enrichment_enabled=False)
    query = " ".join([ao.titre] + ao.technologies_demandees + ao.certifications_obligatoires)
    try:
        # B18-T2: private_rag_manager.search() itself can now raise
        # InvalidRAGEvidenceError (validates before its own threshold
        # filtering) — this used to be uncaught here, producing a generic
        # 500 instead of the same controlled 422 the later scoring call
        # already returns for the RAGEvidence.model_construct()-bypass case.
        evidences = private_rag_manager.search(db, organization_id=ctx.organization_id, owner_user_id=ctx.user.id, query=query, top_k=8)
    except InvalidRAGEvidenceError as exc:
        raise HTTPException(422, {"error_code": exc.error_code, "message": exc.user_message})

    capacity = CapacityAnalyzer().analyze(ao, plan=scoring_ctx.capacity_plan)
    policy_snapshot = scoring_ctx.snapshot
    try:
        result = ScoringEngine().score(ao, company, evidences, capacity, policy=policy_snapshot)
    except InvalidRAGEvidenceError as exc:
        # B18-T1: same controlled data error as /api/analyze's job path —
        # no partial/favorable simulation result is ever returned.
        raise HTTPException(422, {"error_code": exc.error_code, "message": exc.user_message})

    # B18-T3: this route deliberately never calls semantic_rerank (see
    # this function's own docstring, "réutiliser des entrées structurées
    # disponibles pour éviter des appels LLM inutiles") — "not_attempted"
    # says so honestly, rather than leaving the model's "unknown" default,
    # which is reserved for data that predates this field entirely.
    result.rag_selection_status = "not_attempted"
    result.rag_selection_reason = None

    payload = result.model_dump()
    payload["simulation"] = True
    payload["policy_version"] = scoring_ctx.policy_version
    payload["criteria_version"] = scoring_ctx.criteria_version
    payload["policy_origin"] = scoring_ctx.origin
    payload["note"] = "Simulation déterministe (sans réordonnancement RAG ni enrichissement LLM) — ne préjuge pas de l'analyse réelle une fois cette configuration activée."
    return payload
