"""Lot 44 — the explicit-criteria contract of a private scoring policy.

What these tests pin (all with hand-built snapshots or a temporary database;
the LLM is never reachable):
A. a new policy starts EMPTY: nothing of the twelve historical criteria, no
   default note, no IT list — the parameters the account entered decide;
B. unknown / ambiguous / not-applicable / zero / false / null weights /
   blockers follow the contract (no invented note, no hidden bonus);
C. activation validation (a new policy is only valid if the user provided the
   criteria, the thresholds and a 100-point weight budget);
D. the Lyon / 500 EUR / 4 interventions / night forbidden scenario is
   computed according to the policy that was ENTERED (no imposed score);
E. analysis and simulation resolve the configuration through the same
   function, for the right organization + owner, and the whole HTTP path
   (empty draft -> criteria -> validate -> simulate -> activate -> analysis ->
   result / history / documents) works on a new-format policy.
"""
from __future__ import annotations

import copy
import json
import re
import time
from pathlib import Path

import pytest

from src.agents import criteria_catalogue
from src.agents.scoring_engine import ScoringEngine, ScoringPolicySnapshot
from src.agents.scoring_policy_validation import validate_criteria_policy
from src.core.models import AOContext, CapacityResult, CompanyProfile, ExtractedFact, RAGEvidence
from src.web import jobs
from tests.conftest import make_active_starter_user

FIXTURES = Path(__file__).parent / "fixtures"
def _lyon_normalized():
    # Explicit synthetic fixture; no historical/private QA artifact dependency.
    return json.loads((FIXTURES / "lot56_synthetic_facts.json").read_text(encoding="utf-8"))


def crit(cid, evaluator, params, weight, *, label=None, blocking=False, on_missing=None, enabled=True, disabled_reason=None):
    return {"id": cid, "label": label or cid, "evaluator": evaluator, "params": params, "weight": weight, "blocking": blocking,
            "on_missing": on_missing or {"mode": "incomplete"}, "enabled": enabled, "disabled_reason": disabled_reason}


def policy(criteria, *, go=80, sr=55, settings=None, facts=None, competences=(), certs=()):
    return ScoringPolicySnapshot(
        criteria=criteria, threshold_go=go, threshold_sous_reserve=sr, declared_facts=facts or {},
        mastered_technologies=frozenset(competences), certifications_held=frozenset(certs),
        settings={**criteria_catalogue.default_settings(), **(settings or {})}, version=1, origin="user",
    )


CAPACITY = CapacityResult(charge_actuelle_pct=40, capacite_restante_pct=60, equipe_disponible=True, commentaire="Capacité de test.")


def score(ao, pol, *, evidences=()):
    return ScoringEngine().score(ao, CompanyProfile(), list(evidences), CAPACITY, policy=pol)


def by_id(result):
    return {c.critere_id: c for c in result.criteres}


TIERS = {"source": "budget", "fact_key": None, "tiers": [{"at_least": 100000, "score": 90}, {"at_least": 50000, "score": 60}],
         "below_score": 20, "zero_score": None, "minimum_blocking": None}


# ---------------------------------------------------------------------------
# A — a new policy starts empty; the account's parameters are what count
# ---------------------------------------------------------------------------

def test_a_new_policy_carries_only_the_criteria_the_account_entered():
    pol = policy([crit("budget", "numeric_tiers", TIERS, 100)])
    result = score(AOContext(titre="AO", budget_estime=120000.0), pol)
    assert [c.critere_id for c in result.criteres] == ["budget"], "none of the twelve historical criteria is implied"
    assert result.score_global == 90.0 and result.decision == "GO"
    assert result.criteria_version == 1 and result.policy_origin == "user" and result.score_provisoire is False


def test_the_entered_parameters_decide_the_note_no_hidden_default():
    ao = AOContext(titre="AO", budget_estime=120000.0)
    strict = copy.deepcopy(TIERS)
    strict["tiers"] = [{"at_least": 500000, "score": 90}]
    strict["below_score"] = 10
    assert score(ao, policy([crit("budget", "numeric_tiers", strict, 100)])).score_global == 10.0
    assert score(ao, policy([crit("budget", "numeric_tiers", TIERS, 100)])).score_global == 90.0


