"""Lot 48 — targeted regressions for the audit's confirmed findings.

1. `disponibilite_minimum_pct` (the private threshold that decides "Disponibilité de l'équipe") was exposed by
   the API since B08-T1 but had NO field anywhere in the UI: every account silently got the schema's default
   (10 %), with no way to see or change it — a hidden business hypothesis, not a configured one. Fixed here:
   the capacity modal (`templates/app_analyze.html`, `static/js/analyze.js`) now shows and sends it.
2. `available_criteria` / `_IT_WEIGHTS_TEMPLATE` (an inert "modèle informatique" weight template) was confirmed
   dead code — already flagged unused in lot 44's own report, verified again here by grepping every static
   script, template and test for a reader before removal. Removed from both HTTP responses and the template.

Real HTTP paths, a real ScoringEngine, real private repositories — nothing about scoring is simulated.
"""
from __future__ import annotations

import re

from tests.conftest import make_active_starter_user
from tests.test_lot44_criteria_contract import _capacity, _login, _put, crit

PROFILE = {"raison_sociale": "Compte de test (lot 48)", "competences": [], "certifications": []}


def _activate_capacity_only_policy(client, csrf):
    """A policy whose ENTIRE weight rests on `capacity_availability` — isolates the effect of the private
    threshold on the decision, without any other criterion able to compensate or mask it."""
    assert _put(client, csrf, "/api/scoring-config/profile", PROFILE).status_code == 200
    criteria = [crit("dispo", "capacity_availability",
                     {"available_score": 100, "unavailable_score": 0, "max_charge_blocking": None}, 100,
                     label="Disponibilité de l'équipe")]
    assert _put(client, csrf, "/api/scoring-config/policy", {"criteria": criteria, "threshold_go": 80, "threshold_sous_reserve": 50}).status_code == 200
    assert client.post("/api/scoring-config/policy/activate", json={"expected_active_version": None}, headers={"X-CSRF-Token": csrf}).status_code == 200


