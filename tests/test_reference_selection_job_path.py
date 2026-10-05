"""B18-T3 (DEFECT F12/E-4) — test group C: the real /api/analyze job path,
confirming the reference SELECTION contract holds end-to-end: only the
references the (simulated) LLM actually selected reach ScoringEngine, the
selection status persists/rereads correctly, private scope is never
crossed, and an old result without this metadata still reads as
"unknown".

Real authentication/authorization throughout (TestClient + the actual
FastAPI app + an isolated SQLite DB) — no real LLM/Pappers call; the LLM
provider is a minimal fake that inspects the prompt text to answer the
reference-selection call differently from the (separate) scoring
enrichment call, since both share the same underlying `llm.json_complete`
call site in src/web/jobs.py.
"""
from __future__ import annotations

import json
import re
import time

from src.core.models import RAGEvidence
from tests.conftest import default_org_id, make_active_starter_user

VALID_WEIGHTS = {
    "Adequation expertise": 20, "References similaires": 15, "Disponibilite equipe": 10,
    "Rentabilite estimee": 10, "Faisabilite delai": 10, "Certifications requises": 10,
    "Complexite technique": 5, "Connaissance secteur": 5, "Potentiel commercial": 5,
    "Risque contractuel": 5, "Solidite client": 3, "Valeur strategique": 2,
}

VALID_AO_TEXT = (
    "Appel d'offres - Portail client\n"
    "Acheteur : Collectivite Exemple\n"
    "Le prestataire realisera le portail et ses livrables.\n"
    "Budget : 250 000 euros. Date limite : 30/11/2026.\n"
    "Exigences : Python et Django.\n"
)


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