def test_two_accounts_with_different_activities_are_scored_on_their_own_criteria():
    ao = AOContext(titre="AO", budget_estime=80000.0, technologies_demandees=["Python", "Django"], texte_source="Prestation.",
                   extracted_facts={"surface": ExtractedFact(value=900.0, unit="m2", status="found", provenance="llm")})
    it = policy([crit("competences", "technology_coverage", {"none_requested_score": None, "max_unmastered_blocking": None}, 60),
                 crit("budget", "numeric_tiers", TIERS, 40)], competences=["python"])
    cleaning = policy(
        [crit("surface", "numeric_threshold", {"fact_key": "surface", "comparison": "provider_gte_ao", "pass_score": 100, "fail_score": 0}, 100)],
        facts={"surface": {"key": "surface", "label": "Surface", "type": "number", "unit": "m2", "value": 1500}})
    r_it, r_cleaning = score(ao, it), score(ao, cleaning)
    assert [c.critere_id for c in r_it.criteres] == ["competences", "budget"]
    assert [c.critere_id for c in r_cleaning.criteres] == ["surface"]
    assert r_it.score_global == round((50.0 * 60 + 60 * 40) / 100, 1)
    assert r_cleaning.score_global == 100.0


# ---------------------------------------------------------------------------
# B — unknown, ambiguous, not applicable, zero / false, null weights, blockers
# ---------------------------------------------------------------------------

def test_an_unknown_required_datum_is_not_evaluated_no_invented_note_and_the_decision_is_incomplete():
    pol = policy([crit("budget", "numeric_tiers", TIERS, 60), crit("capacite", "capacity_availability",
                 {"available_score": 100, "unavailable_score": 0, "max_charge_blocking": None}, 40)])
    result = score(AOContext(titre="AO"), pol)  # budget_estime is None
    budget = by_id(result)["budget"]
    assert budget.etat == "manquant" and budget.score == 0.0 and "non évalué" in budget.justification
    assert result.decision == "INCOMPLET" and "criterion:budget" in result.scoring_missing
    assert result.scoring_completeness == "incomplete"
    # the computable part is a PROVISIONAL score, never a success probability
    assert result.score_provisoire is True and result.score_global == 40.0
    assert any("provisoire" in r for r in result.recommandations)


def test_an_ambiguous_extracted_fact_is_not_evaluated_and_names_its_reason():
    pol = policy([crit("zone", "list_coverage", {"fact_key": "zone", "pass_score": 100, "fail_score": 0}, 100)],
                 facts={"zone": {"key": "zone", "label": "Zone", "type": "list", "unit": None, "value": ["Lyon"]}})
    ao = AOContext(titre="AO", extracted_facts={"zone": ExtractedFact(status="ambiguous", provenance="llm", reason="llm_list_may_omit: Marseille")})
    result = score(ao, policy(pol.criteria, facts=pol.declared_facts))
    row = by_id(result)["zone"]
    assert row.etat == "manquant" and "llm_list_may_omit: Marseille" in row.justification
    assert result.decision == "INCOMPLET" and "custom:zone" in result.scoring_missing


def test_a_note_chosen_by_the_user_for_an_absent_datum_is_a_visible_hypothesis_not_a_fact():
    pol = policy([crit("budget", "numeric_tiers", TIERS, 100, on_missing={"mode": "explicit_score", "score": 50})])
    result = score(AOContext(titre="AO"), pol)
    row = by_id(result)["budget"]
    assert row.etat == "hypothese" and row.score == 50.0 and "Hypothèse de la politique" in row.justification
    assert result.scoring_assumptions and "budget" in result.scoring_assumptions[0]
    assert result.decision != "INCOMPLET" and result.score_global == 50.0
    assert any("hypothèses" in r for r in result.recommandations)


def test_a_blocking_criterion_with_unknown_data_is_never_neutralized_by_an_explicit_note():
    spec = crit("zone", "list_coverage", {"fact_key": "zone", "pass_score": 100, "fail_score": 0}, 100, blocking=True,
                on_missing={"mode": "explicit_score", "score": 100})
    pol = policy([spec], facts={"zone": {"key": "zone", "label": "Zone", "type": "list", "unit": None, "value": ["Lyon"]}})
    result = score(AOContext(titre="AO"), pol)
    assert by_id(result)["zone"].etat == "manquant" and result.decision == "INCOMPLET"
    errors = criteria_catalogue.validate_criterion(spec, known_facts=pol.declared_facts, settings={})
    assert any("bloquant" in e and "hypothèse" in e for e in errors), "and validation refuses it before activation"


