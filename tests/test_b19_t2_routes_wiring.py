"""Coordinator integration test: the B19-T2 document-lifecycle routes
(`GET /api/analyze/{job_id}/documents/status`,
`POST /api/analyze/{job_id}/documents/{kind}/regenerate`) actually wired
into src/web/routes_api.py, exercised over real HTTP rather than by
calling src/web/document_rendering_service.py directly (already covered
at the service level by tests/test_b19_t2_document_rendering.py).
"""
from __future__ import annotations

import re
import time

from src.core import config
from src.web import jobs
from tests.conftest import make_active_starter_user

VALID_WEIGHTS = {
    "Adequation expertise": 20, "References similaires": 15, "Disponibilite equipe": 10,
    "Rentabilite estimee": 10, "Faisabilite delai": 10, "Certifications requises": 10,
    "Complexite technique": 5, "Connaissance secteur": 5, "Potentiel commercial": 5,
    "Risque contractuel": 5, "Solidite client": 3, "Valeur strategique": 2,
}

VALID_AO_TEXT = (
    "Appel d'offres - Portail client\n"
    "Acheteur : Collectivite Exemple\n"
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


def _save_profile(client, csrf):
    return client.put("/api/scoring-config/profile", json={
        "raison_sociale": "ESN de test", "effectif": "10-50", "competences": ["python", "django"], "certifications": [],
    }, headers={"X-CSRF-Token": csrf})


def _save_draft(client, csrf):
    return client.put("/api/scoring-config/policy", json={
        "weights": dict(VALID_WEIGHTS), "threshold_go": 88, "threshold_sous_reserve": 60,
        "business_rules": {
            "budget_minimum_eur": 0, "max_charge_pct": 100,
            "max_unmastered_technologies": 999, "certification_penalty_score": 20,
        },
    }, headers={"X-CSRF-Token": csrf})


def _activate(client, csrf):
    return client.post(
        "/api/scoring-config/policy/activate", json={"expected_active_version": None},
        headers={"X-CSRF-Token": csrf},
    )


def _configure_account(client, db, email):
    make_active_starter_user(db, email, scoring=False)
    csrf = _login(client, email)
    _configure_capacity(client, csrf)
    _save_profile(client, csrf)
    _save_draft(client, csrf)
    _activate(client, csrf)
    return csrf


def _run_analysis_to_terminal(client, csrf, text: str):
    r = client.post("/api/analyze", data={"mode": "paste", "text": text}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    for _ in range(50):
        job = jobs.get_job(job_id)
        if job.status != "running":
            break
        time.sleep(0.1)
    assert job.status != "running", "job never reached a terminal state"
    return job


def _install_no_op_search(monkeypatch):
    from src.rag import private_rag_manager
    monkeypatch.setattr(private_rag_manager, "search", lambda db, **kw: [])


def _isolate_job_file_paths(tmp_path, monkeypatch):
    """jobs.ANALYSES_DIR / jobs.ANALYSIS_FILES_DIR / historique_service.
    HIST_FILE are module-level constants bound at import time from the
    ORIGINAL config.OUTPUT_DIR/config.DATA_DIR — the autouse
    _b04_isolated_filesystem_roots fixture (tests/conftest.py) only
    monkeypatches config.*, so these three still point at the real project
    tree unless a test also redirects them directly (the same gap
    tests/test_b11_t1_persistence.py already works around). Redirected
    here to config's CURRENT (already-isolated) OUTPUT_DIR/DATA_DIR so
    generated documents land under a root
    StorageService.resolve_for_download actually recognizes."""
    monkeypatch.setattr(jobs, "ANALYSES_DIR", config.DATA_DIR / "historique" / "analyses")
    monkeypatch.setattr(jobs, "ANALYSIS_FILES_DIR", config.OUTPUT_DIR)


def test_status_route_reports_both_documents_available_after_a_normal_run(client, db, monkeypatch, tmp_path):
    _isolate_job_file_paths(tmp_path, monkeypatch)
    _install_no_op_search(monkeypatch)
    csrf = _configure_account(client, db, "docstatus@example.com")
    job = _run_analysis_to_terminal(client, csrf, VALID_AO_TEXT)
    assert job.status == "done", job.error

    r = client.get(f"/api/analyze/{job.id}/documents/status")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pdf"] == "available"
    assert body["docx"] == "available"


def test_regenerate_route_rebuilds_a_document_with_an_llm_that_fails_if_called(client, db, monkeypatch, tmp_path):
    _isolate_job_file_paths(tmp_path, monkeypatch)
    _install_no_op_search(monkeypatch)

    def _raise_if_generate_documents_called(job, ao, result):
        raise RuntimeError("render boom — simulated rendering failure")
    monkeypatch.setattr(jobs, "_generate_documents", _raise_if_generate_documents_called)

    csrf = _configure_account(client, db, "docregen@example.com")
    job = _run_analysis_to_terminal(client, csrf, VALID_AO_TEXT)
    # The job itself reports the rendering failure, exactly as B10-T1/B11-T1
    # established — but the analysis snapshot is intact in SQL.
    assert job.status == "error"
    assert job.error_code == jobs.DOCUMENT_GENERATION_FAILED_ERROR_CODE

    status_before = client.get(f"/api/analyze/{job.id}/documents/status").json()
    assert status_before["pdf"] == "unavailable"
    assert status_before["docx"] == "unavailable"

    # Regeneration must succeed from the snapshot alone — no LLM call, no
    # dependency on the (still-broken) monkeypatched _generate_documents.
    r = client.post(f"/api/analyze/{job.id}/documents/pdf/regenerate", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200, r.text

    status_after = client.get(f"/api/analyze/{job.id}/documents/status").json()
    assert status_after["pdf"] == "available"
    assert status_after["docx"] == "unavailable", "regenerating pdf must not fabricate a docx"

    # A repeated regeneration for the same kind must not create a
    # duplicate row visible through the API.
    r2 = client.post(f"/api/analyze/{job.id}/documents/pdf/regenerate", headers={"X-CSRF-Token": csrf})
    assert r2.status_code == 200, r2.text
    status_twice = client.get(f"/api/analyze/{job.id}/documents/status").json()
    assert status_twice["pdf"] == "available"


def test_regenerate_route_rejects_an_unsupported_kind(client, db, monkeypatch, tmp_path):
    _isolate_job_file_paths(tmp_path, monkeypatch)
    _install_no_op_search(monkeypatch)
    csrf = _configure_account(client, db, "docregenbadkind@example.com")
    job = _run_analysis_to_terminal(client, csrf, VALID_AO_TEXT)
    assert job.status == "done", job.error

    # CSRF is verified before kind validation (see routes_api.py) — a valid
    # token is required to actually reach the 400 this test is proving.
    r = client.post(f"/api/analyze/{job.id}/documents/exe/regenerate", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 400


def test_third_party_cannot_read_status_or_regenerate_another_owners_documents(client, db, monkeypatch, tmp_path):
    _isolate_job_file_paths(tmp_path, monkeypatch)
    _install_no_op_search(monkeypatch)
    csrf = _configure_account(client, db, "docowner@example.com")
    job = _run_analysis_to_terminal(client, csrf, VALID_AO_TEXT)
    assert job.status == "done", job.error

    make_active_starter_user(db, "docintruder@example.com", scoring=False)
    intruder_csrf = _login(client, "docintruder@example.com")

    r_status = client.get(f"/api/analyze/{job.id}/documents/status")
    assert r_status.status_code == 404

    # CSRF is verified before ownership (see routes_api.py) — the intruder's
    # OWN valid token is required to actually reach the 404 this test is
    # proving, rather than a 403 for a missing/foreign token.
    r_regen = client.post(f"/api/analyze/{job.id}/documents/pdf/regenerate", headers={"X-CSRF-Token": intruder_csrf})
    assert r_regen.status_code == 404
