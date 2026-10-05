"""Pure unit tests for src/agents/business_facts.py — no DB, no web layer,
mirrors tests/test_scoring_policy_validation.py's own idiom.

Two synthetic, explicitly-non-IT scenarios exercised throughout (ticket:
"deux comptes de métiers différents... sans dépendre d'une liste de
technologies IT"): a cleaning company (zone d'intervention, fréquence de
nettoyage) and a construction company (matériel disponible, capacité de
chantier). Both are test fixtures only, not real business data.
"""
from __future__ import annotations

from types import SimpleNamespace

from src.agents.business_facts import (
    FACT_TYPES,
    OPERATORS,
    custom_criteria_weight_total,
    evaluate_custom_criterion,
    requested_facts_from_criteria,
    validate_business_fact,
    validate_business_facts,
    validate_custom_criteria,
    validate_custom_criterion,
)

ZONE_FACT = {"key": "zone_intervention", "label": "Zone d'intervention", "type": "list", "unit": None}
FREQUENCE_FACT = {"key": "frequence_nettoyage", "label": "Fréquence de nettoyage", "type": "number", "unit": "par_semaine"}
MATERIEL_FACT = {"key": "materiel_disponible", "label": "Matériel disponible", "type": "list", "unit": None}
CAPACITE_FACT = {"key": "capacite_chantier", "label": "Capacité de chantier", "type": "number", "unit": "equipes"}

CLEANING_FACTS = {"zone_intervention": ZONE_FACT, "frequence_nettoyage": FREQUENCE_FACT}
BTP_FACTS = {"materiel_disponible": MATERIEL_FACT, "capacite_chantier": CAPACITE_FACT}


def _fact(**overrides):
    base = {"value": None, "unit": None, "status": "absent"}
    base.update(overrides)
    return SimpleNamespace(**base)


# --- fact definitions ---------------------------------------------------

def test_valid_fact_definitions_pass():
    assert validate_business_fact(ZONE_FACT) == []
    assert validate_business_fact(FREQUENCE_FACT) == []


def test_fact_with_unsupported_type_is_rejected():
    errors = validate_business_fact({"key": "x", "label": "X", "type": "matrix", "unit": None})
    assert any("non pris en charge" in e for e in errors)


def test_fact_unit_only_allowed_on_number_type():
    errors = validate_business_fact({"key": "zone", "label": "Zone", "type": "list", "unit": "km"})
    assert any("unité" in e for e in errors)


def test_fact_missing_key_or_label_is_rejected():
    assert validate_business_fact({"key": "", "label": "X", "type": "text"}) != []
    assert validate_business_fact({"key": "x", "label": "", "type": "text"}) != []


def test_business_facts_dict_key_mismatch_is_rejected():
    broken = {"zone_intervention": {**ZONE_FACT, "key": "autre_cle"}}
    errors = validate_business_facts(broken)
    assert any("correspondre" in e for e in errors)


def test_business_facts_valid_dict_passes():
    assert validate_business_facts(CLEANING_FACTS) == []
    assert validate_business_facts(BTP_FACTS) == []


# --- custom criteria ------------------------------------------------------

def _zone_criterion(**overrides):
    base = {
        "id": "zone_couverte", "label": "Zone d'intervention couverte", "fact_key": "zone_intervention",
        "operator": "list_coverage", "weight": 10, "blocking": True, "pass_score": 100, "fail_score": 0,
    }
    base.update(overrides)
    return base


def _frequence_criterion(**overrides):
    base = {
        "id": "frequence_ok", "label": "Fréquence de nettoyage compatible", "fact_key": "frequence_nettoyage",
        "operator": "numeric_threshold", "comparison": "provider_gte_ao",
        "weight": 15, "blocking": False, "pass_score": 100, "fail_score": 30,
    }
    base.update(overrides)
    return base