def test_not_applicable_is_neither_satisfied_nor_failed_nor_a_zero_weight():
    criteria = [crit("budget", "numeric_tiers", TIERS, 50, on_missing={"mode": "not_applicable"}),
                crit("capacite", "capacity_availability", {"available_score": 80, "unavailable_score": 0, "max_charge_blocking": None}, 50)]
    ao = AOContext(titre="AO")  # no budget: the budget criterion is not applicable for THIS analysis
    # no weighting rule was explicitly validated -> never redistributed, never a complete score
    incomplete = score(ao, policy(criteria))
    row = by_id(incomplete)["budget"]
    assert row.etat == "non_applicable" and row.score == 0.0 and row.motif == "budget_missing"
    assert incomplete.decision == "INCOMPLET" and "not_applicable:budget" in incomplete.scoring_missing
    assert incomplete.scoring_not_applicable == ["budget"] and incomplete.score_provisoire is True
    assert incomplete.score_global == 40.0, "the excluded criterion is not given 100 (nor its weight redistributed)"
    # only an explicitly validated rule lets a complete score be published: the applicable weights only
    renormalized = score(ao, policy(criteria, settings={"not_applicable_rule": "renormalize", "not_applicable_rule_confirmed": True}))
    assert renormalized.decision == "GO" and renormalized.score_global == 80.0
    assert renormalized.scoring_completeness == "complete" and renormalized.score_provisoire is False
    assert by_id(renormalized)["budget"].etat == "non_applicable"
    # an unconfirmed "renormalize" is not a validated rule
    unconfirmed = score(ao, policy(criteria, settings={"not_applicable_rule": "renormalize", "not_applicable_rule_confirmed": False}))
    assert unconfirmed.decision == "INCOMPLET"


def test_a_criterion_disabled_in_a_new_version_is_excluded_with_its_reason_and_needs_no_data():
    criteria = [crit("budget", "numeric_tiers", TIERS, 0, enabled=False, disabled_reason="Non pertinent pour notre activité"),
                crit("capacite", "capacity_availability", {"available_score": 70, "unavailable_score": 0, "max_charge_blocking": None}, 100)]
    result = score(AOContext(titre="AO"), policy(criteria))
    row = by_id(result)["budget"]
    assert row.etat == "non_applicable" and row.motif == "disabled_by_policy" and "Non pertinent" in row.justification
    assert result.decision == "GO" if 70 >= 80 else result.decision == "GO SOUS RESERVE"
    assert result.scoring_missing == [] and result.scoring_not_applicable == ["budget"]
    assert result.score_global == 70.0


def test_zero_and_false_are_real_values_never_absent():
    zero_tier = copy.deepcopy(TIERS)
    zero_tier["zero_score"] = 33
    assert by_id(score(AOContext(titre="AO", budget_estime=0.0), policy([crit("b", "numeric_tiers", zero_tier, 100)])))["b"].score == 33.0
    assert by_id(score(AOContext(titre="AO", budget_estime=0.0), policy([crit("b", "numeric_tiers", TIERS, 100)])))["b"].etat == "evalue"
    night = crit("nuit", "equality", {"fact_key": "nuit", "pass_score": 100, "fail_score": 0}, 100)
    facts = {"nuit": {"key": "nuit", "label": "Nuit", "type": "boolean", "unit": None, "value": False}}
    ao = AOContext(titre="AO", extracted_facts={"nuit": ExtractedFact(value=False, status="found", provenance="llm")})
    assert score(ao, policy([night], facts=facts)).score_global == 100.0
    assert score(AOContext(titre="AO", extracted_facts={"nuit": ExtractedFact(status="absent", provenance="absent")}),
                 policy([night], facts=facts)).decision == "INCOMPLET"


def test_a_zero_weight_never_disables_a_confirmed_blocker():
    criteria = [crit("charge", "capacity_availability", {"available_score": 100, "unavailable_score": 0, "max_charge_blocking": 30}, 0),
                crit("budget", "numeric_tiers", TIERS, 100)]
    result = score(AOContext(titre="AO", budget_estime=200000.0), policy(criteria))
    assert result.decision == "NO-GO" and any("Charge equipe superieure a 30%" in b for b in result.criteres_bloquants)
    assert by_id(result)["charge"].bloquant is True


def test_a_confirmed_blocker_gives_no_go_even_when_other_data_is_unknown():
    criteria = [crit("charge", "capacity_availability", {"available_score": 100, "unavailable_score": 0, "max_charge_blocking": 30}, 50),
                crit("budget", "numeric_tiers", TIERS, 50)]
    result = score(AOContext(titre="AO"), policy(criteria))  # budget unknown, but the load blocker is confirmed
    assert result.decision == "NO-GO" and result.scoring_completeness == "incomplete" and result.score_provisoire is True


def test_a_missing_or_invalid_weight_is_never_silently_zero_counted_as_evaluated():
    result = score(AOContext(titre="AO", budget_estime=1.0), policy([crit("b", "numeric_tiers", TIERS, None)]))
    assert by_id(result)["b"].etat == "manquant" and result.decision == "INCOMPLET"


