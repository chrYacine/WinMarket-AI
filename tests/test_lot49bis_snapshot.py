"""Lot 49 bis — stabilizing a revision's inputs before the modern RAG: the parent's provider snapshot
(competences/certifications/business facts) is FROZEN at analysis time and reused as-is by a completion
revision; only an explicitly-confirmed `declare_prestataire` fact may amend it, and only that fact. An
unrelated profile change made by the account between the parent analysis and the completion (or even
between completion submission and the worker actually running) must never silently reach the recompute.

Reuses lot 49's own fixtures/helpers (real HTTP paths, real ScoringEngine, real private repositories — only
the RAG search is stubbed, never network) — no prefabricated scoring result.
"""
from __future__ import annotations

from src.web import jobs
from tests.test_lot44_criteria_contract import _put
from tests.test_lot49_completion import (
    PROFILE, _clear_declared_frequency, _complete, _make_account, _needs, _wait, account,
)


def _profile_with(base_facts: dict, **overrides) -> dict:
    """`business_facts` is a wholesale replace on save (see `_clear_declared_frequency`'s own docstring) —
    `base_facts` must be the account's CURRENT full declared set, not the original PROFILE constant, or this
    would silently resurrect a fact the test already cleared."""
    payload = {k: v for k, v in PROFILE.items() if k != "business_facts"}
    payload["business_facts"] = {**base_facts, **overrides}
    return payload


def _cleared_facts() -> dict:
    """The account's declared facts after `_clear_declared_frequency` — frequence_nettoyage cleared, the
    rest unchanged, exactly what `_clear_declared_frequency` itself sends."""
    return {**PROFILE["business_facts"], "frequence_nettoyage": {**PROFILE["business_facts"]["frequence_nettoyage"], "value": None}}


# ---------------------------------------------------------------------------
# 1 — an unrelated profile change made AFTER the parent analysis is not adopted
# ---------------------------------------------------------------------------

def test_a_profile_change_made_after_the_parent_analysis_is_not_adopted_by_the_revision(client, db, account):
    """Reproduces the exact scenario the ticket names: analyze A (zone "Lyon" passes, a BLOCKING criterion),
    then change the account's declared zone to something that would FAIL that same blocking criterion if it
    were read live, then complete an entirely unrelated need (budget). The old code re-read the profile live
    at worker run time — this would have flipped the frozen, already-passing zone criterion to a blocker and
    the decision to NO-GO. The fix must keep the frozen "Lyon" for this revision."""
    job = _analyze_default(client, account)
    assert job.result.decision == "INCOMPLET"
    assert job.result.provider_snapshot["business_facts"]["zone_intervention"]["value"] == ["Lyon"]

    # An unrelated live profile change — nothing about the completion below is about "zone".
    assert _put(client, account, "/api/scoring-config/profile", _profile_with(
        PROFILE["business_facts"],
        zone_intervention={"key": "zone_intervention", "label": "Zone d'intervention", "type": "list", "unit": None, "value": ["Marseille"]},
    )).status_code == 200

    r = _complete(client, account, job.id, [{"need_id": "criterion:budget", "value": 150000}])
    assert r.status_code == 200, r.text
    revision = _wait(r.json()["job_id"])
    assert revision.result.decision == "GO", "the mutated (Marseille-only) profile must never reach this recompute"
    assert not revision.result.criteres_bloquants
    assert revision.result.provider_snapshot["business_facts"]["zone_intervention"]["value"] == ["Lyon"], \
        "the revision's OWN frozen snapshot must still show what the parent was actually scored with"


def test_a_profile_change_made_after_submission_has_no_effect_on_the_worker(client, db, account):
    """Same reproduction, but the mutation happens AFTER the completion request already returned 200 (spec
    already built, job already enqueued) — proving the worker itself never re-reads ProviderProfile."""
    job = _analyze_default(client, account)
    r = _complete(client, account, job.id, [{"need_id": "criterion:budget", "value": 150000}])
    assert r.status_code == 200, r.text
    new_job_id = r.json()["job_id"]

    assert _put(client, account, "/api/scoring-config/profile", _profile_with(
        PROFILE["business_facts"],
        zone_intervention={"key": "zone_intervention", "label": "Zone d'intervention", "type": "list", "unit": None, "value": ["Marseille"]},
    )).status_code == 200

    revision = _wait(new_job_id)
    assert revision.result.decision == "GO", "a post-submission profile change must not reach the already-enqueued worker"


def _analyze_default(client, csrf):
    from tests.test_lot49_completion import _analyze
    return _analyze(client, csrf)


# ---------------------------------------------------------------------------
# 2 — only the confirmed fact is applied; every other permanent field is preserved
# ---------------------------------------------------------------------------

