"""B10-T1 (DEFECT confirmed): src/web/jobs.py's _run_analysis called
get_pipeline() (now build_analysis_services(), lot 43) OUTSIDE its own try/except, and the worker thread's launch
itself was never protected — either one raising left a Job stuck at
status="running" forever, invisible to any polling client. The final
generic exception handler also embedded the raw exception's own text
directly into job.error (f"...: {exc}"), which /api/analyze/{job_id}/
status returns verbatim to the client — any internal detail the
exception happened to carry could leak into the public API response.

Test groups, per the ticket:
A. Error injection in the pipeline factory and at worker launch — a
   terminal status and a safe code, never a job stuck at "running".
B. A parametrized fatal error at extraction, RAG and rendering — correct
   step/code, no subsequent operation called; an already-computed result
   survives a later rendering failure; a normal LLM fallback remains a
   working path.
C. A failing save — no fake durable success, no reversion to "running";
   the real status response for the owner, no raw detail leak (a
   synthetic marker), and a third party's access still refused.
"""
from __future__ import annotations

import re
import time

import pytest

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


def _run_analysis_to_terminal(client, text: str, csrf):
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


# ---------------------------------------------------------------------------
# Group A — pipeline factory and worker-launch error injection.
# ---------------------------------------------------------------------------

def test_pipeline_factory_error_leaves_job_terminal_not_running(client, db, monkeypatch):
    # Lot 43: the process-wide `get_pipeline` singleton became the per-job
    # `build_analysis_services` factory — same contract, same assertions.
    monkeypatch.setattr(jobs, "build_analysis_services", lambda: (_ for _ in ()).throw(RuntimeError("pipeline factory boom")))
    csrf = _configure_account(client, db, "pipelinefactory@example.com")
    job = _run_analysis_to_terminal(client, VALID_AO_TEXT, csrf)

    assert job.status == "error", "an exception in the pipeline factory must never leave a job stuck running"
    assert job.error_code == jobs.UNEXPECTED_ERROR_CODE
    assert "pipeline factory boom" not in (job.error or "")


class _RaisingThread:
    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        raise RuntimeError("thread pool exhausted")


def test_worker_launch_failure_leaves_job_terminal(monkeypatch):
    """B12-T1: jobs.start_analysis no longer launches a thread directly —
    it delegates to src/web/job_executor.py's bounded pool, whose worker
    threads are long-lived and started once per process
    (job_executor._ensure_workers_started), not one per submitted job.
    The "launching a worker can fail" scenario this test guards now lives
    there: forcing `_workers_started` back to False makes this submission
    the one that (re-)triggers pool startup, so a thread-creation failure
    at that point still reaches jobs.start_analysis's own
    WORKER_LAUNCH_FAILED_ERROR_CODE catch-all, exactly as before."""
    from src.web import job_executor

    monkeypatch.setattr(job_executor, "_workers_started", False)
    monkeypatch.setattr(job_executor.threading, "Thread", _RaisingThread)
    job = jobs.create_job(source_label="test")
    jobs.start_analysis(job, "un texte d'appel d'offres quelconque")

    assert job.status == "error"
    assert job.error_code == jobs.WORKER_LAUNCH_FAILED_ERROR_CODE
    assert "thread pool exhausted" not in (job.error or "")


# ---------------------------------------------------------------------------
# Group B — fatal error at extraction / RAG / rendering; correct step and
# code; subsequent operations never called; an already-computed result
# survives a rendering failure; a normal LLM fallback stays exploitable.
# ---------------------------------------------------------------------------

def test_extraction_fatal_error_stops_before_rag_and_reports_the_right_code(client, db, monkeypatch):
    import src.agents.ao_extractor as ao_extractor_module
    from src.rag import private_rag_manager

    def _raise_extraction(self, text):
        raise RuntimeError("extraction boom")
    monkeypatch.setattr(ao_extractor_module.AOExtractor, "extract", _raise_extraction)

    search_calls = []
    monkeypatch.setattr(private_rag_manager, "search", lambda db, **kw: search_calls.append(1) or [])

    csrf = _configure_account(client, db, "extractionfatal@example.com")
    job = _run_analysis_to_terminal(client, VALID_AO_TEXT, csrf)

    assert job.status == "error"
    assert job.error_code == "extraction_failed"
    assert search_calls == [], "RAG search must never run after a fatal extraction error"
    assert "extraction boom" not in (job.error or "")


def test_rag_fatal_error_stops_before_scoring_and_reports_the_right_code(client, db, monkeypatch):
    from src.rag import private_rag_manager
    from src.agents.scoring_engine import ScoringEngine

    monkeypatch.setattr(private_rag_manager, "search", lambda db, **kw: (_ for _ in ()).throw(RuntimeError("rag boom")))

    score_calls = []
    original_score = ScoringEngine.score

    def _spy_score(self, *args, **kwargs):
        score_calls.append(1)
        return original_score(self, *args, **kwargs)
    monkeypatch.setattr(ScoringEngine, "score", _spy_score)

    csrf = _configure_account(client, db, "ragfatal@example.com")
    job = _run_analysis_to_terminal(client, VALID_AO_TEXT, csrf)

    assert job.status == "error"
    assert job.error_code == "rag_failed"
    assert score_calls == [], "scoring must never run after a fatal RAG error"
    assert "rag boom" not in (job.error or "")


