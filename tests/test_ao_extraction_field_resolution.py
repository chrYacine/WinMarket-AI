"""B05-T2 (DEFECT confirmed): the extraction prompt always allowed a null
`deadline_reponse`, but `AOContext.deadline_reponse` was `str = ""` — a
null response made the WHOLE `AOContext(**data)` construction raise,
discarding every OTHER already-valid field and forcing the entire local
regex fallback. src/agents/ao_extractor.py now resolves each field
independently; this file proves that per-field contract end to end.

Test groups, per the ticket:
A. A synthetic response with a null deadline and otherwise-valid facts —
   those facts survive, including after persistence/reread.
B. Mixed/malformed response shapes — a valid field survives, an invalid
   one is flagged, the provider's own response dict is never mutated;
   budget 0 vs. absent are distinguished; NaN/bool are rejected.
C. Provider exception and disabled LLM, with a known AO text — a
   controlled fallback with correct provenance, zero calls when disabled;
   a real job consumer accepts an AO with no deadline at all.
"""
from __future__ import annotations

import json
import math
import re
import time

import pytest

from src.agents.ao_extractor import AOExtractor, _validate_float, _validate_int_like
from src.core.models import AOContext
from tests.conftest import make_active_starter_user

SAMPLE_TEXT = (
    "Appel d'offres - Portail client\n"
    "Acheteur : Collectivite Exemple\n"
    "Budget : 300 000 euros.\n"
    "Duree : 12 mois.\n"
    "ISO 27001 est obligatoire.\n"
)


# ---------------------------------------------------------------------------
# Direct-call helpers used only by Group B's precision tests — the module
# is small enough that testing _build_ao directly (rather than only
# through the LLM facade) gives exact field-provenance assertions without
# depending on json_complete's own retry/parsing internals.
# ---------------------------------------------------------------------------

def _build(data: dict | None, text: str = SAMPLE_TEXT) -> AOContext:
    return AOExtractor.__new__(AOExtractor)._build_ao(text, data=data, status=None, reason=None) if data is not None \
        else AOExtractor.__new__(AOExtractor)._build_ao(text, data=None, status="fallback_local", reason="no_content")


class _FakeProvider:
    name = "primary"
    enabled = True

    def __init__(self, response, raises: Exception | None = None):
        self._response = response
        self._raises = raises
        self.calls = 0

    def complete(self, prompt, system=None, temperature=None, max_tokens=None):
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return json.dumps(self._response) if not isinstance(self._response, str) else self._response


def _extractor_with(monkeypatch, response=None, raises=None, enabled=True):
    import src.agents.ao_extractor as ao_extractor_module
    from src.core import config
    from src.agents.llm_client import LLMClient

    provider = _FakeProvider(response, raises=raises)
    monkeypatch.setattr(config, "LLM_ENABLED", enabled)
    monkeypatch.setattr(ao_extractor_module, "ClaudeClient", lambda: LLMClient([provider]))
    return AOExtractor(), provider


VALID_RESPONSE = {
    "titre": "Portail client", "client": "Collectivite Exemple", "secteur": "Collectivité/Public",
    "budget_estime": 300000.0, "deadline_reponse": None, "duree_projet_mois": 12,
    "technologies_demandees": ["Python"], "competences_requises": ["Gestion de projet Agile"],
    "questions_client": [], "livrables": [], "contraintes": [],
    "certifications_obligatoires": ["ISO 27001"],
}


# ---------------------------------------------------------------------------
# Group A — a null deadline never destroys the rest of a valid response,
# including through real persistence/reread.
# ---------------------------------------------------------------------------

def test_null_deadline_does_not_discard_other_valid_facts(monkeypatch):
    extractor, provider = _extractor_with(monkeypatch, response=VALID_RESPONSE)
    ao = extractor.extract(SAMPLE_TEXT)

    assert provider.calls == 1
    assert ao.deadline_reponse is None
    assert ao.client == "Collectivite Exemple"
    assert ao.budget_estime == 300000.0
    assert ao.duree_projet_mois == 12
    assert ao.certifications_obligatoires == ["ISO 27001"]
    assert ao.field_provenance["deadline_reponse"] == "absent"
    assert ao.field_provenance["client"] == "llm"
    assert ao.field_provenance["budget_estime"] == "llm"
    assert ao.field_provenance["duree_projet_mois"] == "llm"
    assert ao.field_provenance["certifications_obligatoires"] == "llm"


VALID_WEIGHTS = {
    "Adequation expertise": 20, "References similaires": 15, "Disponibilite equipe": 10,
    "Rentabilite estimee": 10, "Faisabilite delai": 10, "Certifications requises": 10,
    "Complexite technique": 5, "Connaissance secteur": 5, "Potentiel commercial": 5,
    "Risque contractuel": 5, "Solidite client": 3, "Valeur strategique": 2,
}


