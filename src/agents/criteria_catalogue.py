"""Lot 44 — the explicit, versioned CRITERIA contract of a private scoring
policy.

One technical engine (src/agents/scoring_engine.py) evaluates a LIST of
criteria; each criterion names an evaluator from the closed catalogue below
and carries its own parameters. Nothing here is executable: a parameter is a
number, a list of texts, a list of tiers or one choice from a fixed list —
validated data, never a formula string.

A criterion (schema version `SCHEMA_VERSION`):

    {"id": "budget", "label": "Budget estimé", "evaluator": "numeric_tiers",
     "params": {...},               # validated against the evaluator's schema
     "weight": 20,                  # 0..100, enabled weights sum to 100
     "blocking": false,             # comparators: unsatisfied => NO-GO
     "on_missing": {"mode": "incomplete"},   # or explicit_score / not_applicable
     "enabled": true, "disabled_reason": null}

What stays technical and coded (not business assumptions): the 0–100 bounds,
the rounding, the weighted sum, the decision order (blocker > INCOMPLET >
thresholds), the evaluator implementations and this catalogue.

Historical policies: `materialize_legacy` turns the 12 historical criteria,
their four business rules and the custom criteria of a pre-lot-44 policy into
explicit criteria carrying their CURRENT values (same weights, thresholds,
notes and unknown-data handling). It is a representation migration, not an
approval of those values by the account. The few historical rules that cannot
be expressed as neutral parameters (keyword/regex detections, the buyer's
financial-solidity label, the "sector unknown" test) are kept as
`legacy_*_v1` evaluators — identified by version, reported as a remainder.
"""
from __future__ import annotations

import math
import re
from typing import Any, Optional

from src.agents import business_facts

SCHEMA_VERSION = 1

ON_MISSING_MODES = ("incomplete", "explicit_score", "not_applicable")
NOT_APPLICABLE_RULES = ("incomplete", "renormalize")

# The twelve historical criteria: policy key -> display label (the `nom` a
# historical result carries) and the evaluator that reproduces it.
LEGACY_LABELS = {
    "Adequation expertise": "Adéquation expertise",
    "References similaires": "Références similaires",
    "Disponibilite equipe": "Disponibilité équipe",
    "Rentabilite estimee": "Rentabilité estimée",
    "Faisabilite delai": "Faisabilité délai",
    "Certifications requises": "Certifications requises",
    "Complexite technique": "Complexité technique",
    "Connaissance secteur": "Connaissance secteur",
    "Potentiel commercial": "Potentiel commercial",
    "Risque contractuel": "Risque contractuel",
    "Solidite client": "Solidité client",
    "Valeur strategique": "Valeur stratégique",
}
_LEGACY_IDS = {
    "Adequation expertise": "adequation_expertise", "References similaires": "references_similaires",
    "Disponibilite equipe": "disponibilite_equipe", "Rentabilite estimee": "rentabilite_estimee",
    "Faisabilite delai": "faisabilite_delai", "Certifications requises": "certifications_requises",
    "Complexite technique": "complexite_technique", "Connaissance secteur": "connaissance_secteur",
    "Potentiel commercial": "potentiel_commercial", "Risque contractuel": "risque_contractuel",
    "Solidite client": "solidite_client", "Valeur strategique": "valeur_strategique",
}

_FACT_EVALUATORS = ("list_coverage", "numeric_threshold", "equality")

# Parameter types understood by `_validate_param` (and by the settings form).
# name, type, required, label, [choices]
def _p(name, ptype, label, required=True, choices=None):
    return {"name": name, "type": ptype, "label": label, "required": required, **({"choices": choices} if choices else {})}