def test_certifications_the_absence_of_extraction_is_not_a_proof_of_conformity():
    params = {"covered_score": 100, "missing_score": 0, "block_when_missing": True, "when_extraction_unknown": "missing"}
    unknown = AOContext(titre="AO", field_provenance={"certifications_obligatoires": "absent"})
    assert score(unknown, policy([crit("c", "certifications_required", params, 100)])).decision == "INCOMPLET"
    none_required = AOContext(titre="AO", field_provenance={"certifications_obligatoires": "llm"})
    assert score(none_required, policy([crit("c", "certifications_required", params, 100)])).score_global == 100.0
    # the historical migration keeps the historical treatment, visibly, as a parameter
    compat = dict(params, when_extraction_unknown="treat_as_none")
    assert score(unknown, policy([crit("c", "certifications_required", compat, 100)])).score_global == 100.0
    missing = AOContext(titre="AO", certifications_obligatoires=["SecNumCloud"])
    blocked = score(missing, policy([crit("c", "certifications_required", params, 100)], certs=["iso 27001"]))
    assert blocked.decision == "NO-GO" and "SecNumCloud" in blocked.criteres_bloquants[0]
    no_block = score(missing, policy([crit("c", "certifications_required", dict(params, block_when_missing=False), 100)]))
    assert no_block.decision != "NO-GO" or no_block.score_global < 55


def test_the_blocking_technologies_message_is_deterministic_for_new_results():
    ao = AOContext(titre="AO", technologies_demandees=["Cobol", "Fortran", "Pascal", "Ada", "Basic", "Algol"])
    result = score(ao, policy([crit("t", "technology_coverage", {"none_requested_score": None, "max_unmastered_blocking": 2}, 100)]))
    assert result.criteres_bloquants == ["Trop de technologies non maitrisees : ada, algol, basic, cobol, fortran"]


def test_no_hypothesis_of_the_historical_engine_survives_in_the_texts_of_a_new_result():
    pol = policy([crit("t", "technology_coverage", {"none_requested_score": 70, "max_unmastered_blocking": None}, 50),
                  crit("b", "numeric_tiers", TIERS, 50)], go=60, sr=30)
    result = score(AOContext(titre="AO", budget_estime=1000.0, deadline_reponse="2026-12-01"), pol)
    text = " ".join(result.recommandations + result.risques + result.forces + result.faiblesses + [c.justification for c in result.criteres])
    for forbidden in ("ESN", "48h", "48 h", "6-12 mois", "avant-vente", "rentab", "très limité", "tres limite", "positionnement IA"):
        assert forbidden not in text, forbidden
    assert result.forces == [] and result.faiblesses == [], "no 78/60 display thresholds unless the policy sets them"
    settings = {"strengths_at_least": 65, "weaknesses_below": 30}
    with_thresholds = score(AOContext(titre="AO", budget_estime=1000.0), policy(pol.criteria, go=60, sr=30, settings=settings))
    assert with_thresholds.forces == ["t"] and with_thresholds.faiblesses == ["b"]


# ---------------------------------------------------------------------------
# C — activation validation of a policy authored in the new format
# ---------------------------------------------------------------------------

class _Profile:
    raison_sociale = "Prestataire test"
    business_facts: dict = {}


def _errors(criteria, *, settings=None, go=80, sr=55, profile=_Profile()):
    return validate_criteria_policy(criteria=criteria, settings=settings if settings is not None else criteria_catalogue.default_settings(),
                                    threshold_go=go, threshold_sous_reserve=sr, profile=profile)


def test_an_empty_policy_cannot_be_activated_and_a_valid_one_needs_thresholds_and_a_100_point_budget():
    assert "criteria" in _errors([]) and any("au moins un critère" in e for e in _errors([])["criteria"])
    assert "thresholds" in _errors([crit("b", "numeric_tiers", TIERS, 100)], go=None, sr=None)
    assert any("exactement 100" in e for e in _errors([crit("b", "numeric_tiers", TIERS, 60)])["criteria"])
    assert _errors([crit("b", "numeric_tiers", TIERS, 100)]) == {}
    assert "profile" in _errors([crit("b", "numeric_tiers", TIERS, 100)], profile=None)