def test_valid_custom_criterion_passes():
    assert validate_custom_criterion(_zone_criterion(), known_facts=CLEANING_FACTS) == []
    assert validate_custom_criterion(_frequence_criterion(), known_facts=CLEANING_FACTS) == []


def test_criterion_referencing_unknown_fact_is_rejected():
    errors = validate_custom_criterion(_zone_criterion(fact_key="fait_inexistant"), known_facts=CLEANING_FACTS)
    assert any("inconnu" in e for e in errors)


def test_criterion_operator_incompatible_with_fact_type_is_rejected():
    # numeric_threshold on a "list" fact — not pris en charge.
    errors = validate_custom_criterion(
        _zone_criterion(operator="numeric_threshold", comparison="provider_gte_ao"), known_facts=CLEANING_FACTS,
    )
    assert any("pas compatible" in e for e in errors)


def test_numeric_threshold_without_comparison_is_rejected():
    errors = validate_custom_criterion(_frequence_criterion(comparison=None), known_facts=CLEANING_FACTS)
    assert any("comparaison" in e for e in errors)


def test_unsupported_operator_is_rejected():
    errors = validate_custom_criterion(_zone_criterion(operator="sql_query"), known_facts=CLEANING_FACTS)
    assert any("non prise en charge" in e for e in errors)


def test_criterion_missing_blocking_flag_is_rejected():
    criterion = _zone_criterion()
    del criterion["blocking"]
    errors = validate_custom_criterion(criterion, known_facts=CLEANING_FACTS)
    assert any("bloquant" in e for e in errors)


def test_criterion_score_out_of_range_is_rejected():
    errors = validate_custom_criterion(_zone_criterion(pass_score=150), known_facts=CLEANING_FACTS)
    assert any("pass_score" in e for e in errors)


def test_duplicate_criterion_ids_are_rejected():
    errors = validate_custom_criteria([_zone_criterion(), _zone_criterion()], known_facts=CLEANING_FACTS)
    assert any("dupliqué" in e for e in errors)


def test_weight_total_sums_only_well_formed_weights():
    criteria = [_zone_criterion(weight=10), _frequence_criterion(weight=15), {"weight": "not-a-number"}]
    assert custom_criteria_weight_total(criteria) == 25.0


# --- evaluation -----------------------------------------------------------

def test_list_coverage_passes_when_provider_covers_required_zone():
    criterion = _zone_criterion()
    ao_fact = _fact(value=["Lyon"], status="found")
    score, passed, reason = evaluate_custom_criterion(
        criterion, ao_fact=ao_fact, provider_value=["Lyon", "Grenoble"], provider_unit=None,
    )
    assert (score, passed, reason) == (100.0, True, "ok")


def test_list_coverage_fails_when_provider_does_not_cover_required_zone():
    criterion = _zone_criterion()
    ao_fact = _fact(value=["Marseille"], status="found")
    score, passed, reason = evaluate_custom_criterion(
        criterion, ao_fact=ao_fact, provider_value=["Lyon", "Grenoble"], provider_unit=None,
    )
    assert (score, passed, reason) == (0.0, False, "ok")


def test_numeric_threshold_provider_gte_ao_passes_when_capacity_sufficient():
    criterion = _frequence_criterion()
    ao_fact = _fact(value=2, unit="par_semaine", status="found")
    score, passed, reason = evaluate_custom_criterion(
        criterion, ao_fact=ao_fact, provider_value=3, provider_unit="par_semaine",
    )
    assert (score, passed, reason) == (100.0, True, "ok")


def test_numeric_threshold_fails_when_capacity_insufficient():
    criterion = _frequence_criterion()
    ao_fact = _fact(value=5, unit="par_semaine", status="found")
    score, passed, reason = evaluate_custom_criterion(
        criterion, ao_fact=ao_fact, provider_value=3, provider_unit="par_semaine",
    )
    assert (score, passed, reason) == (30.0, False, "ok")


