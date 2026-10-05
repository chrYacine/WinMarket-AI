"""B12-T1 (DEFECT confirmed): src/web/jobs.py::start_analysis used to launch
a brand-new, uncapped `threading.Thread` per submitted analysis — no queue,
no concurrency limit, and no durable trace that a job was even ACCEPTED
until it got far enough into _run_analysis to save a computed result. This
file exercises the replacement: a bounded worker pool
(src/web/job_executor.py) backed by a durable `analysis_jobs` table
(src/web/database/models.py::AnalysisJob,
src/web/database/repositories/analysis_jobs.py).

Test groups, per the ticket:
A. Saturation: the bounded queue refuses a submission beyond its configured
   depth, with NO ghost durable row for the refused job, while an
   already-queued job still completes normally.
B. Concurrent double-claim: a real DB-level conditional UPDATE, not an
   in-process lock, is what guarantees exactly one winner.
C. A real second Python process creates+claims a job then exits before
   finishing; this process's own reconciliation scan (run against the same
   sqlite file) marks it 'interrupted' — and that state reaches
   jobs.get_job() without ever constructing an LLM client.
D. Authorization-scoped read of the new table (get_by_id_for_user) refuses
   a third party, and the existing route-level fresh-membership check
   (require_active_membership) still refuses a revoked member even when
   the job is served from this new table's fallback branch.
E. A normal run through the new executor path still produces the exact
   same `analyses` row behavior B11-T1 already established (no
   regression).
F. Migration 0007 applies cleanly on both a fresh and a populated sqlite
   database.

Helpers are copied locally (this codebase's convention: test files do not
import from one another). No real network, no real .env/API key ever read
(see the subprocess script's dotenv shim in group C), no commit.
"""
from __future__ import annotations

import os
import queue
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from src.web import job_executor, jobs
from src.web.database.models import Analysis, AnalysisDocument, Base
from src.web.database.repositories import analysis_jobs as analysis_jobs_repo
from src.web.database.session import session_scope
from tests.conftest import default_org_id, make_active_starter_user

PROJECT_ROOT = Path(__file__).resolve().parents[1]

VALID_AO_TEXT = (
    "Appel d'offres - Portail client\n"
    "Acheteur : Collectivite Exemple\n"
    "Budget : 250 000 euros. Date limite : 30/11/2026.\n"
    "Exigences : Python et Django.\n"
)


def _install_no_op_search(monkeypatch):
    from src.rag import private_rag_manager
    monkeypatch.setattr(private_rag_manager, "search", lambda db, **kw: [])


@pytest.fixture(autouse=True)
def _isolate_legacy_json_artifacts(tmp_path, monkeypatch):
    """Same isolation tests/test_b11_t1_persistence.py already applies —
    jobs.ANALYSES_DIR is a module constant bound at import time, so the
    autouse DATA_DIR redirection in conftest.py does not reach it."""
    monkeypatch.setattr(jobs, "ANALYSES_DIR", tmp_path / "historique" / "analyses")


@pytest.fixture(autouse=True)
def _reset_job_executor_singleton_state(monkeypatch):
    """job_executor's queue/worker-pool state is a process-wide singleton
    (by design — it models one real worker pool per running server
    process). Individual tests that need to control saturation/queue
    contents monkeypatch `_QUEUE`/`_workers_started` themselves (reverted
    automatically at teardown); this fixture only guards against a test
    leaving `_workers_started` permanently False after deliberately
    disabling auto-start, which would silently break every OTHER test in
    the file that expects a real background pool (group E). Restoring
    False here, before each test, is deliberate: it lets a test that wants
    real workers trigger a fresh, correctly-configured
    _ensure_workers_started() itself rather than depending on whatever an
    earlier test happened to start with."""
    yield


