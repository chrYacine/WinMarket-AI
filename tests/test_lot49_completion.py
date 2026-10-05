"""Lot 49 — completing an INCOMPLET (or otherwise gap-carrying) analysis without inventing data or losing
history: the needs contract (src/agents/completion_needs.py), the guided completion route
(POST /api/analyze/{job}/complete) and the resulting revision (src/web/jobs.py::_run_revision).

Real HTTP paths, a real ScoringEngine, real private repositories. Only the LLM enrichment adapter is
simulated where used (never network) — no scoring result is ever prefabricated.
"""
from __future__ import annotations

import time

import pytest

from src.core import config
from src.web import jobs
from tests.conftest import make_active_starter_user
from tests.test_lot44_criteria_contract import _capacity, _login, _put, crit

PROFILE = {
    "raison_sociale": "Nettoyage Pro (lot 49)", "competences": [], "certifications": [],
    "business_facts": {
        "zone_intervention": {"key": "zone_intervention", "label": "Zone d'intervention", "type": "list", "unit": None, "value": ["Lyon"]},
        # A DECLARED (valid, non-null) value — a criterion referencing a fact with no value cannot even be
        # activated (criteria_catalogue.validate_criterion). `provider_fact_missing` is reached the other
        # way round: declared at activation time, then CLEARED afterwards by a later profile save (see
        # _clear_declared_frequency below) — exactly what "the account never declared it" looks like once a
        # criterion already depends on it.
        "frequence_nettoyage": {"key": "frequence_nettoyage", "label": "Fréquence de nettoyage", "type": "number", "unit": "par_semaine", "value": 5},
    },
}


def _clear_declared_frequency(client, csrf):
    """Simulates "the account never declared this fact" for an ALREADY ACTIVE criterion: the account
    cleared it from their profile after activation (the only way this state is reachable — see PROFILE's
    own comment). `business_facts` is a wholesale replace on save, so the full declared set is resent."""
    payload = dict(PROFILE)
    payload["business_facts"] = {
        **PROFILE["business_facts"],
        "frequence_nettoyage": {**PROFILE["business_facts"]["frequence_nettoyage"], "value": None},
    }
    assert _put(client, csrf, "/api/scoring-config/profile", payload).status_code == 200
BUDGET_TIERS = {"source": "budget", "fact_key": None, "tiers": [{"at_least": 100000, "score": 90}],
                "below_score": 20, "zero_score": None, "minimum_blocking": None}


def _criteria(*, freq_blocking=False, second_budget=False):
    out = [
        crit("zone", "list_coverage", {"fact_key": "zone_intervention", "pass_score": 100, "fail_score": 0}, 30, label="Sites couverts", blocking=True),
        crit("frequence", "numeric_threshold", {"fact_key": "frequence_nettoyage", "comparison": "provider_gte_ao", "pass_score": 100, "fail_score": 20},
             20, label="Fréquence compatible", blocking=freq_blocking),
        crit("budget", "numeric_tiers", BUDGET_TIERS, 50, label="Budget estimé"),
    ]
    if second_budget:
        out[2]["weight"] = 25  # zone 30 + freq 20 + budget 25 + budget2 25 = 100
        out.append(crit("budget2", "numeric_tiers", BUDGET_TIERS, 25, label="Budget estimé (bis)"))
    return out


def _make_account(client, db, email, *, freq_blocking=False, second_budget=False):
    make_active_starter_user(db, email, scoring=False)
    csrf = _login(client, email)
    _capacity(client, csrf)
    assert _put(client, csrf, "/api/scoring-config/profile", PROFILE).status_code == 200
    criteria = _criteria(freq_blocking=freq_blocking, second_budget=second_budget)
    assert _put(client, csrf, "/api/scoring-config/policy", {"criteria": criteria, "threshold_go": 80, "threshold_sous_reserve": 55}).status_code == 200
    assert client.post("/api/scoring-config/policy/activate", json={"expected_active_version": None}, headers={"X-CSRF-Token": csrf}).status_code == 200
    return csrf