def test_criteria_validation_reports_the_precise_problem_and_never_promises_an_unavailable_calculation():
    errs = lambda c: " | ".join(_errors([c] if isinstance(c, dict) else c).get("criteria", []))  # noqa: E731
    assert "évaluateur non pris en charge" in errs(crit("x", "profitability_margin", {}, 100))
    assert "valeur requise" in errs(crit("b", "numeric_tiers", {**TIERS, "tiers": None}, 100))
    assert "même seuil" in errs(crit("b", "numeric_tiers", {**TIERS, "tiers": [{"at_least": 5, "score": 1}, {"at_least": 5, "score": 2}]}, 100))
    assert "entre 0 et 100" in errs(crit("b", "numeric_tiers", {**TIERS, "below_score": 250}, 100))
    assert "dupliqué" in errs([crit("a", "numeric_tiers", TIERS, 50), crit("a", "numeric_tiers", TIERS, 50)])
    assert "paramètre inconnu" in errs(crit("b", "numeric_tiers", {**TIERS, "formula": "x*2"}, 100))
    assert "pas de condition d'échec" in errs(crit("b", "numeric_tiers", TIERS, 100, blocking=True))
    assert "n'a pas de donnée d'AO absente" in errs(crit("r", "reference_evidence", {"base_score": 1, "per_reference": 1, "similarity_weight": 1}, 100,
                                                        on_missing={"mode": "explicit_score", "score": 10}))
    assert "motif" in errs(crit("b", "numeric_tiers", TIERS, 0, enabled=False))
    assert "poids de 0" in errs(crit("b", "numeric_tiers", TIERS, 20, enabled=False, disabled_reason="x"))
    unavailable = [item["label"] for item in criteria_catalogue.UNAVAILABLE]
    assert any("Rentabilité" in label for label in unavailable), "the form says what cannot be computed"
    assert "budget SEUL" in criteria_catalogue.EVALUATORS["numeric_tiers"]["description"], "a budget comparison says it compares the budget alone"
    assert "renormalize" in " ".join(_errors([crit("b", "numeric_tiers", TIERS, 100)],
                                             settings={"not_applicable_rule": "renormalize", "not_applicable_rule_confirmed": False}).get("settings", []))


def test_a_fact_criterion_is_validated_against_the_accounts_declared_facts():
    spec = crit("z", "list_coverage", {"fact_key": "zone", "pass_score": 100, "fail_score": 0}, 100)
    assert "inconnu ou non déclaré" in " ".join(_errors([spec])["criteria"])

    class _P(_Profile):
        business_facts = {"zone": {"key": "zone", "label": "Zone", "type": "number", "unit": "x", "value": 3}}

    assert "pas compatible" in " ".join(_errors([spec], profile=_P())["criteria"]) or "compatible" in " ".join(_errors([spec], profile=_P())["criteria"])


def test_the_only_template_is_a_draft_without_thresholds_and_without_the_keyword_rules():
    template = criteria_catalogue.proposed_templates()[0]
    assert template["settings"]["not_applicable_rule"] == "incomplete"
    assert not any(c["evaluator"].startswith("legacy_") for c in template["criteria"]), "no keyword-detection rule is proposed"
    assert all(c["on_missing"]["mode"] == "incomplete" for c in template["criteria"]), "no favorable note on unknown data is proposed"
    assert "threshold_go" not in template, "thresholds must be entered by the user"


# ---------------------------------------------------------------------------
# D — the Lyon / 500 EUR / 4 interventions / night forbidden scenario
# ---------------------------------------------------------------------------

def _lyon_facts(extraction=None):
    raw = extraction if extraction is not None else _lyon_normalized()
    return {k: ExtractedFact(**{kk: vv for kk, vv in v.items() if kk in ("value", "unit", "status", "provenance", "reason")}) for k, v in raw.items()}


LYON_DECLARED = {
    "zone_intervention": {"key": "zone_intervention", "label": "Sites", "type": "list", "unit": None, "value": ["Lyon"]},
    "frequence_nettoyage": {"key": "frequence_nettoyage", "label": "Fréquence", "type": "number", "unit": "par_semaine", "value": 3},
    "travail_de_nuit": {"key": "travail_de_nuit", "label": "Travail de nuit", "type": "boolean", "unit": None, "value": False},
}


def _lyon_criteria(*, freq_blocking):
    return [
        crit("zone", "list_coverage", {"fact_key": "zone_intervention", "pass_score": 100, "fail_score": 0}, 40, label="Sites couverts", blocking=True),
        crit("frequence", "numeric_threshold", {"fact_key": "frequence_nettoyage", "comparison": "provider_gte_ao", "pass_score": 100, "fail_score": 20},
             40, label="Fréquence compatible", blocking=freq_blocking),
        crit("nuit", "equality", {"fact_key": "travail_de_nuit", "pass_score": 100, "fail_score": 0}, 20, label="Travail de nuit cohérent"),
    ]