def _save_profile(client, csrf, raison_sociale="ESN de test"):
    return client.put("/api/scoring-config/profile", json={
        "raison_sociale": raison_sociale, "effectif": "10-50", "competences": ["python", "django"], "certifications": [],
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


class _SelectiveFakeProvider:
    """Answers the reference-SELECTION call and the scoring-ENRICHMENT
    call differently, by inspecting the prompt text — the two share the
    same `llm.json_complete(...)` call site in src/web/jobs.py, so a
    single stateless fake must distinguish them itself. Never touches
    real network/credentials — a pure in-memory stand-in."""
    name = "primary"
    enabled = True

    def __init__(self, selection_payload: dict):
        self._selection_payload = selection_payload

    def complete(self, prompt, system=None, temperature=None, max_tokens=None):
        if "selected_ids" in prompt:
            return json.dumps(self._selection_payload)
        return json.dumps({"justifications": {}})  # minimal, valid, inert enrichment response


def _install_fake_llm(monkeypatch, selection_payload: dict):
    from src.core import config
    import src.agents.llm_client as llm_client_module
    from src.agents.llm_client import LLMClient

    monkeypatch.setattr(config, "LLM_ENABLED", True)
    monkeypatch.setattr(llm_client_module, "ClaudeClient", lambda: LLMClient([_SelectiveFakeProvider(selection_payload)]))


def _install_fake_search(monkeypatch, evidences: list[RAGEvidence]):
    from src.rag import private_rag_manager
    monkeypatch.setattr(private_rag_manager, "search", lambda db, **kw: list(evidences))


def test_only_selected_reference_reaches_the_engine_and_persists(client, db, monkeypatch):
    """The exact scenario the audit found broken: two candidates, the LLM
    selects only one — the excluded one must never reach ScoringEngine,
    never appear in the persisted evidence_pack, and the status must be
    visible after a real reload from disk."""
    relevant = RAGEvidence(query="q", source="relevant.md", score=0.8, content="Reference Python/Django pertinente.")
    off_topic = RAGEvidence(query="q", source="off_topic.md", score=0.3, content="Reference totalement hors sujet.")
    _install_fake_search(monkeypatch, [relevant, off_topic])
    _install_fake_llm(monkeypatch, {"selected_ids": [1], "synthese": "Seule la reference pertinente est retenue."})

    csrf = _configure_account(client, db, "selectiononly@example.com")
    job = _run_analysis_to_completion(client, csrf, VALID_AO_TEXT)

    assert [ev.source for ev in job.result.evidence_pack] == ["relevant.md"]
    assert job.result.rag_selection_status == "applied"
    assert job.result.rag_selection_reason is None

    # Persistence/reread: force a real reload from the JSON file, not the
    # in-memory job the thread just built.
    from src.web import jobs as jobs_module
    del jobs_module._JOBS[job.id]
    reloaded = jobs_module.get_job(job.id)
    assert reloaded is not None
    assert [ev.source for ev in reloaded.result.evidence_pack] == ["relevant.md"]
    assert reloaded.result.rag_selection_status == "applied"


def test_empty_selection_reaches_the_engine_as_no_evidence_at_all(client, db, monkeypatch):
    relevant = RAGEvidence(query="q", source="relevant.md", score=0.8, content="Reference potentiellement utile.")
    _install_fake_search(monkeypatch, [relevant])
    _install_fake_llm(monkeypatch, {"selected_ids": [], "synthese": "ignorée"})

    csrf = _configure_account(client, db, "selectionempty@example.com")
    job = _run_analysis_to_completion(client, csrf, VALID_AO_TEXT)

    assert job.result.evidence_pack == []
    assert job.result.rag_selection_status == "applied"
    assert job.result.rag_synthesis == ""


def test_third_party_id_never_resolves_a_reference_outside_this_call(client, db, monkeypatch):
    """An id the model might send (say, 5) that would be meaningful for a
    DIFFERENT call with more candidates must be rejected here — there is
    no global reference-id space to accidentally resolve into, only the
    candidates presented for THIS specific call."""
    relevant = RAGEvidence(query="q", source="relevant.md", score=0.8, content="Reference pertinente.")
    _install_fake_search(monkeypatch, [relevant])
    _install_fake_llm(monkeypatch, {"selected_ids": [5], "synthese": "invalide"})

    csrf = _configure_account(client, db, "selectionoutofscope@example.com")
    job = _run_analysis_to_completion(client, csrf, VALID_AO_TEXT)

    # Fallback: the ORIGINAL candidate is kept (not dropped, not some other
    # organization's document, not a crash) — never a partial/favorable result.
    assert [ev.source for ev in job.result.evidence_pack] == ["relevant.md"]
    assert job.result.rag_selection_status == "fallback"
    assert job.result.rag_selection_reason == "unknown_id"


def test_two_accounts_never_share_a_selection_or_leak_across_scope(client, db, monkeypatch):
    """B03 isolation reused for this touched path: A's private evidence
    must never appear in B's job, even with the same fake LLM/selection
    logic installed for both."""
    a_evidence = RAGEvidence(query="q", source="a_only.md", score=0.8, content="Reference exclusive de A.")
    b_evidence = RAGEvidence(query="q", source="b_only.md", score=0.8, content="Reference exclusive de B.")

    csrf = _configure_account(client, db, "isolationa@example.com")
    _install_fake_search(monkeypatch, [a_evidence])
    _install_fake_llm(monkeypatch, {"selected_ids": [1], "synthese": "A"})
    job_a = _run_analysis_to_completion(client, csrf, VALID_AO_TEXT)
    assert [ev.source for ev in job_a.result.evidence_pack] == ["a_only.md"]

    csrf = _configure_account(client, db, "isolationb@example.com")
    _install_fake_search(monkeypatch, [b_evidence])
    _install_fake_llm(monkeypatch, {"selected_ids": [1], "synthese": "B"})
    job_b = _run_analysis_to_completion(client, csrf, VALID_AO_TEXT)
    assert [ev.source for ev in job_b.result.evidence_pack] == ["b_only.md"]
    assert "a_only.md" not in [ev.source for ev in job_b.result.evidence_pack]


def test_old_result_without_selection_metadata_reads_as_unknown(tmp_path, monkeypatch):
    from src.web import jobs as jobs_module

    analyses_dir = tmp_path / "historique" / "analyses"
    analyses_dir.mkdir(parents=True)
    monkeypatch.setattr(jobs_module, "ANALYSES_DIR", analyses_dir)

    legacy_result = {
        "decision": "GO", "score_global": 85.0,
        "criteres": [{"nom": "Adéquation expertise", "poids": 20.0, "score": 90.0, "justification": "ok"}],
        "criteres_bloquants": [], "forces": [], "faiblesses": [], "risques": [], "recommandations": [],
        "evidence_pack": [{"query": "q", "source": "old.md", "score": 0.6, "content": "ancienne reference"}],
        "company_profile": None, "capacity": None, "rag_synthesis": "Synthese historique.", "ai_content": {},
        # no rag_selection_status/rag_selection_reason key at all — exactly
        # what a pre-B18-T3 persisted analysis looks like.
    }
    (analyses_dir / "legacyselection0001.json").write_text(json.dumps({
        "id": "legacyselection0001", "user_id": None, "organization_id": None, "created_at": 0,
        "source_label": "Legacy", "ao": {"titre": "AO historique"}, "result": legacy_result, "files": {},
    }), encoding="utf-8")

    reloaded = jobs_module.get_job("legacyselection0001")
    assert reloaded is not None
    assert reloaded.status == "done"
    assert reloaded.result.rag_selection_status == "unknown"
    assert reloaded.result.rag_selection_reason is None
    assert reloaded.result.decision == "GO"
    assert reloaded.result.score_global == 85.0, "no forced recalculation/decision change for old data"
