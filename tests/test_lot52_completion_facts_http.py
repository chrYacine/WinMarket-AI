"""Lot 52 — real HTTP paths for "Chercher dans mes documents" (POST /api/analyze/{job}/completion/
search-facts) and its re-verified acceptance inside POST /api/analyze/{job}/complete
(src/web/completion_service.py's `search_facts`/`_resolve_sourced_origin`). Real ScoringEngine, real
private knowledge base (SQLite — declared lexical mode, exactly like every other lot-49-family test), real
AO extraction fallback (no LLM). Only `src.agents.llm_client.ClaudeClient` is replaced with a small stub for
the fact-search call itself — never a prefabricated scoring result.
"""
from __future__ import annotations

import io
import time

import pytest

from src.web import jobs
from tests.conftest import make_active_starter_user
from tests.test_lot44_criteria_contract import _capacity, _login, _put, crit

PROFILE = {
    "raison_sociale": "Nettoyage Pro (lot 52)", "competences": [], "certifications": [],
    "business_facts": {
        "frequence_nettoyage": {"key": "frequence_nettoyage", "label": "Fréquence de nettoyage", "type": "number", "unit": "par_semaine", "value": 5},
    },
}
BUDGET_TIERS = {"source": "budget", "fact_key": None, "tiers": [{"at_least": 100000, "score": 100}], "below_score": 0, "zero_score": None, "minimum_blocking": None}


class _StubLLM:
    def __init__(self, response):
        self.enabled = True
        self.last_provider_used = "stub"
        self._response = response
        self.calls = 0

    def json_complete(self, prompt, system=None, temperature=None, max_tokens=None):
        self.calls += 1
        return self._response


def _patch_llm(monkeypatch, response):
    import src.agents.llm_client as llm_client_module
    stub = _StubLLM(response)
    monkeypatch.setattr(llm_client_module, "ClaudeClient", lambda: stub)
    return stub


def _clear_declared_frequency(client, csrf):
    payload = dict(PROFILE)
    payload["business_facts"] = {"frequence_nettoyage": {**PROFILE["business_facts"]["frequence_nettoyage"], "value": None}}
    assert _put(client, csrf, "/api/scoring-config/profile", payload).status_code == 200


def _make_prestataire_fact_account(client, db, email):
    make_active_starter_user(db, email, scoring=False)
    csrf = _login(client, email)
    _capacity(client, csrf)
    assert _put(client, csrf, "/api/scoring-config/profile", PROFILE).status_code == 200
    criteria = [crit(
        "frequence", "numeric_threshold", {"fact_key": "frequence_nettoyage", "comparison": "provider_gte_ao", "pass_score": 100, "fail_score": 0},
        100, label="Fréquence compatible",
    )]
    assert _put(client, csrf, "/api/scoring-config/policy", {"criteria": criteria, "threshold_go": 50, "threshold_sous_reserve": 20}).status_code == 200
    assert client.post("/api/scoring-config/policy/activate", json={"expected_active_version": None}, headers={"X-CSRF-Token": csrf}).status_code == 200
    _clear_declared_frequency(client, csrf)
    return csrf


def _make_budget_account(client, db, email):
    make_active_starter_user(db, email, scoring=False)
    csrf = _login(client, email)
    _capacity(client, csrf)
    assert _put(client, csrf, "/api/scoring-config/profile", {"raison_sociale": "Budget Co (lot 52)", "competences": [], "certifications": [], "business_facts": {}}).status_code == 200
    criteria = [crit("budget", "numeric_tiers", BUDGET_TIERS, 100, label="Budget estimé")]
    assert _put(client, csrf, "/api/scoring-config/policy", {"criteria": criteria, "threshold_go": 50, "threshold_sous_reserve": 20}).status_code == 200
    assert client.post("/api/scoring-config/policy/activate", json={"expected_active_version": None}, headers={"X-CSRF-Token": csrf}).status_code == 200
    return csrf


