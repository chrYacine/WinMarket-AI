"""Lot 41 — reliability of the sector-neutral scoring (D3-01, D3-02, D3-03).

D3-01  the scoring simulation runs the SAME business computation as a real
       analysis (custom criteria + declared facts), with no external call.
D3-02  the local extraction fallback never presents a partial recognition as
       a complete requirement, and never reads a boolean from the mere
       presence of its label.
D3-03  fact/criterion validation is total (no TypeError, no infinite
       capacity, no boolean-as-number) and the engine survives inconsistent
       historical data without a favorable result.

Every fixture is synthetic and explicitly a test (cleaning, IT); no real
provider is ever reachable.
"""
from __future__ import annotations

import re

import pytest

from src.agents import business_facts
from src.agents.ao_extractor import _extract_fact_locally
from src.agents.scoring_engine import ScoringEngine, ScoringPolicySnapshot
from src.core.models import AOContext, CapacityResult, CompanyProfile, ExtractedFact
from tests.conftest import make_active_starter_user

ZONE = {"label": "Zone d'intervention", "type": "list", "unit": None, "recognition_vocabulary": ["Lyon", "Villeurbanne"]}
NIGHT = {"label": "Travail de nuit", "type": "boolean", "unit": None}
FREQ = {"label": "Fréquence de nettoyage", "type": "number", "unit": "par_semaine"}


# ---------------------------------------------------------------------------
# D3-02 — extraction fallback
# ---------------------------------------------------------------------------

def test_lyon_et_marseille_against_a_provider_covering_only_lyon_is_never_a_complete_list():
    fact = _extract_fact_locally("Le prestataire interviendra sur les sites de Lyon et Marseille.", ZONE)
    assert fact.status == "ambiguous"
    assert fact.value is None
    assert fact.reason == "partial_list_possible"


@pytest.mark.parametrize("text", [
    "Zones : Marseille, Lyon.",
    "Sites concernés : Lyon, Marseille.",
    "Lyon / Marseille / Nice",
    "Interventions :\n- Lyon\n- Marseille",
    "Lyon, Villeurbanne, etc.",
    "Lyon notamment.",
])
def test_any_enumeration_that_may_hide_unrecognized_items_is_ambiguous(text):
    assert _extract_fact_locally(text, ZONE).status == "ambiguous"


def test_an_enumeration_made_only_of_recognized_names_is_a_complete_found():
    fact = _extract_fact_locally("Sites : Lyon et Villeurbanne.", ZONE)
    assert fact.status == "found"
    assert fact.value == ["Lyon", "Villeurbanne"]


def test_a_single_mention_followed_by_unrelated_text_is_still_found():
    fact = _extract_fact_locally("Intervention sur le site de Lyon, 3 fois par semaine.", ZONE)
    assert fact.status == "found" and fact.value == ["Lyon"]


def test_a_longer_word_containing_a_known_name_is_not_a_mention():
    assert _extract_fact_locally("Cuisine lyonnaise.", ZONE).status == "absent"


@pytest.mark.parametrize("text,expected", [
    ("Travail de nuit : non.", False),
    ("Travail de nuit : oui.", True),
    ("Le travail de nuit n'est pas exigé.", False),
    ("Le travail de nuit est exigé.", True),
    ("Aucun travail de nuit ne sera demandé.", False),
])
def test_boolean_fallback_reads_polarity_not_presence(text, expected):
    fact = _extract_fact_locally(text, NIGHT)
    assert fact.status == "found"
    assert fact.value is expected, "0/false is a real value, never dropped and never turned into true"


def test_boolean_fallback_reports_uncertainty_instead_of_guessing():
    assert _extract_fact_locally("Le travail de nuit sera discuté ultérieurement.", NIGHT).status == "ambiguous"
    contradictory = "Travail de nuit : non. Plus loin : travail de nuit exigé."
    assert _extract_fact_locally(contradictory, NIGHT).status == "ambiguous"
    assert _extract_fact_locally("Aucune mention utile.", NIGHT).status == "absent"