EVALUATORS: dict[str, dict] = {
    "list_coverage": {
        "family": "fact", "label": "Couverture d'une liste (fait métier)",
        "description": "Ce que l'AO exige (liste extraite) doit être inclus dans ce que le compte déclare couvrir.",
        "requires": ["fait métier déclaré par le compte", "fait extrait de l'AO (même identifiant)"],
        "params": [_p("fact_key", "fact_key", "Fait métier"), _p("pass_score", "score", "Note si satisfait"),
                   _p("fail_score", "score", "Note si non satisfait")],
        "supports_blocking": True, "missing_ao_data_is_recoverable": True,
    },
    "numeric_threshold": {
        "family": "fact", "label": "Seuil numérique (fait métier)",
        "description": "Compare une valeur déclarée par le compte à celle extraite de l'AO (unités identiques, jamais converties).",
        "requires": ["fait métier numérique déclaré par le compte", "fait extrait de l'AO, même unité"],
        "params": [_p("fact_key", "fact_key", "Fait métier"),
                   _p("comparison", "choice", "Comparaison", choices=sorted(business_facts.NUMERIC_COMPARISONS)),
                   _p("pass_score", "score", "Note si satisfait"), _p("fail_score", "score", "Note si non satisfait")],
        "supports_blocking": True, "missing_ao_data_is_recoverable": True,
    },
    "equality": {
        "family": "fact", "label": "Égalité stricte (fait métier)",
        "description": "La valeur déclarée par le compte doit être égale à celle extraite de l'AO (booléen, nombre ou texte).",
        "requires": ["fait métier déclaré par le compte", "fait extrait de l'AO (même identifiant)"],
        "params": [_p("fact_key", "fact_key", "Fait métier"), _p("pass_score", "score", "Note si satisfait"),
                   _p("fail_score", "score", "Note si non satisfait")],
        "supports_blocking": True, "missing_ao_data_is_recoverable": True,
    },
    "numeric_tiers": {
        "family": "structural", "label": "Paliers sur une valeur numérique",
        "description": "Note par paliers d'une valeur numérique : le budget estimé de l'AO (le budget SEUL — coûts, marge et périmètre ne sont pas évalués) ou un fait numérique extrait.",
        "requires": ["budget estimé de l'AO, ou fait numérique extrait de l'AO"],
        "params": [_p("source", "choice", "Source", choices=["budget", "fact"]),
                   _p("fact_key", "fact_key", "Fait numérique (si source = fait)", required=False),
                   _p("tiers", "tiers_at_least", "Paliers (valeur ≥ seuil → note)"),
                   _p("below_score", "score", "Note sous le plus petit palier"),
                   _p("zero_score", "score", "Note si la valeur est exactement 0", required=False),
                   _p("minimum_blocking", "amount", "Minimum bloquant (valeur inférieure → NO-GO)", required=False)],
        "supports_blocking": False, "missing_ao_data_is_recoverable": True,
    },
    "technology_coverage": {
        "family": "structural", "label": "Couverture des technologies/compétences demandées",
        "description": "Part des technologies et compétences demandées par l'AO figurant dans les compétences déclarées du compte.",
        "requires": ["technologies et compétences extraites de l'AO", "compétences déclarées dans le profil"],
        "params": [_p("none_requested_score", "score", "Note si l'AO n'en demande aucune (vide = donnée manquante)", required=False),
                   _p("max_unmastered_blocking", "count", "Bloquant à partir de N technologies non maîtrisées", required=False)],
        "supports_blocking": False, "missing_ao_data_is_recoverable": True,
    },
    "technology_count_tiers": {
        "family": "structural", "label": "Nombre de technologies demandées (paliers)",
        "description": "Note selon le nombre de technologies et compétences demandées par l'AO.",
        "requires": ["technologies et compétences extraites de l'AO"],
        "params": [_p("tiers", "tiers_at_most", "Paliers (nombre ≤ seuil → note)"), _p("above_score", "score", "Note au-delà du dernier palier"),
                   _p("empty_is_unknown", "bool", "Aucune technologie identifiée = donnée manquante")],
        "supports_blocking": False, "missing_ao_data_is_recoverable": True,
    },
    "keyword_set_match": {
        "family": "structural", "label": "Présence d'au moins un élément d'un ensemble",
        "description": "Note selon qu'au moins une des technologies/compétences demandées appartient à l'ensemble de mots (comparaison exacte, sans casse) choisi par le compte.",
        "requires": ["technologies et compétences extraites de l'AO"],
        "params": [_p("any_of", "text_list", "Ensemble de mots du compte"), _p("match_score", "score", "Note si présent"),
                   _p("no_match_score", "score", "Note si absent"), _p("empty_is_unknown", "bool", "Aucune technologie identifiée = donnée manquante")],
        "supports_blocking": False, "missing_ao_data_is_recoverable": True,
    },
    "reference_evidence": {
        "family": "structural", "label": "Références internes probantes",
        "description": "Note = min(100, base + par référence × nombre + poids de similarité × similarité moyenne), à partir des références retenues dans la base privée du compte.",
        "requires": ["références retenues dans la base de connaissances du compte"],
        "params": [_p("base_score", "score", "Note de base"), _p("per_reference", "amount", "Points par référence"),
                   _p("similarity_weight", "amount", "Points × similarité moyenne")],
        "supports_blocking": False, "missing_ao_data_is_recoverable": False,
    },
    "capacity_availability": {
        "family": "structural", "label": "Disponibilité de l'équipe",
        "description": "Note selon que le plan de capacité du compte laisse ou non la marge minimale qu'il a déclarée.",
        "requires": ["plan de capacité privé du compte"],
        "params": [_p("available_score", "score", "Note si disponible"), _p("unavailable_score", "score", "Note si non disponible"),
                   _p("max_charge_blocking", "pct", "Charge maximale bloquante (%)", required=False)],
        "supports_blocking": False, "missing_ao_data_is_recoverable": False,
    },
    "certifications_required": {
        "family": "structural", "label": "Certifications exigées par l'AO",
        "description": "Compare les certifications obligatoires extraites de l'AO à celles déclarées par le compte.",
        "requires": ["certifications obligatoires extraites de l'AO", "certifications déclarées dans le profil"],
        "params": [_p("covered_score", "score", "Note si aucune n'est manquante"),
                   _p("missing_score", "score", "Note si une certification manque (vide = non évalué)", required=False),
                   _p("block_when_missing", "bool", "Une certification manquante est bloquante"),
                   _p("when_extraction_unknown", "choice", "Extraction des certifications inconnue", choices=["missing", "treat_as_none"])],
        "supports_blocking": False, "missing_ao_data_is_recoverable": True,
    },
    # ---- compatibility evaluators, identified by version (historical rules
    # that cannot be expressed as neutral parameters) --------------------------
    "legacy_tight_deadline_v1": {
        "family": "legacy", "label": "Historique v1 — délai serré détecté par mots-clés",
        "description": "Règle historique : détecte dans le texte de l'AO « 4/6/8 semaines », « impératif », « aucun report », « démarrage impératif ». Détection par mots, pas comparaison de faits ; conservée pour compatibilité.",
        "requires": ["texte de l'AO"],
        "params": [_p("tight_score", "score", "Note si détecté"), _p("otherwise_score", "score", "Note sinon")],
        "supports_blocking": False, "missing_ao_data_is_recoverable": False,
    },
    "legacy_contract_clauses_v1": {
        "family": "legacy", "label": "Historique v1 — clauses détectées par mots-clés",
        "description": "Règle historique : détecte « pénalité », « garantie » ou « sla » dans le texte de l'AO. Détection par mots ; conservée pour compatibilité.",
        "requires": ["texte de l'AO"],
        "params": [_p("clauses_score", "score", "Note si détecté"), _p("otherwise_score", "score", "Note sinon")],
        "supports_blocking": False, "missing_ao_data_is_recoverable": False,
    },
    "legacy_sector_known_v1": {
        "family": "legacy", "label": "Historique v1 — secteur de l'acheteur connu",
        "description": "Règle historique : une note si le secteur de l'acheteur est renseigné (quel qu'il soit), donnée manquante sinon. Ne compare pas aux secteurs du compte.",
        "requires": ["secteur de l'acheteur (extraction / enrichissement)"],
        "params": [_p("known_score", "score", "Note si le secteur est connu")],
        "supports_blocking": False, "missing_ao_data_is_recoverable": True,
    },
    "legacy_client_solvency_v1": {
        "family": "legacy", "label": "Historique v1 — solidité financière de l'acheteur",
        "description": "Règle historique : « Bonne » ou « A verifier » (sans accent) → note favorable, tout autre libellé → note de repli. « À vérifier » avec accent reste dans la branche prudente (décision B04 conservée).",
        "requires": ["libellé de solidité de l'acheteur (enrichissement)"],
        "params": [_p("favorable_score", "score", "Note favorable"), _p("otherwise_score", "score", "Note de repli")],
        "supports_blocking": False, "missing_ao_data_is_recoverable": False,
    },
    "invalid": {
        "family": "legacy", "label": "Critère historique invalide",
        "description": "Représentation d'une donnée historique incohérente : jamais évaluable, le résultat est INCOMPLET.",
        "requires": [], "params": [], "supports_blocking": False, "missing_ao_data_is_recoverable": False,
    },
}

