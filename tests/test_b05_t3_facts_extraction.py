"""B05-T3 — AOExtractor's additive, sector-neutral business-fact
extraction (`requested_facts` parameter). Two synthetic, explicitly-non-IT
scenarios (cleaning: zone d'intervention + fréquence de nettoyage; BTP:
matériel disponible + capacité de chantier), plus the LLM-disabled/no-op
regression proving every pre-existing caller (extract_ao with no third
argument) is byte-for-byte unaffected.
"""
from __future__ import annotations

import json

from src.agents.ao_extractor import AOExtractor, _extract_fact_locally, _validate_extracted_fact_value
from src.core.models import ExtractedFact

ZONE_SPEC = {"label": "Zone d'intervention", "type": "list", "unit": None, "recognition_vocabulary": ["Lyon", "Villeurbanne"]}
FREQUENCE_SPEC = {"label": "Fréquence de nettoyage", "type": "number", "unit": "par_semaine"}
CAPACITE_SPEC = {"label": "Capacité de chantier", "type": "number", "unit": "equipes"}


# --- _extract_fact_locally (deterministic, no-LLM fallback) ---------------

def test_local_fallback_finds_a_known_list_value_mentioned_in_the_ao():
    text = "Le prestataire devra intervenir sur le site de Lyon à partir de janvier."
    fact = _extract_fact_locally(text, ZONE_SPEC)
    assert fact.status == "found"
    assert fact.value == ["Lyon"]
    assert fact.provenance == "fallback"


def test_local_fallback_absent_when_no_known_value_mentioned():
    text = "Le prestataire devra intervenir sur le site de Marseille."
    fact = _extract_fact_locally(text, ZONE_SPEC)
    assert fact.status == "absent"
    assert fact.value is None


def test_local_fallback_finds_a_number_near_its_unit():
    text = "Le nettoyage devra être réalisé 3 fois par semaine dans les locaux."
    fact = _extract_fact_locally(text, FREQUENCE_SPEC)
    assert fact.status == "found"
    assert fact.value == 3.0
    assert fact.unit == "par_semaine"


def test_local_fallback_absent_when_no_number_found():
    fact = _extract_fact_locally("Aucune fréquence n'est précisée dans ce document.", FREQUENCE_SPEC)
    assert fact.status == "absent"
    assert fact.value is None


def test_local_fallback_never_fabricates_for_text_type():
    fact = _extract_fact_locally("Texte quelconque.", {"label": "Note libre", "type": "text"})
    assert fact.status == "absent"


# --- _validate_extracted_fact_value ----------------------------------------

def test_validate_number_rejects_bool_and_nan():
    assert _validate_extracted_fact_value(True, "number") == (None, False)
    assert _validate_extracted_fact_value(float("nan"), "number")[1] is False
    assert _validate_extracted_fact_value(4, "number") == (4.0, True)


def test_validate_list_rejects_non_string_items():
    assert _validate_extracted_fact_value(["Lyon", 5], "list") == (None, False)
    assert _validate_extracted_fact_value(["Lyon", "Villeurbanne"], "list") == (["Lyon", "Villeurbanne"], True)


def test_validate_boolean_rejects_non_bool():
    assert _validate_extracted_fact_value("true", "boolean") == (None, False)
    assert _validate_extracted_fact_value(True, "boolean") == (True, True)


# --- AOExtractor.extract — backward compatibility --------------------------

def test_extract_with_no_requested_facts_is_unaffected(monkeypatch):
    """Ticket: every pre-existing caller (src/core/analysis_service.py::
    extract_ao) never passes a third argument — this must remain
    byte-for-byte identical."""
    from src.core import config
    monkeypatch.setattr(config, "LLM_ENABLED", False)
    ao = AOExtractor().extract("Marché de nettoyage de bureaux à Lyon.")
    assert ao.extracted_facts == {}


