"""B06-T5 / B05-T3 — private, additive, sector-neutral business-fact
framework.

Three distinct things this lot needed to keep separate (ticket: "distinguer
paramètres privés, catalogue fixe et formules spécifiques IT"):

1. **Catalogue fixe** — the small, hardcoded set of fact TYPES and
   comparison OPERATORS this module defines. Technical, shared by every
   account, never configurable per organization.
2. **Paramètres privés** — the actual fact DEFINITIONS and DECLARED VALUES
   an account enters (src.web.database.models.ProviderProfile.
   business_facts) and the CRITERIA it builds on top of them
   (ScoringPolicy.custom_criteria) — organization+owner scoped, exactly
   like every other private configuration in this codebase (competences,
   certifications, capacity).
3. **Formules spécifiques IT** — src.agents.scoring_engine.ScoringEngine's
   pre-existing 12 fixed criteria (mastered technologies, IT certifications,
   technology-count complexity, ...). UNCHANGED by this module: an account
   that configures no custom criterion keeps getting exactly the same
   formula it always has. Custom criteria are ADDITIVE alongside them, in
   the SAME weight budget (see ScoringEngine.score / validate_weights'
   `custom_criteria_weight_total`) — never a second scoring engine, never a
   replacement of ScoringPolicy.

No eval/SQL/free-form code is ever executed from a fact or a criterion
definition: every comparison is one of the fixed OPERATORS below, applied
by fixed Python. A criterion's "barème" is two plain numbers (pass_score/
fail_score) — data, never a formula string.

Lot 41 (D3-03) hardening, applied uniformly below:
- every value is type-checked BEFORE it is used as a lookup key or fed to a
  conversion — a list where a string is expected must yield a structured
  error, never a TypeError (unhashable) from `x in frozenset(...)`;
- a number is an int/float that is neither a bool nor NaN/infinite — a
  string such as "inf" is never converted (float("inf") is a real Python
  value and would become an infinite, always-sufficient capacity);
- a boolean is strictly a bool (never 0/1, never "true");
- the scoring-time evaluation defends itself against inconsistent
  historical data (a criterion or value that was never validated, or that
  predates a rule): it returns "cannot be computed" (score None), never
  raises and never guesses a favorable outcome.

Two examples used throughout this module's own tests (synthetic, clearly
not real customer data): a cleaning company's "zone d'intervention"
(list) and "fréquence de nettoyage" (number, per week); a construction
company's "matériel disponible" (list) and "capacité de chantier" (number).
"""
from __future__ import annotations

import math
import re
import unicodedata
from typing import Any, Optional

FACT_TYPES = frozenset({"number", "list", "boolean", "text"})
OPERATORS = frozenset({"numeric_threshold", "list_coverage", "equality"})

# Which fact TYPE each operator can honestly be applied to — checked at
# validation time (ticket: "critère non pris en charge ou ambigu : signaler
# la limite avant activation"), never guessed at scoring time.
_OPERATOR_COMPATIBLE_TYPES: dict[str, frozenset[str]] = {
    "numeric_threshold": frozenset({"number"}),
    "list_coverage": frozenset({"list"}),
    "equality": frozenset({"number", "boolean", "text"}),
}

# Public, read-only view of the compatibility table, for a settings screen
# that guides the choice (the server stays the authority: /validate and
# /activate re-check every criterion with the private table above).
OPERATOR_FACT_TYPES: dict[str, list[str]] = {op: sorted(types) for op, types in _OPERATOR_COMPATIBLE_TYPES.items()}
# Only a "number" fact can carry a unit.
UNIT_FACT_TYPES: list[str] = ["number"]

# "provider_gte_ao": the provider's declared value must be >= the AO's
# extracted requirement to pass (e.g. a declared cleaning frequency
# capacity, a site capacity). "provider_lte_ao": the reverse (e.g. a
# maximum acceptable cost/delay). Never inferred — a criterion using
# numeric_threshold must state one explicitly.
NUMERIC_COMPARISONS = frozenset({"provider_gte_ao", "provider_lte_ao"})