# What the account cannot (yet) ask the engine to compute — shown by the form
# so no criterion is promised whose data or operator does not exist.
UNAVAILABLE = [
    {"label": "Rentabilité / marge",
     "reason": "Le calcul d'une marge exige les coûts et le périmètre de l'AO, qui ne sont pas modélisés. Seule la comparaison du budget estimé est disponible (« Paliers sur une valeur numérique », source = budget)."},
    {"label": "Délai et échéance",
     "reason": "Pas d'évaluateur dédié : créez un fait numérique (ex. délai demandé en semaines) et comparez-le avec « Seuil numérique »."},
    {"label": "Secteur et positionnement stratégique",
     "reason": "Pas d'évaluateur dédié : créez un fait « liste » (secteurs visés) et utilisez « Couverture d'une liste », ou l'ensemble de mots « Présence d'un élément »."},
    {"label": "Distinction certification déclarée / vérifiée",
     "reason": "Stockée dans le profil mais non exploitée par le calcul : toute certification déclarée compte comme détenue (limite connue)."},
]


def evaluator_family(key: Any) -> Optional[str]:
    return EVALUATORS[key]["family"] if isinstance(key, str) and key in EVALUATORS else None


def catalogue_payload() -> dict:
    """JSON-able catalogue for the settings screen. An aid to form building —
    the server re-validates everything."""
    return {
        "schema_version": SCHEMA_VERSION,
        "evaluators": {k: {kk: vv for kk, vv in v.items()} for k, v in EVALUATORS.items() if k != "invalid"},
        "on_missing_modes": list(ON_MISSING_MODES),
        "not_applicable_rules": list(NOT_APPLICABLE_RULES),
        "unavailable": UNAVAILABLE,
    }


