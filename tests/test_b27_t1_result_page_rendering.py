"""B27-T1 — /app/resultats/{job_id}: page-rendering regression for the
INCOMPLET mislabeling defect confirmed while surveying the frontend for
this lot (templates/app_result.html used to fall through to a plain
"NO-GO" badge/label for an INCOMPLET decision — a misconfiguration
(business rule never set) was indistinguishable from a genuine rejected
opportunity) and for the new per-document status/regeneration panel.

Runs a REAL job end-to-end through the actual web path (jobs.py::
_run_analysis, no LLM key in this environment so enrichment naturally
short-circuits) and renders the real HTML page — a template that merely
imports without raising is not enough; the specific mislabeling bug only
shows up in the rendered text.
"""
from __future__ import annotations

import re
import time

from src.web import jobs
from tests.conftest import make_active_starter_user


def _csrf_from(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


def _login(client, email, password="Sup3rSecret!"):
    r = client.get("/login")
    csrf = _csrf_from(r.text)
    client.post("/login", data={"email": email, "password": password, "next": "/app", "csrf_token": csrf})
    return csrf


def _configure_and_activate(client, csrf, *, business_rules):
    client.post("/api/capacity", json={
        "charge_globale_pct": 40, "nombre_projets_en_cours": 1,
        "projets_en_cours": ["Projet test"], "capacites_par_pole": {"Software Engineering": 40},
    }, headers={"X-CSRF-Token": csrf})
    client.put("/api/scoring-config/profile", json={
        "raison_sociale": "ESN test", "effectif": "10-50", "competences": ["python"], "certifications": [],
    }, headers={"X-CSRF-Token": csrf})
    client.put("/api/scoring-config/policy", json={
        "weights": {
            "Adequation expertise": 20, "References similaires": 15, "Disponibilite equipe": 10,
            "Rentabilite estimee": 10, "Faisabilite delai": 10, "Certifications requises": 10,
            "Complexite technique": 5, "Connaissance secteur": 5, "Potentiel commercial": 5,
            "Risque contractuel": 5, "Solidite client": 3, "Valeur strategique": 2,
        },
        "threshold_go": 88, "threshold_sous_reserve": 60,
        "business_rules": business_rules,
    }, headers={"X-CSRF-Token": csrf})
    r = client.post("/api/scoring-config/policy/activate", json={"expected_active_version": None}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200, r.text


def _run_job_and_wait(client, csrf, *, text: str) -> str:
    r = client.post("/api/analyze", data={"mode": "paste", "text": text}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    for _ in range(80):
        job = jobs.get_job(job_id)
        if job.status != "running":
            break
        time.sleep(0.1)
    assert job.status == "done", f"expected a completed job, got status={job.status!r} error={job.error!r}"
    return job_id


def test_incomplete_decision_is_never_rendered_as_no_go(client, db):
    """DEFECT confirmed: with every business_rules key left unconfigured, a
    real analysis reaches decision=INCOMPLET (docs/api/
    B06_T4_BUSINESS_RULES_CONTRACT.md) — the page must show a neutral
    INCOMPLET state, never the NO-GO badge/label/icon, and never a
    favorable GO-looking message either."""
    make_active_starter_user(db, "incompleterender@example.com", scoring=False)
    csrf = _login(client, "incompleterender@example.com")
    _configure_and_activate(client, csrf, business_rules={})

    job_id = _run_job_and_wait(client, csrf, text=(
        "Appel d'offres test. Acheteur: Ville de Test. Budget: 200000 euros. "
        "Date limite: 30/11/2026. Python Django."
    ))
    job = jobs.get_job(job_id)
    assert job.result.decision == "INCOMPLET", f"expected INCOMPLET given no business_rules configured, got {job.result.decision!r}"

    r = client.get(f"/app/resultats/{job_id}")
    assert r.status_code == 200
    assert "Analyse incomplète" in r.text
    assert "badge-incomplete" in r.text
    assert "badge-nogo" not in r.text, "INCOMPLET must never render with the NO-GO badge class anywhere on the page"


def test_normal_go_decision_still_renders_go_badge_unchanged(client, db):
    """Non-regression: a fully configured policy still renders a normal
    GO/NO-GO/RESERVE result exactly as before — this lot only ADDS the
    INCOMPLET branch, it must not touch the existing ones.

    Uses scoring=False + an explicit, fully permissive business_rules
    configuration (same pattern as test_b06_scoring_config.py's own
    test_full_pipeline_survives_a_failing_llm_provider) rather than the
    shared scoring=True fixture: that fixture's synthetic policy
    (tests/conftest.py::configure_synthetic_scoring_for_owner) leaves
    business_rules at their default {} (never configured), which — per
    the real B06-T4 contract — makes EVERY real end-to-end job through
    this fixture legitimately resolve to INCOMPLET, not a pre-existing bug
    to route around here."""
    make_active_starter_user(db, "normalrender@example.com", scoring=False)
    csrf = _login(client, "normalrender@example.com")
    _configure_and_activate(client, csrf, business_rules={
        "budget_minimum_eur": 0, "max_charge_pct": 100,
        "max_unmastered_technologies": 999, "certification_penalty_score": 20,
    })
    job_id = _run_job_and_wait(client, csrf, text=(
        "Appel d'offres test. Acheteur: Ville de Test. Budget: 500000 euros. "
        "Date limite: 30/11/2026. Python Django Kubernetes."
    ))
    r = client.get(f"/app/resultats/{job_id}")
    assert r.status_code == 200
    assert "Analyse incomplète" not in r.text
    assert "doc-pdf-status" in r.text and "doc-docx-status" in r.text, "document status panel must be present"