def _lyon_ao(extraction=None):
    return AOContext(titre="Prestations IT à Lyon", client="Client", budget_estime=500.0, texte_source="Prestations IT à Lyon. 4 fois par semaine.",
                     extracted_facts=_lyon_facts(extraction))


def test_lyon_scenario_non_blocking_frequency_is_computed_from_the_entered_policy_not_an_imposed_score():
    result = score(_lyon_ao(), policy(_lyon_criteria(freq_blocking=False), facts=LYON_DECLARED))
    assert result.score_global == 68.0 and result.decision == "GO SOUS RESERVE"  # (100*40 + 20*40 + 100*20)/100, thresholds 80/55
    assert result.score_global != 66.5, "66.5 belongs to the historical configuration, not to a new one"
    changed = score(_lyon_ao(), policy(_lyon_criteria(freq_blocking=False), facts=LYON_DECLARED, go=65, sr=40))
    assert changed.decision == "GO" and changed.score_global == 68.0, "the thresholds are the account's"


def test_lyon_scenario_an_insufficient_blocking_frequency_is_a_no_go():
    result = score(_lyon_ao(), policy(_lyon_criteria(freq_blocking=True), facts=LYON_DECLARED))
    assert result.decision == "NO-GO" and result.criteres_bloquants == ["Fréquence compatible : condition bloquante non satisfaite"]
    assert result.scoring_completeness == "complete"


def test_lyon_scenario_a_missing_datum_is_incomplete_unless_a_blocker_is_confirmed():
    without_frequency = {k: v for k, v in _lyon_normalized().items() if k != "frequence_nettoyage"}
    incomplete = score(_lyon_ao(without_frequency), policy(_lyon_criteria(freq_blocking=True), facts=LYON_DECLARED))
    assert incomplete.decision == "INCOMPLET" and "custom:frequence" in incomplete.scoring_missing and incomplete.score_provisoire is True
    # the frequency is fine, the ZONE (blocking) is confirmed insufficient while the night datum is unknown -> NO-GO anyway
    extraction = {**{k: v for k, v in _lyon_normalized().items() if k != "travail_de_nuit"},
                  "zone_intervention": {**_lyon_normalized()["zone_intervention"], "value": ["Lyon", "Marseille"]}}
    confirmed = score(_lyon_ao(extraction), policy(_lyon_criteria(freq_blocking=False), facts=LYON_DECLARED))
    assert confirmed.decision == "NO-GO" and any("Sites couverts" in b for b in confirmed.criteres_bloquants)
    assert confirmed.scoring_completeness == "incomplete" and "custom:nuit" in confirmed.scoring_missing


# ---------------------------------------------------------------------------
# E — one configuration resolver, and the whole HTTP path on a new policy
# ---------------------------------------------------------------------------

def _login(client, email):
    client.cookies.clear()
    page = client.get("/login")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    client.post("/login", data={"email": email, "password": "Sup3rSecret!", "next": "/app", "csrf_token": csrf})
    return csrf


def _put(client, csrf, path, body):
    return client.put(path, json=body, headers={"X-CSRF-Token": csrf})