# ---------------------------------------------------------------------------
# Historical policy -> explicit criteria (representation migration)
# ---------------------------------------------------------------------------

def default_settings() -> dict:
    return {"not_applicable_rule": "incomplete", "not_applicable_rule_confirmed": False,
            "strengths_at_least": None, "weaknesses_below": None, "legacy_unconfigured_rules": []}


def _base(spec_id, label, evaluator, params, weight, *, blocking=False, on_missing=None, legacy_key=None):
    out = {"id": spec_id, "label": label, "evaluator": evaluator, "params": params, "weight": weight,
           "blocking": blocking, "on_missing": on_missing or {"mode": "incomplete"}, "enabled": True, "disabled_reason": None}
    if legacy_key:
        out["legacy_key"] = legacy_key
    return out


def _rule(business_rules: dict, name: str):
    """A historical business rule: the configured number, or None."""
    value = business_rules.get(name) if isinstance(business_rules, dict) else None
    return value if business_facts._is_finite_number(value) else None


def materialize_legacy(*, weights: Any, business_rules: Any, custom_criteria: Any) -> tuple[list[dict], dict]:
    """The historical formulas and values of a pre-lot-44 policy as explicit
    criteria. Deterministic and pure. A rule the policy never configured is
    NOT given a value: it is listed in `settings["legacy_unconfigured_rules"]`
    and keeps making the analysis INCOMPLET, exactly as before."""
    settings = default_settings()
    settings["strengths_at_least"] = 78
    settings["weaknesses_below"] = 60
    rules = business_rules if isinstance(business_rules, dict) else {}
    unconfigured = [n for n in ("budget_minimum_eur", "max_charge_pct", "max_unmastered_technologies", "certification_penalty_score")
                    if _rule(rules, n) is None]
    settings["legacy_unconfigured_rules"] = unconfigured
    budget_min, max_charge = _rule(rules, "budget_minimum_eur"), _rule(rules, "max_charge_pct")
    max_unmastered, cert_penalty = _rule(rules, "max_unmastered_technologies"), _rule(rules, "certification_penalty_score")

    builders = {
        "Adequation expertise": lambda w, k: _base(_LEGACY_IDS[k], LEGACY_LABELS[k], "technology_coverage",
            {"none_requested_score": 80, "max_unmastered_blocking": max_unmastered}, w, legacy_key=k),
        "References similaires": lambda w, k: _base(_LEGACY_IDS[k], LEGACY_LABELS[k], "reference_evidence",
            {"base_score": 30, "per_reference": 6, "similarity_weight": 40}, w, legacy_key=k),
        "Disponibilite equipe": lambda w, k: _base(_LEGACY_IDS[k], LEGACY_LABELS[k], "capacity_availability",
            {"available_score": 90, "unavailable_score": 45, "max_charge_blocking": max_charge}, w, legacy_key=k),
        "Rentabilite estimee": lambda w, k: _base(_LEGACY_IDS[k], LEGACY_LABELS[k], "numeric_tiers",
            {"source": "budget", "fact_key": None,
             "tiers": [{"at_least": 200000, "score": 90}, {"at_least": 100000, "score": 80}, {"at_least": 60000, "score": 55}],
             "below_score": 40, "zero_score": 65, "minimum_blocking": budget_min},
            w, on_missing={"mode": "explicit_score", "score": 65}, legacy_key=k),
        "Faisabilite delai": lambda w, k: _base(_LEGACY_IDS[k], LEGACY_LABELS[k], "legacy_tight_deadline_v1",
            {"tight_score": 45, "otherwise_score": 75}, w, legacy_key=k),
        "Certifications requises": lambda w, k: _base(_LEGACY_IDS[k], LEGACY_LABELS[k], "certifications_required",
            {"covered_score": 100, "missing_score": cert_penalty, "block_when_missing": True,
             "when_extraction_unknown": "treat_as_none"}, w, legacy_key=k),
        "Complexite technique": lambda w, k: _base(_LEGACY_IDS[k], LEGACY_LABELS[k], "technology_count_tiers",
            {"tiers": [{"at_most": 4, "score": 80}, {"at_most": 7, "score": 60}], "above_score": 40, "empty_is_unknown": False},
            w, legacy_key=k),
        "Connaissance secteur": lambda w, k: _base(_LEGACY_IDS[k], LEGACY_LABELS[k], "legacy_sector_known_v1",
            {"known_score": 80}, w, on_missing={"mode": "explicit_score", "score": 65}, legacy_key=k),
        "Potentiel commercial": lambda w, k: _base(_LEGACY_IDS[k], LEGACY_LABELS[k], "numeric_tiers",
            {"source": "budget", "fact_key": None,
             "tiers": [{"at_least": 150000, "score": 85}, {"at_least": 70000, "score": 60}],
             "below_score": 40, "zero_score": None, "minimum_blocking": None},
            w, on_missing={"mode": "explicit_score", "score": 40}, legacy_key=k),
        "Risque contractuel": lambda w, k: _base(_LEGACY_IDS[k], LEGACY_LABELS[k], "legacy_contract_clauses_v1",
            {"clauses_score": 60, "otherwise_score": 80}, w, legacy_key=k),
        "Solidite client": lambda w, k: _base(_LEGACY_IDS[k], LEGACY_LABELS[k], "legacy_client_solvency_v1",
            {"favorable_score": 85, "otherwise_score": 60}, w, legacy_key=k),
        "Valeur strategique": lambda w, k: _base(_LEGACY_IDS[k], LEGACY_LABELS[k], "keyword_set_match",
            {"any_of": ["ia", "rag", "llm", "cloud", "azure", "aws", "data"], "match_score": 90, "no_match_score": 70,
             "empty_is_unknown": False}, w, legacy_key=k),
    }
    criteria: list[dict] = []
    if isinstance(weights, dict):
        for key, weight in weights.items():
            builder = builders.get(key)
            if builder is None:
                criteria.append(_base(f"invalide_{len(criteria)}", str(key), "invalid", {}, weight))
            else:
                criteria.append(builder(weight, key))
    if isinstance(custom_criteria, list):
        for index, spec in enumerate(custom_criteria):
            if not isinstance(spec, dict):
                criteria.append(_base(f"invalide_{index}", f"invalide_{index}", "invalid", {}, 0))
                continue
            operator = spec.get("operator")
            params = {"fact_key": spec.get("fact_key"), "pass_score": spec.get("pass_score"), "fail_score": spec.get("fail_score")}
            if operator == "numeric_threshold" or "comparison" in spec:
                params["comparison"] = spec.get("comparison")
            spec_id = spec.get("id") if isinstance(spec.get("id"), str) and spec.get("id") else f"invalide_{index}"
            label = spec.get("label") if isinstance(spec.get("label"), str) and spec.get("label").strip() else spec_id
            evaluator = operator if isinstance(operator, str) and operator in _FACT_EVALUATORS else "invalid"
            criteria.append(_base(spec_id, label, evaluator, params if evaluator != "invalid" else {}, spec.get("weight"),
                                  blocking=spec.get("blocking") if isinstance(spec.get("blocking"), bool) else False))
    elif custom_criteria not in (None, []):
        criteria.append(_base("configuration", "configuration", "invalid", {}, 0))
    return criteria, settings


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _is_num(v: Any) -> bool:
    return business_facts._is_finite_number(v)