def test_rendering_fatal_error_preserves_the_already_computed_result(client, db, monkeypatch):
    _install_no_op_search(monkeypatch)
    monkeypatch.setattr(jobs, "_generate_documents", lambda job, ao, result: (_ for _ in ()).throw(RuntimeError("render boom")))

    csrf = _configure_account(client, db, "renderfatal@example.com")
    job = _run_analysis_to_terminal(client, VALID_AO_TEXT, csrf)

    assert job.status == "error"
    assert job.error_code == jobs.DOCUMENT_GENERATION_FAILED_ERROR_CODE
    assert "render boom" not in (job.error or "")
    # The core of ticket section 4: a rendering failure must NEVER erase an
    # already-valid, already-computed analysis.
    assert job.ao is not None
    assert job.result is not None
    # B06-T4: this test's own draft configures no business_rules, so the
    # engine's real, honest verdict for this AO/capacity may legitimately
    # be "INCOMPLET" rather than a GO/NO-GO — the point of this assertion
    # is that a REAL, already-computed value survived the rendering
    # failure, not which of the four values it is.
    assert job.result.decision in ("GO", "GO SOUS RESERVE", "NO-GO", "INCOMPLET")
    assert job.files == {}, "no file is ever announced as available when none was produced"


def test_normal_llm_disabled_fallback_still_completes_successfully(client, db, monkeypatch):
    """A normal, already-handled LLM fallback (B05/B06/B18) must never be
    converted into a fatal job failure by this ticket's broader
    try/except wrapping."""
    from src.core import config
    monkeypatch.setattr(config, "LLM_ENABLED", False)
    _install_no_op_search(monkeypatch)
    csrf = _configure_account(client, db, "normalfallback@example.com")
    job = _run_analysis_to_terminal(client, VALID_AO_TEXT, csrf)

    assert job.status == "done"
    assert job.error is None
    assert job.result is not None


# ---------------------------------------------------------------------------
# Group C — failing save: no fake durable success, no reversion to
# running; real status response for the owner, no raw detail leak;
# a third party's access is still refused.
# ---------------------------------------------------------------------------

def test_persistence_failure_keeps_job_done_and_signals_the_failure_distinctly(client, db, monkeypatch):
    _install_no_op_search(monkeypatch)
    monkeypatch.setattr(jobs, "_persist", lambda job: (_ for _ in ()).throw(OSError("disk full")))

    csrf = _configure_account(client, db, "persistfail@example.com")
    job = _run_analysis_to_terminal(client, VALID_AO_TEXT, csrf)

    assert job.status == "done", "the analysis itself succeeded — a storage hiccup is not the same failure class"
    assert job.error_code == jobs.PERSISTENCE_FAILED_ERROR_CODE
    assert "disk full" not in (job.error or "")
    assert job.result is not None
    assert job.ao is not None


MARKER = "SYNTHETIC-SENTINEL-MARKER-7f3a9c"


def test_owner_status_response_never_leaks_raw_exception_detail(client, db, monkeypatch):
    _install_no_op_search(monkeypatch)
    monkeypatch.setattr(jobs, "_generate_documents", lambda job, ao, result: (_ for _ in ()).throw(RuntimeError(MARKER)))

    csrf = _configure_account(client, db, "noleak@example.com")
    r = client.post("/api/analyze", data={"mode": "paste", "text": VALID_AO_TEXT}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]

    status_body = None
    for _ in range(50):
        status_r = client.get(f"/api/analyze/{job_id}/status")
        assert status_r.status_code == 200
        status_body = status_r.json()
        if status_body["status"] != "running":
            break
        time.sleep(0.1)

    assert status_body["status"] == "error"
    assert MARKER not in str(status_body), "the raw exception message must never reach the public API response"
    assert status_body["error_code"] == jobs.DOCUMENT_GENERATION_FAILED_ERROR_CODE


def test_third_party_cannot_read_another_owners_job_status(client, db, monkeypatch):
    _install_no_op_search(monkeypatch)
    csrf = _configure_account(client, db, "statusowner@example.com")
    job = _run_analysis_to_terminal(client, VALID_AO_TEXT, csrf)

    make_active_starter_user(db, "statusintruder@example.com", scoring=False)
    _login(client, "statusintruder@example.com")
    r = client.get(f"/api/analyze/{job.id}/status")
    assert r.status_code == 404, "a job belonging to another account must be indistinguishable from one that doesn't exist"