def test_conflicting_numbers_for_the_same_unit_are_ambiguous_not_first_match():
    fact = _extract_fact_locally("3 fois par semaine en été, 5 fois par semaine en hiver.", FREQ)
    assert fact.status == "ambiguous" and fact.reason == "conflicting_values"


def test_an_astronomically_large_number_never_becomes_infinity():
    fact = _extract_fact_locally("9" * 400 + " fois par semaine", FREQ)
    assert fact.status == "absent"


def _policy(specs, facts, **rules):
    base = dict(budget_minimum_eur=0, max_charge_pct=100, max_unmastered_technologies=999, certification_penalty_score=20)
    base.update(rules)
    weights = {
        "Adequation expertise": 0, "References similaires": 10, "Disponibilite equipe": 15, "Rentabilite estimee": 15,
        "Faisabilite delai": 10, "Certifications requises": 10, "Complexite technique": 0, "Connaissance secteur": 10,
        "Potentiel commercial": 5, "Risque contractuel": 5, "Solidite client": 5, "Valeur strategique": 0,
    }
    return ScoringPolicySnapshot.from_legacy(
        weights=weights, threshold_go=80, threshold_sous_reserve=55, custom_criteria=specs, declared_facts=facts, **base,
    )


def _ao(**facts):
    return AOContext(titre="AO test", client="Client", budget_estime=80_000, texte_source="AO de test.", extracted_facts=facts)


ZONE_CRITERION = {
    "id": "zone_couverte", "label": "Zone couverte", "fact_key": "zone", "operator": "list_coverage",
    "weight": 15, "blocking": True, "pass_score": 100, "fail_score": 0,
}
CAPACITY = CapacityResult(charge_actuelle_pct=40, capacite_restante_pct=60, equipe_disponible=True, commentaire="ok")


def test_a_partial_zone_recognition_ends_incomplet_never_a_favorable_decision():
    """Requirement 'Lyon et Marseille' against a provider covering Lyon only:
    extraction says ambiguous -> the engine must say INCOMPLET (and never
    validate coverage)."""
    fact = _extract_fact_locally("Sites de Lyon et Marseille.", ZONE)
    result = ScoringEngine().score(
        _ao(zone=fact), CompanyProfile(), [], CAPACITY,
        policy=_policy([ZONE_CRITERION], {"zone": {"type": "list", "value": ["Lyon"]}}),
    )
    assert result.decision == "INCOMPLET"
    assert "custom:zone_couverte" in result.scoring_missing
    assert "partial_list_possible" in next(c.justification for c in result.criteres if c.nom == "Zone couverte")


# ---------------------------------------------------------------------------
# D3-03 — validation and defense of the computation
# ---------------------------------------------------------------------------

def test_type_given_as_a_list_is_a_structured_error_not_a_typeerror():
    errors = business_facts.validate_business_fact({"key": "x", "label": "X", "type": []})
    assert any("non pris en charge" in e for e in errors)
    criterion = dict(ZONE_CRITERION, operator=[])
    assert any("non prise en charge" in e for e in business_facts.validate_custom_criterion(
        criterion, known_facts={"zone": {"key": "zone", "label": "Z", "type": "list"}}))
    assert any("pas compatible" in e for e in business_facts.validate_custom_criterion(
        ZONE_CRITERION, known_facts={"zone": {"key": "zone", "label": "Z", "type": {}}}))


@pytest.mark.parametrize("bad", ["inf", "Infinity", "nan", "12", True, False, float("inf"), float("nan"), [1]])
def test_a_number_fact_only_accepts_a_finite_real_number(bad):
    fact = {"key": "capacite", "label": "Capacité", "type": "number", "unit": "equipes", "value": bad}
    assert business_facts.validate_business_fact(fact), f"{bad!r} must be refused as a number value"