def _validate_param(spec: dict, value: Any, *, criterion_id: str) -> Optional[str]:
    name, ptype, required = spec["name"], spec["type"], spec["required"]
    label = f"Critère « {criterion_id} », paramètre « {spec['label']} »"
    if value is None:
        return f"{label} : valeur requise." if required else None
    if ptype == "score" and not (_is_num(value) and 0 <= value <= 100):
        return f"{label} doit être un nombre fini entre 0 et 100."
    if ptype == "pct" and not (_is_num(value) and 0 <= value <= 100):
        return f"{label} doit être un pourcentage fini entre 0 et 100."
    if ptype == "amount" and not (_is_num(value) and value >= 0):
        return f"{label} doit être un nombre fini positif ou nul."
    if ptype == "count" and not (isinstance(value, int) and not isinstance(value, bool) and value >= 0):
        return f"{label} doit être un entier positif ou nul."
    if ptype == "bool" and not isinstance(value, bool):
        return f"{label} doit être strictement vrai ou faux."
    if ptype == "choice" and not (isinstance(value, str) and value in spec.get("choices", [])):
        return f"{label} doit être l'un de : {', '.join(spec.get('choices', []))}."
    if ptype == "fact_key" and not business_facts._valid_identifier(value):
        return f"{label} doit être l'identifiant d'un fait métier."
    if ptype == "text_list":
        if not (isinstance(value, list) and 1 <= len(value) <= 50 and all(isinstance(v, str) and 0 < len(v.strip()) <= 80 for v in value)):
            return f"{label} doit être une liste de 1 à 50 textes non vides (80 caractères maximum)."
    if ptype in ("tiers_at_least", "tiers_at_most"):
        key = "at_least" if ptype == "tiers_at_least" else "at_most"
        if not (isinstance(value, list) and 1 <= len(value) <= 12):
            return f"{label} doit contenir de 1 à 12 paliers."
        seen = set()
        for tier in value:
            if not (isinstance(tier, dict) and _is_num(tier.get(key)) and tier[key] >= 0 and _is_num(tier.get("score")) and 0 <= tier["score"] <= 100):
                return f"{label} : chaque palier doit avoir « {key} » ≥ 0 et une note entre 0 et 100."
            if tier[key] in seen:
                return f"{label} : deux paliers ont le même seuil ({tier[key]})."
            seen.add(tier[key])
    return None