def _analyze(client, csrf, text="Appel d'offres de nettoyage à Lyon, cahier des charges : 3 fois par semaine."):
    r = client.post("/api/analyze", data={"mode": "paste", "text": text}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200, r.text
    return _wait(r.json()["job_id"])


def _wait(job_id):
    for _ in range(300):
        job = jobs.get_job(job_id)
        if job is not None and job.status != "running":
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def _needs(client, job_id):
    r = client.get(f"/api/analyze/{job_id}/completion")
    assert r.status_code == 200, r.text
    return r.json()


def _complete(client, csrf, job_id, items, **extra):
    return client.post(f"/api/analyze/{job_id}/complete", json={"items": items, **extra}, headers={"X-CSRF-Token": csrf})


def _error(r):
    return r.json()["detail"]


@pytest.fixture()
def account(client, db, monkeypatch):
    from src.rag import private_rag_manager
    monkeypatch.setattr(private_rag_manager, "search", lambda db_, **kw: [])
    return _make_account(client, db, "l49-a@example.com")


# ---------------------------------------------------------------------------
# A — the needs contract
# ---------------------------------------------------------------------------

def test_a_need_is_deduced_and_shared_across_the_criteria_that_need_it(client, db, account):
    """Budget is missing (no amount stated), zone/frequency are found — ONE need for budget, shared by
    BOTH criteria that reference it (deduplicated, not duplicated per criterion)."""
    job = _analyze(client, account)
    assert job.result.decision == "INCOMPLET"
    state = _needs(client, job.id)
    assert state["can_complete"] is True and state["policy_version"] == 1
    budget_needs = [n for n in state["needs"] if n["field_key"] == "budget_estime"]
    assert len(budget_needs) == 1 and sorted(budget_needs[0]["criteria"]) == ["budget"]


def test_a_need_shared_by_two_criteria_is_a_single_entry(client, db, monkeypatch):
    from src.rag import private_rag_manager
    monkeypatch.setattr(private_rag_manager, "search", lambda db_, **kw: [])
    csrf = _make_account(client, db, "l49-shared@example.com", second_budget=True)
    job = _analyze(client, csrf)
    state = _needs(client, job.id)
    budget_needs = [n for n in state["needs"] if n["field_key"] == "budget_estime"]
    assert len(budget_needs) == 1 and sorted(budget_needs[0]["criteria"]) == ["budget", "budget2"]


def test_ao_side_and_provider_side_are_never_merged_even_for_the_same_fact(client, db, monkeypatch):
    """The frequency fact is missing on the PROVIDER side (declared value is None) while present in the AO
    text — a separate, correctly-attributed need from any AO-side need, never fused."""
    from src.rag import private_rag_manager
    monkeypatch.setattr(private_rag_manager, "search", lambda db_, **kw: [])
    csrf = _make_account(client, db, "l49-sides@example.com")
    _clear_declared_frequency(client, csrf)
    job = _analyze(client, csrf, "Appel d'offres de nettoyage à Lyon. 3 fois par semaine. Budget : 120 000 euros.")
    state = _needs(client, job.id)
    freq = next(n for n in state["needs"] if n["field_key"] == "frequence_nettoyage")
    assert freq["subject"] == "prestataire" and freq["action"] == "declare_prestataire"
    assert all(n["field_key"] != "zone_intervention" for n in state["needs"]), "zone was found on both sides: no need"


def test_needs_are_recomputed_fresh_never_trusting_a_client_supplied_shape(client, db, account):
    job = _analyze(client, account)
    r = _complete(client, account, job.id, [{"need_id": "criterion:budget", "value": 999999, "extra": "hack"}])
    assert r.status_code == 422 and _error(r)["error_code"] == "UNKNOWN_FIELD"
    r = _complete(client, account, job.id, [{"need_id": "not-a-real-need", "value": 1}])
    assert r.status_code == 422 and _error(r)["error_code"] == "UNKNOWN_NEED"


# ---------------------------------------------------------------------------
# B — complete -> real recompute
# ---------------------------------------------------------------------------

def test_a_valid_ao_complement_triggers_a_real_recompute_to_go(client, db, account):
    job = _analyze(client, account)
    assert job.result.decision == "INCOMPLET"
    r = _complete(client, account, job.id, [{"need_id": "criterion:budget", "value": 150000}])
    assert r.status_code == 200, r.text
    body = r.json()
    # Lot 53: "changes" gained subject/field_key/origin/source_json (additive — for display/PDF/DOCX, see
    # src/web/result_presentation.py and ScoringResult.completion_changes) — the original field/before/after
    # contract this test asserts is unchanged.
    assert body["parent_job_id"] == job.id and body["changes"] == [{
        "field": "Budget estimé", "subject": "ao", "field_key": "budget_estime", "before": None, "after": 150000.0,
        "origin": "declared_user", "source_json": None,
    }]
    revision = _wait(body["job_id"])
    assert revision.status == "done" and revision.result.decision == "GO" and revision.parent_job_id == job.id
    assert revision.ao.budget_estime == 150000.0 and revision.ao.field_provenance["budget_estime"] == "declared_user"


def test_skipping_a_need_leaves_it_incomplete_never_a_favourable_guess(client, db, account):
    job = _analyze(client, account)
    r = _complete(client, account, job.id, [])
    assert r.status_code == 400 and _error(r)["error_code"] == "NOTHING_TO_APPLY"


def test_a_confirmed_blocker_stays_no_go_even_after_completing_an_unrelated_need(client, db, monkeypatch):
    """The frequency criterion is BLOCKING and genuinely EVALUATED (found in the AO text, compared to the
    5/week the account declared) — 10/week required fails that comparison for real, a proven blocker, not a
    missing datum. Budget stays missing alongside it; completing budget must never lift the blocker."""
    from src.rag import private_rag_manager
    monkeypatch.setattr(private_rag_manager, "search", lambda db_, **kw: [])
    csrf = _make_account(client, db, "l49-blocker@example.com", freq_blocking=True)
    job = _analyze(client, csrf, "Appel d'offres de nettoyage à Lyon, cahier des charges : 10 fois par semaine.")
    assert job.result.decision == "NO-GO" and job.result.criteres_bloquants
    r = _complete(client, csrf, job.id, [{"need_id": "criterion:budget", "value": 150000}])
    assert r.status_code == 200, r.text
    revision = _wait(r.json()["job_id"])
    assert revision.result.decision == "NO-GO" and revision.result.criteres_bloquants, "a proven blocker is never lifted by completing something else"


def test_a_dossier_conflict_is_never_resolved_by_typing_a_value(client, db, account):
    import io
    from docx import Document

    def docx(*paras):
        doc = Document()
        for p in paras:
            doc.add_paragraph(p)
        b = io.BytesIO()
        doc.save(b)
        return b.getvalue()

    # PDF base fonts cannot draw "€" (a known rendering gap — see docs/qa/lot_47_bis_20260921): both pieces
    # here use real-text formats (.txt / .docx) so the amount is genuinely readable by the local fallback.
    files = [
        ("rc", ("rc.txt", "Règlement de consultation du marché. Site de Lyon. 3 fois par semaine. Budget : 90 000 €.".encode(), "text/plain")),
        ("cctp", ("cctp.docx", docx("Cahier des charges du marché.", "Budget : 150 000 €."), "application/octet-stream")),
    ]
    r = client.post("/api/analyze", data={"mode": "dossier"}, files=files, headers={"X-CSRF-Token": account})
    assert r.status_code == 200, r.text
    job = _wait(r.json()["job_id"])
    assert job.ao.field_provenance.get("budget_estime") == "conflict"
    state = _needs(client, job.id)
    conflict = next(n for n in state["needs"] if n["field_key"] == "budget_estime")
    assert conflict["kind"] == "conflict" and conflict["action"] == "none"
    r2 = _complete(client, account, job.id, [{"need_id": conflict["id"], "value": 150000}])
    assert r2.status_code == 422 and _error(r2)["error_code"] == "NOT_DECLARABLE" and _error(r2)["kind"] == "conflict"


def test_zero_references_is_informational_and_never_changes_a_note(client, db, monkeypatch):
    from src.rag import private_rag_manager
    monkeypatch.setattr(private_rag_manager, "search", lambda db_, **kw: [])
    make_active_starter_user(db, "l49-zeroref@example.com", scoring=False)
    csrf = _login(client, "l49-zeroref@example.com")
    _capacity(client, csrf)
    assert _put(client, csrf, "/api/scoring-config/profile", {"raison_sociale": "Test", "competences": [], "certifications": []}).status_code == 200
    criteria = [crit("refs", "reference_evidence", {"base_score": 30, "per_reference": 6, "similarity_weight": 40}, 100, label="Références")]
    assert _put(client, csrf, "/api/scoring-config/policy", {"criteria": criteria, "threshold_go": 80, "threshold_sous_reserve": 20}).status_code == 200
    client.post("/api/scoring-config/policy/activate", json={"expected_active_version": None}, headers={"X-CSRF-Token": csrf})
    job = _analyze(client, csrf, "Appel d'offres générique. Cahier des charges sans exigence particulière.")
    assert job.result.score_global == 30.0  # base_score only, zero references — never flagged as "missing"
    state = _needs(client, job.id)
    ref_need = next(n for n in state["needs"] if n["id"] == "references:zero_results")
    assert ref_need["kind"] == "informational" and ref_need["action"] == "add_reference"
    r = _complete(client, csrf, job.id, [{"need_id": "references:zero_results", "value": 1}])
    assert r.status_code == 422 and _error(r)["error_code"] == "NOT_DECLARABLE"


def test_a_policy_configuration_gap_is_never_offered_as_a_data_field(client, db, monkeypatch):
    """A legacy policy's never-configured business rule (`max_unmastered_technologies`) surfaces as a
    POLICY need (link to /app/parametres) — never as something this endpoint accepts a value for. This
    legacy evaluator flags the gap on EVERY evaluation regardless of the AO's own content (see
    criteria_evaluators._technology_coverage: `missing_rules` is attached unconditionally)."""
    from src.rag import private_rag_manager
    monkeypatch.setattr(private_rag_manager, "search", lambda db_, **kw: [])
    from tests.conftest import default_org_id
    user = make_active_starter_user(db, "l49-legacy@example.com", scoring=False, capacity=True)
    org_id = default_org_id(db, user)
    csrf = _login(client, "l49-legacy@example.com")
    # a bare legacy policy with an unconfigured business rule (max_unmastered_technologies never set)
    from src.web.database.repositories import scoring_policy as scoring_policy_repo
    scoring_policy_repo.save_draft(
        db, organization_id=org_id, owner_user_id=user.id, created_by_user_id=user.id,
        weights={"Adequation expertise": 100}, threshold_go=80, threshold_sous_reserve=55, business_rules={}, custom_criteria=[],
    )
    scoring_policy_repo.activate_draft(db, organization_id=org_id, owner_user_id=user.id, expected_active_version=None)
    db.commit()
    job = _analyze(client, csrf, "Appel d'offres générique, cahier des charges sans mention technique particulière.")
    assert job.result.decision == "INCOMPLET" and "max_unmastered_technologies" in job.result.scoring_missing
    state = _needs(client, job.id)
    rule_need = next(n for n in state["needs"] if n.get("reason") == "max_unmastered_technologies")
    assert rule_need["kind"] == "policy" and rule_need["action"] == "configure_policy"
    r = _complete(client, csrf, job.id, [{"need_id": rule_need["id"], "value": 1}])
    assert r.status_code == 422 and _error(r)["error_code"] == "NOT_DECLARABLE"


# ---------------------------------------------------------------------------
# C — profile confirmation, policy pinning, history preserved
# ---------------------------------------------------------------------------

def test_a_provider_fact_requires_explicit_confirmation_and_is_then_permanently_saved(client, db, monkeypatch):
    from src.rag import private_rag_manager
    monkeypatch.setattr(private_rag_manager, "search", lambda db_, **kw: [])
    csrf = _make_account(client, db, "l49-confirm@example.com")
    _clear_declared_frequency(client, csrf)
    job = _analyze(client, csrf, "Appel d'offres de nettoyage à Lyon, cahier des charges : 3 fois par semaine. Budget : 150 000 €.")
    state = _needs(client, job.id)
    freq_need = next(n for n in state["needs"] if n["field_key"] == "frequence_nettoyage")
    assert freq_need["action"] == "declare_prestataire"

    r = _complete(client, csrf, job.id, [{"need_id": freq_need["id"], "value": 3}])
    assert r.status_code == 422 and _error(r)["error_code"] == "CONFIRMATION_REQUIRED"

    r2 = _complete(
        client, csrf, job.id, [{"need_id": freq_need["id"], "value": 3}],
        confirm_profile_write=True, expected_profile_version=state["profile_version"],
    )
    assert r2.status_code == 200, r2.text
    revision = _wait(r2.json()["job_id"])
    assert revision.result.decision == "GO"

    profile = client.get("/api/scoring-config").json()["profile"]
    assert profile["business_facts"]["frequence_nettoyage"]["value"] == 3, "a confirmed profile complement is a REAL, permanent write"


def test_an_ao_correction_never_silently_feeds_the_profile(client, db, account):
    job = _analyze(client, account)
    _complete(client, account, job.id, [{"need_id": "criterion:budget", "value": 150000}])
    profile = client.get("/api/scoring-config").json()["profile"]
    assert "budget_estime" not in profile["business_facts"], "an AO-scoped complement must never appear in the account's own profile"


def test_the_revision_uses_the_exact_original_policy_version_not_whatever_is_active_now(client, db, account):
    job = _analyze(client, account)  # scored under policy version 1
    # activate a NEW, incompatible policy afterwards
    new_criteria = [crit("only", "numeric_tiers", BUDGET_TIERS, 100, label="Seul le budget compte")]
    assert _put(client, account, "/api/scoring-config/policy", {"criteria": new_criteria, "threshold_go": 1, "threshold_sous_reserve": 0}).status_code == 200
    assert client.post("/api/scoring-config/policy/activate", json={"expected_active_version": 1}, headers={"X-CSRF-Token": account}).status_code == 200

    r = _complete(client, account, job.id, [{"need_id": "criterion:budget", "value": 150000}])
    assert r.status_code == 200, r.text
    revision = _wait(r.json()["job_id"])
    assert revision.scoring_policy_version == 1, "the revision is scored by the policy that produced the PARENT, never the new active one"
    assert {c.critere_id for c in revision.result.criteres} == {"zone", "frequence", "budget"}, "not the new policy's single criterion"


def test_the_parent_analysis_and_its_deliverables_are_untouched_after_completion(client, db, account):
    job = _analyze(client, account)
    pdf_before = client.get(f"/api/download/{job.id}/pdf").content
    r = _complete(client, account, job.id, [{"need_id": "criterion:budget", "value": 150000}])
    revision = _wait(r.json()["job_id"])
    reread = jobs.get_job(job.id)
    assert reread.result.decision == "INCOMPLET" and reread.result.score_global == job.result.score_global
    pdf_after = client.get(f"/api/download/{job.id}/pdf").content
    assert pdf_after == pdf_before, "the parent's own PDF is not regenerated/replaced by a completion"
    assert revision.files.get("pdf") != job.files.get("pdf"), "the revision gets its OWN, separate deliverables"
    child_pdf = client.get(f"/api/download/{revision.id}/pdf").content
    assert child_pdf[:4] == b"%PDF" and child_pdf != pdf_before


def test_a_new_revision_never_reads_the_parents_dossier_pieces_again(client, db, account, monkeypatch):
    """No re-extraction: patch the extractor to explode, and prove the revision never calls it."""
    job = _analyze(client, account)

    def boom(*a, **k):
        raise AssertionError("a revision must never re-extract the AO")

    import src.agents.ao_extractor as ao_extractor_module
    monkeypatch.setattr(ao_extractor_module.AOExtractor, "extract", boom)
    r = _complete(client, account, job.id, [{"need_id": "criterion:budget", "value": 150000}])
    assert r.status_code == 200
    revision = _wait(r.json()["job_id"])
    assert revision.status == "done" and revision.result.decision == "GO"


# ---------------------------------------------------------------------------
# D — double submission, staleness, dossier link continuity, resume vs complete
# ---------------------------------------------------------------------------

def test_a_second_completion_on_an_already_completed_analysis_is_refused(client, db, account):
    job = _analyze(client, account)
    r1 = _complete(client, account, job.id, [{"need_id": "criterion:budget", "value": 150000}])
    _wait(r1.json()["job_id"])
    r2 = _complete(client, account, job.id, [{"need_id": "criterion:budget", "value": 200000}])
    assert r2.status_code == 409 and _error(r2)["error_code"] == "REVISION_ALREADY_EXISTS"
    assert _error(r2)["existing_job_id"] == r1.json()["job_id"]

    from src.web.database.models import Analysis
    db.expire_all()
    count = db.query(Analysis).filter(Analysis.parent_job_id == job.id).count()
    assert count == 1, "no duplicate revision was created"


def test_completion_and_dossier_resume_are_two_distinct_actions_with_their_own_guards(client, db, account):
    import io
    import fitz

    def pdf(text):
        d = fitz.open()
        d.new_page().insert_textbox(fitz.Rect(72, 72, 500, 700), text)
        return d.tobytes()

    files = [("rc", ("rc.pdf", pdf("Règlement de consultation du marché. Site de Lyon. 3 fois par semaine."), "application/octet-stream"))]
    r = client.post("/api/analyze", data={"mode": "dossier"}, files=files, headers={"X-CSRF-Token": account})
    job = _wait(r.json()["job_id"])
    assert job.status == "done"

    # `resume` refuses a DONE job — completion is the correct action instead.
    resume = client.post(f"/api/analyze/{job.id}/resume", headers={"X-CSRF-Token": account})
    assert resume.status_code == 409 and _error(resume)["error_code"] == "JOB_ALREADY_DONE"

    comp = _complete(client, account, job.id, [{"need_id": "criterion:budget", "value": 150000}])
    assert comp.status_code == 200, comp.text
    revision = _wait(comp.json()["job_id"])
    assert revision.result.decision == "GO"

    # BOTH jobs still resolve the SAME original dossier — the lot 47 bis reserve, fixed.
    assert client.get(f"/api/analyze/{job.id}/dossier").status_code == 200
    assert client.get(f"/api/analyze/{revision.id}/dossier").status_code == 200
    assert client.get(f"/api/analyze/{job.id}/dossier").json()["id"] == client.get(f"/api/analyze/{revision.id}/dossier").json()["id"]


def test_an_interrupted_job_is_resumed_not_completed_and_a_finished_one_is_completed_not_resumed(client, db, account, monkeypatch):
    from src.web import routes_api
    # .txt, not .pdf: PDF base fonts cannot draw "€" (see the dossier-conflict test above).
    files = [("rc", ("rc.txt", "Règlement de consultation du marché. Site de Lyon. Budget : 150 000 €. 3 fois par semaine.".encode(), "text/plain"))]
    with monkeypatch.context() as m:
        m.setattr(routes_api, "_start_job", lambda *a, **k: None)  # simulate: accepted, never actually ran
        r = client.post("/api/analyze", data={"mode": "dossier"}, files=files, headers={"X-CSRF-Token": account})
    assert r.status_code == 200
    orphan_id = r.json()["job_id"]
    jobs._JOBS.clear()

    # completion refuses: this job never even reached a durable "queued" row (nothing to complete;
    # `jobs.get_job` cannot distinguish it from a truly unknown job) — `resume` is the correct action here.
    comp = _complete(client, account, orphan_id, [{"need_id": "criterion:budget", "value": 1}])
    assert comp.status_code == 404

    resumed = client.post(f"/api/analyze/{orphan_id}/resume", headers={"X-CSRF-Token": account})
    assert resumed.status_code == 200
    finished = _wait(resumed.json()["job_id"])
    assert finished.status == "done" and finished.result.decision == "GO"


def test_a_revision_survives_a_restart_and_reads_back_its_parent_link(client, db, account):
    job = _analyze(client, account)
    r = _complete(client, account, job.id, [{"need_id": "criterion:budget", "value": 150000}])
    revision = _wait(r.json()["job_id"])
    jobs._JOBS.clear()
    reread = jobs.get_job(revision.id)
    assert reread is not None and reread.parent_job_id == job.id and reread.result.decision == "GO"
    page = client.get(f"/app/resultats/{revision.id}")
    assert page.status_code == 200 and "complète" in page.text


# ---------------------------------------------------------------------------
# E — isolation, CSRF, roles
# ---------------------------------------------------------------------------

def test_another_account_cannot_see_or_complete_a_foreign_analysis(client, db, monkeypatch):
    from src.rag import private_rag_manager
    monkeypatch.setattr(private_rag_manager, "search", lambda db_, **kw: [])
    csrf_a = _make_account(client, db, "l49-iso-a@example.com")
    job = _analyze(client, csrf_a)
    csrf_b = _make_account(client, db, "l49-iso-b@example.com")
    assert client.get(f"/api/analyze/{job.id}/completion").status_code == 404
    r = _complete(client, csrf_b, job.id, [{"need_id": "criterion:budget", "value": 1}])
    assert r.status_code == 404


def test_complete_requires_csrf(client, db, account):
    job = _analyze(client, account)
    r = client.post(f"/api/analyze/{job.id}/complete", json={"items": [{"need_id": "criterion:budget", "value": 1}]})
    assert r.status_code in (401, 403)


def test_a_viewer_role_cannot_complete_an_analysis(client, db, monkeypatch):
    from src.rag import private_rag_manager
    monkeypatch.setattr(private_rag_manager, "search", lambda db_, **kw: [])
    csrf = _make_account(client, db, "l49-viewerowner@example.com")
    job = _analyze(client, csrf)
    from tests.test_b02_organizations import _add_member
    from tests.conftest import default_org_id
    from src.web.auth import service as auth_service
    from src.web.database.repositories import users as users_repo
    owner = users_repo.get_by_email(db, "l49-viewerowner@example.com")
    org_id = default_org_id(db, owner)
    _add_member(db, organization_id=org_id, email="l49-viewer@example.com", role="viewer")
    db.commit()
    viewer_csrf = _login(client, "l49-viewer@example.com")
    r = _complete(client, viewer_csrf, job.id, [{"need_id": "criterion:budget", "value": 1}])
    assert r.status_code == 403