# A stable identifier (fact key / criterion id): lowercase slug. Criterion
# ids end up inside `scoring_missing` ("custom:<id>") and fact keys are
# JSON object keys referenced by criteria — neither may contain spaces or
# free text.
_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _in_catalogue(value: Any, catalogue: frozenset[str]) -> bool:
    """`value in frozenset` raises TypeError for an unhashable value (a
    list/dict sent where a string is expected) — type first, lookup after."""
    return isinstance(value, str) and value in catalogue


def _is_finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _valid_identifier(value: Any) -> bool:
    return isinstance(value, str) and bool(_IDENTIFIER_RE.match(value))


# ---------------------------------------------------------------------------
# Validation — pure, no I/O, mirrors src/agents/scoring_policy_validation.py's
# own idiom (a dict of field -> list[str] errors), reused identically by a
# /validate dry-run and /activate (never two different rule sets).
# ---------------------------------------------------------------------------

def validate_fact_value(fact_type: str, value: Any) -> Optional[str]:
    """One DECLARED value against its fact type. Returns an error message,
    or None if the value is valid. `None` itself (no value declared) is not
    judged here — the caller decides whether a value is required."""
    if fact_type == "number":
        if not _is_finite_number(value):
            return "doit être un nombre fini (ni texte, ni booléen, ni NaN/infini)"
    elif fact_type == "list":
        if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
            return "doit être une liste de textes non vides"
    elif fact_type == "boolean":
        if not isinstance(value, bool):
            return "doit être strictement vrai ou faux (true/false)"
    elif fact_type == "text":
        if not isinstance(value, str) or not value.strip():
            return "doit être un texte non vide"
    return None


def validate_business_fact(fact: dict) -> list[str]:
    """One fact DEFINITION: {key, label, type, unit, value}. `unit` is only
    ever meaningful (and only ever allowed) for type="number" — a list/
    boolean/text fact declaring a unit is rejected rather than silently
    ignored. `value` (the account's DECLARED value) is optional here —
    a draft may hold a fact whose value isn't entered yet — but when
    present it must match the fact's type."""
    if not isinstance(fact, dict):
        return ["Un fait métier doit être un objet {clé, libellé, type, unité, valeur}."]
    key = fact.get("key")
    label = fact.get("label")
    errors: list[str] = []
    if not _valid_identifier(key):
        errors.append(
            "Chaque fait métier doit avoir un identifiant stable (minuscules, chiffres et _, commençant par une lettre)."
        )
    display_key = key if isinstance(key, str) and key else "?"
    if not isinstance(label, str) or not label.strip():
        errors.append(f"Le fait « {display_key} » doit avoir un libellé non vide.")
    fact_type = fact.get("type")
    if not _in_catalogue(fact_type, FACT_TYPES):
        errors.append(
            f"Le fait « {display_key} » a un type non pris en charge : {fact_type!r} "
            f"(types acceptés : {', '.join(sorted(FACT_TYPES))})."
        )
        return errors  # the unit/value checks below need a known, valid type
    unit = fact.get("unit")
    if unit is not None:
        if fact_type != "number":
            errors.append(f"Le fait « {display_key} » ne peut avoir une unité que s'il est de type « number ».")
        elif not isinstance(unit, str) or not unit.strip():
            errors.append(f"L'unité du fait « {display_key} », si fournie, doit être une chaîne non vide.")
    value = fact.get("value")
    if value is not None:
        value_error = validate_fact_value(fact_type, value)
        if value_error:
            errors.append(f"La valeur déclarée du fait « {display_key} » {value_error}.")
    return errors


def validate_business_facts(facts: dict) -> list[str]:
    """The full private catalogue: {key: fact_definition}. Each definition's
    own `key` must match the dict key it's stored under — a mismatch would
    let a criterion's `fact_key` reference resolve to the wrong definition."""
    if not isinstance(facts, dict):
        return ["Les faits métier doivent être un objet {clé: définition}."]
    errors: list[str] = []
    for key, fact in facts.items():
        if not isinstance(fact, dict) or fact.get("key") != key:
            errors.append(f"La clé déclarée du fait « {key} » doit correspondre exactement à son identifiant.")
        errors.extend(validate_business_fact(fact) if isinstance(fact, dict) else [])
    return errors


