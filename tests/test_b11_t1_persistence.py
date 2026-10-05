"""B11-T1 (DEFECT confirmed): the SQL `analyses` table was WRITE-ONLY as far
as reading a saved analysis back was concerned. jobs.get_job() checked the
in-process `_JOBS` dict and then a LOCAL JSON FILE on this process's own
disk, and never queried SQL at all — so an analysis whose row was sitting
right there in the database became unreadable through
/api/analyze/{job_id}/status, /api/download/... and /app/resultats/... as
soon as the process restarted (losing `_JOBS`) or the request landed on an
instance without that file.

Test groups, per the ticket:
A. With `_JOBS` emptied AND the local JSON file gone, a real analysis is
   still fully readable — from SQL alone — for a real GO/NO-GO decision AND
   for an honest "INCOMPLET" one, which must survive verbatim rather than
   being promoted to a real decision or defaulted. Another authenticated
   user asking for the same job_id still gets a 404.
B. A SQL write failure never produces a fake durable success: the job
   reports the existing PERSISTENCE_FAILED_ERROR_CODE and a later read does
   not claim a database row that was never written.
C. Idempotency of the analysis upsert: two attempts for the same job_id
   leave exactly one row carrying the SECOND attempt's data, and an attempt
   under a different owner is refused rather than silently applied.
D. Idempotency of the document upsert: a re-render updates the existing row
   of that mime_type instead of adding a second one.
E. Reordering proof: the analysis row reaches SQL with its real computed
   score even when document rendering fails outright — i.e. independently
   of, and before, rendering.

Helpers are copied locally (this codebase's convention: test files do not
import from one another). No real network, no real key, no commit.
"""
from __future__ import annotations

import re
import time

import pytest
from sqlalchemy import func, select

from src.web import jobs
from src.web.database.models import Analysis, AnalysisDocument
from tests.conftest import default_org_id, make_active_starter_user

VALID_WEIGHTS = {
    "Adequation expertise": 20, "References similaires": 15, "Disponibilite equipe": 10,
    "Rentabilite estimee": 10, "Faisabilite delai": 10, "Certifications requises": 10,
    "Complexite technique": 5, "Connaissance secteur": 5, "Potentiel commercial": 5,
    "Risque contractuel": 5, "Solidite client": 3, "Valeur strategique": 2,
}

# Permissive, fully-configured business rules: with these, the engine
# reaches one of the three REAL verdicts rather than the honest "INCOMPLET"
# it must return when a rule is unconfigured (B06-T4).
COMPLETE_BUSINESS_RULES = {
    "budget_minimum_eur": 0, "max_charge_pct": 100,
    "max_unmastered_technologies": 999, "certification_penalty_score": 20,
}

VALID_AO_TEXT = (
    "Appel d'offres - Portail client\n"
    "Acheteur : Collectivite Exemple\n"
    "Budget : 250 000 euros. Date limite : 30/11/2026.\n"
    "Exigences : Python et Django.\n"
)


@pytest.fixture(autouse=True)
def _isolate_legacy_json_artifacts(tmp_path, monkeypatch):
    """jobs.ANALYSES_DIR is a module
    constant bound at import time from config.DATA_DIR, so conftest's
    autouse DATA_DIR redirection does not reach them (several existing test
    files patch ANALYSES_DIR by hand for the same reason). Redirected here
    for every test in this file: these tests deliberately delete the legacy
    JSON artifact, which must never mean deleting anything under the real
    project data/ directory."""
    monkeypatch.setattr(jobs, "ANALYSES_DIR", tmp_path / "historique" / "analyses")