def test_only_the_confirmed_fact_is_applied_other_profile_data_is_preserved_everywhere(client, db, monkeypatch):
    from src.rag import private_rag_manager
    monkeypatch.setattr(private_rag_manager, "search", lambda db_, **kw: [])
    csrf = _make_account(client, db, "l49bis-confirm@example.com")
    _clear_declared_frequency(client, csrf)
    from tests.test_lot49_completion import _analyze as _reanalyze
    job = _reanalyze(client, csrf, "Appel d'offres de nettoyage à Lyon, cahier des charges : 3 fois par semaine. Budget : 150 000 €.")
    state = _needs(client, job.id)
    freq_need = next(n for n in state["needs"] if n["field_key"] == "frequence_nettoyage")

    # An unrelated fact appears in the CURRENT profile between preview and submission — confirming the
    # frequency need must not import it into the revision's snapshot, even though the version check below
    # is refreshed against this very state (the version check guards the WRITE, not what gets frozen).
    put = _put(client, csrf, "/api/scoring-config/profile", _profile_with(
        _cleared_facts(),
        certification_qualite={"key": "certification_qualite", "label": "Certification qualité", "type": "text", "unit": None, "value": "ISO 9001"},
    ))
    assert put.status_code == 200
    fresh_version = _needs(client, job.id)["profile_version"]

    r = _complete(
        client, csrf, job.id, [{"need_id": freq_need["id"], "value": 3}],
        confirm_profile_write=True, expected_profile_version=fresh_version,
    )
    assert r.status_code == 200, r.text
    revision = _wait(r.json()["job_id"])
    assert revision.result.decision == "GO"

    facts = revision.result.provider_snapshot["business_facts"]
    assert facts["frequence_nettoyage"]["value"] == 3, "the explicitly confirmed fact IS applied"
    assert "certification_qualite" not in facts, "an unrelated fact present in the CURRENT profile is never imported into the snapshot"

    profile = client.get("/api/scoring-config").json()["profile"]
    assert profile["business_facts"]["frequence_nettoyage"]["value"] == 3, "the permanent profile write did happen"
    assert profile["business_facts"]["certification_qualite"]["value"] == "ISO 9001", "other CURRENT permanent fields are preserved, not replaced by the old snapshot"
    assert profile["raison_sociale"] == PROFILE["raison_sociale"], "identity fields untouched by a fact-only completion"


# ---------------------------------------------------------------------------
# 3 — preview/submission profile-version mismatch is refused, no partial write
# ---------------------------------------------------------------------------

def test_a_stale_profile_version_at_submission_is_refused_with_no_write_at_all(client, db, monkeypatch):
    from src.rag import private_rag_manager
    monkeypatch.setattr(private_rag_manager, "search", lambda db_, **kw: [])
    csrf = _make_account(client, db, "l49bis-stale@example.com")
    _clear_declared_frequency(client, csrf)
    from tests.test_lot49_completion import _analyze as _reanalyze
    job = _reanalyze(client, csrf, "Appel d'offres de nettoyage à Lyon, cahier des charges : 3 fois par semaine. Budget : 150 000 €.")
    state = _needs(client, job.id)
    freq_need = next(n for n in state["needs"] if n["field_key"] == "frequence_nettoyage")
    stale_version = state["profile_version"]

    # The account changes something in the profile in the meantime (a real version bump).
    assert _put(client, csrf, "/api/scoring-config/profile", _profile_with(
        _cleared_facts(),
        certification_qualite={"key": "certification_qualite", "label": "Certification qualité", "type": "text", "unit": None, "value": "ISO 9001"},
    )).status_code == 200

    r = _complete(
        client, csrf, job.id, [{"need_id": freq_need["id"], "value": 3}],
        confirm_profile_write=True, expected_profile_version=stale_version,
    )
    assert r.status_code == 409
    assert r.json()["detail"]["error_code"] == "PROFILE_CHANGED"

    profile = client.get("/api/scoring-config").json()["profile"]
    assert profile["business_facts"]["frequence_nettoyage"]["value"] is None, "the refused write never touched the profile"

    from src.web.database.repositories import analyses as analyses_repo
    db.expire_all()
    assert analyses_repo.get_by_parent_job_id(db, job.id) is None, "a refused submission must not leave an inconsistent revision"


def test_confirming_a_profile_write_without_a_version_is_refused(client, db, account):
    csrf = account
    _clear_declared_frequency(client, csrf)
    from tests.test_lot49_completion import _analyze as _reanalyze
    job = _reanalyze(client, csrf, "Appel d'offres de nettoyage à Lyon, cahier des charges : 3 fois par semaine. Budget : 150 000 €.")
    state = _needs(client, job.id)
    freq_need = next(n for n in state["needs"] if n["field_key"] == "frequence_nettoyage")
    r = _complete(client, csrf, job.id, [{"need_id": freq_need["id"], "value": 3}], confirm_profile_write=True)
    assert r.status_code == 409 and r.json()["detail"]["error_code"] == "PROFILE_VERSION_REQUIRED"


# ---------------------------------------------------------------------------
# 4 — completion chain A -> B -> C, and a safe retry after a refused enqueue
# ---------------------------------------------------------------------------

