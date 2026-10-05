"""Lot 49 — the server-side contract and logic behind "Compléter les informations".

Orchestrates (but does not itself implement) three existing things: `src/agents/completion_needs.py`
(what is missing and how it may be filled), the account's OWN existing forms (profile/capacity —
reused, never re-implemented) and `src/web/jobs.py`'s revision pipeline (the actual, deterministic
recompute). This module owns exactly one responsibility: turning a validated, whitelisted completion
request into (a) the plain data a revision job needs and (b) the durable trail of what was declared —
never a second scoring engine, never a silent write to data this request did not explicitly touch.
"""
from __future__ import annotations

import copy
import math
import uuid
from typing import Any, Optional

from src.agents import completion_needs
from src.agents.capacity_analyzer import CapacityAnalyzer
from src.agents.document_llm_support import verify_citation
from src.core.capacity_plan import CapacityPlan
from src.core.models import AOContext, CompanyProfile, ExtractedFact
from src.web.database.repositories import private_capacity as private_capacity_repo
from src.web.database.repositories import provider_profile as provider_profile_repo
from src.web.database.repositories import scoring_policy as scoring_policy_repo

_MAX_TEXT_LEN = 500
_MAX_LIST_ITEMS = 50
_MAX_LIST_ITEM_LEN = 200
# Lot 52 — an explicit, user-triggered action for a handful of needs at a time (never a background/batch
# job): bounds how many "Chercher dans mes documents" calls (each a real LLM call, when a provider is
# configured) one request can trigger, so a single click can never fan out into an unbounded number of
# calls "sans objectif" (ticket, verbatim).
_MAX_NEEDS_PER_SEARCH = 8


class CompletionError(Exception):
    def __init__(self, error_code: str, message: str, *, http_status: int = 422, **details: Any):
        self.error_code = error_code
        self.message = message
        self.http_status = http_status
        self.details = details
        super().__init__(message)


def _is_finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _plan_from_row(plan_row) -> CapacityPlan:
    return CapacityPlan(
        charge_globale_pct=plan_row.charge_globale_pct, nombre_projets_en_cours=plan_row.nombre_projets_en_cours,
        projets_en_cours=list(plan_row.projets_en_cours or []), capacites_par_pole=dict(plan_row.capacites_par_pole or {}),
        disponibilite_minimum_pct=plan_row.disponibilite_minimum_pct,
    )


def needs_for_job(db, job) -> tuple[list[dict], Optional[str]]:
    """(needs, unavailable_reason). `unavailable_reason` is None normally, "policy_version_unavailable" when
    the exact policy that produced this result can no longer be found for this owner (never substituted by
    the currently active policy — see scoring_context.resolve_scoring_context's own "version" mode
    docstring), or "parent_snapshot_unavailable" (lot 49 bis) when this result predates
    `ScoringResult.provider_snapshot` — there is then no frozen record of the provider inputs it was scored
    with, so completion refuses outright rather than silently substituting a live profile read."""
    if job.result is None or job.scoring_policy_version is None:
        return [], "policy_version_unavailable"
    policy = scoring_policy_repo.get_by_version(
        db, organization_id=job.organization_id, owner_user_id=job.user_id, version=job.scoring_policy_version,
    )
    if policy is None or not isinstance(policy.criteria, list):
        return [], "policy_version_unavailable"
    snapshot = job.result.provider_snapshot
    if not isinstance(snapshot, dict):
        return [], "parent_snapshot_unavailable"
    declared_facts = snapshot.get("business_facts") if isinstance(snapshot.get("business_facts"), dict) else {}
    needs = completion_needs.compute_needs(ao=job.ao, result=job.result, criteria=policy.criteria, declared_facts=declared_facts)
    return needs, None