def test_zero_and_false_are_valid_declared_values():
    assert business_facts.validate_business_fact({"key": "capacite", "label": "C", "type": "number", "unit": "u", "value": 0}) == []
    assert business_facts.validate_business_fact({"key": "nuit", "label": "N", "type": "boolean", "value": False}) == []
    facts = {"nuit": {"key": "nuit", "label": "N", "type": "boolean", "value": False}}
    crit = {"id": "c", "label": "C", "fact_key": "nuit", "operator": "equality", "weight": 0, "blocking": True,
            "pass_score": 100, "fail_score": 0}
    assert business_facts.validate_referenced_fact_values([crit], known_facts=facts) == []
    assert business_facts.validate_custom_criterion(crit, known_facts=facts) == []


@pytest.mark.parametrize("field,value", [
    ("weight", float("nan")), ("weight", float("inf")), ("weight", True), ("weight", "10"), ("weight", 101),
    ("pass_score", float("inf")), ("fail_score", float("nan")), ("pass_score", True), ("fail_score", -1),
    ("blocking", 1), ("blocking", "true"),
])
def test_weights_scores_and_blocking_must_be_finite_numbers_and_strict_booleans(field, value):
    facts = {"zone": {"key": "zone", "label": "Z", "type": "list", "value": ["Lyon"]}}
    assert business_facts.validate_custom_criterion(dict(ZONE_CRITERION, **{field: value}), known_facts=facts)


def test_referenced_fact_without_a_declared_value_is_an_activation_error():
    facts = {"zone": {"key": "zone", "label": "Z", "type": "list"}}
    assert business_facts.validate_referenced_fact_values([ZONE_CRITERION], known_facts=facts)


def test_evaluation_never_converts_a_string_and_never_raises_on_bad_history():
    threshold = {"id": "f", "operator": "numeric_threshold", "comparison": "provider_gte_ao", "pass_score": 100, "fail_score": 0}
    found = ExtractedFact(value=50.0, status="found")
    assert business_facts.evaluate_custom_criterion(threshold, ao_fact=found, provider_value="inf", provider_unit=None)[0] is None
    assert business_facts.evaluate_custom_criterion(threshold, ao_fact=found, provider_value=True, provider_unit=None)[0] is None
    for broken in (None, {}, {"operator": []}, {"operator": "equality"}, dict(threshold, pass_score=float("nan")), "x"):
        score, passed, _reason = business_facts.evaluate_custom_criterion(
            broken, ao_fact=found, provider_value=3, provider_unit=None)
        assert score is None and passed is False


def test_an_infinite_declared_capacity_is_never_sufficient():
    threshold = {"id": "f", "operator": "numeric_threshold", "comparison": "provider_gte_ao", "pass_score": 100, "fail_score": 0}
    score, passed, reason = business_facts.evaluate_custom_criterion(
        threshold, ao_fact=ExtractedFact(value=50.0, status="found"), provider_value=float("inf"), provider_unit=None)
    assert score is None and passed is False and reason == "non_numeric_value"


def test_true_is_never_equal_to_one():
    equality = {"id": "e", "operator": "equality", "pass_score": 100, "fail_score": 0}
    assert business_facts.evaluate_custom_criterion(
        equality, ao_fact=ExtractedFact(value=True, status="found"), provider_value=1, provider_unit=None)[0] is None


def test_engine_survives_inconsistent_historical_criteria_and_never_favors():
    weird_specs = [
        dict(ZONE_CRITERION),
        "not-a-dict",
        {"id": "no_fact", "label": "Sans fait", "operator": "list_coverage", "weight": 5, "blocking": True,
         "pass_score": 100, "fail_score": 0},
        {"id": "bad_weight", "label": "Poids fou", "fact_key": "zone", "operator": "list_coverage",
         "weight": float("inf"), "blocking": False, "pass_score": 100, "fail_score": 0},
    ]
    result = ScoringEngine().score(
        _ao(zone=ExtractedFact(value=["Lyon"], status="found")), CompanyProfile(), [], CAPACITY,
        policy=_policy(weird_specs, {"zone": {"type": "list", "value": ["Lyon"]}}),
    )
    assert result.decision == "INCOMPLET"
    assert {"custom:no_fact", "custom:bad_weight"} <= set(result.scoring_missing)
    assert all(c.poids != float("inf") for c in result.criteres)