def test_numeric_threshold_unit_mismatch_is_never_converted():
    """Ticket: 'ne pas convertir une heure-personne en disponibilité
    globale sans données permettant le calcul' — a unit mismatch is an
    honest 'cannot be computed', never a silent conversion."""
    criterion = _frequence_criterion()
    ao_fact = _fact(value=2, unit="par_mois", status="found")  # different unit than provider
    score, passed, reason = evaluate_custom_criterion(
        criterion, ao_fact=ao_fact, provider_value=3, provider_unit="par_semaine",
    )
    assert score is None
    assert reason == "unit_mismatch"


def test_ao_fact_absent_is_never_guessed():
    criterion = _zone_criterion()
    ao_fact = _fact(value=None, status="absent")
    score, passed, reason = evaluate_custom_criterion(
        criterion, ao_fact=ao_fact, provider_value=["Lyon"], provider_unit=None,
    )
    assert score is None
    assert passed is False
    assert reason == "ao_fact_missing"


def test_ao_fact_ambiguous_is_never_guessed():
    criterion = _zone_criterion()
    ao_fact = _fact(value=None, status="ambiguous")
    score, passed, reason = evaluate_custom_criterion(
        criterion, ao_fact=ao_fact, provider_value=["Lyon"], provider_unit=None,
    )
    assert score is None
    assert reason == "ao_fact_missing"


def test_provider_fact_missing_is_never_guessed():
    criterion = _zone_criterion()
    ao_fact = _fact(value=["Lyon"], status="found")
    score, passed, reason = evaluate_custom_criterion(
        criterion, ao_fact=ao_fact, provider_value=None, provider_unit=None,
    )
    assert score is None
    assert reason == "provider_fact_missing"


def test_equality_operator_matches_exact_value():
    criterion = {
        "id": "materiel_grue", "label": "Grue disponible", "fact_key": "materiel_disponible",
        "operator": "equality", "weight": 5, "blocking": False, "pass_score": 100, "fail_score": 0,
    }
    ao_fact = _fact(value="grue", status="found")
    score, passed, reason = evaluate_custom_criterion(criterion, ao_fact=ao_fact, provider_value="grue", provider_unit=None)
    assert (score, passed, reason) == (100.0, True, "ok")
    score, passed, reason = evaluate_custom_criterion(criterion, ao_fact=ao_fact, provider_value="nacelle", provider_unit=None)
    assert (score, passed, reason) == (0.0, False, "ok")


# --- requested_facts_from_criteria (extraction spec builder) --------------

def test_requested_facts_includes_recognition_vocabulary_for_list_facts():
    declared = {
        "zone_intervention": {**ZONE_FACT, "value": ["Lyon", "Villeurbanne"]},
        "frequence_nettoyage": {**FREQUENCE_FACT, "value": 3},
    }
    requested = requested_facts_from_criteria(
        [_zone_criterion(), _frequence_criterion()], known_facts=declared,
    )
    assert requested["zone_intervention"]["type"] == "list"
    assert requested["zone_intervention"]["recognition_vocabulary"] == ["Lyon", "Villeurbanne"]
    assert requested["frequence_nettoyage"]["type"] == "number"
    assert requested["frequence_nettoyage"]["unit"] == "par_semaine"
    assert "recognition_vocabulary" not in requested["frequence_nettoyage"]


def test_requested_facts_skips_criteria_referencing_unknown_facts():
    requested = requested_facts_from_criteria([_zone_criterion(fact_key="inconnu")], known_facts=CLEANING_FACTS)
    assert requested == {}


def test_catalogue_constants_are_stable():
    """A change to these frozensets is a deliberate product decision, not
    an accident — this test exists so the exact catalogue is visible in
    one place and any change is a conscious diff."""
    assert FACT_TYPES == frozenset({"number", "list", "boolean", "text"})
    assert OPERATORS == frozenset({"numeric_threshold", "list_coverage", "equality"})
