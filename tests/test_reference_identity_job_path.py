"""B18-T4 (DEFECT F09/F12/E-5) — test group C: the real /api/analyze job
path, confirming exact-copy deduplication holds end-to-end together with
B18-T3's selection contract: score invariance under repeated references,
identity/provenance surviving persistence/reread, an empty selection and a
B18-T3 exclusion both still preserved, and B03 isolation for the touched
path (same content at a different owner never mixes cache/sources/result).

A deterministic fake LLM proves the MECHANISM is invariant to repetition —
it does not, and cannot, prove a real LLM's semantic stability; that
remains untested here, honestly.
"""
from __future__ import annotations

import io
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

REFERENCE_TEXT = "Reference projet portail client stack Python Django secteur retail livre en 2024."


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


class _SelectiveFakeProvider:
    """Same idiom as tests/test_reference_selection_job_path.py — answers
    the reference-SELECTION call and the scoring-ENRICHMENT call
    differently by inspecting the prompt text (both share one
    `llm.json_complete` call site in src/web/jobs.py)."""
    name = "primary"
    enabled = True

    def __init__(self, selection_payload: dict):
        self._selection_payload = selection_payload

    def complete(self, prompt, system=None, temperature=None, max_tokens=None):
        if "selected_ids" in prompt:
            return json.dumps(self._selection_payload)
        return json.dumps({"justifications": {}})


def _install_fake_llm(monkeypatch, selection_payload: dict):
    from src.core import config
    import src.agents.llm_client as llm_client_module
    from src.agents.llm_client import LLMClient

    monkeypatch.setattr(config, "LLM_ENABLED", True)
    monkeypatch.setattr(llm_client_module, "ClaudeClient", lambda: LLMClient([_SelectiveFakeProvider(selection_payload)]))


def _install_fake_search(monkeypatch, evidences: list[RAGEvidence]):
    from src.rag import private_rag_manager
    monkeypatch.setattr(private_rag_manager, "search", lambda db, **kw: list(evidences))


def test_score_invariant_when_llm_selects_all_duplicated_candidates(client, db, monkeypatch):
    """Even if the (simulated) LLM's selection includes every duplicate
    candidate by id, the ENGINE-level dedup (B18-T4 section 5) still
    collapses them — selection breadth must never restore a repetition
    bonus B18-T3's own exclusion mechanism removed."""
    duplicates = [
        RAGEvidence(query="q", source=f"copy_{i}.md", score=0.8, content=REFERENCE_TEXT)
        for i in range(4)
    ]
    unique_single = [RAGEvidence(query="q", source="solo.md", score=0.8, content=REFERENCE_TEXT)]

    csrf = _configure_account(client, db, "invariantall@example.com")
    _install_fake_search(monkeypatch, duplicates)
    _install_fake_llm(monkeypatch, {"selected_ids": [1, 2, 3, 4], "synthese": "Toutes retenues."})
    job_all_selected = _run_analysis_to_completion(client, csrf, VALID_AO_TEXT)

    csrf = _configure_account(client, db, "invariantsolo@example.com")
    _install_fake_search(monkeypatch, unique_single)
    _install_fake_llm(monkeypatch, {"selected_ids": [1], "synthese": "Seule reference retenue."})
    job_solo = _run_analysis_to_completion(client, csrf, VALID_AO_TEXT)

    assert job_all_selected.result.score_global == job_solo.result.score_global
    assert job_all_selected.result.decision == job_solo.result.decision
    assert len(job_all_selected.result.evidence_pack) == 1