def test_engine_reports_a_non_list_custom_configuration_as_incomplete():
    result = ScoringEngine().score(
        _ao(), CompanyProfile(), [], CAPACITY, policy=_policy({"id": "x"}, "not-a-dict"),
    )
    assert result.decision == "INCOMPLET" and "custom:configuration" in result.scoring_missing


# ---------------------------------------------------------------------------
# HTTP — D3-01 simulation and D3-03 activation refusal
# ---------------------------------------------------------------------------

WEIGHTS = {
    "Adequation expertise": 0, "References similaires": 10, "Disponibilite equipe": 15, "Rentabilite estimee": 15,
    "Faisabilite delai": 10, "Certifications requises": 10, "Complexite technique": 0, "Connaissance secteur": 10,
    "Potentiel commercial": 5, "Risque contractuel": 5, "Solidite client": 5, "Valeur strategique": 0,
}  # 85 -> 15 points left for the custom criteria
RULES = {"budget_minimum_eur": 0, "max_charge_pct": 100, "max_unmastered_technologies": 999, "certification_penalty_score": 20}
IT_WEIGHTS = {
    "Adequation expertise": 20, "References similaires": 15, "Disponibilite equipe": 10, "Rentabilite estimee": 10,
    "Faisabilite delai": 10, "Certifications requises": 10, "Complexite technique": 5, "Connaissance secteur": 5,
    "Potentiel commercial": 5, "Risque contractuel": 5, "Solidite client": 3, "Valeur strategique": 2,
}


def _login(client, email):
    client.cookies.clear()
    page = client.get("/login")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    client.post("/login", data={"email": email, "password": "Sup3rSecret!", "next": "/app", "csrf_token": csrf})
    return csrf


