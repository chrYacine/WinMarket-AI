"""Lot 49 — the server-side contract of "what would let this INCOMPLET analysis be completed".

Pure function, no I/O: given the FROZEN `ao`/`result` of one already-computed analysis, the ORIGINAL
policy's own `criteria` list (so a fact-based need can be traced back to its `fact_key`/`source` — never
guessed from `CriterionScore.motif` alone, which is a reason code, not a business-API to parse) and the
account's CURRENT declared business facts (for type/unit/label), returns one "need" per DISTINCT piece of
information — sharing one need across every criterion it feeds (never fused across AO and prestataire).

`scoring_missing` / `scoring_missing_labels` (existing, tested contract — src/agents/scoring_engine.py) are
the input, never re-derived independently: this module reads them, it does not compute its own notion of
"what is missing". `CriterionScore.motif` (== the evaluator's `Outcome.reason`) is read only to CLASSIFY a
need it already knows exists from `scoring_missing` — never treated as a stable API of its own to branch
arbitrary behavior on beyond that classification.

A need's `kind`:
  "declarable"    — a data value this account (or this analysis) can genuinely supply; see `action`.
  "conflict"      — two pieces of a dossier (or two extraction mentions) disagree; NOT resolvable by typing
                    a replacement value here (that would be an arbitrary legal/factual choice this module
                    must never make) — both values and their sources are shown, with a pointer to a new,
                    corrected dossier analysis.
  "policy"        — the gap is in the ACCOUNT'S OWN POLICY configuration (an unconfigured legacy rule, no
                    note chosen for a missing certification, a not_applicable weighting rule never
                    confirmed, a structurally invalid criterion) — never a data-entry field; the only
                    correct action is to go configure the policy.
  "informational" — nothing is actually missing in the sense of `scoring_missing` (e.g. zero RAG references
                    retained) but a pointer is useful; never changes any note or creates an artificial
                    missing state.

A need's `action` (what a client MAY do about it):
  "declare_ao"        — a per-analysis declaration (AOContext-scoped fact or fixed field). Never verified,
                         never fed back into the account's profile.
  "declare_acheteur"  — a per-analysis declaration about the buyer (e.g. its sector). Never re-triggers an
                         external lookup and never claims to be a verification.
  "declare_prestataire" — a value for the account's OWN declared profile fact; applying it is a REAL,
                         permanent profile write, gated by the caller on an explicit confirmation flag.
  "configure_policy"  — no field here; go to /app/parametres.
  "add_reference"     — no field here; go to /app/base-connaissances.
  "none"              — a conflict or a pointer only; no input is accepted for it.
"""
from __future__ import annotations

from typing import Any, Optional

FACT_EVALUATORS = ("list_coverage", "numeric_threshold", "equality")

# Raw legacy business-rule keys that can appear directly in `scoring_missing` (never prefixed with
# "criterion:"/"custom:" — see src/agents/scoring_engine.py's `missing.extend(outcome.missing_rules)`),
# with a human label for when `scoring_missing_labels` (which names the CRITERION, not the rule) is not
# specific enough on its own.
_LEGACY_RULE_LABELS = {
    "budget_minimum_eur": "Budget minimum bloquant (règle historique)",
    "max_charge_pct": "Charge maximale bloquante (règle historique)",
    "max_unmastered_technologies": "Nombre de technologies non maîtrisées bloquant (règle historique)",
    "certification_penalty_score": "Note de pénalité de certification manquante (règle historique)",
}


def _criterion_row(result, cid: str):
    for row in result.criteres:
        if row.critere_id == cid:
            return row
    return None


def _fact_definition(declared_facts: dict, fact_key: Optional[str]) -> Optional[dict]:
    if not fact_key or not isinstance(declared_facts, dict):
        return None
    fact = declared_facts.get(fact_key)
    return fact if isinstance(fact, dict) else None


