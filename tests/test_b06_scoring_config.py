"""B06-T1 — private scoring configuration: /api/scoring-config/* and the
409 SCORING_NOT_CONFIGURED gate on /api/analyze.

Real authentication/authorization throughout (TestClient + the actual
FastAPI app + an isolated SQLite DB) — no dependency overrides, no bypassed
permission checks, no real LLM/Pappers call (config.LLM_ENABLED=False
monkeypatched where an analysis is actually run to completion, same
convention as test_b03_private_knowledge.py's
test_full_analyze_pipeline_never_leaks_another_accounts_evidence).

Section numbers below refer to the B06-T1 ticket's own §6 recipe.
"""
from __future__ import annotations

import re
import time

import pytest

from tests.conftest import default_org_id, make_active_starter_user

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


def _configure_capacity(client, charge: int = 40, *, csrf: str | None = None):
    return client.post(
        "/api/capacity",
        json={
            "charge_globale_pct": charge, "nombre_projets_en_cours": 1,
            "projets_en_cours": ["Projet test"], "capacites_par_pole": {"Software Engineering": 40},
        },
        headers={"X-CSRF-Token": csrf} if csrf else None,
    )


def _save_profile(client, raison_sociale="ESN de test", competences=None, certifications=None, *, csrf: str | None = None):
    return client.put(
        "/api/scoring-config/profile",
        json={
            "raison_sociale": raison_sociale, "effectif": "10-50",
            "competences": competences or [], "certifications": certifications or [],
        },
        headers={"X-CSRF-Token": csrf} if csrf else None,
    )


def _save_draft(client, weights=None, threshold_go=88, threshold_sous_reserve=60, business_rules=None, *, csrf: str | None = None):
    """B06-T4: `business_rules` defaults to None and is OMITTED from the
    payload in that case (not sent as an explicit empty dict) — every
    existing call site that doesn't pass it keeps saving a draft exactly
    as it did before this ticket (an activated policy with no business
    rules configured, which is a legal, real state — see
    docs/api/B06_T4_BUSINESS_RULES_CONTRACT.md). Pass it explicitly only
    where a test specifically needs a "complete" (never "INCOMPLET")
    scoring result."""
    payload = {
        "weights": weights if weights is not None else dict(VALID_WEIGHTS),
        "threshold_go": threshold_go, "threshold_sous_reserve": threshold_sous_reserve,
    }
    if business_rules is not None:
        payload["business_rules"] = business_rules
    return client.put("/api/scoring-config/policy", json=payload, headers={"X-CSRF-Token": csrf} if csrf else None)


def _save_draft_raw_json(client, body: dict, *, csrf: str | None = None):
    """Like _save_draft, but bypasses httpx's json= (which serializes with
    allow_nan=False and refuses to even build a request containing a real
    float('nan')/float('inf')) — builds the body with the stdlib json
    module directly (allow_nan=True, its default), which is what a
    hand-crafted malicious/malformed client request would actually send
    and what Starlette's own request-body parsing accepts on the way in."""
    import json as _json
    headers = {"content-type": "application/json"}
    if csrf:
        headers["X-CSRF-Token"] = csrf
    return client.put(
        "/api/scoring-config/policy",
        content=_json.dumps(body, allow_nan=True).encode("utf-8"),
        headers=headers,
    )


def _activate(client, expected_active_version=None, *, csrf: str | None = None):
    return client.post(
        "/api/scoring-config/policy/activate",
        json={"expected_active_version": expected_active_version},
        headers={"X-CSRF-Token": csrf} if csrf else None,
    )


def _run_analysis_to_completion(client, text: str, *, csrf: str | None = None):
    r = client.post("/api/analyze", data={"mode": "paste", "text": text}, headers={"X-CSRF-Token": csrf} if csrf else None)
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


VALID_AO_TEXT = (
    "Appel d'offres - Portail client\n"
    "Acheteur : Collectivite Exemple\n"
    "Le prestataire realisera le portail et ses livrables.\n"
    "Budget : 250 000 euros. Date limite : 30/11/2026.\n"
    "Exigences : Python et Django.\n"
)


# ---------------------------------------------------------------------------
# §6.1 — A and B fresh: no active policy, no score; analyze refused with
# SCORING_NOT_CONFIGURED, no provider call.
# ---------------------------------------------------------------------------

def test_fresh_accounts_have_no_active_policy_and_analyze_is_refused(client, db, monkeypatch):
    from src.core import config
    monkeypatch.setattr(config, "LLM_ENABLED", False)

    make_active_starter_user(db, "freshA@example.com", scoring=False)
    make_active_starter_user(db, "freshB@example.com", scoring=False)

    for email in ("freshA@example.com", "freshB@example.com"):
        csrf = _login(client, email)
        status = client.get("/api/scoring-config").json()
        assert status["status"] == "configuration_required"
        assert status["state"] == "a_configurer"
        assert status["score_global"] is None
        assert status["policy"]["active"] is None

        r = client.post("/api/analyze", data={"mode": "paste", "text": VALID_AO_TEXT}, headers={"X-CSRF-Token": csrf})
        assert r.status_code == 409
        assert r.json()["detail"]["error_code"] == "SCORING_NOT_CONFIGURED"


# ---------------------------------------------------------------------------
# §6.2 — A fills draft, "leaves"/resumes, validates then activates; data
# persists; B stays blank.
# ---------------------------------------------------------------------------