def validate_criterion(criterion: Any, *, known_facts: dict, settings: dict, index: int = 0) -> list[str]:
    if not isinstance(criterion, dict):
        return [f"Le critère n°{index + 1} doit être un objet."]
    errors: list[str] = []
    cid = criterion.get("id")
    if not business_facts._valid_identifier(cid):
        errors.append(f"Le critère n°{index + 1} doit avoir un identifiant stable (minuscules, chiffres et _, commençant par une lettre).")
    shown = cid if isinstance(cid, str) and cid else f"n°{index + 1}"
    if not isinstance(criterion.get("label"), str) or not criterion["label"].strip():
        errors.append(f"Le critère « {shown} » doit avoir un libellé non vide.")
    evaluator = criterion.get("evaluator")
    if not (isinstance(evaluator, str) and evaluator in EVALUATORS and evaluator != "invalid"):
        errors.append(f"Le critère « {shown} » utilise un évaluateur non pris en charge : {evaluator!r}.")
        return errors
    definition = EVALUATORS[evaluator]
    params = criterion.get("params")
    if not isinstance(params, dict):
        errors.append(f"Le critère « {shown} » doit avoir un objet « params ».")
        params = {}
    known_names = {p["name"] for p in definition["params"]}
    for extra in sorted(set(params) - known_names):
        errors.append(f"Critère « {shown} » : paramètre inconnu « {extra} » pour l'évaluateur « {evaluator} ».")
    for pspec in definition["params"]:
        error = _validate_param(pspec, params.get(pspec["name"]), criterion_id=shown)
        if error:
            errors.append(error)

    if not business_facts.has_valid_weight(criterion):
        errors.append(f"Le poids du critère « {shown} » doit être un nombre fini entre 0 et 100.")
    blocking = criterion.get("blocking", False)
    if not isinstance(blocking, bool):
        errors.append(f"Le critère « {shown} » doit préciser strictement s'il est bloquant (true/false).")
    elif blocking and not definition["supports_blocking"]:
        errors.append(f"Le critère « {shown} » : l'évaluateur « {evaluator} » n'a pas de condition d'échec ; exprimez le blocage par son paramètre dédié.")

    on_missing = criterion.get("on_missing", {"mode": "incomplete"})
    if not isinstance(on_missing, dict) or on_missing.get("mode") not in ON_MISSING_MODES:
        errors.append(f"Le critère « {shown} » : « on_missing.mode » doit être l'un de {', '.join(ON_MISSING_MODES)}.")
    else:
        mode = on_missing["mode"]
        if mode == "explicit_score" and not (_is_num(on_missing.get("score")) and 0 <= on_missing["score"] <= 100):
            errors.append(f"Le critère « {shown} » : la note choisie pour une donnée absente doit être un nombre entre 0 et 100.")
        if mode != "incomplete" and blocking is True:
            errors.append(f"Le critère « {shown} » est bloquant : une exigence bloquante inconnue ne peut être ni notée par hypothèse ni déclarée non applicable.")
        if mode != "incomplete" and not definition["missing_ao_data_is_recoverable"]:
            errors.append(f"Le critère « {shown} » : l'évaluateur « {evaluator} » n'a pas de donnée d'AO absente à traiter ; laissez « incomplete ».")
        if mode == "not_applicable" and settings.get("not_applicable_rule") == "renormalize" and settings.get("not_applicable_rule_confirmed") is not True:
            errors.append("Une règle de pondération « renormalize » doit être confirmée explicitement (not_applicable_rule_confirmed).")

    enabled = criterion.get("enabled", True)
    if not isinstance(enabled, bool):
        errors.append(f"Le critère « {shown} » : « enabled » doit être vrai ou faux.")
    elif not enabled:
        if not isinstance(criterion.get("disabled_reason"), str) or not criterion["disabled_reason"].strip():
            errors.append(f"Le critère désactivé « {shown} » doit indiquer un motif (disabled_reason).")
        if business_facts.has_valid_weight(criterion) and criterion["weight"] != 0:
            errors.append(f"Le critère désactivé « {shown} » doit avoir un poids de 0 : réallouez explicitement ses points aux autres critères.")

    # facts referenced by the criterion
    fact_key = params.get("fact_key")
    needs_fact = evaluator in _FACT_EVALUATORS or (evaluator == "numeric_tiers" and params.get("source") == "fact")
    if evaluator == "numeric_tiers":
        if params.get("source") == "fact" and not fact_key:
            errors.append(f"Critère « {shown} » : une source « fait » exige « fact_key ».")
        if params.get("source") == "budget" and fact_key:
            errors.append(f"Critère « {shown} » : « fact_key » n'a pas de sens quand la source est le budget.")
    if needs_fact and isinstance(fact_key, str) and fact_key:
        fact = known_facts.get(fact_key) if isinstance(known_facts, dict) else None
        if not isinstance(fact, dict):
            errors.append(f"Le critère « {shown} » référence un fait métier inconnu ou non déclaré : {fact_key!r}.")
        elif evaluator in _FACT_EVALUATORS:
            legacy = _legacy_fact_spec(criterion)
            # Only the operator/fact-type compatibility check is taken from the
            # shared validator (every other rule is already covered above).
            errors.extend(e for e in business_facts.validate_custom_criterion(legacy, known_facts=known_facts)
                          if "n'est pas compatible avec le type" in e)
            if fact.get("value") is None or (isinstance(fact.get("value"), list) and not fact.get("value")):
                errors.append(f"Le fait « {fact_key} » utilisé par le critère « {shown} » n'a pas de valeur déclarée — renseignez-la avant d'activer.")
        elif fact.get("type") != "number":
            errors.append(f"Le critère « {shown} » : le fait « {fact_key} » doit être de type « number » pour des paliers numériques.")
    return errors