def _cleaning_draft(client, csrf, *, frequency_blocking=True):
    client.post("/api/capacity", json={
        "charge_globale_pct": 40, "nombre_projets_en_cours": 1, "projets_en_cours": ["Chantier"],
        "capacites_par_pole": {"Nettoyage": 40}}, headers={"X-CSRF-Token": csrf})
    r = client.put("/api/scoring-config/profile", json={
        "raison_sociale": "Nettoyage Pro (test)", "competences": [], "certifications": [],
        "business_facts": {
            "zone_intervention": {"key": "zone_intervention", "label": "Zone d'intervention", "type": "list",
                                  "unit": None, "value": ["Lyon", "Villeurbanne"]},
            "frequence_nettoyage": {"key": "frequence_nettoyage", "label": "Fréquence de nettoyage",
                                    "type": "number", "unit": "par_semaine", "value": 3},
        }}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200, r.text
    r = client.put("/api/scoring-config/policy", json={
        "weights": WEIGHTS, "threshold_go": 80, "threshold_sous_reserve": 55, "business_rules": RULES,
        "custom_criteria": [
            {"id": "zone_couverte", "label": "Zone couverte", "fact_key": "zone_intervention", "operator": "list_coverage",
             "weight": 10, "blocking": False, "pass_score": 100, "fail_score": 0},
            {"id": "frequence_ok", "label": "Fréquence compatible", "fact_key": "frequence_nettoyage",
             "operator": "numeric_threshold", "comparison": "provider_gte_ao", "weight": 5,
             "blocking": frequency_blocking, "pass_score": 100, "fail_score": 10},
        ]}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200, r.text


class _ProviderThatMustNeverBeCalled:
    enabled = True
    name = "forbidden"

    def json_complete(self, *args, **kwargs):
        raise AssertionError("the simulation must never call a provider")

    def complete(self, *args, **kwargs):
        raise AssertionError("the simulation must never call a provider")


def _simulate(client, csrf, text):
    return client.post("/api/scoring-config/simulate", data={"mode": "paste", "text": text}, headers={"X-CSRF-Token": csrf})


def test_simulation_applies_custom_weights_and_blocker_without_any_provider(client, db, monkeypatch):
    import src.agents.ao_extractor as extractor_module
    monkeypatch.setattr(extractor_module, "ClaudeClient", lambda: _ProviderThatMustNeverBeCalled())
    make_active_starter_user(db, "sim41@example.com", scoring=False)
    csrf = _login(client, "sim41@example.com")
    _cleaning_draft(client, csrf)

    ok = _simulate(client, csrf, "Nettoyage de locaux. Site de Lyon, 2 fois par semaine. Budget 80000 euros.")
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["simulation"] is True
    by_name = {c["nom"]: c for c in body["criteres"]}
    assert by_name["Zone couverte"]["poids"] == 10 and by_name["Zone couverte"]["score"] == 100
    assert by_name["Fréquence compatible"]["poids"] == 5 and by_name["Fréquence compatible"]["score"] == 100
    assert body["scoring_completeness"] == "complete"
    assert body["score_global"] == round(sum(c["score"] * c["poids"] for c in body["criteres"]) / 100, 1)

    blocked = _simulate(client, csrf, "Nettoyage de locaux. Site de Lyon, 10 fois par semaine. Budget 80000 euros.").json()
    assert blocked["decision"] == "NO-GO"
    assert any("Fréquence compatible" in b for b in blocked["criteres_bloquants"])
    assert next(c for c in blocked["criteres"] if c["nom"] == "Fréquence compatible")["score"] == 10


def test_simulation_with_insufficient_local_extraction_is_incomplet_not_ignored(client, db):
    make_active_starter_user(db, "sim41b@example.com", scoring=False)
    csrf = _login(client, "sim41b@example.com")
    _cleaning_draft(client, csrf)

    body = _simulate(client, csrf, "Sites de Lyon et Marseille, 2 fois par semaine.").json()
    assert body["decision"] == "INCOMPLET"
    assert "custom:zone_couverte" in body["scoring_missing"]
    zone = next(c for c in body["criteres"] if c["nom"] == "Zone couverte")
    assert zone["score"] == 0 and "partial_list_possible" in zone["justification"]

    absent = _simulate(client, csrf, "Nettoyage de locaux, aucune précision.").json()
    assert absent["decision"] == "INCOMPLET"
    assert {"custom:zone_couverte", "custom:frequence_ok"} <= set(absent["scoring_missing"])


def test_simulation_never_activates_the_draft_nor_writes_history(client, db):
    make_active_starter_user(db, "sim41c@example.com", scoring=False)
    csrf = _login(client, "sim41c@example.com")
    _cleaning_draft(client, csrf)
    assert _simulate(client, csrf, "Site de Lyon, 2 fois par semaine.").status_code == 200

    state = client.get("/api/scoring-config").json()
    assert state["state"] == "brouillon" and state["policy"]["active"] is None
    assert state["policy"]["draft"]["status"] == "draft"
    assert client.get("/api/history").json()["total"] == 0
    assert client.post("/api/analyze", data={"mode": "paste", "text": "x"}, headers={"X-CSRF-Token": csrf}).status_code == 409


def _put_profile_raw(client, csrf, facts_json: str):
    return client.put(
        "/api/scoring-config/profile",
        content='{"raison_sociale": "Test", "competences": [], "certifications": [], "business_facts": ' + facts_json + "}",
        headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"},
    )


@pytest.mark.parametrize("facts_json", [
    '{"cap": {"label": "Capacité", "type": "number", "unit": "equipes", "value": "inf"}}',
    '{"cap": {"label": "Capacité", "type": "number", "unit": "equipes", "value": Infinity}}',
    '{"cap": {"label": "Capacité", "type": "number", "unit": "equipes", "value": true}}',
    '{"cap": {"label": "Capacité", "type": []}}',
])
def test_activation_refuses_invalid_fact_values_and_types_with_structured_errors(client, db, facts_json):
    make_active_starter_user(db, "act41@example.com", scoring=False)
    csrf = _login(client, "act41@example.com")
    client.post("/api/capacity", json={"charge_globale_pct": 40, "nombre_projets_en_cours": 1, "projets_en_cours": [],
                                        "capacites_par_pole": {"N": 40}}, headers={"X-CSRF-Token": csrf})
    # A draft holding an incomplete/invalid fact stays SAVEABLE (never a 500)...
    assert _put_profile_raw(client, csrf, facts_json).status_code == 200
    client.put("/api/scoring-config/policy", json={
        "weights": IT_WEIGHTS, "threshold_go": 88, "threshold_sous_reserve": 60, "business_rules": RULES,
        "custom_criteria": []}, headers={"X-CSRF-Token": csrf})
    validation = client.post("/api/scoring-config/policy/validate", headers={"X-CSRF-Token": csrf})
    assert validation.status_code == 200 and validation.json()["valid"] is False
    assert "business_facts" in validation.json()["errors"]
    # ...but validation/activation refuse it, with no partial activation.
    refused = client.post("/api/scoring-config/policy/activate", json={"expected_active_version": None},
                          headers={"X-CSRF-Token": csrf})
    assert refused.status_code == 422
    assert refused.json()["detail"]["error_code"] == "SCORING_POLICY_INVALID"
    assert "business_facts" in refused.json()["detail"]["errors"]
    state = client.get("/api/scoring-config").json()
    assert state["policy"]["active"] is None and state["policy"]["draft"]["status"] == "draft"


def test_invalid_criterion_values_are_refused_at_activation_and_leave_the_draft_untouched(client, db):
    import json
    make_active_starter_user(db, "act41b@example.com", scoring=False)
    csrf = _login(client, "act41b@example.com")
    _cleaning_draft(client, csrf)
    body = (
        '{"weights": ' + json.dumps(WEIGHTS) + ', "threshold_go": 80, "threshold_sous_reserve": 55,'
        ' "business_rules": ' + json.dumps(RULES) + ','
        ' "custom_criteria": [{"id": "zone_couverte", "label": "Z", "fact_key": "zone_intervention",'
        ' "operator": "list_coverage", "weight": NaN, "blocking": "oui", "pass_score": Infinity, "fail_score": 0}]}'
    )
    saved = client.put("/api/scoring-config/policy", content=body,
                       headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"})
    assert saved.status_code == 200, saved.text
    draft = client.get("/api/scoring-config").json()["policy"]["draft"]
    assert draft["custom_criteria"][0]["weight"] == "nan", "non-finite JSON numbers are kept as text and flagged, never stored as numbers"
    refused = client.post("/api/scoring-config/policy/activate", json={"expected_active_version": None},
                          headers={"X-CSRF-Token": csrf})
    assert refused.status_code == 422
    assert len(refused.json()["detail"]["errors"]["custom_criteria"]) >= 3
    assert client.get("/api/scoring-config").json()["policy"]["active"] is None


def test_a_valid_non_it_configuration_and_a_valid_it_configuration_both_activate(client, db):
    make_active_starter_user(db, "nonit41@example.com", scoring=False)
    csrf = _login(client, "nonit41@example.com")
    _cleaning_draft(client, csrf)
    assert client.post("/api/scoring-config/policy/validate", headers={"X-CSRF-Token": csrf}).json()["valid"] is True
    r = client.post("/api/scoring-config/policy/activate", json={"expected_active_version": None}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200 and r.json()["status"] == "active"

    make_active_starter_user(db, "it41@example.com", scoring=False)
    csrf = _login(client, "it41@example.com")
    client.post("/api/capacity", json={"charge_globale_pct": 40, "nombre_projets_en_cours": 1, "projets_en_cours": [],
                                        "capacites_par_pole": {"Software": 40}}, headers={"X-CSRF-Token": csrf})
    client.put("/api/scoring-config/profile", json={"raison_sociale": "ESN (test)", "competences": ["python"],
                                                    "certifications": []}, headers={"X-CSRF-Token": csrf})
    client.put("/api/scoring-config/policy", json={"weights": IT_WEIGHTS, "threshold_go": 88, "threshold_sous_reserve": 60,
                                                    "business_rules": RULES}, headers={"X-CSRF-Token": csrf})
    r = client.post("/api/scoring-config/policy/activate", json={"expected_active_version": None}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200 and r.json()["custom_criteria"] == []
