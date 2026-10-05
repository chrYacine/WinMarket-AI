"""Pure unit tests for src/agents/scoring_policy_validation.py — no DB, no
web layer. Same validation functions are reused by /validate and /activate
(ticket B06-T1 section 4), so testing them in isolation covers both."""
from __future__ import annotations

import math
from types import SimpleNamespace

from src.agents.scoring_policy_validation import (
    CRITERIA_KEYS,
    validate_for_activation,
    validate_profile_completeness,
    validate_thresholds,
    validate_weights,
)

VALID_WEIGHTS = {
    "Adequation expertise": 20, "References similaires": 15, "Disponibilite equipe": 10,
    "Rentabilite estimee": 10, "Faisabilite delai": 10, "Certifications requises": 10,
    "Complexite technique": 5, "Connaissance secteur": 5, "Potentiel commercial": 5,
    "Risque contractuel": 5, "Solidite client": 3, "Valeur strategique": 2,
}


def test_criteria_keys_match_scoring_engine_exactly():
    from src.agents.scoring_engine import ScoringEngine
    assert CRITERIA_KEYS == frozenset(ScoringEngine.labels.keys())


def test_valid_weights_pass():
    assert validate_weights(VALID_WEIGHTS) == []


def test_weights_missing_a_criterion_is_rejected():
    incomplete = dict(VALID_WEIGHTS)
    del incomplete["Valeur strategique"]
    errors = validate_weights(incomplete)
    assert any("manquants" in e for e in errors)


def test_weights_with_unknown_criterion_is_rejected():
    extra = dict(VALID_WEIGHTS)
    extra["Critere Invente"] = 5
    errors = validate_weights(extra)
    assert any("inconnus" in e for e in errors)


def test_weights_sum_not_100_is_rejected():
    off = dict(VALID_WEIGHTS)
    off["Valeur strategique"] = 99
    errors = validate_weights(off)
    assert any("somme" in e.lower() for e in errors)


def test_weights_negative_is_rejected():
    negative = dict(VALID_WEIGHTS)
    negative["Valeur strategique"] = -2
    negative["Adequation expertise"] = 24  # keep the arithmetic sum at 100 to isolate the negativity check
    errors = validate_weights(negative)
    assert any("négatif" in e for e in errors)


def test_weights_nan_and_infinite_are_rejected():
    for bad_value in (math.nan, math.inf, -math.inf):
        broken = dict(VALID_WEIGHTS)
        broken["Valeur strategique"] = bad_value
        errors = validate_weights(broken)
        assert any("fini" in e for e in errors), f"value={bad_value}"


def test_weights_non_numeric_is_rejected():
    broken = dict(VALID_WEIGHTS)
    broken["Valeur strategique"] = "beaucoup"
    errors = validate_weights(broken)
    assert any("nombre" in e for e in errors)


def test_thresholds_valid_pass():
    assert validate_thresholds(88, 60) == []


def test_thresholds_reversed_is_rejected():
    errors = validate_thresholds(60, 88)  # sous_reserve >= go
    assert any("inférieur" in e for e in errors)


def test_thresholds_equal_is_rejected():
    """Boundary case: equal thresholds are NOT a valid ordering (must be
    STRICTLY inférieur, per ticket section 4)."""
    errors = validate_thresholds(70, 70)
    assert any("inférieur" in e for e in errors)


def test_thresholds_out_of_range_is_rejected():
    assert any("0 et 100" in e for e in validate_thresholds(150, 60))
    assert any("0 et 100" in e for e in validate_thresholds(88, -5))


def test_thresholds_none_is_rejected():
    errors = validate_thresholds(None, None)
    assert any("requis" in e for e in errors)


def test_thresholds_nan_is_rejected():
    errors = validate_thresholds(math.nan, 60)
    assert any("fini" in e for e in errors)


def test_profile_missing_raison_sociale_is_rejected():
    assert validate_profile_completeness(None) != []
    empty_profile = SimpleNamespace(raison_sociale="")
    assert validate_profile_completeness(empty_profile) != []
    whitespace_profile = SimpleNamespace(raison_sociale="   ")
    assert validate_profile_completeness(whitespace_profile) != []


def test_profile_with_raison_sociale_passes_even_with_no_other_field():
    """A facultative field (effectif, competences, certifications) must
    never block — only raison_sociale is required (ticket section 4)."""
    profile = SimpleNamespace(raison_sociale="Ma Petite ESN")
    assert validate_profile_completeness(profile) == []