def validate_custom_criterion(criterion: dict, *, known_facts: dict[str, dict]) -> list[str]:
    """One CRITERION built on a known fact: {id, label, fact_key, operator,
    comparison (numeric_threshold only), weight, blocking, pass_score,
    fail_score}. `known_facts` is the account's own declared business_facts
    dict (key -> definition) — a criterion referencing an undeclared or
    unknown fact is refused outright, never silently skipped (ticket:
    "critère non pris en charge ou ambigu : signaler la limite avant
    activation")."""
    if not isinstance(criterion, dict):
        return ["Chaque critère personnalisé doit être un objet."]
    errors: list[str] = []
    crit_id = criterion.get("id")
    if not _valid_identifier(crit_id):
        errors.append(
            "Chaque critère personnalisé doit avoir un identifiant stable (minuscules, chiffres et _, commençant par une lettre)."
        )
    display_id = crit_id if isinstance(crit_id, str) and crit_id else "?"
    label = criterion.get("label")
    if not isinstance(label, str) or not label.strip():
        errors.append(f"Le critère « {display_id} » doit avoir un libellé non vide.")

    fact_key = criterion.get("fact_key")
    fact = known_facts.get(fact_key) if isinstance(fact_key, str) and isinstance(known_facts, dict) else None
    if not isinstance(fact, dict):
        errors.append(f"Le critère « {display_id} » référence un fait métier inconnu ou non déclaré : {fact_key!r}.")
        return errors  # nothing else here can be checked meaningfully without a known fact/type

    operator = criterion.get("operator")
    if not _in_catalogue(operator, OPERATORS):
        errors.append(
            f"Le critère « {display_id} » utilise une comparaison non prise en charge : {operator!r} "
            f"(comparaisons acceptées : {', '.join(sorted(OPERATORS))})."
        )
        return errors
    fact_type = fact.get("type")
    if not _in_catalogue(fact_type, _OPERATOR_COMPATIBLE_TYPES[operator]):
        errors.append(
            f"Le critère « {display_id} » : la comparaison « {operator} » n'est pas compatible avec le type "
            f"« {fact_type} » du fait « {fact_key} »."
        )
    if operator == "numeric_threshold" and not _in_catalogue(criterion.get("comparison"), NUMERIC_COMPARISONS):
        errors.append(
            f"Le critère « {display_id} » (seuil numérique) doit préciser une comparaison parmi : "
            f"{', '.join(sorted(NUMERIC_COMPARISONS))}."
        )

    weight = criterion.get("weight")
    if not _is_finite_number(weight) or not (0 <= weight <= 100):
        errors.append(f"Le poids du critère « {display_id} » doit être un nombre fini entre 0 et 100.")

    for score_key in ("pass_score", "fail_score"):
        value = criterion.get(score_key)
        if not _is_finite_number(value) or not (0 <= value <= 100):
            errors.append(f"Le critère « {display_id} » : « {score_key} » doit être un nombre fini entre 0 et 100.")

    if not isinstance(criterion.get("blocking"), bool):
        errors.append(f"Le critère « {display_id} » doit préciser explicitement s'il est bloquant (true/false).")

    return errors


def validate_custom_criteria(criteria: list, *, known_facts: dict[str, dict]) -> list[str]:
    if not isinstance(criteria, list):
        return ["Les critères personnalisés doivent être une liste."]
    errors: list[str] = []
    seen_ids: set[str] = set()
    for criterion in criteria:
        errors.extend(validate_custom_criterion(criterion, known_facts=known_facts))
        crit_id = criterion.get("id") if isinstance(criterion, dict) else None
        if isinstance(crit_id, str) and crit_id:
            if crit_id in seen_ids:
                errors.append(f"Identifiant de critère personnalisé dupliqué : {crit_id!r}.")
            seen_ids.add(crit_id)
    return errors


def validate_referenced_fact_values(criteria: list, *, known_facts: dict[str, dict]) -> list[str]:
    """A criterion can only ever be evaluated against a value the account
    actually DECLARED for its fact — a referenced fact with no value (or an
    empty list) would make every analysis INCOMPLET. Reported at activation
    time (a draft may still hold the criterion), never guessed as a default
    at scoring time. `0` and `false` are real declared values and pass."""
    errors: list[str] = []
    if not isinstance(criteria, list) or not isinstance(known_facts, dict):
        return errors
    reported: set[str] = set()
    for criterion in criteria:
        if not isinstance(criterion, dict):
            continue
        fact_key = criterion.get("fact_key")
        fact = known_facts.get(fact_key) if isinstance(fact_key, str) else None
        if not isinstance(fact, dict) or fact_key in reported:
            continue
        value = fact.get("value")
        if value is None or (isinstance(value, list) and not value):
            reported.add(fact_key)
            errors.append(
                f"Le fait « {fact_key} » utilisé par un critère n'a pas de valeur déclarée — "
                "renseignez-la avant d'activer."
            )
    return errors