def _csrf_from(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


def _login(client, email, password="Sup3rSecret!"):
    r = client.get("/login")
    csrf = _csrf_from(r.text)
    client.post("/login", data={"email": email, "password": password, "next": "/app", "csrf_token": csrf})
    return csrf


def _configure_capacity(client, csrf, charge: int = 40):
    return client.post("/api/capacity", json={
        "charge_globale_pct": charge, "nombre_projets_en_cours": 1,
        "projets_en_cours": ["Projet test"], "capacites_par_pole": {"Software Engineering": 40},
    }, headers={"X-CSRF-Token": csrf})


def _save_profile(client, csrf):
    return client.put("/api/scoring-config/profile", json={
        "raison_sociale": "ESN de test", "effectif": "10-50", "competences": ["python", "django"], "certifications": [],
    }, headers={"X-CSRF-Token": csrf})


def _save_draft(client, csrf):
    return client.put("/api/scoring-config/policy", json={
        "weights": dict(VALID_WEIGHTS), "threshold_go": 88, "threshold_sous_reserve": 60,
    }, headers={"X-CSRF-Token": csrf})


def _activate(client, csrf):
    return client.post("/api/scoring-config/policy/activate", json={"expected_active_version": None}, headers={"X-CSRF-Token": csrf})


def _configure_account(client, db, email):
    make_active_starter_user(db, email, scoring=False)
    csrf = _login(client, email)
    _configure_capacity(client, csrf)
    _save_profile(client, csrf)
    _save_draft(client, csrf)
    _activate(client, csrf)
    return csrf


def _run_analysis_to_completion(client, csrf, text: str):
    r = client.post("/api/analyze", data={"mode": "paste", "text": text}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    from src.web import jobs
    for _ in range(50):
        job = jobs.get_job(job_id)
        if job.status != "running":
            break
        time.sleep(0.1)
    assert job.status == "done", job.error
    return job


def _install_fake_extraction(monkeypatch, response=None, raises=None, enabled=True):
    import src.agents.ao_extractor as ao_extractor_module
    from src.core import config
    from src.agents.llm_client import LLMClient

    provider = _FakeProvider(response, raises=raises)
    monkeypatch.setattr(config, "LLM_ENABLED", enabled)
    monkeypatch.setattr(ao_extractor_module, "ClaudeClient", lambda: LLMClient([provider]))
    return provider


def _install_no_op_search(monkeypatch):
    from src.rag import private_rag_manager
    monkeypatch.setattr(private_rag_manager, "search", lambda db, **kw: [])


def test_null_deadline_survives_persistence_and_reread_through_a_real_job(client, db, monkeypatch):
    _install_no_op_search(monkeypatch)
    _install_fake_extraction(monkeypatch, response=VALID_RESPONSE)
    csrf = _configure_account(client, db, "extractnulldeadline@example.com")
    job = _run_analysis_to_completion(client, csrf, SAMPLE_TEXT)

    assert job.ao.deadline_reponse is None
    assert job.ao.client == "Collectivite Exemple"
    assert job.ao.budget_estime == 300000.0
    assert job.ao.certifications_obligatoires == ["ISO 27001"]

    from src.web import jobs as jobs_module
    del jobs_module._JOBS[job.id]
    reloaded = jobs_module.get_job(job.id)
    assert reloaded is not None
    assert reloaded.ao.deadline_reponse is None
    assert reloaded.ao.client == "Collectivite Exemple"
    assert reloaded.ao.budget_estime == 300000.0
    assert reloaded.ao.certifications_obligatoires == ["ISO 27001"]
    assert reloaded.ao.field_provenance["deadline_reponse"] == "absent"


# ---------------------------------------------------------------------------
# Group B — mixed/malformed response shapes.
# ---------------------------------------------------------------------------

def test_one_invalid_field_does_not_destroy_the_others(monkeypatch):
    mixed = dict(VALID_RESPONSE)
    mixed["duree_projet_mois"] = "douze mois"  # wrong type — a string, not an int
    ao = _build(mixed)

    assert ao.field_provenance["duree_projet_mois"] == "fallback"  # SAMPLE_TEXT has "12 mois"
    assert ao.duree_projet_mois == 12
    # Every other field is untouched.
    assert ao.client == "Collectivite Exemple"
    assert ao.budget_estime == 300000.0
    assert ao.certifications_obligatoires == ["ISO 27001"]
    assert ao.field_provenance["client"] == "llm"


def test_invalid_field_with_no_local_evidence_is_marked_rejected_not_silently_dropped():
    mixed = dict(VALID_RESPONSE)
    mixed["duree_projet_mois"] = "douze mois"
    text_without_duration_hint = "Appel d'offres sans aucune mention de duree dans le texte."
    ao = _build(mixed, text=text_without_duration_hint)

    assert ao.field_provenance["duree_projet_mois"] == "rejected"
    assert ao.duree_projet_mois is None


def test_provider_response_dict_is_never_mutated():
    mixed = dict(VALID_RESPONSE)
    mixed["duree_projet_mois"] = True  # boolean — must be rejected, never coerced to 1/0
    snapshot = dict(mixed)
    _build(mixed)
    assert mixed == snapshot, "the raw provider response dict must never be mutated by field resolution"


def test_budget_zero_is_distinguished_from_budget_absent():
    zero_budget = dict(VALID_RESPONSE)
    zero_budget["budget_estime"] = 0
    ao_zero = _build(zero_budget)
    assert ao_zero.budget_estime == 0.0
    assert ao_zero.field_provenance["budget_estime"] == "llm"

    text_without_budget_hint = "Appel d'offres sans aucun montant mentionne dans le texte."
    absent_budget = dict(VALID_RESPONSE)
    absent_budget["budget_estime"] = None
    ao_absent = _build(absent_budget, text=text_without_budget_hint)
    assert ao_absent.budget_estime is None
    assert ao_absent.field_provenance["budget_estime"] == "absent"


@pytest.mark.parametrize("bad_value", [True, False, float("nan"), float("inf"), "300000"])
def test_non_finite_or_boolean_or_string_budget_is_rejected(bad_value):
    from src.agents.ao_extractor import _INVALID
    assert _validate_float(bad_value) is _INVALID


@pytest.mark.parametrize("bad_value", [True, False, "12", 12.5])
def test_boolean_or_string_or_fractional_duration_is_rejected(bad_value):
    from src.agents.ao_extractor import _INVALID
    assert _validate_int_like(bad_value) is _INVALID


def test_malformed_list_field_does_not_crash_and_is_rejected():
    mixed = dict(VALID_RESPONSE)
    mixed["technologies_demandees"] = "Python"  # a string, not a list
    ao = _build(mixed, text="Appel d'offres sans technologie mentionnee explicitement ici.")
    assert ao.field_provenance["technologies_demandees"] == "rejected"
    assert ao.technologies_demandees == []


def test_competences_requises_never_falls_back_to_the_technology_vocabulary():
    missing_competences = dict(VALID_RESPONSE)
    del missing_competences["competences_requises"]
    ao = _build(missing_competences, text="Texte mentionnant Python et Django mais aucune competence metier.")
    assert ao.competences_requises == []
    assert ao.field_provenance["competences_requises"] == "absent"


def test_non_dict_response_never_reaches_dict_methods_and_falls_back(monkeypatch):
    extractor, provider = _extractor_with(monkeypatch, response=["not", "a", "dict"])
    ao = extractor.extract(SAMPLE_TEXT)
    assert ao.extraction_status == "fallback_local"
    assert ao.extraction_reason == "invalid_response_shape"
    # The local fallback still recovers real facts from the known text.
    assert ao.certifications_obligatoires == ["ISO 27001"]


# ---------------------------------------------------------------------------
# Group C — provider exception, disabled LLM, and a real job consumer.
# ---------------------------------------------------------------------------

def test_provider_exception_yields_a_controlled_local_fallback(monkeypatch):
    extractor, provider = _extractor_with(monkeypatch, response=None, raises=RuntimeError("boom"))
    ao = extractor.extract(SAMPLE_TEXT)
    assert ao.extraction_status == "fallback_local"
    assert ao.extraction_reason == "no_content"
    assert ao.certifications_obligatoires == ["ISO 27001"]
    assert ao.duree_projet_mois == 12


def test_disabled_llm_makes_zero_calls_and_still_extracts_locally(monkeypatch):
    extractor, provider = _extractor_with(monkeypatch, response=VALID_RESPONSE, enabled=False)
    ao = extractor.extract(SAMPLE_TEXT)
    assert provider.calls == 0, "no network/provider call must be made when the LLM is disabled"
    assert ao.extraction_status == "fallback_local"
    assert ao.extraction_reason == "llm_disabled"
    assert ao.certifications_obligatoires == ["ISO 27001"]
    assert ao.deadline_reponse is None


def test_real_job_completes_with_an_ao_that_has_no_deadline_at_all(client, db, monkeypatch):
    """The exact end-to-end scenario the audited bug broke: previously, a
    null deadline from the model could make AOContext construction raise
    inside AOExtractor.extract, and the job either crashed or silently
    lost every other field via the full local fallback. Now the job must
    simply complete, with the null deadline intact."""
    _install_no_op_search(monkeypatch)
    _install_fake_extraction(monkeypatch, response=VALID_RESPONSE)
    csrf = _configure_account(client, db, "realjobnodeadline@example.com")
    job = _run_analysis_to_completion(client, csrf, SAMPLE_TEXT)

    assert job.status == "done"
    assert job.ao.deadline_reponse is None
    assert job.result is not None  # scoring/document consumers accepted the AO without crashing