def completion_state(db, job, *, organization_id: uuid.UUID, user_id: uuid.UUID) -> dict:
    from src.web.database.repositories import analyses as analyses_repo

    if job.status != "done" or job.result is None:
        return {
            "can_complete": False, "reason": "job_not_complete", "parent_job_id": job.id,
            "existing_revision_job_id": None, "needs": [], "capacity": None, "policy_version": job.scoring_policy_version,
        }
    needs, unavailable = needs_for_job(db, job)
    existing = analyses_repo.get_by_parent_job_id(db, job.id)

    capacity_block = None
    plan_row = private_capacity_repo.get_for_owner(db, organization_id=organization_id, owner_user_id=user_id)
    frozen_capacity = job.result.capacity.model_dump() if job.result.capacity else None
    current_capacity = None
    if plan_row is not None and getattr(plan_row, "status", "configured") == "configured":
        current_capacity = CapacityAnalyzer().analyze(job.ao, _plan_from_row(plan_row)).model_dump()
    capacity_block = {
        "frozen": frozen_capacity, "current": current_capacity,
        "changed": current_capacity is not None and current_capacity != frozen_capacity,
    }

    # Lot 49 bis: the CURRENT profile's optimistic-concurrency version — the client must echo it back
    # unchanged as `expected_profile_version` on submission when it confirms any `declare_prestataire`
    # item; a mismatch means the profile changed between preview and submission (see apply_completion).
    profile_row = provider_profile_repo.get_for_owner(db, organization_id=organization_id, owner_user_id=user_id)
    profile_version = getattr(profile_row, "version", None) if profile_row is not None else None

    return {
        "can_complete": unavailable is None and existing is None,
        "reason": unavailable or ("revision_already_exists" if existing else None),
        "parent_job_id": job.id,
        "existing_revision_job_id": existing.job_id if existing else None,
        "needs": needs,
        "capacity": capacity_block,
        "policy_version": job.scoring_policy_version,
        "profile_version": profile_version,
    }


def search_facts(
    db, *, job, need_ids: list[str], organization_id: uuid.UUID, owner_user_id: uuid.UUID,
) -> list[dict[str, Any]]:
    """Lot 52 — "Chercher dans mes documents": runs `src.agents.fact_search` for each requested need,
    against the FRESHLY recomputed needs (never the client's own claim about a need's type/action/label —
    same whitelist discipline as `apply_completion`). Returns a proposal (or an explicit non-proposal
    status: "no_source"/"absent"/"llm_unavailable"/"llm_invalid_response"/"unknown_need") per need_id,
    never a partial write, never itself a scoring input — the caller (routes_api.py) never persists
    anything here, only echoes this response back to the client to be re-submitted (and RE-verified,
    server-side) via apply_completion below."""
    if job.status != "done" or job.result is None:
        raise CompletionError("JOB_NOT_COMPLETE", "Cette analyse n'est pas terminée : rien à chercher.", http_status=409)
    if not isinstance(need_ids, list) or not (1 <= len(need_ids) <= _MAX_NEEDS_PER_SEARCH):
        raise CompletionError(
            "INVALID_VALUE", f"« need_ids » doit être une liste de 1 à {_MAX_NEEDS_PER_SEARCH} identifiants.",
        )
    needs, unavailable = needs_for_job(db, job)
    if unavailable:
        raise CompletionError(
            "ORIGINAL_POLICY_UNAVAILABLE" if unavailable == "policy_version_unavailable" else "PARENT_SNAPSHOT_UNAVAILABLE",
            "Les informations de cette analyse ne sont plus disponibles pour une recherche.", http_status=409,
        )
    needs_by_id = {n["id"]: n for n in needs}

    from src.agents import fact_search
    from src.agents.llm_client import ClaudeClient

    llm = ClaudeClient()
    results: list[dict[str, Any]] = []
    for need_id in need_ids:
        need = needs_by_id.get(need_id)
        if need is None:
            results.append({"need_id": need_id, "status": "unknown_need"})
            continue
        outcome = fact_search.search_fact_for_need(
            db, job=job, need=need, llm=llm, organization_id=organization_id, owner_user_id=owner_user_id,
        )
        results.append({"need_id": need_id, **outcome.to_dict()})
    return results


def _validate_value(need: dict, raw_value: Any) -> Any:
    field_type = need.get("type")
    label = need.get("label") or need.get("id")
    if field_type == "number":
        if not _is_finite_number(raw_value):
            raise CompletionError("INVALID_VALUE", f"« {label} » doit être un nombre fini.", need_id=need["id"])
        return float(raw_value)
    if field_type == "list":
        if not (isinstance(raw_value, list) and 1 <= len(raw_value) <= _MAX_LIST_ITEMS
                and all(isinstance(v, str) and 0 < len(v.strip()) <= _MAX_LIST_ITEM_LEN for v in raw_value)):
            raise CompletionError(
                "INVALID_VALUE",
                f"« {label} » doit être une liste de 1 à {_MAX_LIST_ITEMS} textes non vides ({_MAX_LIST_ITEM_LEN} caractères maximum chacun).",
                need_id=need["id"],
            )
        return [v.strip() for v in raw_value]
    if field_type == "boolean":
        if not isinstance(raw_value, bool):
            raise CompletionError("INVALID_VALUE", f"« {label} » doit être vrai ou faux.", need_id=need["id"])
        return raw_value
    if field_type in ("text", None):
        if not (isinstance(raw_value, str) and raw_value.strip() and len(raw_value) <= _MAX_TEXT_LEN):
            raise CompletionError(
                "INVALID_VALUE", f"« {label} » doit être un texte non vide ({_MAX_TEXT_LEN} caractères maximum).", need_id=need["id"],
            )
        return raw_value.strip()
    raise CompletionError("INVALID_VALUE", f"Type de champ non pris en charge pour « {label} ».", need_id=need["id"])