def _make(*, need_id, subject, kind, label, criteria, field_key=None, field_type=None, unit=None,
          reason=None, action="none", current=None, note=None) -> dict:
    return {
        "id": need_id, "subject": subject, "kind": kind, "label": label, "criteria": list(criteria),
        "field_key": field_key, "type": field_type, "unit": unit, "reason": reason, "action": action,
        "current": current, "note": note,
    }


def _classify_one(code: str, result, criteria_by_id: dict, ao, declared_facts: dict) -> Optional[dict]:
    label = (result.scoring_missing_labels or {}).get(code, code)
    prefix, _, cid = code.partition(":")

    if prefix in ("criterion", "custom") and cid:
        row = _criterion_row(result, cid)
        evaluateur = row.evaluateur if row else None
        motif = row.motif if row else None
        crit = criteria_by_id.get(cid) or {}
        params = crit.get("params") if isinstance(crit.get("params"), dict) else {}

        if evaluateur in FACT_EVALUATORS:
            fact_key = params.get("fact_key")
            fact_def = _fact_definition(declared_facts, fact_key)
            field_type = fact_def.get("type") if fact_def else None
            unit = fact_def.get("unit") if fact_def else None
            field_label = fact_def.get("label") if fact_def else label
            if motif == "ao_fact_missing":
                return _make(need_id=code, subject="ao", kind="declarable", label=field_label, criteria=[cid],
                             field_key=fact_key, field_type=field_type, unit=unit, reason=motif, action="declare_ao")
            if motif == "provider_fact_missing":
                return _make(need_id=code, subject="prestataire", kind="declarable", label=field_label, criteria=[cid],
                             field_key=fact_key, field_type=field_type, unit=unit, reason=motif, action="declare_prestataire")
            if motif in ("unit_mismatch", "non_numeric_value", "type_mismatch"):
                return _make(need_id=code, subject="ao", kind="declarable", label=field_label, criteria=[cid],
                             field_key=fact_key, field_type=field_type, unit=unit, reason=motif, action="declare_ao",
                             note="Incohérence de type ou d'unité entre l'appel d'offres et votre profil : vérifiez "
                                  "aussi la valeur déclarée dans votre profil (Paramètres de scoring).")
            return _make(need_id=code, subject="politique", kind="policy", label=label, criteria=[cid], reason=motif, action="configure_policy")

        if evaluateur == "numeric_tiers":
            if params.get("source") == "budget":
                if motif == "budget_conflict":
                    return _make(need_id=code, subject="ao", kind="conflict", label=label, criteria=[cid],
                                 field_key="budget_estime", reason=motif, action="none",
                                 current=_budget_conflict_payload(ao))
                if motif == "budget_missing":
                    return _make(need_id=code, subject="ao", kind="declarable", label="Budget estimé", criteria=[cid],
                                 field_key="budget_estime", field_type="number", reason=motif, action="declare_ao")
            else:
                fact_key = params.get("fact_key")
                fact_def = _fact_definition(declared_facts, fact_key)
                ao_fact = (ao.extracted_facts or {}).get(fact_key) if fact_key else None
                if getattr(ao_fact, "reason", None) == "conflicting_values":
                    return _make(need_id=code, subject="ao", kind="conflict", label=label, criteria=[cid],
                                 field_key=fact_key, reason="conflicting_values", action="none")
                if motif == "ao_fact_missing":
                    return _make(need_id=code, subject="ao", kind="declarable",
                                 label=(fact_def.get("label") if fact_def else label), criteria=[cid],
                                 field_key=fact_key, field_type=(fact_def.get("type") if fact_def else None),
                                 unit=(fact_def.get("unit") if fact_def else None), reason=motif, action="declare_ao")
            return _make(need_id=code, subject="politique", kind="policy", label=label, criteria=[cid], reason=motif, action="configure_policy")

        if evaluateur == "certifications_required":
            if motif == "certifications_extraction_unknown":
                return _make(need_id=code, subject="ao", kind="declarable", label="Certifications obligatoires",
                             criteria=[cid], field_key="certifications_obligatoires", field_type="list",
                             reason=motif, action="declare_ao")
            return _make(need_id=code, subject="politique", kind="policy", label=label, criteria=[cid], reason=motif, action="configure_policy")

        if evaluateur == "legacy_sector_known_v1" and motif == "buyer_sector_unknown":
            return _make(need_id=code, subject="acheteur", kind="declarable", label="Secteur de l'acheteur",
                         criteria=[cid], field_key="secteur", field_type="text", reason=motif, action="declare_acheteur")

        # Any other structural/legacy evaluator's "manquant" state (capacity_availability never reaches
        # here per contract — see docs/qa/lot_48_20260922/PASSATION_LOT_49.md; a genuinely unexpected one is
        # a configuration gap, never guessed as data-completable).
        return _make(need_id=code, subject="politique", kind="policy", label=label, criteria=[cid], reason=motif, action="configure_policy")

    if prefix == "not_applicable":
        return _make(need_id=code, subject="politique", kind="policy", label=f"{label} — règle de pondération à confirmer",
                     criteria=[cid] if cid else [], action="configure_policy")

    # Raw legacy business-rule key (never prefixed) or "criteria:configuration" — always a policy gap.
    return _make(need_id=code, subject="politique", kind="policy",
                 label=_LEGACY_RULE_LABELS.get(code, label), criteria=[], reason=code, action="configure_policy")


