"""Lot 42 — regression built on the response REALLY returned by claude-sonnet-4-6
for the synthetic cleaning scenario (one recorded provider send, replayed here
through a fake provider: no network, no credential).

Confirmed defect: the extraction prompt asks the model to report a unit AS
WRITTEN in the document. The model answered "par semaine" for an account that
declared the slug "par_semaine"; the strict string comparison then made the
frequency criterion incalculable (INCOMPLET on a criterion that was in fact
resolvable). The fix normalizes the SPELLING of one unit — it never converts.
"""
from __future__ import annotations

import json
from pathlib import Path

from src.agents import business_facts
from src.agents.scoring_engine import ScoringEngine, ScoringPolicySnapshot
from src.core.models import AOContext, CapacityResult, CompanyProfile, ExtractedFact

FIXTURES = Path(__file__).parent / "fixtures"
SCENARIO = json.loads((FIXTURES / "lot42_scenario.json").read_text(encoding="utf-8"))
RECORDED_RESPONSE = (FIXTURES / "lot42_sonnet46_facts_response.txt").read_text(encoding="utf-8")


class _ReplayProvider:
    """Returns the recorded raw text; counts sends (must be exactly one)."""
    name = "recorded"
    enabled = True

    def __init__(self):
        self.sends = 0

    def complete(self, prompt, system, temperature, max_tokens):
        self.sends += 1
        return RECORDED_RESPONSE


def _extract_from_recording(monkeypatch):
    import src.agents.ao_extractor as extractor_module
    from src.agents.llm_client import LLMClient
    from src.core import config

    provider = _ReplayProvider()
    monkeypatch.setattr(config, "LLM_ENABLED", True)
    monkeypatch.setattr(extractor_module, "ClaudeClient", lambda: LLMClient([provider]))
    requested = business_facts.requested_facts_from_criteria(
        SCENARIO["custom_criteria"], known_facts=SCENARIO["provider_business_facts"])
    extracted = extractor_module.AOExtractor()._resolve_requested_facts(SCENARIO["ao_text"], requested, allow_llm=True)
    return extracted, provider


def _score(extracted):
    rules = SCENARIO["business_rules"]
    snapshot = ScoringPolicySnapshot.from_legacy(
        weights=dict(SCENARIO["fixed_weights"]), threshold_go=SCENARIO["threshold_go"],
        threshold_sous_reserve=SCENARIO["threshold_sous_reserve"], budget_minimum_eur=rules["budget_minimum_eur"],
        max_charge_pct=rules["max_charge_pct"], max_unmastered_technologies=rules["max_unmastered_technologies"],
        certification_penalty_score=rules["certification_penalty_score"],
        custom_criteria=SCENARIO["custom_criteria"], declared_facts=SCENARIO["provider_business_facts"])
    ao = AOContext(titre="Nettoyage (synthétique)", client="Client", budget_estime=120000.0,
                   texte_source=SCENARIO["ao_text"], extracted_facts=extracted)
    return ScoringEngine().score(ao, CompanyProfile(), [], CapacityResult(**SCENARIO["capacity"]), policy=snapshot)


def test_the_recorded_model_response_parses_through_the_real_extractor(monkeypatch):
    extracted, provider = _extract_from_recording(monkeypatch)
    assert provider.sends == 1, "one provider send only (no JSON retry on a valid, fenced response)"
    assert {k: v.provenance for k, v in extracted.items()} == {k: "llm" for k in extracted}
    assert sorted(v.casefold() for v in extracted["zone_intervention"].value) == ["lyon", "marseille"]
    assert extracted["frequence_nettoyage"].value == 4.0
    assert extracted["frequence_nettoyage"].unit == "par semaine", "the unit as the model wrote it"
    assert extracted["travail_de_nuit"].value is False


def test_recorded_extraction_scores_every_criterion_and_keeps_the_blocker(monkeypatch):
    extracted, _ = _extract_from_recording(monkeypatch)
    result = _score(extracted)
    by_name = {c.nom: c for c in result.criteres}
    assert result.decision == "NO-GO"
    assert any("Sites couverts" in b for b in result.criteres_bloquants)
    assert by_name["Sites couverts"].score == 0
    assert by_name["Fréquence compatible"].score == 20, "4 required > 3 declared: evaluated as failed, not incomplete"
    assert by_name["Travail de nuit cohérent"].score == 100
    assert result.scoring_missing == [] and result.scoring_completeness == "complete"


def test_unit_spelling_variants_match_but_different_units_never_do():
    threshold = {"id": "f", "operator": "numeric_threshold", "comparison": "provider_gte_ao", "pass_score": 100, "fail_score": 0}
    for written in ("par semaine", "Par  Semaine", "par-semaine", "PAR_SEMAINE", "par_semaine"):
        fact = ExtractedFact(value=2, unit=written, status="found")
        assert business_facts.evaluate_custom_criterion(threshold, ao_fact=fact, provider_value=3, provider_unit="par_semaine")[0] == 100.0
    for other in ("par mois", "par jour", "heures", None):
        fact = ExtractedFact(value=2, unit=other, status="found")
        assert business_facts.evaluate_custom_criterion(threshold, ao_fact=fact, provider_value=3, provider_unit="par_semaine")[0] is None, other
    accented = ExtractedFact(value=2, unit="Équipes", status="found")
    assert business_facts.evaluate_custom_criterion(threshold, ao_fact=accented, provider_value=3, provider_unit="equipes")[0] == 100.0