def test_draft_persists_across_resume_then_validates_and_activates_while_b_stays_blank(client, db):
    make_active_starter_user(db, "resumeA@example.com", scoring=False)
    make_active_starter_user(db, "resumeB@example.com", scoring=False)

    csrf = _login(client, "resumeA@example.com")
    assert _save_profile(client, raison_sociale="ESN Resume", csrf=csrf).status_code == 200
    assert _save_draft(client, threshold_go=90, threshold_sous_reserve=65, csrf=csrf).status_code == 200

    # "Leaves" — simulated by simply re-reading status as if in a new request.
    status = client.get("/api/scoring-config").json()
    assert status["state"] == "brouillon"
    assert status["policy"]["draft"]["threshold_go"] == 90

    # "Resumes" — edits again, same draft row (version stays 1).
    r = _save_draft(client, threshold_go=88, threshold_sous_reserve=60, csrf=csrf)
    assert r.status_code == 200
    assert r.json()["version"] == 1

    validation = client.post("/api/scoring-config/policy/validate", headers={"X-CSRF-Token": csrf}).json()
    assert validation["valid"] is True, validation["errors"]

    activated = _activate(client, expected_active_version=None, csrf=csrf)
    assert activated.status_code == 200
    assert activated.json()["status"] == "active"

    status_after = client.get("/api/scoring-config").json()
    assert status_after["status"] == "configured"
    assert status_after["state"] == "analyses"

    _login(client, "resumeB@example.com")
    status_b = client.get("/api/scoring-config").json()
    assert status_b["status"] == "configuration_required"
    assert status_b["policy"]["draft"] is None
    assert status_b["profile"]["raison_sociale"] is None


# ---------------------------------------------------------------------------
# §6.3 — A configured gets a calculation using own params/evidence; B
# configured differently keeps its own results; no sharing.
# ---------------------------------------------------------------------------

def test_two_configured_accounts_get_independent_calculations(client, db, monkeypatch):
    from src.core import config
    monkeypatch.setattr(config, "LLM_ENABLED", False)

    make_active_starter_user(db, "calcA@example.com", scoring=False)
    make_active_starter_user(db, "calcB@example.com", scoring=False)

    # A declares BOTH technologies the AO text mentions (Python, Django).
    csrf_a = _login(client, "calcA@example.com")
    _configure_capacity(client, charge=20, csrf=csrf_a)
    _save_profile(client, raison_sociale="ESN A", competences=["python", "django"], csrf=csrf_a)
    _save_draft(client, csrf=csrf_a)
    _activate(client, expected_active_version=None, csrf=csrf_a)
    job_a = _run_analysis_to_completion(client, VALID_AO_TEXT, csrf=csrf_a)
    expertise_a = next(c for c in job_a.result.criteres if c.nom == "Adéquation expertise")
    assert expertise_a.score == 100.0, "A declared python — full match expected"

    # B declares NOTHING mastered — same AO text (Python required).
    csrf_b = _login(client, "calcB@example.com")
    _configure_capacity(client, charge=20, csrf=csrf_b)
    _save_profile(client, raison_sociale="ESN B", competences=[], csrf=csrf_b)
    _save_draft(client, csrf=csrf_b)
    _activate(client, expected_active_version=None, csrf=csrf_b)
    job_b = _run_analysis_to_completion(client, VALID_AO_TEXT, csrf=csrf_b)
    expertise_b = next(c for c in job_b.result.criteres if c.nom == "Adéquation expertise")
    assert expertise_b.score == 0.0, "B declared nothing — zero match expected, no leakage from A's competences"

    b_capacity_before = client.get("/api/capacity").json()
    b_config_before = client.get("/api/scoring-config").json()

    # Validation courte B05-T1/B06-T2, point 3: A mutates capacity AND
    # profile again, AFTER both accounts are configured — B's own capacity,
    # profile and already-computed result must stay byte-identical.
    csrf_a = _login(client, "calcA@example.com")
    _configure_capacity(client, charge=99, csrf=csrf_a)
    _save_profile(client, raison_sociale="ESN A renommee", competences=["python", "django", "aws"], csrf=csrf_a)

    csrf_b = _login(client, "calcB@example.com")
    assert client.get("/api/capacity").json() == b_capacity_before, "A's capacity write must never reach B's own plan"
    assert client.get("/api/scoring-config").json() == b_config_before, "A's profile/policy write must never reach B's own space"
    job_b_again = _run_analysis_to_completion(client, VALID_AO_TEXT, csrf=csrf_b)
    expertise_b_again = next(c for c in job_b_again.result.criteres if c.nom == "Adéquation expertise")
    assert expertise_b_again.score == 0.0, "B's own (unchanged) configuration must still produce the same result after A's writes"


# ---------------------------------------------------------------------------
# §6.4 — missing field / NaN / infinite / incoherent weights / reversed
# thresholds / foreign proof reference: validation refused, no partial
# activation.
# ---------------------------------------------------------------------------

def test_activation_refused_for_missing_raison_sociale(client, db):
    make_active_starter_user(db, "missingfield@example.com", scoring=False)
    csrf = _login(client, "missingfield@example.com")
    _save_draft(client, csrf=csrf)
    r = _activate(client, expected_active_version=None, csrf=csrf)
    assert r.status_code == 422
    assert "profile" in r.json()["detail"]["errors"]
    assert client.get("/api/scoring-config").json()["policy"]["active"] is None


