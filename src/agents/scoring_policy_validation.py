"""Pure validation functions for a private ScoringPolicy + ProviderProfile
draft — no I/O, no DB session, so these are trivially unit-testable in
isolation and reusable identically by the /validate and /activate routes
(ticket B06-T1 section 4: "valider" and "activer" must apply the SAME
rules — activation just persists on top of a successful validation).

The canonical set of criteria/weights keys is read from ScoringEngine
itself (never redefined here) so the two can never silently drift apart.
"""
from __future__ import annotations

import math

from src.agents import business_facts, criteria_catalogue
from src.agents.scoring_engine import ScoringEngine

CRITERIA_KEYS = frozenset(ScoringEngine.labels.keys())


def validate_weights(weights: dict, *, custom_criteria_weight_total: float = 0.0) -> list[str]:
    """Types finis, poids non negatifs, somme = 100 (ticket section 4).

    B06-T5: `custom_criteria_weight_total` folds any additive custom
    criteria (src.agents.business_facts) into the SAME 100-point budget —
    default 0.0 keeps every pre-existing caller (nothing knows about
    custom criteria) byte-for-byte identical: the fixed 12 criteria alone
    must still sum to exactly 100. An account WITH custom criteria may
    zero out any fixed criterion irrelevant to its sector (e.g.
    "Complexite technique" for a cleaning company) to make room for them
    — the total (fixed + custom) must still be exactly 100."""
    errors: list[str] = []
    if not isinstance(weights, dict):
        return ["Les poids doivent être un objet {critère: poids}."]

    provided_keys = set(weights.keys())
    missing = CRITERIA_KEYS - provided_keys
    unknown = provided_keys - CRITERIA_KEYS
    if missing:
        errors.append(f"Critères manquants : {', '.join(sorted(missing))}.")
    if unknown:
        errors.append(f"Critères inconnus : {', '.join(sorted(unknown))}.")

    total = 0.0
    for key in provided_keys & CRITERIA_KEYS:
        value = weights[key]
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            errors.append(f"Le poids de « {key} » doit être un nombre.")
            continue
        if not math.isfinite(value):
            errors.append(f"Le poids de « {key} » doit être un nombre fini (pas NaN/infini).")
            continue
        if value < 0:
            errors.append(f"Le poids de « {key} » ne peut pas être négatif.")
            continue
        total += value

    if not missing and not unknown and not any("doit être" in e or "négatif" in e for e in errors):
        total_with_custom = total + custom_criteria_weight_total
        if abs(total_with_custom - 100.0) > 1e-6:
            suffix = " (critères personnalisés inclus)" if custom_criteria_weight_total else ""
            errors.append(f"La somme des poids{suffix} doit être exactement 100 (obtenu : {total_with_custom:g}).")

    return errors


def validate_thresholds(threshold_go, threshold_sous_reserve) -> list[str]:
    """Seuil réserve < seuil GO, tous deux dans [0,100] (ticket section 4)."""
    errors: list[str] = []
    for label, value in (("threshold_go", threshold_go), ("threshold_sous_reserve", threshold_sous_reserve)):
        if value is None:
            errors.append(f"« {label} » est requis avant activation.")
            continue
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            errors.append(f"« {label} » doit être un nombre.")
            continue
        if not math.isfinite(value):
            errors.append(f"« {label} » doit être un nombre fini (pas NaN/infini).")
            continue
        if not (0 <= value <= 100):
            errors.append(f"« {label} » doit être compris entre 0 et 100.")

    if (
        threshold_go is not None and threshold_sous_reserve is not None
        and isinstance(threshold_go, (int, float)) and isinstance(threshold_sous_reserve, (int, float))
        and not isinstance(threshold_go, bool) and not isinstance(threshold_sous_reserve, bool)
        and math.isfinite(threshold_go) and math.isfinite(threshold_sous_reserve)
        and threshold_sous_reserve >= threshold_go
    ):
        errors.append("Le seuil « GO SOUS RÉSERVE » doit être strictement inférieur au seuil « GO ».")

    return errors


def validate_profile_completeness(profile) -> list[str]:
    """Le seul champ métier requis est la raison sociale (ticket section 4:
    "un champ facultatif ne doit pas bloquer inutilement l'activation") —
    profile is a ProviderProfile ORM row or None."""
    if profile is None or not (profile.raison_sociale or "").strip():
        return ["Le nom de votre structure (raison sociale) est requis avant activation."]
    return []


# B06-T4: each of these is OPTIONAL — a missing key means the corresponding
# blocker is simply not evaluated (ScoringEngine.score marks the result
# "incomplete" instead), never a reason to refuse activation. Only a
# PRESENT-but-malformed value is rejected here (wrong type, negative,
# NaN/infinite, out of a sane range) — the same "type-safe if given, never
# required" contract as threshold_go/threshold_sous_reserve historically
# had before they became mandatory for activation.
_BUSINESS_RULE_RANGES = {
    "budget_minimum_eur": (0, None),
    "max_charge_pct": (0, 100),
    "max_unmastered_technologies": (0, None),
    "certification_penalty_score": (0, 100),
}