def _legacy_fact_spec(criterion: dict) -> dict:
    """The historical custom-criterion shape the shared comparators expect."""
    params = criterion.get("params") if isinstance(criterion.get("params"), dict) else {}
    return {"id": criterion.get("id"), "label": criterion.get("label"), "fact_key": params.get("fact_key"),
            "operator": criterion.get("evaluator"), "comparison": params.get("comparison"), "weight": criterion.get("weight"),
            "blocking": criterion.get("blocking") if isinstance(criterion.get("blocking"), bool) else False,
            "pass_score": params.get("pass_score"), "fail_score": params.get("fail_score")}


def validate_settings(settings: Any) -> list[str]:
    if not isinstance(settings, dict):
        return ["Les réglages de politique doivent être un objet."]
    errors: list[str] = []
    rule = settings.get("not_applicable_rule", "incomplete")
    if rule not in NOT_APPLICABLE_RULES:
        errors.append(f"« not_applicable_rule » doit être l'un de : {', '.join(NOT_APPLICABLE_RULES)}.")
    if rule == "renormalize" and settings.get("not_applicable_rule_confirmed") is not True:
        errors.append("La règle « renormalize » (recalcul sur les seuls critères applicables) doit être confirmée explicitement.")
    for key in ("strengths_at_least", "weaknesses_below"):
        value = settings.get(key)
        if value is not None and not (_is_num(value) and 0 <= value <= 100):
            errors.append(f"« {key} » doit être vide ou un nombre entre 0 et 100.")
    return errors