def _csrf_from(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


def _login(client, email, password="Sup3rSecret!"):
    r = client.get("/login")
    csrf = _csrf_from(r.text)
    client.post("/login", data={"email": email, "password": password, "next": "/app", "csrf_token": csrf})
    # B14-T1: mutating routes now require the same double-submit CSRF token
    # via the X-CSRF-Token header; get_or_create_csrf_token() reuses the
    # cookie's token, so the value minted for the login page's hidden field
    # stays valid for the rest of this session — return it for callers.
    return csrf


def _configure_capacity(client, csrf, charge: int = 40):
    return client.post("/api/capacity", json={
        "charge_globale_pct": charge, "nombre_projets_en_cours": 1,
        "projets_en_cours": ["Projet test"], "capacites_par_pole": {"Software Engineering": 40},
    }, headers={"X-CSRF-Token": csrf})


def _save_profile(client, csrf):
    return client.put("/api/scoring-config/profile", json={
        "raison_sociale": "ESN de test", "effectif": "10-50",
        "competences": ["python", "django"], "certifications": [],
    }, headers={"X-CSRF-Token": csrf})


def _save_draft(client, csrf, business_rules=None):
    payload = {"weights": dict(VALID_WEIGHTS), "threshold_go": 88, "threshold_sous_reserve": 60}
    if business_rules is not None:
        payload["business_rules"] = business_rules
    return client.put("/api/scoring-config/policy", json=payload, headers={"X-CSRF-Token": csrf})


def _activate(client, csrf, expected_active_version=None):
    """`expected_active_version` is the optimistic-concurrency check: None
    means "I believe nothing is active yet". Re-activating a new version for
    an account that already has one must pass the version it is replacing,
    otherwise the route correctly refuses with 409."""
    return client.post(
        "/api/scoring-config/policy/activate", json={"expected_active_version": expected_active_version},
        headers={"X-CSRF-Token": csrf},
    )


def _configure_account(client, db, email, *, business_rules=None):
    """`business_rules=None` deliberately leaves them UNCONFIGURED, which is
    a legal, real account state whose honest scoring verdict is
    "INCOMPLET" (B06-T4) — that is exactly what group A's second job needs.
    Pass COMPLETE_BUSINESS_RULES for a job that must produce a real
    GO/NO-GO/RESERVE verdict. Returns the CSRF token for this session, for
    the caller to thread into _run_analysis_to_terminal."""
    make_active_starter_user(db, email, scoring=False)
    csrf = _login(client, email)
    _configure_capacity(client, csrf)
    _save_profile(client, csrf)
    _save_draft(client, csrf, business_rules=business_rules)
    _activate(client, csrf)
    return csrf


def _install_no_op_search(monkeypatch):
    from src.rag import private_rag_manager
    monkeypatch.setattr(private_rag_manager, "search", lambda db, **kw: [])


def _run_analysis_to_terminal(client, csrf, text: str = VALID_AO_TEXT):
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


def _forget_every_non_sql_trace(job_id: str) -> None:
    """Simulate the real defect's conditions: the process restarted (so the
    in-process cache is gone) and the local JSON artifact is not on this
    instance's disk. Whatever is served afterwards can only have come from
    the SQL row."""
    jobs._JOBS.pop(job_id, None)
    json_path = jobs.ANALYSES_DIR / f"{job_id}.json"
    if json_path.exists():
        json_path.unlink()
    assert job_id not in jobs._JOBS
    assert not json_path.exists()


def _count_analyses(db, job_id: str) -> int:
    return db.execute(
        select(func.count()).select_from(Analysis).where(Analysis.job_id == job_id)
    ).scalar_one()


# ---------------------------------------------------------------------------
# Group A — the read path: SQL alone must be able to serve a saved analysis.
# ---------------------------------------------------------------------------

def test_real_decision_is_served_from_sql_alone_after_memory_and_json_are_gone(client, db, monkeypatch):
    _install_no_op_search(monkeypatch)
    csrf = _configure_account(client, db, "sqlread@example.com", business_rules=COMPLETE_BUSINESS_RULES)
    job = _run_analysis_to_terminal(client, csrf)

    assert job.status == "done", job.error
    expected_decision = job.result.decision
    expected_score = job.result.score_global
    expected_completeness = job.result.scoring_completeness
    expected_policy_version = job.scoring_policy_version
    assert expected_decision in ("GO", "GO SOUS RESERVE", "NO-GO"), (
        "this account HAS complete business rules, so the engine must reach a real verdict"
    )

    _forget_every_non_sql_trace(job.id)

    # The HTTP layer, end to end.
    r = client.get(f"/api/analyze/{job.id}/status")
    assert r.status_code == 200, "the analysis row is in SQL — it must not read as missing"
    assert r.json()["status"] == "done"

    # And the reconstruction itself, field by field.
    jobs._JOBS.pop(job.id, None)
    reloaded = jobs.get_job(job.id)
    assert reloaded is not None
    assert reloaded.result is not None
    assert reloaded.result.decision == expected_decision
    assert reloaded.result.score_global == expected_score
    assert reloaded.result.scoring_completeness == expected_completeness
    assert reloaded.ao is not None and reloaded.ao.titre
    # The ownership columns come from the row itself — this is what keeps
    # the routes' unchanged `job.user_id != current_user.id` check working.
    assert reloaded.user_id is not None
    assert reloaded.organization_id is not None
    # The pinned policy version is re-READ, never re-derived.
    assert reloaded.scoring_policy_version == expected_policy_version


def test_incomplet_decision_survives_the_sql_round_trip_verbatim(client, db, monkeypatch):
    """B06-T4/B19-T1: "INCOMPLET" is an honest verdict for an account whose
    business rules are unconfigured — never an error, and never to be
    silently promoted to a real decision or defaulted to 0/"GO" by the
    storage layer."""
    _install_no_op_search(monkeypatch)
    csrf = _configure_account(client, db, "incomplet@example.com")  # no business rules on purpose
    job = _run_analysis_to_terminal(client, csrf)

    assert job.result.decision == "INCOMPLET", "unconfigured business rules must yield the honest verdict"
    expected_score = job.result.score_global
    expected_missing = list(job.result.scoring_missing)
    expected_integrity = job.result.data_integrity
    assert expected_score > 0, "an incomplete result still carries the real weighted score"
    assert expected_missing, "the skipped rules must be named"

    _forget_every_non_sql_trace(job.id)

    reloaded = jobs.get_job(job.id)
    assert reloaded is not None and reloaded.result is not None
    assert reloaded.result.decision == "INCOMPLET", "the honest 'not computable' verdict must not be promoted"
    assert reloaded.result.score_global == expected_score, "the stored score must be re-read, never recomputed/zeroed"
    assert reloaded.result.scoring_completeness == "incomplete"
    assert reloaded.result.scoring_missing == expected_missing
    assert reloaded.result.data_integrity == expected_integrity
    assert reloaded.status == "done", "an INCOMPLET decision is not an error state"
    assert reloaded.error_code is None

    r = client.get(f"/api/analyze/{job.id}/status")
    assert r.status_code == 200
    assert r.json()["status"] == "done"


def test_a_historical_result_is_never_recomputed_against_a_newly_activated_policy(client, db, monkeypatch):
    """The point of the result_data snapshot: after the account activates a
    DIFFERENT policy, the old analysis must keep meaning what it meant when
    it ran."""
    _install_no_op_search(monkeypatch)
    csrf = _configure_account(client, db, "policyshift@example.com")  # INCOMPLET-producing policy
    job = _run_analysis_to_terminal(client, csrf)
    original_decision = job.result.decision
    original_score = job.result.score_global
    original_version = job.scoring_policy_version
    assert original_decision == "INCOMPLET"

    # The account now configures complete business rules and activates that
    # new version — a later analysis would score differently.
    _save_draft(client, csrf, business_rules=COMPLETE_BUSINESS_RULES)
    activated = _activate(client, csrf, expected_active_version=original_version)
    assert activated.status_code == 200, activated.text

    _forget_every_non_sql_trace(job.id)
    reloaded = jobs.get_job(job.id)

    assert reloaded.result.decision == original_decision, (
        "activating a new policy must never retroactively change an already-run analysis"
    )
    assert reloaded.result.score_global == original_score
    assert reloaded.scoring_policy_version == original_version


def test_another_user_still_gets_404_for_a_job_served_from_sql(client, db, monkeypatch):
    """No global fallback: reading from SQL must not make a row belonging to
    a different user readable in any circumstance."""
    _install_no_op_search(monkeypatch)
    csrf = _configure_account(client, db, "sqlowner@example.com", business_rules=COMPLETE_BUSINESS_RULES)
    job = _run_analysis_to_terminal(client, csrf)
    _forget_every_non_sql_trace(job.id)

    make_active_starter_user(db, "sqlintruder@example.com", scoring=False)
    _login(client, "sqlintruder@example.com")

    r = client.get(f"/api/analyze/{job.id}/status")
    assert r.status_code == 404, "a job belonging to another account must be indistinguishable from a missing one"
    r = client.get(f"/api/download/{job.id}/pdf")
    assert r.status_code == 404


def test_a_job_id_with_no_row_anywhere_reads_as_missing(client, db, monkeypatch):
    """Never synthesize a plausible-looking substitute for an absent row."""
    _install_no_op_search(monkeypatch)
    _configure_account(client, db, "nosuchjob@example.com", business_rules=COMPLETE_BUSINESS_RULES)

    assert jobs.get_job("ffffffffffff") is None
    assert client.get("/api/analyze/ffffffffffff/status").status_code == 404


# ---------------------------------------------------------------------------
# Group B — a SQL write failure is reported honestly, never as durability
# the job does not actually have.
# ---------------------------------------------------------------------------

def test_sql_write_failure_reports_persistence_failed_and_claims_no_row(client, db, monkeypatch):
    _install_no_op_search(monkeypatch)
    from src.web.database.repositories import analyses as analyses_repo

    def _boom(*args, **kwargs):
        raise RuntimeError("database write boom")
    monkeypatch.setattr(analyses_repo, "upsert_analysis", _boom)

    csrf = _configure_account(client, db, "sqlwritefail@example.com", business_rules=COMPLETE_BUSINESS_RULES)
    job = _run_analysis_to_terminal(client, csrf)

    # B10-T1's established shape for a persistence failure: the analysis
    # itself succeeded, so the status stays "done" and the real result is
    # still in memory — but the failure IS surfaced, distinctly.
    assert job.status == "done"
    assert job.error_code == jobs.PERSISTENCE_FAILED_ERROR_CODE
    assert "database write boom" not in (job.error or ""), "raw exception detail must never reach the public field"
    assert job.result is not None and job.ao is not None

    r = client.get(f"/api/analyze/{job.id}/status")
    assert r.status_code == 200
    assert r.json()["error_code"] == jobs.PERSISTENCE_FAILED_ERROR_CODE

    # Nothing was durably written — and the read path must not pretend
    # otherwise once the in-process copy is gone.
    assert _count_analyses(db, job.id) == 0
    _forget_every_non_sql_trace(job.id)
    assert jobs.get_job(job.id) is None, "a read must never claim a database row that was never written"


def test_a_failed_early_save_is_recovered_by_the_post_render_refresh(client, db, monkeypatch):
    """The snapshot save runs twice (before and after rendering) and is
    idempotent, so a TRANSIENT failure of the early one self-heals instead
    of losing the analysis — and it is not reported as a failure once the
    row is actually there."""
    _install_no_op_search(monkeypatch)
    from src.web.database.repositories import analyses as analyses_repo

    real_upsert = analyses_repo.upsert_analysis
    calls = {"n": 0}

    def _fail_first_only(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient database blip")
        return real_upsert(*args, **kwargs)
    monkeypatch.setattr(analyses_repo, "upsert_analysis", _fail_first_only)

    csrf = _configure_account(client, db, "transientblip@example.com", business_rules=COMPLETE_BUSINESS_RULES)
    job = _run_analysis_to_terminal(client, csrf)

    assert calls["n"] >= 2, "the snapshot save must be attempted again after rendering"
    assert _count_analyses(db, job.id) == 1
    _forget_every_non_sql_trace(job.id)
    assert jobs.get_job(job.id) is not None, "the retry made the analysis durable after all"


# ---------------------------------------------------------------------------
# Group C — analysis upsert idempotency and owner safety.
# ---------------------------------------------------------------------------

def test_upsert_analysis_twice_updates_one_row_instead_of_inserting_a_second(db):
    from src.web.database.repositories import analyses as analyses_repo

    user = make_active_starter_user(db, "upsertone@example.com")
    org_id = default_org_id(db, user)

    first = analyses_repo.upsert_analysis(
        db, user_id=user.id, organization_id=org_id, job_id="retried-job",
        title="Premier titre", decision="GO", score=77.5, result_data={"pass": 1},
    )
    db.commit()
    first_id = first.id

    second = analyses_repo.upsert_analysis(
        db, user_id=user.id, organization_id=org_id, job_id="retried-job",
        title="Second titre", decision="INCOMPLET", score=42.0, result_data={"pass": 2},
    )
    db.commit()

    assert _count_analyses(db, "retried-job") == 1, "a retried persistence attempt must never duplicate the row"
    assert second.id == first_id, "the same row was updated, not replaced"
    assert second.title == "Second titre"
    assert second.decision == "INCOMPLET"
    assert float(second.score) == 42.0
    assert second.result_data == {"pass": 2}


def test_upsert_analysis_refuses_a_row_owned_by_someone_else(db):
    from src.web.database.repositories import analyses as analyses_repo
    from src.web.database.repositories.analyses import AnalysisOwnershipConflict

    owner = make_active_starter_user(db, "rowowner@example.com")
    intruder = make_active_starter_user(db, "rowintruder@example.com")
    owner_org = default_org_id(db, owner)
    intruder_org = default_org_id(db, intruder)

    analyses_repo.upsert_analysis(
        db, user_id=owner.id, organization_id=owner_org, job_id="contested-job",
        title="Analyse du proprietaire", decision="GO", result_data={"owner": "yes"},
    )
    db.commit()

    with pytest.raises(AnalysisOwnershipConflict):
        analyses_repo.upsert_analysis(
            db, user_id=intruder.id, organization_id=intruder_org, job_id="contested-job",
            title="Tentative", decision="NO-GO", result_data={"owner": "no"},
        )
    db.rollback()

    surviving = analyses_repo.get_by_job_id(db, "contested-job")
    assert surviving.user_id == owner.id, "another owner's row must never be overwritten"
    assert surviving.title == "Analyse du proprietaire"
    assert surviving.result_data == {"owner": "yes"}
    assert _count_analyses(db, "contested-job") == 1


def test_upsert_analysis_requires_a_job_id(db):
    from src.web.database.repositories import analyses as analyses_repo

    user = make_active_starter_user(db, "nojobid@example.com")
    with pytest.raises(ValueError):
        analyses_repo.upsert_analysis(
            db, user_id=user.id, organization_id=default_org_id(db, user), job_id="", result_data={},
        )


# ---------------------------------------------------------------------------
# Group D — document upsert idempotency (a rendering retry).
# ---------------------------------------------------------------------------

def test_upsert_document_twice_for_one_mime_type_keeps_exactly_one_row(db):
    from src.web.database.repositories import analyses as analyses_repo

    user = make_active_starter_user(db, "docupsert@example.com")
    org_id = default_org_id(db, user)
    analysis = analyses_repo.upsert_analysis(
        db, user_id=user.id, organization_id=org_id, job_id="doc-job", result_data={},
    )
    db.commit()

    pdf_mime = "application/pdf"
    docx_mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

    first = analyses_repo.upsert_document(
        db, analysis_id=analysis.id, user_id=user.id, organization_id=org_id,
        filename="v1.pdf", original_filename="rapport.pdf", storage_path="stored/v1.pdf",
        mime_type=pdf_mime, file_size=111,
    )
    db.commit()

    second = analyses_repo.upsert_document(
        db, analysis_id=analysis.id, user_id=user.id, organization_id=org_id,
        filename="v2.pdf", original_filename="rapport.pdf", storage_path="stored/v2.pdf",
        mime_type=pdf_mime, file_size=222,
    )
    db.commit()

    count = db.execute(
        select(func.count()).select_from(AnalysisDocument).where(
            AnalysisDocument.analysis_id == analysis.id, AnalysisDocument.mime_type == pdf_mime,
        )
    ).scalar_one()
    assert count == 1, "a re-render must update the existing document row, never add a second one"
    assert second.id == first.id
    assert second.storage_path == "stored/v2.pdf"
    assert second.file_size == 222

    # A DIFFERENT mime_type is a different document, not a duplicate.
    analyses_repo.upsert_document(
        db, analysis_id=analysis.id, user_id=user.id, organization_id=org_id,
        filename="v1.docx", original_filename="candidature.docx", storage_path="stored/v1.docx",
        mime_type=docx_mime, file_size=333,
    )
    db.commit()
    total = db.execute(
        select(func.count()).select_from(AnalysisDocument).where(AnalysisDocument.analysis_id == analysis.id)
    ).scalar_one()
    assert total == 2


def test_a_full_rerun_of_both_database_operations_leaves_one_row_each(client, db, monkeypatch):
    """The real flow's own idempotency: replaying both operations for an
    already-persisted job (a repair/retry path) must reconcile, not
    duplicate."""
    _install_no_op_search(monkeypatch)
    csrf = _configure_account(client, db, "replayjob@example.com", business_rules=COMPLETE_BUSINESS_RULES)
    job = _run_analysis_to_terminal(client, csrf)

    assert _count_analyses(db, job.id) == 1
    analysis_id = db.execute(select(Analysis.id).where(Analysis.job_id == job.id)).scalar_one()
    before = db.execute(
        select(func.count()).select_from(AnalysisDocument).where(AnalysisDocument.analysis_id == analysis_id)
    ).scalar_one()
    assert before == 2, "a successful run attaches exactly one PDF and one DOCX"

    assert jobs._persist_to_database(job) is True

    assert _count_analyses(db, job.id) == 1
    after = db.execute(
        select(func.count()).select_from(AnalysisDocument).where(AnalysisDocument.analysis_id == analysis_id)
    ).scalar_one()
    assert after == 2, "replaying persistence must not duplicate either the analysis or its documents"


# ---------------------------------------------------------------------------
# Group E — reordering proof.
# ---------------------------------------------------------------------------

def test_analysis_row_reaches_sql_even_when_rendering_fails_entirely(client, db, monkeypatch):
    """The core architectural fix: the analysis snapshot is saved BEFORE and
    INDEPENDENTLY of rendering. With document generation failing outright,
    the row must still be in SQL with the real computed score/decision, and
    there must be no document rows at all (no file was produced)."""
    _install_no_op_search(monkeypatch)
    monkeypatch.setattr(
        jobs, "_generate_documents",
        lambda job, ao, result: (_ for _ in ()).throw(RuntimeError("render boom")),
    )

    csrf = _configure_account(client, db, "renderfailsql@example.com", business_rules=COMPLETE_BUSINESS_RULES)
    job = _run_analysis_to_terminal(client, csrf)

    assert job.status == "error"
    assert job.error_code == jobs.DOCUMENT_GENERATION_FAILED_ERROR_CODE
    assert job.files == {}

    row = db.execute(select(Analysis).where(Analysis.job_id == job.id)).scalar_one_or_none()
    assert row is not None, (
        "the computed analysis must reach the database independently of rendering — "
        "this is exactly what the pre-B11-T1 ordering lost"
    )
    assert row.decision == job.result.decision
    assert float(row.score) == pytest.approx(job.result.score_global)
    assert row.result_data["result"]["decision"] == job.result.decision

    documents = db.execute(
        select(func.count()).select_from(AnalysisDocument).where(AnalysisDocument.analysis_id == row.id)
    ).scalar_one()
    assert documents == 0, "no document row may exist when no document file was ever produced"


def test_anonymous_job_still_falls_back_to_the_legacy_json_artifact():
    """A job with user_id=None never gets a database row (there is no owner
    to file it under), so the local JSON file remains its ONLY durable
    trace — the last-resort branch of the fallback chain must keep
    working."""
    from src.core.models import AOContext, CriterionScore, ScoringResult

    job = jobs.create_job(source_label="anonyme")
    job.ao = AOContext(titre="AO anonyme", client="Client X")
    job.result = ScoringResult(
        decision="INCOMPLET", score_global=55.5,
        criteres=[CriterionScore(nom="Adequation expertise", score=60.0, poids=20.0, justification="x")],
        scoring_completeness="incomplete", scoring_missing=["budget_minimum_eur"],
    )
    job.status = "done"

    # The database write is skipped by design for an ownerless job.
    assert jobs._save_analysis_snapshot(job) is True
    jobs._persist(job)
    jobs._JOBS.pop(job.id, None)

    reloaded = jobs.get_job(job.id)
    assert reloaded is not None, "the legacy JSON artifact must still be readable for a job that has no DB row"
    assert reloaded.result.decision == "INCOMPLET"
    assert reloaded.result.score_global == 55.5
    assert reloaded.result.scoring_missing == ["budget_minimum_eur"]
    assert reloaded.user_id is None


def test_a_corrupted_sql_snapshot_degrades_exactly_like_a_corrupted_json_one(db):
    """One shared reconstruction function means the B18-T2 degradation
    contract applies to a blob read from SQL too — a non-finite stored
    score surfaces the controlled historical_score_unavailable error rather
    than constructing a ScoringResult carrying NaN."""
    from src.web.database.repositories import analyses as analyses_repo

    user = make_active_starter_user(db, "corruptsql@example.com")
    org_id = default_org_id(db, user)
    analyses_repo.upsert_analysis(
        db, user_id=user.id, organization_id=org_id, job_id="corrupt-job",
        result_data={
            "id": "corrupt-job",
            "ao": {"titre": "AO historique", "client": "Client"},
            "result": {
                "decision": "GO",
                "score_global": float("nan"),
                "criteres": [],
            },
            "files": {},
        },
    )
    db.commit()
    jobs._JOBS.pop("corrupt-job", None)

    reloaded = jobs.get_job("corrupt-job")
    assert reloaded is not None
    assert reloaded.status == "error"
    assert reloaded.error_code == jobs.HISTORICAL_SCORE_UNAVAILABLE_ERROR_CODE
    assert reloaded.result is None, "no ScoringResult may be built from a non-finite stored score"
    assert reloaded.ao is not None and reloaded.ao.titre == "AO historique"
    assert reloaded.user_id == user.id


def test_reading_from_sql_never_rewrites_the_stored_snapshot(db):
    """A read must not write back: the sanitized in-memory copy is private
    to the caller (B18-T2: the stored scores are never rewritten)."""
    from src.web.database.repositories import analyses as analyses_repo

    user = make_active_starter_user(db, "readonlysnap@example.com")
    org_id = default_org_id(db, user)
    stored = {
        "id": "readonly-job",
        "ao": {"titre": "AO lecture", "client": "Client"},
        "result": {
            "decision": "INCOMPLET", "score_global": 61.0, "criteres": [],
            # An invalid evidence entry: the read drops it from the copy it
            # returns, and must leave the stored blob untouched.
            "evidence_pack": [{"reference": "R1", "extrait": "x", "score": 42.0}],
        },
        "files": {},
    }
    analyses_repo.upsert_analysis(
        db, user_id=user.id, organization_id=org_id, job_id="readonly-job",
        result_data={k: (v.copy() if isinstance(v, dict) else v) for k, v in stored.items()},
    )
    db.commit()
    jobs._JOBS.pop("readonly-job", None)

    reloaded = jobs.get_job("readonly-job")
    assert reloaded is not None and reloaded.result is not None
    assert reloaded.result.decision == "INCOMPLET"
    assert reloaded.result.data_integrity == "degraded"
    assert reloaded.result.evidence_pack == []

    db.expire_all()
    row = analyses_repo.get_by_job_id(db, "readonly-job")
    assert len(row.result_data["result"]["evidence_pack"]) == 1, (
        "reading an analysis must never rewrite the stored snapshot"
    )
    assert "data_integrity" not in row.result_data["result"]