def _capacity(client, csrf, charge=40):
    r = client.post("/api/capacity", json={"charge_globale_pct": charge, "nombre_projets_en_cours": 1, "projets_en_cours": ["P"],
                                           "capacites_par_pole": {"Pôle": 100 - charge}}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200, r.text


AO_TEXT = ("Appel d'offres - Prestations de nettoyage. Acheteur : Collectivité Exemple. Site de Lyon, 4 fois par semaine. "
           "Budget : 120 000 €. Le travail de nuit n'est pas requis.")
NEW_CRITERIA = [
    {"id": "sites", "label": "Sites couverts", "evaluator": "list_coverage",
     "params": {"fact_key": "zone_intervention", "pass_score": 100, "fail_score": 0}, "weight": 50, "blocking": True,
     "on_missing": {"mode": "incomplete"}, "enabled": True, "disabled_reason": None},
    {"id": "budget", "label": "Budget estimé (comparaison au seul budget)", "evaluator": "numeric_tiers", "params": TIERS, "weight": 50,
     "blocking": False, "on_missing": {"mode": "incomplete"}, "enabled": True, "disabled_reason": None},
]
NEW_PROFILE = {"raison_sociale": "Nettoyage Pro (lot 44)", "competences": [], "certifications": [],
               "business_facts": {"zone_intervention": LYON_DECLARED["zone_intervention"]}}


def test_a_new_policy_starts_empty_and_cannot_be_activated_until_the_user_provides_everything(client, db):
    make_active_starter_user(db, "l44-empty@example.com", scoring=False)
    csrf = _login(client, "l44-empty@example.com")
    state = client.get("/api/scoring-config").json()
    assert state["policy"]["active"] is None and state["policy"]["draft"] is None
    assert "modele_informatique" in [t["id"] for t in state["criteria_templates"]]
    assert "numeric_tiers" in state["criteria_catalogue"]["evaluators"] and state["criteria_catalogue"]["unavailable"]
    saved = _put(client, csrf, "/api/scoring-config/policy", {"criteria": []})
    assert saved.status_code == 200 and saved.json()["criteria"] == [] and saved.json()["origin"] == "user"
    assert saved.json()["threshold_go"] is None and saved.json()["weights"] == {}
    _put(client, csrf, "/api/scoring-config/profile", NEW_PROFILE)
    validation = client.post("/api/scoring-config/policy/validate", headers={"X-CSRF-Token": csrf}).json()
    assert validation["valid"] is False and {"criteria", "thresholds"} <= set(validation["errors"])
    refused = client.post("/api/scoring-config/policy/activate", json={"expected_active_version": None}, headers={"X-CSRF-Token": csrf})
    assert refused.status_code == 422 and client.get("/api/scoring-config").json()["policy"]["active"] is None


def test_new_format_draft_conflicts_with_a_legacy_shaped_save_instead_of_being_overwritten(client, db):
    make_active_starter_user(db, "l44-conflict@example.com", scoring=False)
    csrf = _login(client, "l44-conflict@example.com")
    _put(client, csrf, "/api/scoring-config/policy", {"criteria": NEW_CRITERIA, "threshold_go": 80, "threshold_sous_reserve": 55})
    r = _put(client, csrf, "/api/scoring-config/policy", {"weights": {"Adequation expertise": 100}, "threshold_go": 80, "threshold_sous_reserve": 55})
    assert r.status_code == 409 and r.json()["detail"]["error_code"] == "DRAFT_FORMAT_CONFLICT"
    assert client.get("/api/scoring-config").json()["policy"]["draft"]["criteria"] == NEW_CRITERIA


def test_settings_simulation_activation_analysis_result_history_documents_on_a_new_policy(client, db, monkeypatch):
    from src.rag import private_rag_manager
    monkeypatch.setattr(private_rag_manager, "search", lambda db, **kw: [])
    make_active_starter_user(db, "l44-flow@example.com", scoring=False)
    csrf = _login(client, "l44-flow@example.com")
    _capacity(client, csrf)
    assert _put(client, csrf, "/api/scoring-config/profile", NEW_PROFILE).status_code == 200
    draft = _put(client, csrf, "/api/scoring-config/policy", {"criteria": NEW_CRITERIA, "threshold_go": 80, "threshold_sous_reserve": 55})
    assert draft.status_code == 200 and draft.json()["criteria_version"] == 1 and draft.json()["origin_label"] == "Nouvelle politique"
    assert client.post("/api/scoring-config/policy/validate", headers={"X-CSRF-Token": csrf}).json() == {"valid": True, "errors": {}}
    simulation = client.post("/api/scoring-config/simulate", data={"mode": "paste", "text": AO_TEXT}, headers={"X-CSRF-Token": csrf})
    assert simulation.status_code == 200, simulation.text
    body = simulation.json()
    assert body["simulation"] is True and body["criteria_version"] == 1 and body["policy_origin"] == "user"
    assert {c["critere_id"] for c in body["criteres"]} == {"sites", "budget"}
    assert client.get("/api/history").json()["total"] == 0
    assert client.post("/api/scoring-config/policy/activate", json={"expected_active_version": None},
                       headers={"X-CSRF-Token": csrf}).status_code == 200

    response = client.post("/api/analyze", data={"mode": "paste", "text": AO_TEXT}, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200, response.text
    job_id = response.json()["job_id"]
    for _ in range(300):
        job = jobs.get_job(job_id)
        if job.status != "running":
            break
        time.sleep(0.1)
    assert job.status == "done", (job.error, job.error_code)
    result = job.result
    assert {c.critere_id for c in result.criteres} == {"sites", "budget"} and result.criteria_version == 1
    assert job.scoring_policy_version == 1
    assert client.get(f"/app/resultats/{job_id}").status_code == 200
    assert client.get("/api/history").json()["items"][0]["job_id"] == job_id
    assert client.get(f"/api/download/{job_id}/pdf").content[:4] == b"%PDF"
    assert client.get(f"/api/download/{job_id}/docx").content[:2] == b"PK"


def test_analysis_and_simulation_resolve_the_configuration_through_the_same_function(client, db, monkeypatch):
    import src.web.scoring_context as scoring_context
    from src.rag import private_rag_manager
    monkeypatch.setattr(private_rag_manager, "search", lambda db, **kw: [])
    calls = []
    real = scoring_context.resolve_scoring_context

    def spy(db_, **kwargs):
        calls.append((kwargs["source"], kwargs["organization_id"], kwargs["owner_user_id"]))
        return real(db_, **kwargs)

    monkeypatch.setattr(scoring_context, "resolve_scoring_context", spy)
    user = make_active_starter_user(db, "l44-same@example.com", scoring=False)
    csrf = _login(client, "l44-same@example.com")
    _capacity(client, csrf)
    _put(client, csrf, "/api/scoring-config/profile", NEW_PROFILE)
    _put(client, csrf, "/api/scoring-config/policy", {"criteria": NEW_CRITERIA, "threshold_go": 80, "threshold_sous_reserve": 55})
    assert client.post("/api/scoring-config/simulate", data={"mode": "paste", "text": AO_TEXT}, headers={"X-CSRF-Token": csrf}).status_code == 200
    client.post("/api/scoring-config/policy/activate", json={"expected_active_version": None}, headers={"X-CSRF-Token": csrf})
    job_id = client.post("/api/analyze", data={"mode": "paste", "text": AO_TEXT}, headers={"X-CSRF-Token": csrf}).json()["job_id"]
    for _ in range(300):
        if jobs.get_job(job_id).status != "running":
            break
        time.sleep(0.1)
    assert [c[0] for c in calls] == ["draft", "active"], "simulation reads the draft, the analysis the active policy"
    assert {(c[1], c[2]) for c in calls} == {(calls[0][1], user.id)}, "both for the same organization + owner"


def _historical_result_data(job_id: str) -> dict:
    """A result_data exactly as a pre-lot-44 analysis stored it: no state, evaluator,
    criterion id, criteria version, provisional flag or assumptions."""
    from tests.synthetic_scoring import score_with_synthetic_policy

    ao = AOContext(titre="AO historique", client="Client H", secteur="Public", budget_estime=250000.0,
                   technologies_demandees=["Python", "Django"], texte_source="Développement d'une plateforme.")
    result = score_with_synthetic_policy(ao, CompanyProfile(secteur="Public", solidite_financiere="Bonne"), [], CAPACITY)
    dumped = result.model_dump()
    for row in dumped["criteres"]:
        for key in ("etat", "evaluateur", "critere_id", "bloquant", "motif"):
            row.pop(key)
    for key in ("criteria_version", "policy_origin", "score_provisoire", "scoring_assumptions", "scoring_not_applicable"):
        dumped.pop(key)
    return {"id": job_id, "created_at": 1.0, "source_label": "Texte collé", "ao": ao.model_dump(), "result": dumped, "files": {},
            "scoring_policy_version": 1}


def test_a_result_stored_before_lot_44_is_still_read_and_displayed_unchanged(client, db):
    """Historical analyses are never rewritten: the reader handles the old shape
    (twelve rows, no state), the page renders it, and reading changes nothing."""
    from src.web.database.models import Analysis
    from src.web.database.repositories import analyses as analyses_repo
    from tests.conftest import default_org_id

    user = make_active_starter_user(db, "l44-history@example.com")
    org = default_org_id(db, user)
    stored = _historical_result_data("hist-44")
    analyses_repo.create_analysis(db, user_id=user.id, organization_id=org, result_data=stored, job_id="hist-44", title="AO historique",
                                  client_name="Client H", sector="Public", score=stored["result"]["score_global"],
                                  decision=stored["result"]["decision"], budget="250000", technologies=["python"], summary_data=None)
    db.commit()
    before = json.dumps(db.query(Analysis).filter_by(job_id="hist-44").one().result_data, sort_keys=True)
    _login(client, "l44-history@example.com")

    job = jobs.get_job("hist-44")
    assert job is not None and job.result is not None and len(job.result.criteres) == 12
    assert all(c.etat is None and c.critere_id is None for c in job.result.criteres), "no state is fabricated for an old result"
    assert job.result.criteria_version is None and job.result.score_provisoire is None
    page = client.get("/app/resultats/hist-44")
    assert page.status_code == 200
    for name in ("Adéquation expertise", "Rentabilité estimée", "Valeur stratégique"):
        assert name in page.text
    assert "data-criterion-state" not in page.text and "Score provisoire" not in page.text
    db.expire_all()
    assert json.dumps(db.query(Analysis).filter_by(job_id="hist-44").one().result_data, sort_keys=True) == before