def validate_business_rules(business_rules: dict) -> list[str]:
    errors: list[str] = []
    if not isinstance(business_rules, dict):
        return ["Les règles métier doivent être un objet {règle: valeur}."]
    unknown = set(business_rules.keys()) - set(_BUSINESS_RULE_RANGES.keys())
    if unknown:
        errors.append(f"Règles métier inconnues : {', '.join(sorted(unknown))}.")
    for key, (minimum, maximum) in _BUSINESS_RULE_RANGES.items():
        if key not in business_rules or business_rules[key] is None:
            continue  # not configured — never an error, see module docstring above
        value = business_rules[key]
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            errors.append(f"« {key} » doit être un nombre.")
            continue
        if not math.isfinite(value):
            errors.append(f"« {key} » doit être un nombre fini (pas NaN/infini).")
            continue
        if value < minimum or (maximum is not None and value > maximum):
            bound = f"{minimum} et {maximum}" if maximum is not None else f"au moins {minimum}"
            errors.append(f"« {key} » doit être compris entre {bound}.")
    return errors


def validate_criteria_policy(
    *, criteria, settings, threshold_go, threshold_sous_reserve, profile,
) -> dict[str, list[str]]:
    """Lot 44 — validation of a policy authored in the explicit-criteria
    format (origin='user'). A new policy starts EMPTY: activation requires at
    least one enabled criterion, weights (enabled criteria) summing to
    exactly 100, valid thresholds and a profile with a name — all provided or
    confirmed by the user. Same error-dict idiom as validate_for_activation."""
    errors: dict[str, list[str]] = {}
    raw_known_facts = getattr(profile, "business_facts", None)
    raw_known_facts = raw_known_facts if raw_known_facts is not None else {}
    known_facts = raw_known_facts if isinstance(raw_known_facts, dict) else {}
    criteria_errors = criteria_catalogue.validate_criteria(criteria, known_facts=known_facts, settings=settings)
    if criteria_errors:
        errors["criteria"] = criteria_errors
    settings_errors = criteria_catalogue.validate_settings(settings)
    if settings_errors:
        errors["settings"] = settings_errors
    threshold_errors = validate_thresholds(threshold_go, threshold_sous_reserve)
    if threshold_errors:
        errors["thresholds"] = threshold_errors
    profile_errors = validate_profile_completeness(profile)
    if profile_errors:
        errors["profile"] = profile_errors
    business_facts_errors = business_facts.validate_business_facts(raw_known_facts)
    if business_facts_errors:
        errors["business_facts"] = business_facts_errors
    return errors


def validate_for_activation(
    *, weights: dict, threshold_go, threshold_sous_reserve, profile, business_rules: dict | None = None,
    custom_criteria: list | None = None, origin: str = "legacy", criteria=None, settings=None,
) -> dict[str, list[str]]:
    """Combined validation used identically by /validate (dry-run) and
    /activate (persists only if this returns no errors at all).

    B06-T5: `custom_criteria` (default None -> treated as []) is validated
    against `profile.business_facts` (the account's own declared private
    fact catalogue) — a criterion referencing an unknown/undeclared fact,
    an incompatible operator, or missing a required field is a hard
    activation error, never silently ignored (ticket: "critère non pris en
    charge ou ambigu : signaler la limite avant activation"). Its total
    weight is folded into validate_weights' own 100-point budget check.

    Lot 44: `origin="user"` (a policy authored in the explicit-criteria
    format) is validated by validate_criteria_policy instead — the legacy
    fields are then empty and meaningless."""
    if origin == "user":
        return validate_criteria_policy(
            criteria=criteria, settings=settings, threshold_go=threshold_go,
            threshold_sous_reserve=threshold_sous_reserve, profile=profile,
        )
    errors: dict[str, list[str]] = {}
    custom_criteria = custom_criteria if custom_criteria is not None else []
    raw_known_facts = getattr(profile, "business_facts", None)
    raw_known_facts = raw_known_facts if raw_known_facts is not None else {}
    # Lot 41 (D3-03): a historical/hand-edited non-dict value is reported
    # as a structured error below — never `dict(<list>)`, which raises.
    known_facts = raw_known_facts if isinstance(raw_known_facts, dict) else {}

    weight_errors = validate_weights(
        weights, custom_criteria_weight_total=business_facts.custom_criteria_weight_total(custom_criteria),
    )
    if weight_errors:
        errors["weights"] = weight_errors
    threshold_errors = validate_thresholds(threshold_go, threshold_sous_reserve)
    if threshold_errors:
        errors["thresholds"] = threshold_errors
    profile_errors = validate_profile_completeness(profile)
    if profile_errors:
        errors["profile"] = profile_errors
    business_rule_errors = validate_business_rules(business_rules or {})
    if business_rule_errors:
        errors["business_rules"] = business_rule_errors
    # Reviewer-caught (confirmed): the fact DEFINITIONS themselves
    # (known_facts, i.e. profile.business_facts) were never validated here
    # — only the criteria built on top of them were. An account could keep
    # an malformed fact (unknown type, a unit on a non-number fact, an
    # empty label) forever through /validate and /activate as long as no
    # criterion happened to reference it, contradicting this module's own
    # documented contract (docs/api/B06_SCORING_CONFIG_CONTRACT.md §10:
    # "la validation complète a lieu à /validate/activate").
    business_facts_errors = business_facts.validate_business_facts(raw_known_facts)
    if business_facts_errors:
        errors["business_facts"] = business_facts_errors
    custom_criteria_errors = business_facts.validate_custom_criteria(custom_criteria, known_facts=known_facts)
    custom_criteria_errors += business_facts.validate_referenced_fact_values(custom_criteria, known_facts=known_facts)
    if custom_criteria_errors:
        errors["custom_criteria"] = custom_criteria_errors
    return errors