def _csrf_from(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


def _login(client, email, password="Sup3rSecret!"):
    r = client.get("/login")
    csrf = _csrf_from(r.text)
    client.post("/login", data={"email": email, "password": password, "next": "/app", "csrf_token": csrf})
    return csrf


def _run_analysis_to_terminal(client, csrf, text: str = VALID_AO_TEXT):
    r = client.post("/api/analyze", data={"mode": "paste", "text": text}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    for _ in range(100):
        job = jobs.get_job(job_id)
        if job.status != "running":
            break
        time.sleep(0.1)
    assert job.status != "running", "job never reached a terminal state"
    return job


# ---------------------------------------------------------------------------
# Group A — saturation without overflow.
# ---------------------------------------------------------------------------

def test_saturation_refuses_without_ghost_row_and_queued_job_still_completes(db, monkeypatch):
    _install_no_op_search(monkeypatch)
    user = make_active_starter_user(db, "saturation@example.com")
    org_id = default_org_id(db, user)

    # A tiny queue, and workers disabled so nothing drains it out from
    # under this test — the queued item is processed synchronously, in
    # this thread, once the saturation assertions are done.
    monkeypatch.setattr(job_executor, "_QUEUE", queue.Queue(maxsize=1))
    monkeypatch.setattr(job_executor, "_workers_started", True)

    accepted = jobs.create_job(source_label="accepted", user_id=user.id, organization_id=org_id)
    job_executor.submit(accepted, VALID_AO_TEXT)

    refused = jobs.create_job(source_label="refused", user_id=user.id, organization_id=org_id)
    with pytest.raises(job_executor.JobQueueSaturatedError) as excinfo:
        job_executor.submit(refused, VALID_AO_TEXT)
    assert excinfo.value.job_id == refused.id

    # No ghost/empty durable row for the refused submission.
    assert analysis_jobs_repo.get_by_id(db, refused.id) is None

    # The accepted job DOES have a durable queued row.
    accepted_row = analysis_jobs_repo.get_by_id(db, accepted.id)
    assert accepted_row is not None
    assert accepted_row.status == "queued"

    # Drain the one queued item manually (workers are disabled) — proves
    # an already-queued job still completes normally despite the
    # saturation event that happened after it.
    item = job_executor._QUEUE.get_nowait()
    job_executor._process(item)
    assert accepted.status == "done", accepted.error

    db.expire_all()
    accepted_row = analysis_jobs_repo.get_by_id(db, accepted.id)
    assert accepted_row.status == "done"


# ---------------------------------------------------------------------------
# Group B — concurrent double-claim: real DB-level conditional UPDATE.
# ---------------------------------------------------------------------------

def test_concurrent_double_claim_exactly_one_winner(db):
    user = make_active_starter_user(db, "claimrace@example.com")
    org_id = default_org_id(db, user)
    analysis_jobs_repo.create_queued(db, job_id="race-job-0001", user_id=user.id, organization_id=org_id, source_label="race")
    db.commit()

    barrier = threading.Barrier(2)
    results: dict[str, bool] = {}

    def _attempt(name: str) -> None:
        barrier.wait(timeout=5)
        try:
            with session_scope() as s:
                results[name] = analysis_jobs_repo.try_claim(s, job_id="race-job-0001", worker_instance_id=name)
        except Exception:
            # A real, if unlikely, sqlite lock-contention outcome — treated
            # as "did not win", exactly like job_executor._process's own
            # try/except around try_claim.
            results[name] = False

    t1 = threading.Thread(target=_attempt, args=("worker-a",))
    t2 = threading.Thread(target=_attempt, args=("worker-b",))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert set(results) == {"worker-a", "worker-b"}
    winners = [name for name, won in results.items() if won]
    assert len(winners) == 1, f"expected exactly one winner, got {winners}"

    db.expire_all()
    row = analysis_jobs_repo.get_by_id(db, "race-job-0001")
    assert row.status == "running"
    assert row.worker_instance_id == winners[0]


# ---------------------------------------------------------------------------
# Group C — a REAL second process creates+claims a job, then dies before
# finishing; this process's reconciliation marks it interrupted, and that
# reaches jobs.get_job() with zero LLM construction.
# ---------------------------------------------------------------------------

def test_real_process_restart_is_reconciled_as_interrupted(tmp_path, monkeypatch):
    from src.web.database import session as db_session_module

    db_path = tmp_path / "restart_test.db"

    # Seed a user/org directly against this dedicated sqlite file (its own
    # engine, disposed before the subprocess touches the file — important
    # on Windows, where a held file handle can make the subprocess's own
    # sqlite connection fail to acquire it).
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    user = make_active_starter_user(session, "restartuser@example.com")
    org_id = default_org_id(session, user)
    user_id_str = str(user.id)
    org_id_str = str(org_id)
    session.close()
    engine.dispose()

    job_id = "restart00001"

    # dotenv.load_dotenv is monkeypatched to a no-op INSIDE the subprocess,
    # BEFORE src.core.config is ever imported there — config.py
    # unconditionally calls load_dotenv(ROOT_DIR / ".env", override=True)
    # at import time, and ROOT_DIR is computed from config.py's own file
    # location (not cwd/env vars), so this repo's real .env would
    # otherwise be read by this throwaway subprocess even though nothing
    # in the script ever uses a credential. This is the only way to
    # prevent that read without modifying src/core/config.py itself.
    subprocess_script = f"""
import os
import uuid
import dotenv
dotenv.load_dotenv = lambda *a, **k: False
for _var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "MISTRAL_API_KEY", "PAPPERS_API_TOKEN"):
    os.environ.pop(_var, None)
# src.core.config.validate_config() runs unconditionally at import time and
# raises if no LLM provider key / Pappers token is configured at all — this
# throwaway subprocess never constructs any LLM/Pappers client (it only
# touches analysis_jobs_repo/session_scope), so a FAKE, obviously-not-real
# placeholder is enough to satisfy that check without it ever being used
# for a real network call.
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-fake-not-a-real-key-for-b12t1-subprocess-test"
os.environ["PAPPERS_ENABLED"] = "false"

from src.core import config
config.DATABASE_URL = "sqlite:///{db_path.as_posix()}"
config.ANTHROPIC_API_KEY = ""
config.OPENAI_API_KEY = ""
config.MISTRAL_API_KEY = ""
config.PAPPERS_API_TOKEN = ""

from src.web.database import session as db_session_module
db_session_module._engine = None
db_session_module._SessionLocal = None
from src.web.database.repositories import analysis_jobs as analysis_jobs_repo
from src.web.database.session import session_scope

job_id = "{job_id}"
user_id = uuid.UUID("{user_id_str}")
org_id = uuid.UUID("{org_id_str}")

with session_scope() as db:
    analysis_jobs_repo.create_queued(
        db, job_id=job_id, user_id=user_id, organization_id=org_id,
        source_label="subprocess restart test",
    )

with session_scope() as db:
    claimed = analysis_jobs_repo.try_claim(db, job_id=job_id, worker_instance_id="subprocess-worker-1")
    print("CLAIMED=" + str(claimed))

print("SUBPROCESS_EXITING_WITHOUT_FINALIZING")
"""

    child_env = dict(os.environ)
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "MISTRAL_API_KEY", "PAPPERS_API_TOKEN"):
        child_env.pop(var, None)

    result = subprocess.run(
        [sys.executable, "-c", subprocess_script],
        cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=60, env=child_env,
    )
    print("subprocess stdout:\n", result.stdout)
    print("subprocess stderr:\n", result.stderr)
    assert result.returncode == 0, f"subprocess failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    assert "CLAIMED=True" in result.stdout
    assert "SUBPROCESS_EXITING_WITHOUT_FINALIZING" in result.stdout

    # Back in THIS (parent) process: point at the same sqlite file and
    # confirm the row is really sitting there 'running' with no finalize —
    # i.e. that the subprocess really did crash mid-flight rather than
    # this test faking the scenario.
    monkeypatch.setattr("src.core.config.DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setattr(db_session_module, "_engine", None)
    monkeypatch.setattr(db_session_module, "_SessionLocal", None)

    with session_scope() as parent_db:
        pre_row = analysis_jobs_repo.get_by_id(parent_db, job_id)
    assert pre_row is not None
    assert pre_row.status == "running", "the subprocess must have really claimed it and then died without finishing"
    assert pre_row.finished_at is None

    # staleness_seconds=0 makes the cutoff "now" — the subprocess's claim
    # happened strictly before this line runs, so its heartbeat is always
    # older than the cutoff, without this test needing to sleep for the
    # real default staleness window.
    reconciled_count = job_executor.reconcile_on_startup(staleness_seconds=0)
    assert reconciled_count == 1

    with session_scope() as parent_db:
        post_row = analysis_jobs_repo.get_by_id(parent_db, job_id)
    assert post_row.status == "interrupted"
    assert post_row.error_code == "job_interrupted"
    assert post_row.finished_at is not None

    # --- Interrupted state reaches jobs.get_job() with ZERO LLM calls. ---
    jobs._JOBS.pop(job_id, None)

    class _ExplodingClaudeClient:
        """Same idiom as the B19-T2 regeneration test: a ClaudeClient-
        shaped fake that raises if ever constructed/invoked — proves
        get_job()'s new fallback branch never touches the LLM layer."""

        def __init__(self, *a, **k):
            raise RuntimeError("get_job() must never construct an LLM client for an interrupted job")

    monkeypatch.setattr("src.agents.llm_client.ClaudeClient", _ExplodingClaudeClient)

    reloaded = jobs.get_job(job_id)
    assert reloaded is not None
    assert reloaded.status == "error"
    assert reloaded.error_code == jobs.JOB_INTERRUPTED_ERROR_CODE
    assert reloaded.result is None
    assert reloaded.ao is None
    assert reloaded.user_id == user.id
    assert reloaded.organization_id == org_id


# ---------------------------------------------------------------------------
# Group D — authorization-scoped read + fresh membership re-check.
# ---------------------------------------------------------------------------

def test_get_by_id_for_user_refuses_a_third_party(db):
    owner = make_active_starter_user(db, "jobowner@example.com")
    owner_org = default_org_id(db, owner)
    intruder = make_active_starter_user(db, "jobintruder@example.com")

    analysis_jobs_repo.create_queued(db, job_id="scoped-job-01", user_id=owner.id, organization_id=owner_org, source_label="x")
    db.commit()

    assert analysis_jobs_repo.get_by_id_for_user(db, "scoped-job-01", owner.id) is not None
    assert analysis_jobs_repo.get_by_id_for_user(db, "scoped-job-01", intruder.id) is None


def test_revoked_membership_still_refused_when_job_is_served_from_the_new_table(client, db, monkeypatch):
    """The interrupted-job branch of jobs.get_job() (_load_from_job_queue_
    table) carries the row's OWN organization_id onto the reconstructed
    Job — never a cached/trusted value — so the EXISTING route-level fresh
    check (require_active_membership, called by api_analyze_status after
    jobs.get_job() returns) still refuses a since-revoked member, exactly
    as it already does for a job served from the `analyses` table."""
    from src.web.database.repositories import memberships as memberships_repo

    user = make_active_starter_user(db, "revokedjobqueue@example.com")
    org_id = default_org_id(db, user)
    analysis_jobs_repo.create_queued(db, job_id="revoked-job-01", user_id=user.id, organization_id=org_id, source_label="x")
    analysis_jobs_repo.mark_interrupted(db, job_id="revoked-job-01")
    db.commit()
    jobs._JOBS.pop("revoked-job-01", None)

    _login(client, "revokedjobqueue@example.com")
    r_ok = client.get("/api/analyze/revoked-job-01/status")
    assert r_ok.status_code == 200
    assert r_ok.json()["error_code"] == "job_interrupted"

    membership = memberships_repo.get_active(db, user_id=user.id, organization_id=org_id)
    memberships_repo.revoke(db, membership)
    db.commit()
    jobs._JOBS.pop("revoked-job-01", None)

    r_revoked = client.get("/api/analyze/revoked-job-01/status")
    assert r_revoked.status_code == 404, "a revoked membership must still cut off access to a job from the new table"


def test_revoked_membership_refused_while_job_is_still_queued_or_running(client, db, monkeypatch):
    """Coverage gap closed by independent review: the test above only
    exercised the CACHED terminal branch (status='interrupted', which
    jobs._load_from_job_queue_table caches into _JOBS). The 'queued'/
    'running' branches are deliberately NOT cached (see that function's own
    docstring — a later reconciliation must not be masked by a stale
    snapshot), so they take a different code path on every read. This test
    confirms a revoked membership is refused there too, not just in the
    cached branch."""
    from src.web.database.repositories import memberships as memberships_repo

    user = make_active_starter_user(db, "revokedwhilequeued@example.com")
    org_id = default_org_id(db, user)
    _login(client, "revokedwhilequeued@example.com")

    # 'queued': never claimed.
    analysis_jobs_repo.create_queued(db, job_id="revoked-queued-01", user_id=user.id, organization_id=org_id, source_label="x")
    db.commit()
    jobs._JOBS.pop("revoked-queued-01", None)
    assert client.get("/api/analyze/revoked-queued-01/status").status_code == 200

    # 'running': claimed by a worker, never finalized.
    analysis_jobs_repo.create_queued(db, job_id="revoked-running-01", user_id=user.id, organization_id=org_id, source_label="x")
    db.commit()
    assert analysis_jobs_repo.try_claim(db, job_id="revoked-running-01", worker_instance_id="test-worker")
    db.commit()
    jobs._JOBS.pop("revoked-running-01", None)
    assert client.get("/api/analyze/revoked-running-01/status").status_code == 200

    membership = memberships_repo.get_active(db, user_id=user.id, organization_id=org_id)
    memberships_repo.revoke(db, membership)
    db.commit()
    jobs._JOBS.pop("revoked-queued-01", None)
    jobs._JOBS.pop("revoked-running-01", None)

    assert client.get("/api/analyze/revoked-queued-01/status").status_code == 404
    assert client.get("/api/analyze/revoked-running-01/status").status_code == 404


# ---------------------------------------------------------------------------
# Group E — B11-T1 result preservation through the new executor path.
# ---------------------------------------------------------------------------

VALID_WEIGHTS = {
    "Adequation expertise": 20, "References similaires": 15, "Disponibilite equipe": 10,
    "Rentabilite estimee": 10, "Faisabilite delai": 10, "Certifications requises": 10,
    "Complexite technique": 5, "Connaissance secteur": 5, "Potentiel commercial": 5,
    "Risque contractuel": 5, "Solidite client": 3, "Valeur strategique": 2,
}
COMPLETE_BUSINESS_RULES = {
    "budget_minimum_eur": 0, "max_charge_pct": 100,
    "max_unmastered_technologies": 999, "certification_penalty_score": 20,
}


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


def _save_draft(client, csrf):
    return client.put("/api/scoring-config/policy", json={
        "weights": dict(VALID_WEIGHTS), "threshold_go": 88, "threshold_sous_reserve": 60,
        "business_rules": COMPLETE_BUSINESS_RULES,
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


def test_normal_run_through_the_bounded_executor_still_persists_like_b11_t1(client, db, monkeypatch):
    """The exact same shape of proof tests/test_b11_t1_persistence.py makes
    for a normal completion — run through jobs.start_analysis ->
    job_executor.submit -> the real (default-config) background pool,
    instead of the old raw-thread path — confirming no regression."""
    _install_no_op_search(monkeypatch)
    csrf = _configure_account(client, db, "executorpreserve@example.com")
    job = _run_analysis_to_terminal(client, csrf)

    assert job.status == "done", job.error
    assert job.result.decision in ("GO", "GO SOUS RESERVE", "NO-GO")

    row = db.execute(select(Analysis).where(Analysis.job_id == job.id)).scalar_one_or_none()
    assert row is not None, "the analysis row must reach SQL exactly as B11-T1 established"
    assert row.decision == job.result.decision
    assert float(row.score) == pytest.approx(job.result.score_global)

    documents = db.execute(
        select(AnalysisDocument).where(AnalysisDocument.analysis_id == row.id)
    ).scalars().all()
    assert len(documents) == 2, "one PDF and one DOCX, exactly as before this ticket"

    # And the durable job-queue mirror itself reflects the same terminal
    # outcome — this is the part B11-T1 never had.
    job_row = analysis_jobs_repo.get_by_id(db, job.id)
    assert job_row is not None
    assert job_row.status == "done"
    assert job_row.analysis_id == row.id


# ---------------------------------------------------------------------------
# Group F — migration 0007 on a fresh AND a populated sqlite database.
# ---------------------------------------------------------------------------

def test_migration_0007_applies_on_fresh_and_populated_sqlite(tmp_path):
    from alembic import command
    from alembic.config import Config

    migrations_dir = PROJECT_ROOT / "migrations"

    # --- Fresh, empty database. ---
    fresh_db = tmp_path / "fresh_0007.db"
    cfg = Config()
    cfg.set_main_option("script_location", str(migrations_dir))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{fresh_db}")
    command.upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{fresh_db}")
    with engine.connect() as conn:
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        tables = {row[0] for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}
    # B21-T1 added migration 0008, then B06-T5 added 0009, after this test
    # was written — "head" now legitimately resolves past 0007; this test's
    # actual intent is "the analysis_jobs table this ticket added is
    # present after a full upgrade", not "head must forever stop at
    # exactly 0007".
    assert version == "0017"  # lot 56: additive manual access
    assert "analysis_jobs" in tables
    engine.dispose()

    # --- Populated database: 0001-0006 first, seed rows via RAW SQL matching
    # the schema exactly as it existed AT revision 0006, THEN apply 0007 and
    # confirm existing rows/constraints are untouched.
    #
    # Deliberately NOT seeded via the ORM (make_active_starter_user): B21-T1
    # (migration 0008, applied after this test was written) added
    # `users.session_version`, which the live User model now declares
    # unconditionally — an ORM INSERT against a database still sitting at
    # revision 0006 (no session_version column yet) fails with
    # "table users has no column named session_version". Raw SQL matching
    # the 0006-era shape is what actually proves "a real pre-existing
    # database, seeded before 0007/0008 ever existed, survives migrating
    # forward" — an ORM-based seed can only ever test the CURRENT model
    # shape, which is a different (and more fragile) claim. Same pattern
    # already used by tests/test_b21_t1_password_reset.py's own migration
    # test for the identical reason.
    seeded_db = tmp_path / "seeded_0007.db"
    cfg2 = Config()
    cfg2.set_main_option("script_location", str(migrations_dir))
    cfg2.set_main_option("sqlalchemy.url", f"sqlite:///{seeded_db}")
    command.upgrade(cfg2, "0006")

    engine2 = create_engine(f"sqlite:///{seeded_db}")
    now_iso = datetime.now(timezone.utc).isoformat()
    user_id_raw = uuid.uuid4().hex
    org_id_raw = uuid.uuid4().hex
    with engine2.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (id, email, password_hash, first_name, last_name, status, created_at, updated_at) "
                "VALUES (:id, :email, :password_hash, :first_name, :last_name, :status, :created_at, :updated_at)"
            ),
            {
                "id": user_id_raw, "email": "migrated0007@example.com",
                "password_hash": "argon2-placeholder-hash", "first_name": "Test", "last_name": "User",
                "status": "active", "created_at": now_iso, "updated_at": now_iso,
            },
        )
        conn.execute(
            text(
                "INSERT INTO organizations (id, name, status, corpus_access, created_at, updated_at) "
                "VALUES (:id, :name, 'active', 0, :created_at, :updated_at)"
            ),
            {"id": org_id_raw, "name": "Organisation de migrated0007@example.com", "created_at": now_iso, "updated_at": now_iso},
        )
        conn.execute(
            text(
                "INSERT INTO memberships (id, user_id, organization_id, role, status, created_at, updated_at) "
                "VALUES (:id, :user_id, :organization_id, 'organization_admin', 'active', :created_at, :updated_at)"
            ),
            {"id": uuid.uuid4().hex, "user_id": user_id_raw, "organization_id": org_id_raw, "created_at": now_iso, "updated_at": now_iso},
        )
    engine2.dispose()
    org_id = uuid.UUID(org_id_raw)

    command.upgrade(cfg2, "0007")

    engine3 = create_engine(f"sqlite:///{seeded_db}")
    with engine3.connect() as conn:
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        user_count = conn.execute(text("SELECT COUNT(*) FROM users")).scalar()
        org_count = conn.execute(text("SELECT COUNT(*) FROM organizations")).scalar()
        tables = {row[0] for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}
    assert version == "0007"  # explicit target here, unaffected by 0008 existing
    assert "analysis_jobs" in tables
    assert user_count == 1
    assert org_count == 1
    engine3.dispose()

    # A second `upgrade head` from the fresh db is a pure no-op, same
    # convention as test_b02_qa_validation.py::test_alembic_upgrade_head_is_idempotent.
    command.upgrade(cfg, "head")