def _values_equal(a: Any, b: Any) -> bool:
    if isinstance(a, bool) != isinstance(b, bool):
        return False
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return float(a) == float(b)
    return a == b


def _resolve_sourced_origin(db, *, job, need: dict, value: Any, source_proposal: Any) -> tuple[str, Optional[dict]]:
    """Lot 52 — a `source_proposal` a client echoes back from a prior `search_facts` call is RE-VERIFIED
    here, never trusted at face value. If it is absent, structurally invalid, or its own proposed value no
    longer matches what is actually being submitted (the user corrected it after seeing the proposal), the
    item is recorded as a PLAIN user declaration (`origin="declared_user"`, no source_json) — a citation
    must never be presented as proof of a value it no longer supports (ticket, verbatim; a value mismatch
    is a normal, expected path here, never an error). Only when the proposal's value is UNCHANGED AND its
    underlying source can still be read back with the exact same citation is the item recorded as
    `origin="llm_sourced"` with a frozen provenance. A structural mismatch on an otherwise-matching
    proposal (a chunk/piece/document that no longer resolves, or whose content changed) raises
    CompletionError("SOURCE_CHANGED", 409) rather than silently downgrading to a plain declaration — the
    user believed they were accepting a verified citation, so a stale one must be surfaced, never hidden,
    and never partially applied."""
    if source_proposal is None:
        return "declared_user", None
    if not isinstance(source_proposal, dict):
        raise CompletionError("INVALID_VALUE", "« source_proposal », si fourni, doit être un objet.", need_id=need["id"])
    if not _values_equal(value, source_proposal.get("value")):
        return "declared_user", None

    citation = source_proposal.get("citation")
    source = source_proposal.get("source")
    if not isinstance(citation, str) or not citation.strip() or not isinstance(source, dict):
        raise CompletionError("INVALID_VALUE", "« source_proposal » est incomplet.", need_id=need["id"])
    kind = source.get("kind")
    label = need["label"]

    if kind == "knowledge_document":
        from src.web.database.repositories import knowledge as knowledge_repo
        try:
            chunk_uuid = uuid.UUID(str(source.get("chunk_id")))
        except (ValueError, TypeError, AttributeError):
            raise CompletionError("SOURCE_CHANGED", f"La source de « {label} » est invalide : vérifiez à nouveau.", http_status=409, need_id=need["id"])
        found = knowledge_repo.get_active_chunk_by_id(db, chunk_id=chunk_uuid, organization_id=job.organization_id, owner_user_id=job.user_id)
        if found is None or not verify_citation(found[0].content, citation):
            raise CompletionError(
                "SOURCE_CHANGED", f"Le document source de « {label} » n'est plus disponible ou a changé : vérifiez à nouveau.",
                http_status=409, need_id=need["id"],
            )
    elif kind == "dossier_piece":
        from src.web.ao_dossier import storage as dossier_storage
        from src.web.database.repositories import ao_dossiers as dossiers_repo
        dossier = dossiers_repo.get_by_job(db, job_id=job.id, organization_id=job.organization_id, user_id=job.user_id)
        piece = None
        if dossier is not None and str(dossier.id) == str(source.get("dossier_id")):
            piece = next((p for p in dossier.pieces if str(p.id) == str(source.get("piece_id")) and p.admitted), None)
        chunk_content = None
        if piece is not None and piece.text_storage_key:
            try:
                chunks = dossier_storage.read_json(piece.text_storage_key)["chunks"]
                idx = source.get("chunk_index")
                if isinstance(idx, int) and not isinstance(idx, bool) and 0 <= idx < len(chunks):
                    chunk_content = chunks[idx]["content"]
            except Exception:
                chunk_content = None
        if chunk_content is None or not verify_citation(chunk_content, citation):
            raise CompletionError(
                "SOURCE_CHANGED", f"La pièce source de « {label} » n'est plus disponible ou a changé : vérifiez à nouveau.",
                http_status=409, need_id=need["id"],
            )
    elif kind == "ao_text":
        if not verify_citation(job.ao.texte_source or "", citation):
            raise CompletionError(
                "SOURCE_CHANGED", f"Le texte source de « {label} » a changé depuis la proposition : vérifiez à nouveau.",
                http_status=409, need_id=need["id"],
            )
    else:
        raise CompletionError("INVALID_VALUE", "« source_proposal.source.kind » est invalide.", need_id=need["id"])

    return "llm_sourced", {
        "kind": kind, "citation": citation.strip(), "source_label": source.get("source_label"),
        "document_version_id": source.get("document_version_id"), "chunk_id": source.get("chunk_id"),
        "dossier_id": source.get("dossier_id"), "piece_id": source.get("piece_id"), "chunk_index": source.get("chunk_index"),
        "start_char": source.get("start_char"), "end_char": source.get("end_char"), "offset_frame": source.get("offset_frame"),
        "provider": source_proposal.get("provider"), "prompt_version": source_proposal.get("prompt_version"),
        "extracted_at": source_proposal.get("extracted_at"), "search_mode": source_proposal.get("search_mode"),
    }


