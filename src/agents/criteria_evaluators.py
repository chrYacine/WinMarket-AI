"""Lot 44 — the evaluator implementations behind `criteria_catalogue`.

Pure functions: a validated-shape criterion + an `EvalContext` in, an
`Outcome` out. They never raise for inconsistent data (the engine wraps each
call anyway), never invent a note for an unknown input, and never read
anything but the context they are given — the private configuration is
resolved by the caller.

An evaluator reports one of two states: `evaluated` (with a score) or
`missing` (no score; `recoverable=True` means the cause is UNKNOWN AO DATA, so
the criterion's own `on_missing` rule may apply — an incompatibility or a
configuration gap is never recoverable that way).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from src.agents import business_facts, criteria_catalogue

# Frozen historical detections (compatibility evaluators, identified by
# version). Not editable from a policy: only their two notes are parameters.
_LEGACY_TIGHT_DEADLINE = re.compile(r"(?i)(8|6|4)\s+semaines|imper[ae]tif|aucun\s+report|d[eé]marrage\s+imper")
_LEGACY_CONTRACT_CLAUSES = re.compile(r"(?i)p[eé]nalit[eé]|garantie|sla")


def fold_accents(text: str) -> str:
    return (
        text.lower()
        .replace('é', 'e').replace('è', 'e').replace('ê', 'e').replace('ë', 'e')
        .replace('à', 'a').replace('â', 'a')
        .replace('ù', 'u').replace('û', 'u')
        .replace('î', 'i').replace('ï', 'i')
        .replace('ô', 'o')
        .replace('ç', 'c')
    )


@dataclass
class EvalContext:
    ao: Any
    company: Any
    evidences: list
    capacity: Any
    mastered: frozenset
    certifications_held: frozenset
    declared_facts: dict
    legacy_unconfigured_rules: frozenset = frozenset()

    def __post_init__(self):
        self.techs = {str(t).lower() for t in list(getattr(self.ao, "technologies_demandees", []) or []) + list(getattr(self.ao, "competences_requises", []) or [])}


@dataclass
class Outcome:
    state: str  # "evaluated" | "missing"
    score: Optional[float] = None
    justification: str = ""
    reason: Optional[str] = None
    recoverable: bool = False
    blocker: Optional[str] = None
    missing_rules: list = field(default_factory=list)
    assumed: bool = False


def _missing(reason: str, justification: str, *, recoverable: bool, blocker: Optional[str] = None) -> Outcome:
    return Outcome("missing", None, justification, reason, recoverable, blocker)


def _technology_coverage(p: dict, ctx: EvalContext, spec: dict) -> Outcome:
    unknown = sorted(t for t in ctx.techs if t not in ctx.mastered)
    limit = p.get("max_unmastered_blocking")
    blocker = ("Trop de technologies non maitrisees : " + ", ".join(unknown[:5])) if limit is not None and len(unknown) >= limit else None
    rules = ["max_unmastered_technologies"] if "max_unmastered_technologies" in ctx.legacy_unconfigured_rules else []
    if not ctx.techs:
        note = p.get("none_requested_score")
        if note is None:
            out = _missing("no_technology_identified", "Aucune technologie ou compétence identifiée dans l'AO : critère non évalué.",
                           recoverable=True, blocker=blocker)
        else:
            out = Outcome("evaluated", note, "Aucune technologie ou compétence identifiée dans l'AO.", blocker=blocker)
    else:
        matched = len(ctx.techs) - len(unknown)
        out = Outcome("evaluated", round(100 * matched / len(ctx.techs), 1),
                      f"{matched}/{len(ctx.techs)} technologies ou compétences demandées figurent dans le profil déclaré.", blocker=blocker)
    out.missing_rules = rules
    return out


def _reference_evidence(p: dict, ctx: EvalContext, spec: dict) -> Outcome:
    # Recette corpus utilisateur (2026-09-24), §4 — DEFECT reproduced and fixed: `n` used to be
    # `len(ctx.evidences)` directly, so a single document merely producing several passages
    # (through no choice of the account's — e.g. lot 51's token-based windowing splitting one
    # long section) counted as several "references", inflating both the count and the
    # similarity average. Grouped by document/version identity first (src.core.reference_
    # identity.group_evidences_by_reference) — the SAME B18 "best representative per group,
    # never summed/averaged across duplicates" principle already applied to exact-content
    # duplicates, applied one level higher (per reference, not per exact-content chunk). Every
    # citation stays in ctx.evidences/evidence_pack for display/reranking — only the COUNTING
    # here is deduplicated; no citation is ever discarded.
    from src.core.reference_identity import group_evidences_by_reference

    groups = group_evidences_by_reference(ctx.evidences)
    n = len(groups)
    per_reference_scores = [max(min(e.score, 1.0) for e in group) for group in groups]
    avg = sum(per_reference_scores) / max(n, 1)
    score = min(100, p["base_score"] + n * p["per_reference"] + avg * p["similarity_weight"])
    return Outcome("evaluated", score, f"{n} référence(s) probante(s) retenue(s) (similarité moyenne {avg:.2f}).")


def _capacity_availability(p: dict, ctx: EvalContext, spec: dict) -> Outcome:
    cap = ctx.capacity
    score = p["available_score"] if cap.equipe_disponible else p["unavailable_score"]
    limit = p.get("max_charge_blocking")
    blocker = f"Charge equipe superieure a {limit:g}% — impossible de demarrer" if limit is not None and cap.charge_actuelle_pct > limit else None
    rules = ["max_charge_pct"] if "max_charge_pct" in ctx.legacy_unconfigured_rules else []
    return Outcome("evaluated", score, cap.commentaire, blocker=blocker, missing_rules=rules)


def _numeric_tiers(p: dict, ctx: EvalContext, spec: dict) -> Outcome:
    is_budget = p.get("source") == "budget"
    rules = ["budget_minimum_eur"] if is_budget and "budget_minimum_eur" in ctx.legacy_unconfigured_rules else []
    if is_budget:
        value = getattr(ctx.ao, "budget_estime", None)
        subject, note = "Budget estimé", " Comparaison du budget seul : coûts et périmètre non évalués."
        if (getattr(ctx.ao, "field_provenance", None) or {}).get("budget_estime") == "conflict":
            # Lot 47 bis: two pieces of the dossier state different budgets — no value was retained. NOT recoverable:
            # an explicit "note when the datum is absent" of the policy must never turn a contradiction into a
            # favourable certain conclusion; the decision stays INCOMPLET until the pieces agree.
            out = _missing("budget_conflict", "Budget contradictoire entre les pièces du dossier : critère non évalué.", recoverable=False)
            out.missing_rules = rules
            return out
        if value is None:
            out = _missing("budget_missing", "Budget non communiqué : critère non évalué.", recoverable=True)
            out.missing_rules = rules
            return out
    else:
        fact = ctx.ao.extracted_facts.get(p.get("fact_key")) if isinstance(getattr(ctx.ao, "extracted_facts", None), dict) else None
        subject, note = "Valeur extraite de l'AO", ""
        found = fact is not None and getattr(fact, "status", None) == "found" and business_facts._is_finite_number(getattr(fact, "value", None))
        if not found:
            why = getattr(fact, "reason", None) or getattr(fact, "status", None) or "absent"
            # Lot 47 bis: contradictory values between the pieces of a dossier are not an "absent datum" — no
            # explicit fallback note may turn them into a favourable conclusion.
            return _missing("ao_fact_missing", f"Donnée manquante pour ce critère (raison : {why}) — critère non évalué.",
                            recoverable=getattr(fact, "reason", None) != "conflicting_values")
        value = fact.value
    minimum = p.get("minimum_blocking")
    blocker = None
    if minimum is not None and value < minimum:
        blocker = f"{'Budget' if is_budget else 'Valeur'} inférieur au minimum fixé par la politique ({f'{minimum:,.0f}'.replace(',', ' ')})"
    if p.get("zero_score") is not None and value == 0:
        score, text = p["zero_score"], f"{subject} nul (0)."
    else:
        score, text = p["below_score"], f"{subject} {value:,.0f} : sous le plus petit palier."
        for tier in sorted(p["tiers"], key=lambda t: -t["at_least"]):
            if value >= tier["at_least"]:
                score, text = tier["score"], f"{subject} {value:,.0f} : palier ≥ {tier['at_least']:,.0f} (note {tier['score']:g})."
                break
    return Outcome("evaluated", score, text + note, blocker=blocker, missing_rules=rules)


def _certifications_required(p: dict, ctx: EvalContext, spec: dict) -> Outcome:
    mandatory = list(getattr(ctx.ao, "certifications_obligatoires", []) or [])
    missing = [c for c in mandatory if str(c).lower() not in ctx.certifications_held]
    blocker = ("Certification obligatoire absente : " + ", ".join(missing)) if missing and p.get("block_when_missing") else None
    if not mandatory:
        provenance = (getattr(ctx.ao, "field_provenance", None) or {}).get("certifications_obligatoires")
        if p.get("when_extraction_unknown") == "missing" and provenance in ("absent", "rejected"):
            return _missing("certifications_extraction_unknown",
                            "Certifications exigées non identifiées dans l'AO : l'absence d'extraction n'est pas une preuve de conformité.",
                            recoverable=True)
        return Outcome("evaluated", p["covered_score"], "Aucune certification obligatoire identifiée dans l'AO.")
    if not missing:
        return Outcome("evaluated", p["covered_score"], "Certifications obligatoires couvertes par le profil déclaré.")
    text = f"Certifications manquantes : {', '.join(missing)}"
    if p.get("missing_score") is not None:
        return Outcome("evaluated", p["missing_score"], text, blocker=blocker)
    if "certification_penalty_score" in ctx.legacy_unconfigured_rules:
        # Historical placeholder kept for compatibility: not a business
        # decision — the result is INCOMPLET until the rule is configured.
        return Outcome("evaluated", 20, text, blocker=blocker, missing_rules=["certification_penalty_score"])
    return _missing("missing_score_not_configured", text + " — aucune note choisie par la politique.", recoverable=False, blocker=blocker)


def _technology_count_tiers(p: dict, ctx: EvalContext, spec: dict) -> Outcome:
    count = len(ctx.techs)
    if count == 0 and p.get("empty_is_unknown"):
        return _missing("no_technology_identified", "Aucune technologie identifiée dans l'AO : critère non évalué.", recoverable=True)
    score = p["above_score"]
    for tier in sorted(p["tiers"], key=lambda t: t["at_most"]):
        if count <= tier["at_most"]:
            score = tier["score"]
            break
    return Outcome("evaluated", score, f"{count} technologie(s) ou compétence(s) demandée(s) identifiée(s).")


def _keyword_set_match(p: dict, ctx: EvalContext, spec: dict) -> Outcome:
    if not ctx.techs and p.get("empty_is_unknown"):
        return _missing("no_technology_identified", "Aucune technologie identifiée dans l'AO : critère non évalué.", recoverable=True)
    matched = sorted(ctx.techs & {str(k).lower() for k in p["any_of"]})
    if matched:
        return Outcome("evaluated", p["match_score"], f"Élément(s) de l'ensemble du compte présent(s) dans l'AO : {', '.join(matched)}.")
    return Outcome("evaluated", p["no_match_score"], "Aucun élément de l'ensemble du compte n'est présent dans l'AO.")


def _legacy_tight_deadline(p: dict, ctx: EvalContext, spec: dict) -> Outcome:
    if _LEGACY_TIGHT_DEADLINE.search(getattr(ctx.ao, "texte_source", "") or ""):
        return Outcome("evaluated", p["tight_score"], "Formulation de délai serré ou impératif détectée dans le texte de l'AO (détection par mots-clés).")
    return Outcome("evaluated", p["otherwise_score"], "Aucune formulation de délai serré détectée dans le texte de l'AO (détection par mots-clés).")


def _legacy_contract_clauses(p: dict, ctx: EvalContext, spec: dict) -> Outcome:
    if _LEGACY_CONTRACT_CLAUSES.search(getattr(ctx.ao, "texte_source", "") or ""):
        return Outcome("evaluated", p["clauses_score"], "Mot-clé de clause (pénalité, garantie, SLA) détecté dans le texte de l'AO.")
    return Outcome("evaluated", p["otherwise_score"], "Aucun mot-clé de clause (pénalité, garantie, SLA) détecté dans le texte de l'AO.")


def _legacy_sector_known(p: dict, ctx: EvalContext, spec: dict) -> Outcome:
    sector = getattr(ctx.company, "secteur", "") or ""
    if fold_accents(sector) in ("non renseigne", ""):
        return _missing("buyer_sector_unknown", "Secteur de l'acheteur non renseigné.", recoverable=True)
    return Outcome("evaluated", p["known_score"], f"Secteur de l'acheteur renseigné : {sector}.")


def _legacy_client_solvency(p: dict, ctx: EvalContext, spec: dict) -> Outcome:
    label = getattr(ctx.company, "solidite_financiere", "") or ""
    favorable = label in ("Bonne", "A verifier")
    score = p["favorable_score"] if favorable else p["otherwise_score"]
    if fold_accents(label) in ("non renseigne", ""):
        return Outcome("evaluated", score, "Solidité de l'acheteur non renseignée : note de repli de la politique historique appliquée.", assumed=True)
    return Outcome("evaluated", score, f"Solidité de l'acheteur : {label}.")


def _fact_comparator(p: dict, ctx: EvalContext, spec: dict) -> Outcome:
    fact_key = p.get("fact_key")
    ao_facts = getattr(ctx.ao, "extracted_facts", None)
    ao_fact = ao_facts.get(fact_key) if isinstance(ao_facts, dict) and isinstance(fact_key, str) else None
    declared = ctx.declared_facts.get(fact_key) if isinstance(ctx.declared_facts, dict) and isinstance(fact_key, str) else None
    declared = declared if isinstance(declared, dict) else {}
    score, passed, reason = business_facts.evaluate_custom_criterion(
        criteria_catalogue._legacy_fact_spec(spec), ao_fact=ao_fact,
        provider_value=declared.get("value"), provider_unit=declared.get("unit"),
    )
    if score is None:
        extraction_reason = getattr(ao_fact, "reason", None)
        detail = reason + (f", extraction : {extraction_reason}" if extraction_reason else "")
        return _missing(reason, f"Donnée manquante pour ce critère (raison : {detail}) — critère non évalué.", recoverable=(reason == "ao_fact_missing"))
    blocker = f"{spec.get('label') or spec.get('id')} : condition bloquante non satisfaite" if spec.get("blocking") is True and not passed else None
    return Outcome("evaluated", score, "Condition satisfaite." if passed else "Condition non satisfaite.", blocker=blocker)


def _invalid(p: dict, ctx: EvalContext, spec: dict) -> Outcome:
    return _missing("invalid_criterion", "Critère historique invalide ou incohérent : non évaluable.", recoverable=False)


_IMPLEMENTATIONS = {
    "technology_coverage": _technology_coverage, "reference_evidence": _reference_evidence,
    "capacity_availability": _capacity_availability, "numeric_tiers": _numeric_tiers,
    "certifications_required": _certifications_required, "technology_count_tiers": _technology_count_tiers,
    "keyword_set_match": _keyword_set_match, "legacy_tight_deadline_v1": _legacy_tight_deadline,
    "legacy_contract_clauses_v1": _legacy_contract_clauses, "legacy_sector_known_v1": _legacy_sector_known,
    "legacy_client_solvency_v1": _legacy_client_solvency, "list_coverage": _fact_comparator,
    "numeric_threshold": _fact_comparator, "equality": _fact_comparator, "invalid": _invalid,
}


def evaluate(spec: dict, ctx: EvalContext) -> Outcome:
    """Never raises: an unknown evaluator, a malformed `params` or an
    unexpected data shape is reported as a non-recoverable missing criterion
    (INCOMPLET), never a favorable score."""
    try:
        implementation = _IMPLEMENTATIONS.get(spec.get("evaluator")) if isinstance(spec, dict) else None
        if implementation is None:
            return _invalid({}, ctx, spec)
        params = spec.get("params") if isinstance(spec.get("params"), dict) else {}
        return implementation(params, ctx, spec)
    except Exception:  # noqa: BLE001 - defensive by contract, see docstring
        return _missing("evaluation_error", "Le critère n'a pas pu être évalué (paramètres ou données incohérents).", recoverable=False)