def requested_facts_from_criteria(criteria: list, *, known_facts: dict[str, dict]) -> dict[str, dict]:
    """Builds the extraction SPEC the AO extractor needs
    (src.agents.ao_extractor.AOExtractor.extract's `requested_facts`
    parameter) from the active policy's custom_criteria + the owner's own
    declared fact catalogue: {fact_key: {"label","type","unit",
    "recognition_vocabulary"}}.

    `recognition_vocabulary` is only ever populated for a "list" fact and
    ONLY serves the local, no-LLM fallback as a set of names it can
    recognize in the AO text. It is deliberately NOT the AO's requirement
    and NOT proof that a requirement list is complete: the provider's own
    declared capacities (e.g. covered zones) are one thing, what the AO
    demands is another (lot 41, D3-02 — a fallback that treated the two as
    the same thing validated "Lyon et Marseille" against a provider
    covering only Lyon). A criterion referencing an unknown fact is
    silently skipped here — validate_custom_criteria is what rejects that
    at activation time; extraction simply has nothing to look for."""
    requested: dict[str, dict] = {}
    for criterion in criteria if isinstance(criteria, list) else []:
        if not isinstance(criterion, dict) or not isinstance(known_facts, dict):
            continue
        fact_key = criterion.get("fact_key")
        fact = known_facts.get(fact_key) if isinstance(fact_key, str) else None
        if not isinstance(fact, dict) or fact_key in requested:
            continue
        fact_type = fact.get("type")
        if not _in_catalogue(fact_type, FACT_TYPES):
            continue  # nothing extractable for an unsupported type
        spec = {"label": fact.get("label"), "type": fact_type, "unit": fact.get("unit")}
        if fact_type == "list":
            provider_value = fact.get("value")
            if isinstance(provider_value, list):
                spec["recognition_vocabulary"] = [v for v in provider_value if isinstance(v, str) and v.strip()]
        requested[fact_key] = spec
    return requested


def custom_criteria_weight_total(criteria: list) -> float:
    """Sum of every well-formed (finite) weight — used to fold custom
    criteria into the SAME 100-point budget as the fixed 12 criteria (see
    scoring_policy_validation.validate_weights' custom_criteria_weight_total
    parameter). A malformed weight contributes 0 here; validate_custom_
    criteria above is what actually rejects it."""
    total = 0.0
    for criterion in criteria if isinstance(criteria, list) else []:
        if not isinstance(criterion, dict):
            continue
        weight = criterion.get("weight")
        if _is_finite_number(weight):
            total += weight
    return total


def has_valid_weight(criterion: Any) -> bool:
    weight = criterion.get("weight") if isinstance(criterion, dict) else None
    return _is_finite_number(weight) and 0 <= weight <= 100


def safe_weight(criterion: Any) -> float:
    """The weight the engine may use for a criterion: a finite value in
    [0, 100], else 0.0 (an unusable weight never inflates the score — the
    criterion is additionally reported unresolvable, see ScoringEngine)."""
    return float(criterion["weight"]) if has_valid_weight(criterion) else 0.0


# ---------------------------------------------------------------------------
# Evaluation — pure, called by ScoringEngine.score() for each configured
# custom criterion. Never guesses: any missing/ambiguous/incompatible/
# inconsistent input returns (None, False, <reason>), and the caller
# (ScoringEngine) treats a None score as an INCOMPLETE check (added to
# scoring_missing), exactly like an unconfigured business_rules key —
# never a fabricated pass or fail, and never an exception.
# ---------------------------------------------------------------------------

def _normalize_unit(unit: Any) -> Optional[str]:
    """SPELLING normalization of a unit, never a conversion (lot 42): case,
    accents, and the separators an account may type ("par_semaine",
    "par-semaine", "Par  semaine") collapse to one form. "par semaine" and
    "par mois" stay different units — nothing is ever converted."""
    if not isinstance(unit, str):
        return None
    text = "".join(c for c in unicodedata.normalize("NFKD", unit) if not unicodedata.combining(c)).casefold()
    text = re.sub(r"[\s_\-]+", " ", text).strip()
    return text or None