def _set_capacity(client, csrf, *, charge, seuil):
    r = client.post("/api/capacity", json={
        "charge_globale_pct": charge, "nombre_projets_en_cours": 1, "projets_en_cours": ["Projet test"],
        "capacites_par_pole": {}, "disponibilite_minimum_pct": seuil,
    }, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200, r.text
    return r.json()


def _analyze(client, csrf):
    r = client.post("/api/analyze", data={"mode": "paste", "text": "Appel d'offres de test. Acheteur : Client."}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200, r.text
    from src.web import jobs
    import time
    job_id = r.json()["job_id"]
    for _ in range(300):
        job = jobs.get_job(job_id)
        if job.status != "running":
            break
        time.sleep(0.05)
    assert job.status == "done", (job.error, job.error_code)
    return job.result


# ---------------------------------------------------------------------------
# 1 — the capacity threshold is now a real, effective, account-owned parameter
# ---------------------------------------------------------------------------

def test_get_capacity_reflects_the_saved_threshold_not_a_silent_constant(client, db):
    make_active_starter_user(db, "l48-cap-get@example.com", scoring=False, capacity=False)
    csrf = _login(client, "l48-cap-get@example.com")
    assert client.get("/api/capacity").json()["disponibilite_minimum_pct"] == 10, "unconfigured default stays 10 (compatibility)"
    saved = _set_capacity(client, csrf, charge=20, seuil=42)
    assert saved["disponibilite_minimum_pct"] == 42
    assert client.get("/api/capacity").json()["disponibilite_minimum_pct"] == 42, "the saved value is read back, never reset to the default"


def test_the_same_capacity_gives_a_different_decision_depending_on_the_accounts_own_threshold(client, db, monkeypatch):
    """Same declared load (75 % charge -> 25 % remaining) — only the account's OWN threshold changes: at 10 %
    the team is available (GO), at 30 % it is not (NO-GO). Before the fix this threshold could never be
    anything but the invisible schema default; the account could not have produced this second outcome."""
    from src.rag import private_rag_manager
    monkeypatch.setattr(private_rag_manager, "search", lambda db_, **kw: [])
    make_active_starter_user(db, "l48-cap-decides@example.com", scoring=False, capacity=False)
    csrf = _login(client, "l48-cap-decides@example.com")
    _activate_capacity_only_policy(client, csrf)

    _set_capacity(client, csrf, charge=75, seuil=10)
    lenient = _analyze(client, csrf)
    assert lenient.decision == "GO" and lenient.score_global == 100.0

    _set_capacity(client, csrf, charge=75, seuil=30)
    strict = _analyze(client, csrf)
    assert strict.decision == "NO-GO" and strict.score_global == 0.0
    assert "disponible" in strict.criteres[0].justification.lower() or "sous tension" in strict.criteres[0].justification.lower()


def test_the_capacity_form_now_shows_and_sends_the_threshold_field(client, db):
    """Structural regression on the actual served page/script — this is exactly the gap the audit found: the
    field existed nowhere between `GET/POST /api/capacity` (which always supported it) and the account."""
    make_active_starter_user(db, "l48-cap-ui@example.com", scoring=False, capacity=False)
    _login(client, "l48-cap-ui@example.com")
    page = client.get("/app/analyser").text
    assert 'id="cap-min-dispo"' in page and '<label for="cap-min-dispo">' in page
    assert "Seuil minimum de disponibilité" in page
    script = open("static/js/analyze.js", encoding="utf-8").read()
    assert "disponibilite_minimum_pct" in script
    assert re.search(r'disponibilite_minimum_pct:\s*Number\(document\.getElementById\("cap-min-dispo"\)\.value\)', script)


# ---------------------------------------------------------------------------
# 2 — confirmed dead code removed (available_criteria / _IT_WEIGHTS_TEMPLATE)
# ---------------------------------------------------------------------------

def test_available_criteria_is_gone_from_every_surface_that_used_to_carry_it(client, db):
    make_active_starter_user(db, "l48-dead-code@example.com", scoring=False, capacity=False)
    csrf = _login(client, "l48-dead-code@example.com")
    state = client.get("/api/scoring-config").json()
    assert "available_criteria" not in state, "confirmed dead (lot 44's own report, zero reader in static/js, templates or tests)"
    assert "criteria_templates" in state and "criteria_catalogue" in state, "the REAL, form-driven proposed draft stays"
    page = client.get("/app/parametres").text
    assert "WM_AVAILABLE_CRITERIA" not in page
    assert "WM_CRITERIA_TEMPLATES" in page


def test_removing_the_dead_template_does_not_touch_the_engines_own_criteria_labels(client, db):
    """`ScoringEngine.labels`/`label_display` (used by legacy-format validation, `scoring_policy_validation.
    CRITERIA_KEYS`) are NOT the thing that was removed — only the inert `_IT_WEIGHTS_TEMPLATE` derived from
    them for the dead `available_criteria` payload."""
    from src.agents.scoring_engine import ScoringEngine
    assert len(ScoringEngine.labels) == 12 and len(ScoringEngine.label_display) == 12
    import src.web.routes_scoring_policy as routes_scoring_policy
    assert not hasattr(routes_scoring_policy, "AVAILABLE_CRITERIA")
    assert not hasattr(routes_scoring_policy, "_IT_WEIGHTS_TEMPLATE")


# ---------------------------------------------------------------------------
# 3 — spot-check: the audit's other suspects are confirmed NOT hidden defaults (documented, not "fixed")
# ---------------------------------------------------------------------------

def test_a_new_policy_still_starts_with_zero_criteria_and_no_capacity_plan_is_ever_invented(client, db):
    """Sanity re-check (lot 43/44 contract, still true): nothing in this lot's changes creates an implicit
    profile, threshold or capacity plan for a fresh account."""
    make_active_starter_user(db, "l48-empty@example.com", scoring=False, capacity=False)
    csrf = _login(client, "l48-empty@example.com")
    state = client.get("/api/scoring-config").json()
    assert state["policy"]["active"] is None and state["policy"]["draft"] is None
    assert client.get("/api/capacity").json()["status"] == "unconfigured"
    r = client.post("/api/analyze", data={"mode": "paste", "text": "Appel d'offres."}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 409 and r.json()["detail"]["error_code"] == "CAPACITY_NOT_CONFIGURED"


def test_legacy_compatibility_evaluators_are_still_excluded_from_the_new_criterion_picker(client, db):
    """`legacy_client_solvency_v1`/`legacy_tight_deadline_v1`/... carry a documented, ALWAYS-VISIBLE assumption
    for compatibility (B06 contract, "Compléments du lot 44") — confirmed still restricted to migrated legacy
    policies, never offered as a choice for a brand new criterion (static/js/parametres.js filters `family !=
    "legacy"` for #new-criterion-evaluator). Not a defect of this lot; recorded as an audited, intentional
    reliquat in RAPPORT_LOT_48.md, not silently left unexamined."""
    from src.agents import criteria_catalogue
    script = open("static/js/parametres.js", encoding="utf-8").read()
    assert 'filter(([, d]) => d.family !== "legacy")' in script
    assert criteria_catalogue.EVALUATORS["legacy_client_solvency_v1"]["family"] == "legacy"