def validate_criteria(criteria: Any, *, known_facts: dict, settings: Any) -> list[str]:
    """Structure, parameters, references, unknown-data handling and the
    100-point weight budget (enabled criteria only). A new policy starts with
    NO criterion: activation requires at least one enabled criterion."""
    if not isinstance(criteria, list):
        return ["Les critères doivent être une liste."]
    settings = settings if isinstance(settings, dict) else {}
    errors: list[str] = []
    seen: set[str] = set()
    total = 0.0
    enabled_count = 0
    for index, criterion in enumerate(criteria):
        errors.extend(validate_criterion(criterion, known_facts=known_facts, settings=settings, index=index))
        cid = criterion.get("id") if isinstance(criterion, dict) else None
        if isinstance(cid, str) and cid:
            if cid in seen:
                errors.append(f"Identifiant de critère dupliqué : {cid!r}.")
            seen.add(cid)
        if isinstance(criterion, dict) and criterion.get("enabled", True) is True:
            enabled_count += 1
            if business_facts.has_valid_weight(criterion):
                total += criterion["weight"]
    if enabled_count == 0:
        errors.append("Ajoutez au moins un critère actif avant d'activer : une politique commence vide et aucun critère n'est imposé.")
    elif not any("poids" in e for e in errors) and abs(total - 100.0) > 1e-6:
        errors.append(f"La somme des poids des critères actifs doit être exactement 100 (obtenu : {total:g}).")
    return errors


def fact_requirements(criteria: Any) -> list[dict]:
    """Enabled criteria that need an AO fact, in the shape
    `business_facts.requested_facts_from_criteria` expects."""
    out: list[dict] = []
    for criterion in criteria if isinstance(criteria, list) else []:
        if not isinstance(criterion, dict) or criterion.get("enabled", True) is not True:
            continue
        params = criterion.get("params") if isinstance(criterion.get("params"), dict) else {}
        evaluator = criterion.get("evaluator")
        if evaluator in _FACT_EVALUATORS or (evaluator == "numeric_tiers" and params.get("source") == "fact"):
            out.append({"fact_key": params.get("fact_key")})
    return out


def requested_facts(criteria: Any, *, known_facts: dict) -> dict:
    return business_facts.requested_facts_from_criteria(fact_requirements(criteria), known_facts=known_facts)


# ---------------------------------------------------------------------------
# Proposed drafts (never active; the user loads one explicitly, edits it,
# provides thresholds, then validates / simulates / activates)
# ---------------------------------------------------------------------------

def proposed_templates() -> list[dict]:
    it_criteria = [
        _base("competences_couvertes", "Couverture des technologies demandées", "technology_coverage",
              {"none_requested_score": None, "max_unmastered_blocking": None}, 30),
        _base("references_probantes", "Références internes probantes", "reference_evidence",
              {"base_score": 30, "per_reference": 6, "similarity_weight": 40}, 20),
        _base("disponibilite_equipe", "Disponibilité de l'équipe", "capacity_availability",
              {"available_score": 90, "unavailable_score": 45, "max_charge_blocking": None}, 20),
        _base("certifications_exigees", "Certifications exigées", "certifications_required",
              {"covered_score": 100, "missing_score": 20, "block_when_missing": True, "when_extraction_unknown": "missing"}, 15),
        _base("budget_seul", "Budget estimé (comparaison au seul budget)", "numeric_tiers",
              {"source": "budget", "fact_key": None,
               "tiers": [{"at_least": 200000, "score": 90}, {"at_least": 100000, "score": 80}, {"at_least": 60000, "score": 55}],
               "below_score": 40, "zero_score": None, "minimum_blocking": None}, 15),
    ]
    return [{
        "id": "modele_informatique",
        "label": "Modèle informatique (brouillon à revoir)",
        "description": ("Cinq critères issus des règles historiques, exprimés avec des paramètres explicites : à adapter à votre activité. "
                        "Aucun seuil GO/réserve n'est proposé : vous devez les saisir. Les règles historiques par mots-clés (délai, clauses, "
                        "secteur, solidité) ne sont pas reprises ; utilisez des faits métier. Une donnée absente rend le critère non évalué "
                        "(INCOMPLET), sauf hypothèse que vous choisissez explicitement."),
        "criteria": it_criteria, "settings": default_settings(),
    }]