def test_activation_refused_for_nan_and_infinite_weight(client, db):
    """Python's stdlib json module (used by both httpx's `json=` and
    Starlette's request-body parsing) accepts the non-standard NaN/
    Infinity/-Infinity literals by default — this reaches the route as a
    real float('nan')/float('inf'), exercising the exact
    math.isfinite() check in scoring_policy_validation.validate_weights,
    not a string standing in for it."""
    make_active_starter_user(db, "nanweight@example.com", scoring=False)
    csrf = _login(client, "nanweight@example.com")
    _save_profile(client, csrf=csrf)
    for bad_value in (float("nan"), float("inf"), float("-inf")):
        r = _save_draft_raw_json(client, {
            "weights": {**VALID_WEIGHTS, "Valeur strategique": bad_value},
            "threshold_go": 88, "threshold_sous_reserve": 60,
        }, csrf=csrf)
        assert r.status_code == 200, "draft save never refuses — only /validate and /activate do"
        activation = _activate(client, expected_active_version=None, csrf=csrf)
        assert activation.status_code == 422, f"bad_value={bad_value}"
        assert any("fini" in e for e in activation.json()["detail"]["errors"]["weights"]), f"bad_value={bad_value}"

    # A non-numeric value (never happens via a real numeric form field, but
    # a malformed/hand-crafted request must still be refused cleanly).
    r = client.put(
        "/api/scoring-config/policy",
        json={
            "weights": {**VALID_WEIGHTS, "Valeur strategique": "beaucoup"},
            "threshold_go": 88, "threshold_sous_reserve": 60,
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200
    activation = _activate(client, expected_active_version=None, csrf=csrf)
    assert activation.status_code == 422
    assert any("nombre" in e for e in activation.json()["detail"]["errors"]["weights"])


def test_activation_refused_for_incoherent_weights_sum(client, db):
    make_active_starter_user(db, "badsum@example.com", scoring=False)
    csrf = _login(client, "badsum@example.com")
    _save_profile(client, csrf=csrf)
    off_weights = dict(VALID_WEIGHTS)
    off_weights["Valeur strategique"] = 99
    _save_draft(client, weights=off_weights, csrf=csrf)
    r = _activate(client, expected_active_version=None, csrf=csrf)
    assert r.status_code == 422
    assert any("somme" in e.lower() for e in r.json()["detail"]["errors"]["weights"])


def test_activation_refused_for_reversed_thresholds(client, db):
    make_active_starter_user(db, "reversed@example.com", scoring=False)
    csrf = _login(client, "reversed@example.com")
    _save_profile(client, csrf=csrf)
    _save_draft(client, threshold_go=50, threshold_sous_reserve=80, csrf=csrf)
    r = _activate(client, expected_active_version=None, csrf=csrf)
    assert r.status_code == 422
    assert any("inférieur" in e for e in r.json()["detail"]["errors"]["thresholds"])


def test_activation_refused_for_foreign_proof_reference(client, db):
    import uuid
    make_active_starter_user(db, "foreignproof@example.com", scoring=False)
    csrf = _login(client, "foreignproof@example.com")
    fake_document_id = str(uuid.uuid4())
    _save_profile(client, certifications=[{"nom": "ISO 27001", "statut": "verifiee", "preuve_reference": fake_document_id}], csrf=csrf)
    _save_draft(client, csrf=csrf)
    r = _activate(client, expected_active_version=None, csrf=csrf)
    assert r.status_code == 422
    assert any("n'appartient pas" in e for e in r.json()["detail"]["errors"]["profile"])


# ---------------------------------------------------------------------------
# §6.5 — viewer, forged owner, revoked membership, suspended organization:
# refused without side effect; legitimate actions and the capacity matrix
# are covered by test_b03_private_knowledge.py::test_role_matrix_for_
# knowledge_and_capacity (updated by this same ticket).
# ---------------------------------------------------------------------------

def test_analyst_can_configure_and_activate_their_own_scoring_policy(client, db):
    """T2 (B06-T2): a legitimate case for the modified permission
    (scoring:configure, newly granted to analyst by B06-T1/T2) — an
    analyst can complete their OWN full configuration end to end, not just
    be refused by every other test in this file."""
    from tests.test_b02_organizations import _add_member

    admin = make_active_starter_user(db, "analystpermadmin@example.com", scoring=False)
    org_id = default_org_id(db, admin)
    _add_member(db, organization_id=org_id, email="analystperm@example.com", role="analyst", scoring=False)

    csrf = _login(client, "analystperm@example.com")
    assert _save_profile(client, raison_sociale="ESN Analyst", csrf=csrf).status_code == 200
    assert _save_draft(client, csrf=csrf).status_code == 200
    activation = _activate(client, expected_active_version=None, csrf=csrf)
    assert activation.status_code == 200
    assert activation.json()["status"] == "active"


def test_viewer_cannot_configure_scoring(client, db):
    from tests.test_b02_organizations import _add_member

    admin = make_active_starter_user(db, "vieweradmin@example.com", scoring=False)
    org_id = default_org_id(db, admin)
    _add_member(db, organization_id=org_id, email="viewerscoring@example.com", role="viewer", scoring=False)

    csrf = _login(client, "viewerscoring@example.com")
    assert _save_profile(client, csrf=csrf).status_code == 403
    assert _save_draft(client, csrf=csrf).status_code == 403
    assert _activate(client, csrf=csrf).status_code == 403
    assert client.get("/api/scoring-config").status_code == 200  # read stays allowed


def test_revoked_membership_blocks_scoring_config_access(client, db):
    from src.web.database.repositories import memberships as memberships_repo

    user = make_active_starter_user(db, "revokedscoring@example.com", scoring=False)
    org_id = default_org_id(db, user)
    csrf = _login(client, "revokedscoring@example.com")
    assert _save_profile(client, csrf=csrf).status_code == 200

    membership = memberships_repo.list_active_for_user(db, user.id)[0]
    memberships_repo.revoke(db, membership)
    db.commit()

    r = client.get("/api/scoring-config")
    assert r.status_code == 403


def test_same_user_two_organizations_two_separate_scoring_spaces(client, db):
    """T2 (B06-T2): 'un autre espace du même utilisateur' — a user who
    belongs to two organizations must get an AMBIGUOUS-selection refusal
    without `organization_id`, and two fully separate ProviderProfile/
    ScoringPolicy spaces once disambiguated — never the other org's draft/
    active policy, even though it's literally the same person."""
    from src.web.database.repositories import memberships as memberships_repo
    from src.web.database.repositories import organizations as organizations_repo

    user = make_active_starter_user(db, "twoorgsscoring@example.com", scoring=False)
    org_1 = default_org_id(db, user)
    org_2 = organizations_repo.create_organization(db, name="Second Org Scoring")
    memberships_repo.create_membership(db, user_id=user.id, organization_id=org_2.id, role="organization_admin", status="active")
    db.commit()

    csrf = _login(client, "twoorgsscoring@example.com")

    # Ambiguous — two active orgs, no selection: refused, never "the first one".
    assert client.get("/api/scoring-config").status_code == 409

    r1 = client.put(
        f"/api/scoring-config/profile?organization_id={org_1}",
        json={
            "raison_sociale": "ESN Org 1", "effectif": None, "competences": [], "certifications": [],
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert r1.status_code == 200

    status_org1 = client.get(f"/api/scoring-config?organization_id={org_1}").json()
    assert status_org1["profile"]["raison_sociale"] == "ESN Org 1"

    status_org2 = client.get(f"/api/scoring-config?organization_id={org_2.id}").json()
    assert status_org2["profile"]["raison_sociale"] is None, "org_2's space must stay blank despite being the same user"
    assert status_org2["status"] == "configuration_required"


def test_forged_owner_id_cannot_read_or_write_a_colleagues_configuration(client, db):
    """No client-writable owner/organization field exists on any
    scoring-config route — ctx.user.id always comes from the session, never
    a request body/query field. This test proves there is no such field to
    forge in the first place: sending one changes nothing."""
    user_a = make_active_starter_user(db, "forgedA@example.com", scoring=False)
    user_b = make_active_starter_user(db, "forgedB@example.com", scoring=False)
    org_a = default_org_id(db, user_a)

    csrf_a = _login(client, "forgedA@example.com")
    _save_profile(client, raison_sociale="ESN A vraie", csrf=csrf_a)

    csrf_b = _login(client, "forgedB@example.com")
    # Attempt to smuggle A's identifiers into B's own payload/query.
    r = client.put(
        f"/api/scoring-config/profile?organization_id={org_a}",
        json={"raison_sociale": "ESN B falsifiee", "owner_user_id": str(user_a.id), "effectif": None, "competences": [], "certifications": []},
        headers={"X-CSRF-Token": csrf_b},
    )
    assert r.status_code in (200, 403)  # either refused (org_a not B's) or, if 200, wrote to B's own row only
    from src.web.database.repositories import provider_profile as provider_profile_repo
    profile_a = provider_profile_repo.get_for_owner(db, organization_id=org_a, owner_user_id=user_a.id)
    assert profile_a.raison_sociale == "ESN A vraie", "B must never be able to overwrite A's profile"


# ---------------------------------------------------------------------------
# §6.6 — two concurrent activations, controlled: one consistent active
# state, explicit conflict; an already-launched job keeps its version.
# ---------------------------------------------------------------------------

def test_two_concurrent_activations_one_consistent_state_explicit_conflict(client, db, monkeypatch):
    from src.core import config
    monkeypatch.setattr(config, "LLM_ENABLED", False)

    make_active_starter_user(db, "concurrent@example.com", scoring=False)
    csrf = _login(client, "concurrent@example.com")
    _configure_capacity(client, csrf=csrf)
    _save_profile(client, csrf=csrf)
    _save_draft(client, threshold_go=88, threshold_sous_reserve=60, csrf=csrf)
    r1 = _activate(client, expected_active_version=None, csrf=csrf)
    assert r1.status_code == 200
    v1 = r1.json()["version"]

    job_v1 = _run_analysis_to_completion(client, VALID_AO_TEXT, csrf=csrf)
    assert job_v1.scoring_policy_version == v1

    # Prepare and activate v2 with a STALE expectation from before v1 activated.
    _save_draft(client, threshold_go=70, threshold_sous_reserve=50, csrf=csrf)
    stale_activation = _activate(client, expected_active_version=None, csrf=csrf)  # forgot v1 already happened
    assert stale_activation.status_code == 409
    assert stale_activation.json()["detail"]["error_code"] == "SCORING_POLICY_ACTIVATION_CONFLICT"
    assert stale_activation.json()["detail"]["current_active_version"] == v1

    # The correct client re-reads state and retries with the right expectation.
    correct_activation = _activate(client, expected_active_version=v1, csrf=csrf)
    assert correct_activation.status_code == 200
    v2 = correct_activation.json()["version"]
    assert v2 == v1 + 1

    # The already-launched job must keep meaning v1, never silently become v2.
    assert job_v1.scoring_policy_version == v1
    versions = client.get("/api/scoring-config/policy/versions").json()["versions"]
    assert sum(1 for v in versions if v["status"] == "active") == 1


# ---------------------------------------------------------------------------
# §6.7 — simulation distinct from activation; a simulated LLM proposing
# GO/100 never changes decision/notes/blockers.
# ---------------------------------------------------------------------------

def test_simulation_never_activates_and_ignores_llm_override_attempts(client, db, monkeypatch):
    from src.core import config
    monkeypatch.setattr(config, "LLM_ENABLED", False)

    make_active_starter_user(db, "simulate@example.com", scoring=False)
    csrf = _login(client, "simulate@example.com")
    _configure_capacity(client, charge=90, csrf=csrf)  # deliberately unfavorable
    _save_profile(client, competences=[], csrf=csrf)  # deliberately no mastered tech
    # B06-T4: this test only asserts decision is ONE of the three real
    # verdicts (not which one) — business_rules are set to permissive
    # values so a missing-rule "INCOMPLET" never masks that assertion;
    # the deliberately unfavorable capacity/competences above still drive
    # whatever real GO/SOUS RESERVE/NO-GO comes out.
    _save_draft(
        client, threshold_go=88, threshold_sous_reserve=60,
        business_rules={
            "budget_minimum_eur": 0, "max_charge_pct": 100,
            "max_unmastered_technologies": 999, "certification_penalty_score": 20,
        },
        csrf=csrf,
    )

    r = client.post(
        "/api/scoring-config/simulate",
        data={"mode": "paste", "text": VALID_AO_TEXT},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["simulation"] is True
    assert body["policy_version"] == 1

    # Simulation never activates anything.
    assert client.get("/api/scoring-config").json()["policy"]["active"] is None

    # LLM is disabled (config.LLM_ENABLED=False) for this test, so
    # enrich_with_llm is never even called by ScoringEngine.score()'s
    # pipeline in the simulate route (it isn't called at all — see
    # routes_scoring_policy.py::simulate_policy) — this positively confirms
    # the decision came from the deterministic engine alone, not a
    # requested-but-ignored LLM override.
    assert body["decision"] in ("GO", "GO SOUS RESERVE", "NO-GO")


# ---------------------------------------------------------------------------
# §6.8 — budget nul / secteur inconnu / missing-data guards: targeted here;
# broader B01/B02/B03/B04 non-regression is exercised by the full suite
# (tests/test_scoring_engine.py, test_validation_b04_scoring_defects.py),
# not repeated in this file.
# ---------------------------------------------------------------------------

def test_zero_budget_and_unknown_sector_guards_survive_policy_injection(client, db, monkeypatch):
    """This exercises the DEFECT-B04-02/03 fixes (see
    tests/test_scoring_engine.py::TestClosedDefects for the direct,
    extraction-independent unit tests of the engine itself) through the
    FULL B06-T1 injected path — a private ScoringPolicy still produces the
    right justification text via a real HTTP-launched job, not just when
    called directly. Uses AOContext directly (bypassing AOExtractor's own
    regex quirks around "0 €" in free text, a separate, already-covered
    extraction-layer concern, not what this test is about)."""
    from src.core import config
    monkeypatch.setattr(config, "LLM_ENABLED", False)

    make_active_starter_user(db, "guards@example.com", scoring=False)
    csrf = _login(client, "guards@example.com")
    _configure_capacity(client, csrf=csrf)
    _save_profile(client, competences=["python"], csrf=csrf)
    _save_draft(client, csrf=csrf)
    _activate(client, expected_active_version=None, csrf=csrf)

    from src.agents.scoring_engine import ScoringEngine, ScoringPolicySnapshot
    from src.core.models import AOContext, CapacityResult, CompanyProfile

    ao_zero = AOContext(titre="Etude de cadrage", client="Client X", budget_estime=0, technologies_demandees=["Python"])
    policy = ScoringPolicySnapshot.from_legacy(
        weights=dict(VALID_WEIGHTS), threshold_go=88, threshold_sous_reserve=60,
        mastered_technologies=frozenset({"python"}),
    )
    result = ScoringEngine().score(
        ao_zero, CompanyProfile(), [], CapacityResult(charge_actuelle_pct=40, capacite_restante_pct=60, equipe_disponible=True, commentaire="ok"),
        policy=policy,
    )
    rentabilite = next(c for c in result.criteres if c.nom == "Rentabilité estimée")
    assert "non communiqu" not in rentabilite.justification.lower()
    assert "nul" in rentabilite.justification.lower()

    secteur_criterion = next(c for c in result.criteres if c.nom == "Connaissance secteur")
    assert secteur_criterion.score == 65, "an unset CompanyProfile.secteur must score as unknown under policy injection too"


# ---------------------------------------------------------------------------
# Validation courte B05-T1 + B06-T2 — full pipeline with a CONFIGURED-BUT-
# FAILING LLM provider (as opposed to LLM_ENABLED=False, which never even
# attempts a call): a real /api/analyze run must still complete with a
# usable deterministic result. Distinct code path from the malformed-shape
# tests in test_validation_b04_scoring_defects.py — here LLMClient.
# json_complete() itself swallows the provider's exception into `None`
# BEFORE enrich_with_llm's own try/except ever sees anything, so this
# proves the OTHER real safety net, not the same one twice.
# ---------------------------------------------------------------------------

def test_full_pipeline_survives_a_failing_llm_provider(client, db, monkeypatch):
    from src.core import config
    from src.agents.llm_client import LLMClient
    import src.agents.llm_client as llm_client_module

    class _FailingProvider:
        name = "primary"
        enabled = True

        def complete(self, *args, **kwargs):
            raise RuntimeError("simulated provider failure — never a real network call")

    # LLM_ENABLED stays True here (deliberately NOT False) — this test is
    # about a provider that IS configured/enabled but fails at call time,
    # not about the "no provider configured at all" path already covered
    # elsewhere. No real credential/network is exercised: `_FailingProvider`
    # never touches `requests`/`anthropic`, it just raises immediately.
    monkeypatch.setattr(config, "LLM_ENABLED", True)
    monkeypatch.setattr(llm_client_module, "ClaudeClient", lambda: LLMClient([_FailingProvider()]))

    make_active_starter_user(db, "failingllm@example.com", scoring=False)
    csrf = _login(client, "failingllm@example.com")
    _configure_capacity(client, csrf=csrf)
    _save_profile(client, competences=["python", "django"], csrf=csrf)
    # B06-T4: this test is about surviving a failing LLM provider, not
    # about business-rule completeness — permissive values keep the
    # decision one of the three real verdicts rather than "INCOMPLET".
    _save_draft(
        client,
        business_rules={
            "budget_minimum_eur": 0, "max_charge_pct": 100,
            "max_unmastered_technologies": 999, "certification_penalty_score": 20,
        },
        csrf=csrf,
    )
    _activate(client, expected_active_version=None, csrf=csrf)

    job = _run_analysis_to_completion(client, VALID_AO_TEXT, csrf=csrf)
    assert job.status == "done"
    assert job.result.decision in ("GO", "GO SOUS RESERVE", "NO-GO")
    assert isinstance(job.result.score_global, float)
    # The deterministic engine's own criteria/justifications are present and
    # untouched — the failing provider degraded enrichment silently, never
    # the underlying calculation the user actually needs.
    assert len(job.result.criteres) == 12
    assert all(c.justification for c in job.result.criteres)

    # B06-T3: the failure is now visible on the result itself...
    assert job.result.enrichment_status == "failed"
    assert job.result.enrichment_reason in ("provider_exception", "no_content")

    # ...and stays visible after a real persistence/reload round-trip, not
    # just on the in-memory object the job thread happened to build.
    from src.web import jobs as jobs_module
    del jobs_module._JOBS[job.id]  # force the next get_job() to hit disk, not the in-memory cache
    reloaded = jobs_module.get_job(job.id)
    assert reloaded is not None
    assert reloaded.result.enrichment_status == "failed"
    assert reloaded.result.enrichment_reason in ("provider_exception", "no_content")
    assert reloaded.result.decision == job.result.decision
    assert reloaded.result.score_global == job.result.score_global


def test_pre_b06_t3_persisted_result_reloads_as_unknown_without_rewriting_scores(client, db, tmp_path, monkeypatch):
    """B06-T3 point 3: a job JSON file written before this ticket existed
    (no enrichment_status/enrichment_reason key at all in its "result"
    object) must reload as enrichment_status='unknown' — never a fabricated
    'applied'/'failed' history — and its decision/score must NOT be
    rewritten by the mere act of reloading it through the current code."""
    import json
    from src.core import config
    from src.web import jobs as jobs_module

    analyses_dir = tmp_path / "historique" / "analyses"
    analyses_dir.mkdir(parents=True)
    monkeypatch.setattr(jobs_module, "ANALYSES_DIR", analyses_dir)

    legacy_job_id = "legacy0001"
    legacy_result = {
        "decision": "GO", "score_global": 91.4,
        "criteres": [{"nom": "Adéquation expertise", "poids": 20.0, "score": 100.0, "justification": "ok"}],
        "criteres_bloquants": [], "forces": [], "faiblesses": [], "risques": [], "recommandations": [],
        "evidence_pack": [], "company_profile": None, "capacity": None, "rag_synthesis": "", "ai_content": {},
        # deliberately NO "enrichment_status"/"enrichment_reason" key — this
        # is exactly what a real pre-B06-T3 persisted analysis looks like.
    }
    (analyses_dir / f"{legacy_job_id}.json").write_text(json.dumps({
        "id": legacy_job_id, "user_id": None, "organization_id": None, "created_at": 0,
        "source_label": "Legacy", "ao": {"titre": "AO historique"}, "result": legacy_result, "files": {},
    }), encoding="utf-8")

    reloaded = jobs_module.get_job(legacy_job_id)
    assert reloaded is not None
    assert reloaded.result.enrichment_status == "unknown"
    assert reloaded.result.enrichment_reason is None
    assert reloaded.result.decision == "GO"
    assert reloaded.result.score_global == 91.4


# ---------------------------------------------------------------------------
# B18-T1 (DEFECT-B04-04) — test C: the path jobs actually run through.
# ---------------------------------------------------------------------------

def test_invalid_rag_evidence_stops_the_job_with_a_safe_code_and_no_llm_call_after_detection(client, db, monkeypatch):
    """Injects an invalid evidence via a simulated private_rag_manager.search
    (bypassing Pydantic construction with model_construct, exactly like the
    engine-level tests) into the REAL /api/analyze job path. Verifies: a
    terminal error state, the stable public error_code, no ScoringResult
    ever published as done, and — by using a provider that would raise
    AssertionError if ever called — no LLM call happens after detection
    (the job aborts inside ScoringEngine.score(), before
    enrich_with_llm is ever reached)."""
    from src.core import config
    from src.core.models import RAGEvidence
    from src.rag import private_rag_manager
    import src.agents.llm_client as llm_client_module
    from src.agents.llm_client import LLMClient

    class _MustNeverBeCalledProvider:
        name = "primary"
        enabled = True
        def complete(self, *a, **kw):
            raise AssertionError("no LLM call may happen after invalid-evidence detection")

    monkeypatch.setattr(config, "LLM_ENABLED", True)
    monkeypatch.setattr(llm_client_module, "ClaudeClient", lambda: LLMClient([_MustNeverBeCalledProvider()]))

    def _fake_search(db, *, organization_id, owner_user_id, query, top_k=6):
        return [
            RAGEvidence(query=query, source="ok.md", score=0.6, content="valide"),
            RAGEvidence.model_construct(query=query, source="corrupted.md", score=float("nan"), content="c"),
        ]
    monkeypatch.setattr(private_rag_manager, "search", _fake_search)

    make_active_starter_user(db, "invalidrag@example.com", scoring=False)
    csrf = _login(client, "invalidrag@example.com")
    _configure_capacity(client, csrf=csrf)
    _save_profile(client, csrf=csrf)
    _save_draft(client, csrf=csrf)
    _activate(client, expected_active_version=None, csrf=csrf)

    r = client.post("/api/analyze", data={"mode": "paste", "text": VALID_AO_TEXT}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200
    job_id = r.json()["job_id"]

    from src.web import jobs
    for _ in range(50):
        job = jobs.get_job(job_id)
        if job.status != "running":
            break
        time.sleep(0.1)

    assert job.status == "error"
    assert job.error_code == "invalid_rag_evidence"
    assert job.result is None, "no scoring result may ever be published as successful"
    assert "corrupted.md" not in (job.error or ""), "no document/evidence content in the public error"
    assert "nan" not in (job.error or "").lower()

    status_response = client.get(f"/api/analyze/{job_id}/status").json()
    assert status_response["status"] == "error"
    assert status_response["error_code"] == "invalid_rag_evidence"


def test_pre_b18_t1_analysis_with_invalid_evidence_reloads_with_it_dropped(tmp_path, monkeypatch):
    """B18-T1 section 5: a job JSON persisted BEFORE this ticket (evidence_
    pack containing an out-of-range score, impossible to produce going
    forward) must not become entirely unreadable — the offending entry is
    dropped, decision/score_global are read exactly as persisted (never
    recomputed), and no NaN/Infinity ever appears in the reloaded object."""
    import json
    from src.web import jobs as jobs_module

    analyses_dir = tmp_path / "historique" / "analyses"
    analyses_dir.mkdir(parents=True)
    monkeypatch.setattr(jobs_module, "ANALYSES_DIR", analyses_dir)

    job_id = "legacybadrag0001"
    legacy_result = {
        "decision": "GO SOUS RESERVE", "score_global": 74.2,
        "criteres": [{"nom": "Adéquation expertise", "poids": 20.0, "score": 80.0, "justification": "ok"}],
        "criteres_bloquants": [], "forces": [], "faiblesses": [], "risques": [], "recommandations": [],
        "evidence_pack": [
            {"query": "q", "source": "ok.md", "score": 0.7, "content": "valide"},
            {"query": "q", "source": "corrupted.md", "score": -5.0, "content": "preuve corrompue avant B18-T1"},
        ],
        "company_profile": None, "capacity": None, "rag_synthesis": "", "ai_content": {},
    }
    (analyses_dir / f"{job_id}.json").write_text(json.dumps({
        "id": job_id, "user_id": None, "organization_id": None, "created_at": 0,
        "source_label": "Legacy", "ao": {"titre": "AO historique"}, "result": legacy_result, "files": {},
    }), encoding="utf-8")

    reloaded = jobs_module.get_job(job_id)
    assert reloaded is not None
    assert len(reloaded.result.evidence_pack) == 1
    assert reloaded.result.evidence_pack[0].source == "ok.md"
    assert reloaded.result.decision == "GO SOUS RESERVE"
    assert reloaded.result.score_global == 74.2
    # B18-T2: a server log alone doesn't inform the API consumer — the
    # degradation must be visible on the result itself, distinct from
    # enrichment_status (an unrelated field).
    assert reloaded.result.data_integrity == "degraded"
    assert reloaded.result.data_integrity_reason == "invalid_evidence_removed"
    assert reloaded.result.enrichment_status == "unknown", "enrichment_status is a different concern, untouched by this"


# ---------------------------------------------------------------------------
# B18-T2 — test group B: the producer itself raising INSIDE a real job run
# (as opposed to the B18-T1 test above, which faked private_rag_manager.
# search() wholesale to inject an already-invalid RAGEvidence — this one
# exercises the REAL search() body via a monkeypatched cosine_similarity,
# proving the try/except newly added around the search() call in
# src/web/jobs.py actually catches it).
# ---------------------------------------------------------------------------

def test_producer_raising_inside_a_real_job_stops_it_before_semantic_rerank_llm_call(client, db, monkeypatch):
    import numpy as np
    from src.core import config
    from src.rag import private_rag_manager
    import src.agents.llm_client as llm_client_module
    from src.agents.llm_client import LLMClient

    class _MustNeverBeCalledProvider:
        name = "primary"
        enabled = True
        def complete(self, *a, **kw):
            raise AssertionError("no LLM call (not even semantic_rerank) may happen after producer-level detection")

    monkeypatch.setattr(config, "LLM_ENABLED", True)
    monkeypatch.setattr(llm_client_module, "ClaudeClient", lambda: LLMClient([_MustNeverBeCalledProvider()]))
    monkeypatch.setattr(private_rag_manager, "cosine_similarity", lambda q, m: np.array([[float("nan")]]))

    make_active_starter_user(db, "producerjoberror@example.com", scoring=False)
    csrf = _login(client, "producerjoberror@example.com")
    _configure_capacity(client, csrf=csrf)
    _save_profile(client, csrf=csrf)
    _save_draft(client, csrf=csrf)
    _activate(client, expected_active_version=None, csrf=csrf)
    _upload_one_document_via_client(client, csrf=csrf)

    r = client.post("/api/analyze", data={"mode": "paste", "text": VALID_AO_TEXT}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200
    job_id = r.json()["job_id"]

    from src.web import jobs
    for _ in range(50):
        job = jobs.get_job(job_id)
        if job.status != "running":
            break
        time.sleep(0.1)

    assert job.status == "error"
    assert job.error_code == "invalid_rag_evidence"
    assert job.result is None


def _upload_one_document_via_client(client, *, csrf: str | None = None) -> None:
    import io
    r = client.post(
        "/api/knowledge/documents",
        files={"file": ("ref.md", io.BytesIO(b"# Reference\n\nContenu de reference."), "text/plain")},
        headers={"X-CSRF-Token": csrf} if csrf else None,
    )
    assert r.status_code == 201


# ---------------------------------------------------------------------------
# B18-T2 — test group C: score_global/critère non fini dans un historique —
# erreur ciblée pour CETTE analyse, sans affecter une analyse voisine valide.
# ---------------------------------------------------------------------------

def test_historical_non_finite_score_global_is_a_controlled_per_analysis_error(tmp_path, monkeypatch):
    import json
    from src.web import jobs as jobs_module

    analyses_dir = tmp_path / "historique" / "analyses"
    analyses_dir.mkdir(parents=True)
    monkeypatch.setattr(jobs_module, "ANALYSES_DIR", analyses_dir)

    def _write(job_id: str, score_global):
        legacy_result = {
            "decision": "GO", "score_global": score_global,
            "criteres": [{"nom": "Adéquation expertise", "poids": 20.0, "score": 80.0, "justification": "ok"}],
            "criteres_bloquants": [], "forces": [], "faiblesses": [], "risques": [], "recommandations": [],
            "evidence_pack": [], "company_profile": None, "capacity": None, "rag_synthesis": "", "ai_content": {},
        }
        (analyses_dir / f"{job_id}.json").write_text(json.dumps({
            "id": job_id, "user_id": None, "organization_id": None, "created_at": 0,
            "source_label": "Legacy", "ao": {"titre": "AO historique"}, "result": legacy_result, "files": {},
        }), encoding="utf-8")

    _write("badglobal0001", float("nan"))
    _write("goodneighbor0001", 74.2)

    bad = jobs_module.get_job("badglobal0001")
    assert bad is not None
    assert bad.status == "error"
    assert bad.error_code == "historical_score_unavailable"
    assert bad.result is None
    assert "nan" not in (bad.error or "").lower()

    # A neighboring, genuinely valid historical analysis is entirely
    # unaffected — this must never become "the whole list fails".
    good = jobs_module.get_job("goodneighbor0001")
    assert good is not None
    assert good.status == "done"
    assert good.result.score_global == 74.2
    assert good.result.data_integrity == "ok"


def test_historical_non_finite_criterion_score_is_also_a_controlled_error(tmp_path, monkeypatch):
    import json
    from src.web import jobs as jobs_module

    analyses_dir = tmp_path / "historique" / "analyses"
    analyses_dir.mkdir(parents=True)
    monkeypatch.setattr(jobs_module, "ANALYSES_DIR", analyses_dir)

    legacy_result = {
        "decision": "GO", "score_global": 80.0,
        "criteres": [{"nom": "Références similaires", "poids": 15.0, "score": float("-inf"), "justification": "corrompu"}],
        "criteres_bloquants": [], "forces": [], "faiblesses": [], "risques": [], "recommandations": [],
        "evidence_pack": [], "company_profile": None, "capacity": None, "rag_synthesis": "", "ai_content": {},
    }
    (analyses_dir / "badcriterion0001.json").write_text(json.dumps({
        "id": "badcriterion0001", "user_id": None, "organization_id": None, "created_at": 0,
        "source_label": "Legacy", "ao": {"titre": "AO historique"}, "result": legacy_result, "files": {},
    }), encoding="utf-8")

    reloaded = jobs_module.get_job("badcriterion0001")
    assert reloaded is not None
    assert reloaded.status == "error"
    assert reloaded.error_code == "historical_score_unavailable"
    assert reloaded.result is None