def test_extract_with_llm_disabled_uses_local_fallback_for_requested_facts(monkeypatch):
    from src.core import config
    monkeypatch.setattr(config, "LLM_ENABLED", False)
    text = "Marché de nettoyage. Intervention sur le site de Lyon, 3 fois par semaine."
    ao = AOExtractor().extract(text, requested_facts={
        "zone_intervention": ZONE_SPEC, "frequence_nettoyage": FREQUENCE_SPEC,
    })
    assert ao.extracted_facts["zone_intervention"].status == "found"
    assert ao.extracted_facts["zone_intervention"].value == ["Lyon"]
    assert ao.extracted_facts["frequence_nettoyage"].status == "found"
    assert ao.extracted_facts["frequence_nettoyage"].value == 3.0
    assert ao.extracted_facts["frequence_nettoyage"].provenance == "fallback"


def test_extract_with_llm_disabled_and_absent_fact_is_honestly_absent(monkeypatch):
    from src.core import config
    monkeypatch.setattr(config, "LLM_ENABLED", False)
    text = "Marché de nettoyage sans plus de précision."
    ao = AOExtractor().extract(text, requested_facts={"capacite_chantier": CAPACITE_SPEC})
    assert ao.extracted_facts["capacite_chantier"].status == "absent"
    assert ao.extracted_facts["capacite_chantier"].value is None


# --- AOExtractor.extract — LLM path (a real provider, no real network) ----

class _SequencedFakeProvider:
    """Returns a DIFFERENT canned response per call index — call 1 is the
    primary extraction, call 2 is the additive facts call
    (_extract_facts_via_llm) — so both can be exercised together in one
    extract() invocation, unlike the single-fixed-response fake used by
    tests/test_ao_extraction_field_resolution.py."""
    name = "primary"
    enabled = True

    def __init__(self, responses: list):
        self._responses = responses
        self.calls = 0

    def complete(self, prompt, system=None, temperature=None, max_tokens=None):
        response = self._responses[self.calls]
        self.calls += 1
        return json.dumps(response) if not isinstance(response, str) else response


PRIMARY_RESPONSE = {
    "titre": "Nettoyage bureaux", "client": "Client Synthétique", "secteur": "Services",
    "budget_estime": 80000.0, "deadline_reponse": None, "duree_projet_mois": 12,
    "technologies_demandees": [], "competences_requises": [], "questions_client": [],
    "livrables": [], "contraintes": [], "certifications_obligatoires": [],
}


def _extractor_with_sequence(monkeypatch, responses: list):
    import src.agents.ao_extractor as ao_extractor_module
    from src.agents.llm_client import LLMClient
    from src.core import config

    provider = _SequencedFakeProvider(responses)
    monkeypatch.setattr(config, "LLM_ENABLED", True)
    monkeypatch.setattr(ao_extractor_module, "ClaudeClient", lambda: LLMClient([provider]))
    return AOExtractor(), provider


def test_extract_via_llm_populates_requested_facts_found(monkeypatch):
    facts_response = {"zone_intervention": {"value": ["Lyon"], "unit": None, "found": True}}
    extractor, provider = _extractor_with_sequence(monkeypatch, [PRIMARY_RESPONSE, facts_response])
    # Lot 43: the model's answer must be consistent with the document — a
    # value the text never states ("Lyon" from "Texte de l'AO.") is reported
    # as ambiguous by the omission control (tests/test_lot43_cleanup.py), so
    # this fixture text now actually mentions the site.
    ao = extractor.extract("Intervention sur le site de Lyon.", requested_facts={"zone_intervention": ZONE_SPEC})
    assert provider.calls == 2, "primary extraction and facts extraction must be two separate, isolated calls"
    assert ao.extracted_facts["zone_intervention"].status == "found"
    assert ao.extracted_facts["zone_intervention"].value == ["Lyon"]
    assert ao.extracted_facts["zone_intervention"].provenance == "llm"
    # The primary extraction fields are completely unaffected by the
    # additive facts call.
    assert ao.titre == "Nettoyage bureaux"
    assert ao.budget_estime == 80000.0