def _same_unit(ao_fact: Any, provider_unit: Optional[str]) -> bool:
    """Ticket: "une conversion d'unité doit être explicite et vérifiée" —
    never silently converted, whatever the two units are. Both absent
    counts as compatible (a unit-less fact on both sides).

    Lot 42 (confirmed with a real model call): the extraction prompt asks
    the model to report the unit AS WRITTEN in the document, so an account
    that declared the slug "par_semaine" receives "par semaine". Comparing
    the raw strings made every such criterion incalculable (INCOMPLET) —
    the spelling of ONE unit is now normalized before the comparison."""
    return _normalize_unit(getattr(ao_fact, "unit", None)) == _normalize_unit(provider_unit)


def _as_string_set(value: Any) -> Optional[set[str]]:
    """A list of non-empty strings (or one non-empty string) as a
    normalized set; None if the value is anything else."""
    items = value if isinstance(value, list) else [value]
    if not items or not all(isinstance(v, str) and v.strip() for v in items):
        return None
    return {v.strip().casefold() for v in items}


def evaluate_custom_criterion(
    criterion: dict, *, ao_fact: Optional[Any], provider_value: Any, provider_unit: Optional[str],
) -> tuple[Optional[float], bool, str]:
    """`ao_fact` is an ExtractedFact-like object (duck-typed: needs
    .status/.value/.unit — src.core.models.ExtractedFact) or None.

    Returns (score, passed, reason). `score` is None exactly when the
    comparison could not be made honestly — never a placeholder 0/100."""
    if not isinstance(criterion, dict):
        return None, False, "invalid_criterion"
    operator = criterion.get("operator")
    if not _in_catalogue(operator, OPERATORS):
        return None, False, "unsupported_operator"
    pass_score, fail_score = criterion.get("pass_score"), criterion.get("fail_score")
    if not (_is_finite_number(pass_score) and 0 <= pass_score <= 100
            and _is_finite_number(fail_score) and 0 <= fail_score <= 100):
        return None, False, "invalid_criterion"

    if ao_fact is None or getattr(ao_fact, "status", "absent") != "found":
        return None, False, "ao_fact_missing"
    ao_value = getattr(ao_fact, "value", None)
    if ao_value is None:
        return None, False, "ao_fact_missing"
    if provider_value is None:
        return None, False, "provider_fact_missing"

    if operator == "numeric_threshold":
        if not _same_unit(ao_fact, provider_unit):
            return None, False, "unit_mismatch"
        # Strict numbers only: a string is never converted (float("inf")
        # and float("nan") are valid Python and would silently become an
        # infinite capacity / an always-false comparison).
        if not _is_finite_number(ao_value) or not _is_finite_number(provider_value):
            return None, False, "non_numeric_value"
        comparison = criterion.get("comparison")
        if comparison == "provider_gte_ao":
            passed = provider_value >= ao_value
        elif comparison == "provider_lte_ao":
            passed = provider_value <= ao_value
        else:
            return None, False, "invalid_criterion"
    elif operator == "list_coverage":
        covered = _as_string_set(provider_value) if isinstance(provider_value, list) else None
        if covered is None:
            return None, False, "provider_fact_missing"
        required = _as_string_set(ao_value)
        if required is None:
            return None, False, "ao_fact_missing"
        passed = required.issubset(covered)
    else:  # equality
        if not _same_unit(ao_fact, provider_unit):
            return None, False, "unit_mismatch"
        # Same-type comparison only: in Python True == 1, which would call
        # a boolean requirement "equal" to a numeric capacity.
        if isinstance(provider_value, bool) and isinstance(ao_value, bool):
            passed = provider_value == ao_value
        elif _is_finite_number(provider_value) and _is_finite_number(ao_value):
            passed = provider_value == ao_value
        elif isinstance(provider_value, str) and isinstance(ao_value, str) and provider_value.strip() and ao_value.strip():
            passed = provider_value.strip().casefold() == ao_value.strip().casefold()
        else:
            return None, False, "type_mismatch"

    return float(pass_score if passed else fail_score), passed, "ok"