def test_validate_for_activation_combines_all_three_and_is_empty_only_when_all_pass():
    profile = SimpleNamespace(raison_sociale="Ma Petite ESN")
    errors = validate_for_activation(weights=VALID_WEIGHTS, threshold_go=88, threshold_sous_reserve=60, profile=profile)
    assert errors == {}

    errors_broken = validate_for_activation(weights={}, threshold_go=None, threshold_sous_reserve=None, profile=None)
    assert set(errors_broken.keys()) == {"weights", "thresholds", "profile"}


# --- B06-T5: custom_criteria folded into the same activation check ---------

def test_activation_with_no_custom_criteria_is_unaffected():
    """Default (no custom_criteria argument at all) must behave exactly as
    before this ticket — every pre-existing caller of validate_for_
    activation doesn't know this parameter exists."""
    profile = SimpleNamespace(raison_sociale="Ma Petite ESN", business_facts={})
    errors = validate_for_activation(weights=VALID_WEIGHTS, threshold_go=88, threshold_sous_reserve=60, profile=profile)
    assert errors == {}


def test_activation_with_valid_custom_criterion_reduces_required_fixed_weight_budget():
    """A cleaning-company account zeroes out an IT-specific criterion to
    make room for its own custom one — the total (fixed + custom) must
    still be exactly 100 for activation to pass."""
    weights = dict(VALID_WEIGHTS)
    weights["Valeur strategique"] = 0  # free up the 2 points spent by the custom criterion below
    # Lot 41: a fact used by a criterion must carry a DECLARED value — a
    # criterion with no declared value could only ever yield INCOMPLET.
    profile = SimpleNamespace(
        raison_sociale="Nettoyage Pro",
        business_facts={"zone_intervention": {
            "key": "zone_intervention", "label": "Zone", "type": "list", "unit": None, "value": ["Lyon"],
        }},
    )
    custom_criteria = [{
        "id": "zone_couverte", "label": "Zone couverte", "fact_key": "zone_intervention",
        "operator": "list_coverage", "weight": 2, "blocking": True, "pass_score": 100, "fail_score": 0,
    }]
    errors = validate_for_activation(
        weights=weights, threshold_go=88, threshold_sous_reserve=60, profile=profile, custom_criteria=custom_criteria,
    )
    assert errors == {}


def test_activation_with_custom_criterion_referencing_undeclared_fact_is_rejected():
    profile = SimpleNamespace(raison_sociale="Nettoyage Pro", business_facts={})
    custom_criteria = [{
        "id": "zone_couverte", "label": "Zone couverte", "fact_key": "zone_intervention",
        "operator": "list_coverage", "weight": 0, "blocking": True, "pass_score": 100, "fail_score": 0,
    }]
    errors = validate_for_activation(
        weights=VALID_WEIGHTS, threshold_go=88, threshold_sous_reserve=60, profile=profile, custom_criteria=custom_criteria,
    )
    assert "custom_criteria" in errors
    assert any("inconnu" in e for e in errors["custom_criteria"])


def test_activation_rejects_a_malformed_declared_business_fact_even_if_unreferenced():
    """Reviewer-caught regression: validate_business_facts existed and was
    unit-tested but was never actually called from validate_for_activation
    — an account could keep a malformed fact (unsupported type here)
    forever through /validate and /activate, contradicting this module's
    own documented contract (docs/api/B06_SCORING_CONFIG_CONTRACT.md §10:
    'la validation complète a lieu à /validate/activate')."""
    profile = SimpleNamespace(
        raison_sociale="Nettoyage Pro",
        business_facts={"zone_intervention": {"key": "zone_intervention", "label": "Zone", "type": "matrix", "unit": None}},
    )
    errors = validate_for_activation(weights=VALID_WEIGHTS, threshold_go=88, threshold_sous_reserve=60, profile=profile)
    assert "business_facts" in errors
    assert any("non pris en charge" in e for e in errors["business_facts"])


def test_activation_with_unbalanced_total_weight_including_custom_is_rejected():
    """Fixed weights alone still sum to 100 (unchanged) but a non-zero
    custom criterion weight is simply ADDED on top — this must fail, not
    silently ignore the excess."""
    profile = SimpleNamespace(
        raison_sociale="Nettoyage Pro",
        business_facts={"zone_intervention": {"key": "zone_intervention", "label": "Zone", "type": "list", "unit": None}},
    )
    custom_criteria = [{
        "id": "zone_couverte", "label": "Zone couverte", "fact_key": "zone_intervention",
        "operator": "list_coverage", "weight": 10, "blocking": True, "pass_score": 100, "fail_score": 0,
    }]
    errors = validate_for_activation(
        weights=VALID_WEIGHTS, threshold_go=88, threshold_sous_reserve=60, profile=profile, custom_criteria=custom_criteria,
    )
    assert "weights" in errors
    assert any("somme" in e.lower() for e in errors["weights"])