def _budget_conflict_payload(ao) -> Optional[dict]:
    dossier = getattr(ao, "dossier", None)
    if not isinstance(dossier, dict):
        return None
    conflict = next((c for c in dossier.get("conflits", []) if c.get("champ") == "budget_estime"), None)
    return conflict


def _merge_key(need: dict) -> tuple:
    return (need["subject"], need["kind"], need.get("field_key"))


def compute_needs(*, ao, result, criteria: Optional[list] = None, declared_facts: Optional[dict] = None) -> list[dict]:
    """`criteria`: the criteria list of the SPECIFIC policy version that produced `result` (None if
    unavailable — every fact-based need then degrades to a generic policy pointer rather than guessing a
    fact_key). `declared_facts`: the account's CURRENT `ProviderProfile.business_facts` (None/{} if there is
    no profile at all, which cannot happen for a completed analysis but is handled defensively)."""
    criteria_by_id = {c["id"]: c for c in (criteria or []) if isinstance(c, dict) and isinstance(c.get("id"), str)}
    declared_facts = declared_facts if isinstance(declared_facts, dict) else {}

    merged: dict[tuple, dict] = {}
    order: list[tuple] = []
    for code in sorted(result.scoring_missing or []):
        need = _classify_one(code, result, criteria_by_id, ao, declared_facts)
        if need is None:
            continue
        key = _merge_key(need)
        if key in merged:
            existing = merged[key]
            existing["criteria"] = sorted(set(existing["criteria"]) | set(need["criteria"]))
        else:
            merged[key] = need
            order.append(key)

    needs = [merged[k] for k in order]

    # Informational-only: zero references retained where the policy actually configured a reference
    # criterion — never derived from scoring_missing (this evaluator never reports "manquant"; see
    # criteria_catalogue "reference_evidence" — a search with no result is not proof of no reference).
    has_reference_criterion = any(
        isinstance(c, dict) and c.get("evaluator") == "reference_evidence" and c.get("enabled", True) is not False
        for c in (criteria or [])
    )
    if has_reference_criterion and not (result.evidence_pack or []):
        needs.append(_make(
            need_id="references:zero_results", subject="references", kind="informational",
            label="Aucune référence interne retenue", criteria=[
                c["id"] for c in (criteria or []) if isinstance(c, dict) and c.get("evaluator") == "reference_evidence"
            ],
            action="add_reference",
            note="Aucun document de votre base de connaissances n'a été retenu comme référence pertinente pour cet "
                 "appel d'offres. Ce n'est pas une preuve que vous n'avez aucune référence adaptée — ajoutez ou "
                 "complétez vos documents, ou reformulez leur contenu, puis relancez une analyse.",
        ))
    return needs