def apply_completion(
    db, *, job, items: list[dict], confirm_profile_write: bool, apply_current_capacity: bool,
    expected_profile_version: Optional[int] = None,
) -> dict:
    """Validates `items` against the FRESHLY recomputed needs (never the client's own claims about a need's
    subject/type/action), applies each to a private COPY of the frozen `ao`/`company`, optionally
    recomputes capacity against the account's CURRENT plan, and performs any confirmed profile write.

    Lot 49 bis: also builds the EFFECTIVE provider snapshot the revision must be scored with — a deep copy
    of the parent's own frozen `job.result.provider_snapshot`, with ONLY the explicitly-confirmed
    `declare_prestataire` facts overridden. Nothing else about the account's CURRENT profile (an unrelated
    competency, certification or business fact changed since the parent analysis ran) is ever read into
    this snapshot — see `src/web/jobs.py::_run_revision` and `src/web/scoring_context.py
    ::resolve_policy_snapshot_from_frozen_provider` for how it is later used, unmodified, by the worker.
    When at least one item is a confirmed `declare_prestataire`, `expected_profile_version` must match the
    CURRENT `ProviderProfile.version` exactly (an optimistic-concurrency check against the version the
    client was shown at preview time via `completion_state`) — a mismatch (or a missing value) refuses with
    409 and no write at all, partial or otherwise; the caller re-previews before retrying.

    Returns {"ao", "company", "capacity", "provider_snapshot", "complement_rows", "changes",
    "policy_version"} — the caller (routes_api.py) still owns creating the new job, persisting the
    complement rows under its id, and starting the revision; nothing here creates a job or a durable
    complement row itself, so this function can be safely called before the caller has decided whether a
    revision is even still allowed (e.g. a concurrent completion already exists — the caller checks that
    both before AND after this call)."""
    from src.web.database.repositories import analyses as analyses_repo

    if job.status != "done" or job.result is None:
        raise CompletionError("JOB_NOT_COMPLETE", "Cette analyse n'est pas terminée : rien à compléter.", http_status=409)
    existing = analyses_repo.get_by_parent_job_id(db, job.id)
    if existing is not None:
        raise CompletionError(
            "REVISION_ALREADY_EXISTS", "Cette analyse a déjà été complétée par une révision.",
            http_status=409, existing_job_id=existing.job_id,
        )

    needs, unavailable = needs_for_job(db, job)
    if unavailable == "parent_snapshot_unavailable":
        raise CompletionError(
            "PARENT_SNAPSHOT_UNAVAILABLE",
            "Cette analyse a été calculée avant l'enregistrement des données prestataire utilisées pour la noter : "
            "une nouvelle analyse est nécessaire, sans valeur de secours.",
            http_status=409,
        )
    if unavailable:
        raise CompletionError(
            "ORIGINAL_POLICY_UNAVAILABLE",
            "La politique qui a produit ce résultat n'est plus disponible pour ce compte : une nouvelle analyse est nécessaire.",
            http_status=409,
        )
    needs_by_id = {n["id"]: n for n in needs}
    # Frozen at analysis time — the revision's effective provider inputs start here and are amended ONLY by
    # explicitly-confirmed declare_prestataire items below, never by a live profile read.
    provider_snapshot: dict[str, Any] = copy.deepcopy(job.result.provider_snapshot)

    if not items and not apply_current_capacity:
        raise CompletionError("NOTHING_TO_APPLY", "Aucune information à compléter n'a été fournie.", http_status=400)

    seen: set[str] = set()
    ao: AOContext = job.ao.model_copy(deep=True)
    company: CompanyProfile = job.result.company_profile.model_copy(deep=True) if job.result.company_profile else CompanyProfile()
    profile_writes: list[tuple[str, str, Any, Optional[str], str, str, Optional[dict]]] = []  # (field_key, label, value, unit, need_id, origin, source_json)
    complement_rows: list[dict[str, Any]] = []
    changes: list[dict[str, Any]] = []

    for raw_item in items:
        if not isinstance(raw_item, dict) or set(raw_item.keys()) - {"need_id", "value", "source_proposal"}:
            raise CompletionError(
                "UNKNOWN_FIELD",
                "Chaque élément doit être {\"need_id\", \"value\"} avec, en option, \"source_proposal\" — aucun autre champ n'est accepté.",
            )
        need_id = raw_item.get("need_id")
        need = needs_by_id.get(need_id) if isinstance(need_id, str) else None
        if need is None:
            raise CompletionError("UNKNOWN_NEED", f"Information non attendue pour cette analyse : {need_id!r}.")
        if need_id in seen:
            raise CompletionError("DUPLICATE_NEED", f"Information envoyée plusieurs fois : {need_id!r}.", need_id=need_id)
        seen.add(need_id)
        if need["kind"] != "declarable":
            raise CompletionError(
                "NOT_DECLARABLE", f"« {need['label']} » ne peut pas être renseigné ici ({need['kind']}).",
                need_id=need_id, kind=need["kind"],
            )
        value = _validate_value(need, raw_item.get("value"))
        field_key, label, unit = need["field_key"], need["label"], need.get("unit")
        # Lot 52: re-verified here (never trusted from the client) — "declared_user"/None for a plain typed
        # value or a corrected proposal, "llm_sourced"/{...} only once the exact accepted value's source has
        # been read back again and still supports the exact citation shown at proposal time.
        origin, source_json = _resolve_sourced_origin(db, job=job, need=need, value=value, source_proposal=raw_item.get("source_proposal"))

        if need["action"] == "declare_ao":
            if field_key == "budget_estime":
                changes.append({"field": label, "subject": "ao", "field_key": field_key, "before": ao.budget_estime, "after": value, "origin": origin, "source_json": source_json})
                ao.budget_estime = value
                ao.field_provenance = {**ao.field_provenance, "budget_estime": "declared_user"}
            elif field_key == "certifications_obligatoires":
                changes.append({"field": label, "subject": "ao", "field_key": field_key, "before": list(ao.certifications_obligatoires), "after": value, "origin": origin, "source_json": source_json})
                ao.certifications_obligatoires = list(value)
                ao.field_provenance = {**ao.field_provenance, "certifications_obligatoires": "declared_user"}
            else:
                before = ao.extracted_facts.get(field_key)
                changes.append({"field": label, "subject": "ao", "field_key": field_key, "before": (before.value if before else None), "after": value, "origin": origin, "source_json": source_json})
                ao.extracted_facts = {
                    **ao.extracted_facts,
                    field_key: ExtractedFact(value=value, unit=unit, status="found", provenance="declared_user"),
                }
            complement_rows.append({
                "need_id": need_id, "subject": "ao", "field_key": field_key, "field_label": label, "value": value,
                "unit": unit, "origin": origin, "source_json": source_json,
            })
        elif need["action"] == "declare_acheteur":
            if field_key != "secteur":
                raise CompletionError("UNSUPPORTED_NEED", f"Champ acheteur non pris en charge : {field_key!r}.", need_id=need_id)
            changes.append({"field": label, "subject": "acheteur", "field_key": field_key, "before": company.secteur, "after": value, "origin": origin, "source_json": source_json})
            company.secteur = value
            complement_rows.append({
                "need_id": need_id, "subject": "acheteur", "field_key": field_key, "field_label": label, "value": value,
                "unit": None, "origin": origin, "source_json": source_json,
            })
        elif need["action"] == "declare_prestataire":
            if not confirm_profile_write:
                raise CompletionError(
                    "CONFIRMATION_REQUIRED",
                    "Confirmez l'enregistrement permanent dans votre profil avant d'appliquer cette information.",
                    need_id=need_id,
                )
            profile_writes.append((field_key, label, value, unit, need_id, origin, source_json))
        else:
            raise CompletionError("NOT_DECLARABLE", f"« {label} » ne peut pas être renseigné ici.", need_id=need_id)

    if profile_writes:
        if expected_profile_version is None:
            raise CompletionError(
                "PROFILE_VERSION_REQUIRED",
                "La version du profil prestataire prévisualisée est requise pour confirmer cette information.",
                http_status=409,
            )
        profile = provider_profile_repo.get_for_owner(db, organization_id=job.organization_id, owner_user_id=job.user_id)
        if profile is None:
            raise CompletionError("PROFILE_UNAVAILABLE", "Profil prestataire introuvable.", http_status=409)
        if profile.version != expected_profile_version:
            # Lot 49 bis: refuses outright rather than merging into a profile the client has not actually
            # seen — no partial write, the caller re-previews (fresh needs + fresh profile_version) first.
            raise CompletionError(
                "PROFILE_CHANGED",
                "Le profil prestataire a changé depuis l'aperçu : relisez les informations avant de confirmer.",
                http_status=409, current_profile_version=profile.version,
            )
        merged_facts = {k: dict(v) for k, v in (profile.business_facts or {}).items() if isinstance(v, dict)}
        # The snapshot's OWN facts (frozen at analysis time) — confirmed values are applied here, and here
        # only; every other key of `provider_snapshot` (raison_sociale, competences, certifications, and any
        # business fact not explicitly confirmed in this request) is left exactly as the parent left it.
        snapshot_facts = provider_snapshot.get("business_facts")
        snapshot_facts = dict(snapshot_facts) if isinstance(snapshot_facts, dict) else {}
        for field_key, label, value, unit, need_id, origin, source_json in profile_writes:
            fact_def = merged_facts.get(field_key) or {"key": field_key, "label": label, "type": None, "unit": unit}
            changes.append({"field": label, "subject": "prestataire", "field_key": field_key, "before": fact_def.get("value"), "after": value, "origin": origin, "source_json": source_json})
            merged_facts[field_key] = {**fact_def, "value": value}
            snapshot_fact_def = snapshot_facts.get(field_key) or {"key": field_key, "label": label, "type": None, "unit": unit}
            snapshot_facts[field_key] = {**snapshot_fact_def, "value": value}
            complement_rows.append({
                "need_id": need_id, "subject": "prestataire", "field_key": field_key, "field_label": label, "value": value,
                "unit": unit, "origin": origin, "source_json": source_json,
            })
        provider_snapshot["business_facts"] = snapshot_facts
        provider_profile_repo.save_for_owner(
            db, organization_id=job.organization_id, owner_user_id=job.user_id,
            raison_sociale=profile.raison_sociale, effectif=profile.effectif,
            competences=list(profile.competences or []), certifications=list(profile.certifications or []),
            external_enrichment_enabled=bool(profile.external_enrichment_enabled), business_facts=merged_facts,
        )

    capacity_result = job.result.capacity
    if apply_current_capacity:
        plan_row = private_capacity_repo.get_for_owner(db, organization_id=job.organization_id, owner_user_id=job.user_id)
        if plan_row is None or getattr(plan_row, "status", "configured") != "configured":
            raise CompletionError(
                "CAPACITY_NOT_CONFIGURED", "Configurez votre capacité avant d'appliquer cette mise à jour.", http_status=409,
            )
        new_capacity = CapacityAnalyzer().analyze(ao, _plan_from_row(plan_row))
        if capacity_result is None or new_capacity.model_dump() != capacity_result.model_dump():
            changes.append({
                "field": "Disponibilité de l'équipe", "subject": "capacite", "field_key": None,
                "before": capacity_result.model_dump() if capacity_result else None,
                "after": new_capacity.model_dump(), "origin": "declared_user", "source_json": None,
            })
        capacity_result = new_capacity

    return {
        "ao": ao, "company": company, "capacity": capacity_result, "provider_snapshot": provider_snapshot,
        "complement_rows": complement_rows, "changes": changes, "policy_version": job.scoring_policy_version,
    }