def _wait(job_id):
    for _ in range(300):
        job = jobs.get_job(job_id)
        if job is not None and job.status != "running":
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def _analyze(client, csrf, text):
    r = client.post("/api/analyze", data={"mode": "paste", "text": text}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200, r.text
    return _wait(r.json()["job_id"])


def _needs(client, job_id):
    r = client.get(f"/api/analyze/{job_id}/completion")
    assert r.status_code == 200, r.text
    return r.json()


def _search(client, csrf, job_id, need_ids):
    return client.post(f"/api/analyze/{job_id}/completion/search-facts", json={"need_ids": need_ids}, headers={"X-CSRF-Token": csrf})


def _complete(client, csrf, job_id, items, **extra):
    return client.post(f"/api/analyze/{job_id}/complete", json={"items": items, **extra}, headers={"X-CSRF-Token": csrf})


def _upload_doc(client, csrf, name, content):
    r = client.post("/api/knowledge/documents", headers={"X-CSRF-Token": csrf}, files={"file": (name, io.BytesIO(content.encode("utf-8")), "text/plain")})
    assert r.status_code == 201, r.text
    return r.json()


FREQ_DOC = "Notre fréquence de nettoyage standard est de 5 fois par semaine, ajustable selon les besoins du client."
FREQ_CITATION = "fréquence de nettoyage standard est de 5 fois par semaine"
FREQ_RESPONSE = {"found": True, "passage_number": 1, "value": 5, "unit": "par semaine", "citation": FREQ_CITATION, "reason": "valeur explicite trouvée"}


@pytest.fixture()
def prestataire_account(client, db):
    return _make_prestataire_fact_account(client, db, "l52-presta@example.com")


# ---------------------------------------------------------------------------
# A — prestataire subject: search the private knowledge base, accept, revise
# ---------------------------------------------------------------------------

def test_search_proposes_a_citation_verified_value_from_the_knowledge_base(client, db, prestataire_account, monkeypatch):
    csrf = prestataire_account
    _upload_doc(client, csrf, "conditions.md", FREQ_DOC)
    job = _analyze(client, csrf, "Appel d'offres de nettoyage à Lyon. 3 fois par semaine. Budget : 120 000 euros.")
    assert job.result.decision != "GO"
    state = _needs(client, job.id)
    need = next(n for n in state["needs"] if n["field_key"] == "frequence_nettoyage")
    assert need["action"] == "declare_prestataire"

    _patch_llm(monkeypatch, FREQ_RESPONSE)
    r = _search(client, csrf, job.id, [need["id"]])
    assert r.status_code == 200, r.text
    proposal = r.json()["results"][0]
    assert proposal["status"] == "proposed" and proposal["value"] == 5.0
    assert proposal["citation"] == FREQ_CITATION
    assert proposal["source"]["kind"] == "knowledge_document"
    assert proposal["source"]["chunk_id"] is not None

    r = _complete(
        client, csrf, job.id,
        [{"need_id": need["id"], "value": proposal["value"], "source_proposal": proposal}],
        confirm_profile_write=True, expected_profile_version=state["profile_version"],
    )
    assert r.status_code == 200, r.text
    revision = _wait(r.json()["job_id"])
    assert revision.status == "done" and revision.result.decision == "GO"

    from src.web.database.repositories import analysis_complements as complements_repo
    rows = complements_repo.list_for_job(db, job_id=revision.id, organization_id=job.organization_id, user_id=job.user_id)
    row = next(row for row in rows if row.field_key == "frequence_nettoyage")
    assert row.origin == "llm_sourced"
    assert row.source_json["kind"] == "knowledge_document"
    assert row.source_json["citation"] == FREQ_CITATION


def test_a_corrected_value_is_recorded_as_a_plain_declaration_never_llm_sourced(client, db, prestataire_account, monkeypatch):
    """The user accepted the proposal, then edited the value before submitting — the citation no longer
    supports what is actually being declared, so it must never be stored as 'llm_sourced' (ticket, verbatim:
    "sans présenter une citation qui ne soutient plus la valeur saisie comme preuve de cette correction")."""
    csrf = prestataire_account
    _upload_doc(client, csrf, "conditions.md", FREQ_DOC)
    job = _analyze(client, csrf, "Appel d'offres de nettoyage à Lyon. 3 fois par semaine. Budget : 120 000 euros.")
    state = _needs(client, job.id)
    need = next(n for n in state["needs"] if n["field_key"] == "frequence_nettoyage")
    _patch_llm(monkeypatch, FREQ_RESPONSE)
    proposal = _search(client, csrf, job.id, [need["id"]]).json()["results"][0]

    r = _complete(
        client, csrf, job.id,
        [{"need_id": need["id"], "value": 7, "source_proposal": proposal}],  # 7, not the proposed 5
        confirm_profile_write=True, expected_profile_version=state["profile_version"],
    )
    assert r.status_code == 200, r.text
    revision = _wait(r.json()["job_id"])

    from src.web.database.repositories import analysis_complements as complements_repo
    rows = complements_repo.list_for_job(db, job_id=revision.id, organization_id=job.organization_id, user_id=job.user_id)
    row = next(row for row in rows if row.field_key == "frequence_nettoyage")
    assert row.origin == "declared_user" and row.source_json is None
    assert row.value_json == 7.0


def test_a_deleted_document_between_proposal_and_submission_refuses_explicitly_no_partial_write(client, db, prestataire_account, monkeypatch):
    csrf = prestataire_account
    doc = _upload_doc(client, csrf, "conditions.md", FREQ_DOC)
    job = _analyze(client, csrf, "Appel d'offres de nettoyage à Lyon. 3 fois par semaine. Budget : 120 000 euros.")
    state = _needs(client, job.id)
    need = next(n for n in state["needs"] if n["field_key"] == "frequence_nettoyage")
    _patch_llm(monkeypatch, FREQ_RESPONSE)
    proposal = _search(client, csrf, job.id, [need["id"]]).json()["results"][0]

    import uuid as uuid_module
    from src.web.database.repositories import knowledge as knowledge_repo
    document = knowledge_repo.get_document_for_owner(
        db, document_id=uuid_module.UUID(doc["document"]["id"]), organization_id=job.organization_id, owner_user_id=job.user_id,
    )
    knowledge_repo.soft_delete_document(db, document)
    db.commit()

    from src.web.database.repositories import analyses as analyses_repo
    before = analyses_repo.get_by_parent_job_id(db, job.id)
    assert before is None

    r = _complete(
        client, csrf, job.id,
        [{"need_id": need["id"], "value": proposal["value"], "source_proposal": proposal}],
        confirm_profile_write=True, expected_profile_version=state["profile_version"],
    )
    assert r.status_code == 409 and r.json()["detail"]["error_code"] == "SOURCE_CHANGED"
    after = analyses_repo.get_by_parent_job_id(db, job.id)
    assert after is None, "a refused sourced proposal must never create a partial revision"


def test_absent_value_is_an_explicit_status_never_a_crash(client, db, prestataire_account, monkeypatch):
    csrf = prestataire_account
    _upload_doc(client, csrf, "conditions.md", FREQ_DOC)
    job = _analyze(client, csrf, "Appel d'offres de nettoyage à Lyon. 3 fois par semaine. Budget : 120 000 euros.")
    state = _needs(client, job.id)
    need = next(n for n in state["needs"] if n["field_key"] == "frequence_nettoyage")

    _patch_llm(monkeypatch, {"found": False, "passage_number": None, "value": None, "unit": None, "citation": "", "reason": "aucune mention"})
    r = _search(client, csrf, job.id, [need["id"]])
    assert r.status_code == 200
    assert r.json()["results"][0]["status"] == "absent"


def test_llm_unavailable_is_an_explicit_status_never_a_crash(client, db, prestataire_account):
    csrf = prestataire_account
    _upload_doc(client, csrf, "conditions.md", FREQ_DOC)
    job = _analyze(client, csrf, "Appel d'offres de nettoyage à Lyon. 3 fois par semaine. Budget : 120 000 euros.")
    state = _needs(client, job.id)
    need = next(n for n in state["needs"] if n["field_key"] == "frequence_nettoyage")

    # No provider configured at all — the real (unpatched) ClaudeClient is disabled in the test environment
    # (tests/conftest.py blanks every provider key at import time).
    r = _search(client, csrf, job.id, [need["id"]])
    assert r.status_code == 200
    assert r.json()["results"][0]["status"] == "llm_unavailable"


def test_search_facts_bounds_the_number_of_needs_per_call(client, db, prestataire_account):
    csrf = prestataire_account
    job = _analyze(client, csrf, "Appel d'offres de nettoyage à Lyon. 3 fois par semaine. Budget : 120 000 euros.")
    r = _search(client, csrf, job.id, ["a"] * 9)
    assert r.status_code == 422 and r.json()["detail"]["error_code"] == "INVALID_VALUE"


def test_an_unknown_need_id_is_reported_explicitly_not_silently_dropped(client, db, prestataire_account):
    csrf = prestataire_account
    job = _analyze(client, csrf, "Appel d'offres de nettoyage à Lyon. 3 fois par semaine. Budget : 120 000 euros.")
    r = _search(client, csrf, job.id, ["not-a-real-need"])
    assert r.status_code == 200
    assert r.json()["results"][0] == {"need_id": "not-a-real-need", "status": "unknown_need"}


def test_a_foreign_user_cannot_search_facts_on_someone_elses_job(client, db, prestataire_account):
    csrf = prestataire_account
    job = _analyze(client, csrf, "Appel d'offres de nettoyage à Lyon. 3 fois par semaine. Budget : 120 000 euros.")
    make_active_starter_user(db, "l52-outsider@example.com", scoring=False)
    outsider_csrf = _login(client, "l52-outsider@example.com")
    r = _search(client, outsider_csrf, job.id, ["criterion:budget"])
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# B — ao subject: search the AO's own texte_source (no dossier)
# ---------------------------------------------------------------------------

def test_search_proposes_a_value_from_the_aos_own_text_for_an_ao_side_need(client, db, monkeypatch):
    csrf = _make_budget_account(client, db, "l52-budget@example.com")
    ao_text = "Appel d'offres. Budget prévisionnel : cent cinquante mille euros pour cette prestation, sur douze mois."
    job = _analyze(client, csrf, ao_text)
    assert job.ao.budget_estime is None  # written in words: the digit-based regex fallback never finds it
    state = _needs(client, job.id)
    need = next(n for n in state["needs"] if n["field_key"] == "budget_estime")
    assert need["action"] == "declare_ao"

    response = {"found": True, "passage_number": 1, "value": 150000, "unit": None,
                "citation": "cent cinquante mille euros pour cette prestation", "reason": "montant explicite en toutes lettres"}
    _patch_llm(monkeypatch, response)
    r = _search(client, csrf, job.id, [need["id"]])
    assert r.status_code == 200, r.text
    proposal = r.json()["results"][0]
    assert proposal["status"] == "proposed" and proposal["value"] == 150000.0
    assert proposal["source"]["kind"] == "ao_text"

    r = _complete(client, csrf, job.id, [{"need_id": need["id"], "value": proposal["value"], "source_proposal": proposal}])
    assert r.status_code == 200, r.text
    revision = _wait(r.json()["job_id"])
    assert revision.status == "done" and revision.result.decision == "GO"
    assert revision.ao.budget_estime == 150000.0

    from src.web.database.repositories import analysis_complements as complements_repo
    rows = complements_repo.list_for_job(db, job_id=revision.id, organization_id=job.organization_id, user_id=job.user_id)
    row = next(row for row in rows if row.field_key == "budget_estime")
    assert row.origin == "llm_sourced" and row.source_json["kind"] == "ao_text"


def test_no_source_when_the_job_has_no_dossier_and_no_texte_source(client, db, monkeypatch):
    """A defensive edge case: an AO-side need with nothing to search must degrade to "no_source", never
    call the LLM. Simulated by clearing the frozen `texte_source` directly on the in-memory job (the normal
    paste-mode path always populates it; this proves the fallback is genuinely honoured)."""
    csrf = _make_budget_account(client, db, "l52-nosource@example.com")
    job = _analyze(client, csrf, "Appel d'offres. Texte quelconque sans montant explicite, mais budget mentionné.")
    job.ao.texte_source = ""
    state = _needs(client, job.id)
    need = next(n for n in state["needs"] if n["field_key"] == "budget_estime")
    stub = _patch_llm(monkeypatch, {"found": True})
    r = _search(client, csrf, job.id, [need["id"]])
    assert r.status_code == 200
    assert r.json()["results"][0]["status"] == "no_source"
    assert stub.calls == 0