def test_a_completion_chain_of_two_can_be_built_a_then_b_then_c(client, db, monkeypatch):
    from src.rag import private_rag_manager
    monkeypatch.setattr(private_rag_manager, "search", lambda db_, **kw: [])
    csrf = _make_account(client, db, "l49bis-chain@example.com")
    _clear_declared_frequency(client, csrf)
    from tests.test_lot49_completion import _analyze as _reanalyze
    job_a = _reanalyze(client, csrf, "Appel d'offres de nettoyage à Lyon, cahier des charges : 3 fois par semaine.")
    assert job_a.result.decision == "INCOMPLET"

    r_ab = _complete(client, csrf, job_a.id, [{"need_id": "criterion:budget", "value": 150000}])
    assert r_ab.status_code == 200, r_ab.text
    job_b = _wait(r_ab.json()["job_id"])
    assert job_b.parent_job_id == job_a.id
    assert job_b.result.decision == "INCOMPLET", "frequency is still missing on B"

    state_b = _needs(client, job_b.id)
    freq_need = next(n for n in state_b["needs"] if n["field_key"] == "frequence_nettoyage")
    r_bc = _complete(
        client, csrf, job_b.id, [{"need_id": freq_need["id"], "value": 3}],
        confirm_profile_write=True, expected_profile_version=state_b["profile_version"],
    )
    assert r_bc.status_code == 200, r_bc.text
    job_c = _wait(r_bc.json()["job_id"])
    assert job_c.parent_job_id == job_b.id
    assert job_c.result.decision == "GO"

    from src.web.database.repositories import analyses as analyses_repo
    db.expire_all()
    assert analyses_repo.get_by_parent_job_id(db, job_a.id).job_id == job_b.id
    assert analyses_repo.get_by_parent_job_id(db, job_b.id).job_id == job_c.id


# ---------------------------------------------------------------------------
# 5 — the result banner never overclaims reliability (RAPPORT_LOT_49.md §3)
# ---------------------------------------------------------------------------

def test_the_enrichment_unavailable_banner_never_claims_general_reliability(client, db, account):
    """No API key is configured in the test environment (see the "No API key configured" warning every job
    in this suite logs) — `enrichment_status` is genuinely "not_attempted" here, not simulated. The OLD
    wording claimed the score/decision "sont fiables" even for an INCOMPLET result; only the WORDING is
    checked here — the decision/score/status themselves are untouched (see the other tests in this module
    and in test_lot49_completion.py for that)."""
    job = _analyze_default(client, account)
    assert job.result.decision == "INCOMPLET"
    assert job.result.enrichment_status == "not_attempted" and job.result.enrichment_reason == "llm_disabled"

    page = client.get(f"/app/resultats/{job.id}").text
    assert "Enrichissement IA indisponible" in page
    assert "sont fiables" not in page, "the old, overclaiming wording must be gone"
    assert "Les justifications enrichies par l'IA ne sont pas disponibles" in page
    assert "score reste provisoire" in page, "the banner reminds an INCOMPLET score is provisional"


def test_a_completed_revisions_banner_names_declarations_without_claiming_verified_data(client, db, account):
    job = _analyze_default(client, account)
    r = _complete(client, account, job.id, [{"need_id": "criterion:budget", "value": 150000}])
    assert r.status_code == 200, r.text
    revision = _wait(r.json()["job_id"])
    assert revision.result.decision == "GO"

    page = client.get(f"/app/resultats/{revision.id}").text
    assert "tiennent compte des informations déclarées" in page, "the revision banner names the declarations behind its own result"
    assert "pas des données extraites ni vérifiées" in page, "declared values are never presented as verified"
    assert "sont fiables" not in page


def test_a_refused_enqueue_leaves_a_safe_retry_path_no_permanent_block(client, db, account, monkeypatch):
    from src.web import job_executor

    calls = {"n": 0}
    real_submit = job_executor.submit_revision

    def flaky(job, spec):
        calls["n"] += 1
        if calls["n"] == 1:
            raise job_executor.JobQueueSaturatedError(job.id)
        return real_submit(job, spec)

    monkeypatch.setattr(job_executor, "submit_revision", flaky)

    job = _analyze_default(client, account)
    r1 = _complete(client, account, job.id, [{"need_id": "criterion:budget", "value": 150000}])
    assert r1.status_code == 429
    assert r1.json()["detail"]["error_code"] == "JOB_QUEUE_SATURATED"

    # No revision was left behind by the refused attempt — the UNIQUE(parent_job_id) constraint never fired.
    from src.web.database.repositories import analyses as analyses_repo
    db.expire_all()
    assert analyses_repo.get_by_parent_job_id(db, job.id) is None
    state = _needs(client, job.id)
    assert state["can_complete"] is True and state["existing_revision_job_id"] is None

    r2 = _complete(client, account, job.id, [{"need_id": "criterion:budget", "value": 150000}])
    assert r2.status_code == 200, r2.text
    revision = _wait(r2.json()["job_id"])
    assert revision.status == "done" and revision.result.decision == "GO" and revision.parent_job_id == job.id
    assert calls["n"] == 2, "the retry actually went through the (now working) submission path"