def test_extract_via_llm_found_false_falls_back_locally(monkeypatch):
    """The model honestly reports found=false — the extractor still tries
    its OWN deterministic local fallback before giving up (never treats an
    LLM 'not found' as necessarily final if a cheap local check can find
    it — e.g. the provider's own declared vocabulary appearing verbatim in
    the text)."""
    facts_response = {"zone_intervention": {"value": None, "unit": None, "found": False}}
    extractor, provider = _extractor_with_sequence(monkeypatch, [PRIMARY_RESPONSE, facts_response])
    ao = extractor.extract("Intervention sur le site de Lyon.", requested_facts={"zone_intervention": ZONE_SPEC})
    assert ao.extracted_facts["zone_intervention"].status == "found"
    assert ao.extracted_facts["zone_intervention"].provenance == "fallback"


def test_extract_via_llm_reports_the_ao_own_unit_never_the_requested_one(monkeypatch):
    """Reviewer-caught regression: the extractor used to stamp every
    LLM-found value with the REQUESTED unit regardless of what the AO
    actually said, making a genuine unit mismatch undetectable downstream
    (evaluate_custom_criterion's unit check always saw two equal units by
    construction). The AO here expresses the value in 'par_mois', not the
    requested 'par_semaine' — the extracted fact must carry 'par_mois'."""
    facts_response = {"frequence_nettoyage": {"value": 2, "unit": "par_mois", "found": True}}
    extractor, provider = _extractor_with_sequence(monkeypatch, [PRIMARY_RESPONSE, facts_response])
    ao = extractor.extract("Nettoyage deux fois par mois.", requested_facts={"frequence_nettoyage": FREQUENCE_SPEC})
    fact = ao.extracted_facts["frequence_nettoyage"]
    assert fact.status == "found"
    assert fact.unit == "par_mois", "the AO's own reported unit must survive, never silently replaced by the requested one"

    from src.agents.business_facts import evaluate_custom_criterion
    score, passed, reason = evaluate_custom_criterion(
        {
            "id": "frequence_ok", "operator": "numeric_threshold", "comparison": "provider_gte_ao",
            "pass_score": 100, "fail_score": 30,
        },
        ao_fact=fact, provider_value=3, provider_unit="par_semaine",
    )
    assert score is None
    assert reason == "unit_mismatch", "a real unit mismatch must actually be detectable, never silently equalized"


def test_extract_via_llm_type_mismatch_is_rejected_and_falls_back_locally(monkeypatch):
    """The model claims found=true but sends a value of the wrong type —
    never trusted as-is (ticket: 'jamais un résultat favorable fabriqué')."""
    facts_response = {"frequence_nettoyage": {"value": "trois fois", "unit": "par_semaine", "found": True}}
    extractor, provider = _extractor_with_sequence(monkeypatch, [PRIMARY_RESPONSE, facts_response])
    ao = extractor.extract("Nettoyage 3 fois par semaine.", requested_facts={"frequence_nettoyage": FREQUENCE_SPEC})
    assert ao.extracted_facts["frequence_nettoyage"].status == "found"
    assert ao.extracted_facts["frequence_nettoyage"].provenance == "fallback"
    assert ao.extracted_facts["frequence_nettoyage"].value == 3.0


def test_extract_via_llm_provider_failure_falls_back_locally_for_every_fact(monkeypatch):
    """A provider exception on the SECOND (facts) call must never break
    the primary extraction result, and must fall back locally — the two
    calls are fully isolated."""
    import src.agents.ao_extractor as ao_extractor_module
    from src.agents.llm_client import LLMClient
    from src.core import config

    class _FailingSecondCall:
        name = "primary"
        enabled = True

        def __init__(self):
            self.calls = 0

        def complete(self, prompt, system=None, temperature=None, max_tokens=None):
            self.calls += 1
            if self.calls == 1:
                return json.dumps(PRIMARY_RESPONSE)
            raise RuntimeError("simulated provider failure — never a real network call")

    provider = _FailingSecondCall()
    monkeypatch.setattr(config, "LLM_ENABLED", True)
    monkeypatch.setattr(ao_extractor_module, "ClaudeClient", lambda: LLMClient([provider]))

    ao = AOExtractor().extract("Intervention sur le site de Lyon.", requested_facts={"zone_intervention": ZONE_SPEC})
    assert ao.titre == "Nettoyage bureaux", "the primary extraction must survive a failure in the separate facts call"
    assert ao.extracted_facts["zone_intervention"].status == "found"
    assert ao.extracted_facts["zone_intervention"].provenance == "fallback"