def test_identity_and_provenance_survive_persistence_and_reread(client, db, monkeypatch):
    duplicates = [
        RAGEvidence(query="q", source="original.md", score=0.7, content=REFERENCE_TEXT),
        RAGEvidence(query="q", source="renamed_copy.md", score=0.9, content=REFERENCE_TEXT),
    ]
    csrf = _configure_account(client, db, "provenance@example.com")
    _install_fake_search(monkeypatch, duplicates)
    _install_fake_llm(monkeypatch, {"selected_ids": [1, 2], "synthese": "Les deux retenues."})
    job = _run_analysis_to_completion(client, csrf, VALID_AO_TEXT)

    assert len(job.result.evidence_pack) == 1
    kept = job.result.evidence_pack[0]
    assert kept.score == 0.9  # best of the two duplicates
    assert kept.source == "renamed_copy.md"
    assert kept.duplicate_sources == ["original.md"]

    from src.web import jobs as jobs_module
    del jobs_module._JOBS[job.id]
    reloaded = jobs_module.get_job(job.id)
    assert reloaded is not None
    assert len(reloaded.result.evidence_pack) == 1
    assert reloaded.result.evidence_pack[0].duplicate_sources == ["original.md"]
    assert reloaded.result.data_integrity == "ok"
    assert reloaded.result.rag_selection_status == "applied"


def test_empty_selection_and_b18_t3_exclusion_both_preserved_alongside_dedup(client, db, monkeypatch):
    duplicates = [
        RAGEvidence(query="q", source="a.md", score=0.7, content=REFERENCE_TEXT),
        RAGEvidence(query="q", source="b.md", score=0.7, content=REFERENCE_TEXT),
    ]
    # Empty selection: dedup is irrelevant once nothing is selected at all.
    csrf = _configure_account(client, db, "emptyafterdedup@example.com")
    _install_fake_search(monkeypatch, duplicates)
    _install_fake_llm(monkeypatch, {"selected_ids": [], "synthese": "ignoree"})
    job_empty = _run_analysis_to_completion(client, csrf, VALID_AO_TEXT)
    assert job_empty.result.evidence_pack == []
    assert job_empty.result.rag_selection_status == "applied"

    # B18-T3 exclusion: only id=1 selected among 2 exact duplicates.
    csrf = _configure_account(client, db, "exclusionafterdedup@example.com")
    _install_fake_search(monkeypatch, duplicates)
    _install_fake_llm(monkeypatch, {"selected_ids": [1], "synthese": "Une seule retenue."})
    job_excluded = _run_analysis_to_completion(client, csrf, VALID_AO_TEXT)
    assert [ev.source for ev in job_excluded.result.evidence_pack] == ["a.md"]


def test_same_content_at_another_owner_never_mixes_cache_sources_or_result(client, db):
    """B03 isolation, reused for this touched path (real corpus, not a
    fake search) — the SAME exact reference text uploaded by two different
    owners must never cross into each other's cache/results."""
    from src.rag import private_rag_manager

    user_a = make_active_starter_user(db, "dedupisoa@example.com", scoring=False)
    org_a = default_org_id(db, user_a)
    csrf = _login(client, "dedupisoa@example.com")
    r = client.post(
        "/api/knowledge/documents",
        files={"file": ("shared_text.md", io.BytesIO(REFERENCE_TEXT.encode("utf-8")), "text/plain")},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 201

    user_b = make_active_starter_user(db, "dedupisob@example.com", scoring=False)
    org_b = default_org_id(db, user_b)
    csrf = _login(client, "dedupisob@example.com")
    r = client.post(
        "/api/knowledge/documents",
        files={"file": ("shared_text.md", io.BytesIO(REFERENCE_TEXT.encode("utf-8")), "text/plain")},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 201

    results_a = private_rag_manager.search(db, organization_id=org_a, owner_user_id=user_a.id, query="portail client python retail", top_k=6)
    results_b = private_rag_manager.search(db, organization_id=org_b, owner_user_id=user_b.id, query="portail client python retail", top_k=6)

    assert len(results_a) == 1 and len(results_b) == 1
    # Identical content at two DIFFERENT owners must never be recognized as
    # duplicates of EACH OTHER — the fingerprint groups within one owner's
    # snapshot only, never across accounts.
    assert results_a[0].duplicate_sources == []
    assert results_b[0].duplicate_sources == []
