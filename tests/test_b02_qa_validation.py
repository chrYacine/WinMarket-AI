"""B02 QA validation pass — gaps not already covered by
tests/test_b02_organizations.py, tests/test_b02_migration.py, or the B01
suite (tests/test_document_isolation*.py, tests/test_saas_isolation.py).

This file is diagnostic: it exists to prove or disprove specific claims in
the B02 QA ticket (T01, T03, T06, T09, T10, T12, T14, T15, T16), not to
re-implement application code. Where a real gap was found, the test is
marked xfail(strict=True) with the defect explained in its reason= — never
silently weakened to a passing assertion. See docs/qa/B02_VALIDATION.md for
the full defect writeup.
"""
from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest
from docx import Document as DocxDocument
from pypdf import PdfReader

from src.core.models import AOContext, ScoringResult
from tests.conftest import default_org_id, make_active_starter_user


def _csrf_from(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


def _login(client, email, password="Sup3rSecret!"):
    r = client.get("/login")
    csrf = _csrf_from(r.text)
    client.post("/login", data={"email": email, "password": password, "next": "/app", "csrf_token": csrf})
    # B14-T1: mutating routes now require the same double-submit CSRF token
    # via the X-CSRF-Token header. get_or_create_csrf_token() reuses the
    # cookie's token, so the value minted for the login page's hidden field
    # stays valid for the rest of this session — return it for callers.
    return csrf


def _ao(titre: str = "AO test") -> AOContext:
    return AOContext(titre=titre, client="Client Test", secteur="Retail")


def _result(decision: str = "GO", score: float = 90.0) -> ScoringResult:
    return ScoringResult(decision=decision, score_global=score, criteres=[])


@pytest.fixture()
def isolated_storage(tmp_path, monkeypatch):
    from src.core import config
    from src.web import jobs as jobs_module

    output_dir = tmp_path / "outputs"
    output_dir.mkdir()
    monkeypatch.setattr(config, "OUTPUT_DIR", output_dir)
    monkeypatch.setattr(config, "LOCAL_STORAGE_PATH", tmp_path)
    monkeypatch.setattr(jobs_module, "ANALYSIS_FILES_DIR", output_dir)
    return output_dir


def _persist_job_to_db(job, ao, result, db):
    from src.livrables.document_generator import _safe_name
    from src.web.database.repositories import analyses as analyses_repo
    from src.web.storage.service import get_storage_service

    analysis = analyses_repo.create_analysis(
        db, user_id=job.user_id, organization_id=job.organization_id, job_id=job.id, title=ao.titre,
        client_name=ao.client, score=result.score_global, decision=result.decision,
        result_data={"ao": ao.model_dump(), "result": result.model_dump()},
    )
    storage = get_storage_service()
    readable_stem = _safe_name(ao.titre)
    original_names = {"pdf": f"rapport_decision_{readable_stem}.pdf", "docx": f"candidature_{readable_stem}.docx"}
    for kind, path_str in job.files.items():
        path = Path(path_str)
        analyses_repo.add_document(
            db, analysis_id=analysis.id, user_id=job.user_id, organization_id=job.organization_id,
            filename=path.name, original_filename=original_names[kind],
            storage_path=storage.save(path),
            mime_type=("application/pdf" if kind == "pdf" else
                       "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
            file_size=path.stat().st_size,
        )
    db.commit()
    return analysis


# ---------------------------------------------------------------------------
# T01 — registration atomicity under an injected mid-transaction failure
# ---------------------------------------------------------------------------

def test_registration_atomic_rollback_on_injected_failure(client, db, monkeypatch):
    from src.web.auth import service as auth_service
    from src.web.database.repositories import memberships as memberships_repo
    from src.web.database.repositories import users as users_repo

    def _boom(db, user):
        raise RuntimeError("simulated failure after user+subscription, before organization")

    monkeypatch.setattr(auth_service, "create_private_organization_for_user", _boom)

    r = client.get("/register")
    csrf = _csrf_from(r.text)
    with pytest.raises(RuntimeError):
        client.post("/register", data={
            "first_name": "Test", "last_name": "Atomic", "email": "atomicfail@example.com",
            "company": "AtomicCo", "job_title": "", "phone": "",
            "password": "Sup3rSecret!", "password_confirm": "Sup3rSecret!",
            "accept_terms": "on", "csrf_token": csrf,
        })

    # The route commits exactly once, after create_private_organization_for_user
    # returns; closing an uncommitted session rolls it back (SQLAlchemy
    # Session.close() semantics) — nothing here should have survived.
    fresh_user = users_repo.get_by_email(db, "atomicfail@example.com")
    assert fresh_user is None, "a failed registration must leave no user row behind"


# ---------------------------------------------------------------------------
# T03 — a client-supplied user_id in a form body changes nothing
# ---------------------------------------------------------------------------

def test_spoofed_user_id_field_does_not_change_job_owner(client, db):
    from src.web import jobs

    user_a = make_active_starter_user(db, "spoof_target@example.com")
    user_b = make_active_starter_user(db, "spoof_actor@example.com")

    csrf = _login(client, "spoof_actor@example.com")
    r = client.post("/api/analyze", data={
        "mode": "paste", "text": "Texte d'appel d'offres.",
        "user_id": str(user_a.id),  # not a declared field anywhere — must be ignored
        "organization_id": str(default_org_id(db, user_a)),
    }, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200
    job_id = r.json()["job_id"]
    job = jobs.get_job(job_id)
    assert job.user_id == user_b.id
    assert job.organization_id == default_org_id(db, user_b)


# ---------------------------------------------------------------------------
# T04 — CSV export is scoped by owner, not just the HTML history page
# ---------------------------------------------------------------------------

def test_history_export_csv_is_scoped_by_owner(client, db):
    from src.web.database.repositories import analyses as analyses_repo

    user_a = make_active_starter_user(db, "exporta@example.com")
    user_b = make_active_starter_user(db, "exportb@example.com")
    analyses_repo.create_analysis(
        db, user_id=user_a.id, organization_id=default_org_id(db, user_a),
        job_id="export-jobA", title="TITRE_EXCLUSIF_EXPORT_A", result_data={},
    )
    analyses_repo.create_analysis(
        db, user_id=user_b.id, organization_id=default_org_id(db, user_b),
        job_id="export-jobB", title="TITRE_EXCLUSIF_EXPORT_B", result_data={},
    )
    db.commit()

    _login(client, "exporta@example.com")
    r = client.get("/api/history/export.csv")
    assert r.status_code == 200
    assert "TITRE_EXCLUSIF_EXPORT_A" in r.text
    assert "TITRE_EXCLUSIF_EXPORT_B" not in r.text


# ---------------------------------------------------------------------------
# T06 — profile update cannot self-promote a role; capacity write permission
# ---------------------------------------------------------------------------

def test_profile_update_cannot_change_role(client, db):
    from src.web.database.repositories import memberships as memberships_repo

    admin = make_active_starter_user(db, "selfpromoadmin@example.com")
    org_id = default_org_id(db, admin)
    from tests.test_b02_organizations import _add_member  # reuse existing helper
    viewer = _add_member(db, organization_id=org_id, email="selfpromo@example.com", role="viewer")

    r = client.get("/login")
    csrf = _csrf_from(r.text)
    client.post("/login", data={"email": "selfpromo@example.com", "password": "Sup3rSecret!", "next": "/app", "csrf_token": csrf})

    r = client.get("/account")
    csrf2 = _csrf_from(r.text)
    client.post("/account", data={
        "first_name": "Still", "last_name": "Viewer", "company": "X", "job_title": "",
        "phone": "", "role": "organization_admin",  # not a real field — must be ignored
        "csrf_token": csrf2,
    })

    membership = memberships_repo.get_active(db, user_id=viewer.id, organization_id=org_id)
    assert membership.role == "viewer", "the /account form must never be able to change a Membership role"


def test_viewer_cannot_modify_capacity_configuration(client, db):
    """B02-C1, closed: POST /api/capacity now depends on
    require_permission('org:configure'), so a viewer is refused before the
    handler body — and even in B03's private-capacity world, there is no
    longer a real global file this could accidentally touch (routes_api.py
    writes only PrivateCapacityPlan rows via private_capacity_repo).
    Confirms both the refusal AND that the refusal has no side effect: the
    org's own private capacity plan (already configured by
    make_active_starter_user for realism) is unchanged after the attempt."""
    from tests.test_b02_organizations import _add_member
    from src.web.database.repositories import private_capacity as private_capacity_repo

    admin = make_active_starter_user(db, "capacityadmin@example.com")
    org_id = default_org_id(db, admin)
    viewer = _add_member(db, organization_id=org_id, email="capacityviewer@example.com", role="viewer", capacity=False)

    before = private_capacity_repo.get_for_owner(db, organization_id=org_id, owner_user_id=admin.id)
    before_version = before.version

    csrf = _login(client, "capacityviewer@example.com")
    r = client.post(
        "/api/capacity",
        json={"charge_globale_pct": 99, "nombre_projets_en_cours": 1, "projets_en_cours": [], "capacites_par_pole": {}},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 403

    # No side effect: the admin's own plan (the only one that could even be
    # confused with "the organization's capacity") is untouched, and the
    # viewer never got a plan of their own either.
    after = private_capacity_repo.get_for_owner(db, organization_id=org_id, owner_user_id=admin.id)
    assert after.version == before_version
    assert after.charge_globale_pct != 99
    assert private_capacity_repo.get_for_owner(db, organization_id=org_id, owner_user_id=viewer.id) is None


def test_knowledge_html_page_resolves_access_context_like_its_json_equivalent(client, db, monkeypatch):
    """B02-C2, closed (in its B03 form): /app/base-connaissances now calls
    get_access_context (the same resolution /api/knowledge uses) instead of
    reading the global pipeline directly. There is no longer a
    'corpus_access flag' scenario to gate here — the meaningful proof is
    that a genuinely unauthorized state (zero active memberships, e.g.
    after every membership was revoked) is refused cleanly by the page too,
    exactly like the JSON route, rather than crashing or falling back to
    something global."""
    from src.web.database.repositories import memberships as memberships_repo

    user = make_active_starter_user(db, "pagegate@example.com")
    org_id = default_org_id(db, user)
    _login(client, "pagegate@example.com")

    # Normal case: the page renders using this account's own (empty) corpus.
    r_ok = client.get("/app/base-connaissances")
    assert r_ok.status_code == 200

    # Revoke the only membership — get_access_context now has zero active
    # memberships to resolve, exactly the case /api/knowledge already
    # refuses with 403. The page must refuse the same way, not silently
    # render an empty page or fall back to a global corpus.
    membership = memberships_repo.get_active(db, user_id=user.id, organization_id=org_id)
    memberships_repo.revoke(db, membership)
    db.commit()

    r_revoked = client.get("/app/base-connaissances")
    assert r_revoked.status_code == 403
    r_json = client.get("/api/knowledge")
    assert r_json.status_code == 403


# ---------------------------------------------------------------------------
# T09 — a membership never activates a subscription
# ---------------------------------------------------------------------------

def test_membership_alone_does_not_activate_subscription(client, db):
    from src.web.auth import service as auth_service
    from src.web.database.repositories import memberships as memberships_repo
    from src.web.database.repositories import users as users_repo

    admin = make_active_starter_user(db, "subadmin@example.com")
    org_id = default_org_id(db, admin)

    # A user whose OWN subscription was never activated (registered, never
    # approved) — being added as a member of an already-paying org's
    # organization must not bypass that.
    user = users_repo.create_user(
        db, email="nosub@example.com", password_hash=auth_service.hash_password("Sup3rSecret!"),
        first_name="No", last_name="Sub", status="active",  # account itself active...
    )  # ...but deliberately no Subscription row created at all
    memberships_repo.create_membership(db, user_id=user.id, organization_id=org_id, role="analyst", status="active")
    db.commit()

    csrf = _login(client, "nosub@example.com")
    r = client.post("/api/analyze", data={"mode": "paste", "text": "texte"}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 403  # blocked by require_active_starter_user, unrelated to org role


# ---------------------------------------------------------------------------
# T10 — confirm SQLite FK enforcement is actually active for the app's engine
# ---------------------------------------------------------------------------

def test_sqlite_foreign_keys_pragma_is_active(db):
    from sqlalchemy import text

    value = db.execute(text("PRAGMA foreign_keys")).scalar()
    assert value == 1, "FK enforcement must be ON for the composite AnalysisDocument constraint to mean anything"


# ---------------------------------------------------------------------------
# T12 — real `alembic upgrade head`, run twice, is a no-op the second time
# ---------------------------------------------------------------------------

def test_alembic_upgrade_head_is_idempotent(tmp_path):
    from alembic import command
    from alembic.config import Config

    db_path = tmp_path / "alembic_head_idempotent.db"
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")

    command.upgrade(cfg, "head")  # 0001 -> ... -> 0006 against an empty DB (0 orphans, trivially passes 0003's check)
    command.upgrade(cfg, "head")  # must be a pure no-op: same head, nothing to do, no error

    from sqlalchemy import create_engine, text
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        tables = {row[0] for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}
    # B12-T1 added migration 0007 (analysis_jobs); B21-T1 added 0008
    # (session_version + password_reset_tokens.token_digest); B06-T5 added
    # 0009 (business_facts/custom_criteria) — "head" tracks whichever is
    # current, this test's intent is "a full upgrade is a no-op the second
    # time and reaches every table added so far".
    assert version == "0017"  # lot 56: additive manual access
    assert {
        "organizations", "memberships", "analyses", "analysis_documents",
        "knowledge_corpora", "knowledge_documents", "knowledge_document_versions", "knowledge_chunks",
        "private_capacity_plans", "provider_profiles", "scoring_policies", "analysis_jobs",
    } <= tables
    engine.dispose()


# ---------------------------------------------------------------------------
# T14 — an analysis exists in the DB but has no document row *and* the
# in-memory job has no files either (generation genuinely never completed)
# ---------------------------------------------------------------------------

def test_analysis_without_any_document_returns_404_not_fallback(client, db):
    from src.web.database.repositories import analyses as analyses_repo
    from src.web import jobs

    user = make_active_starter_user(db, "nodocument@example.com")
    org_id = default_org_id(db, user)
    job = jobs.create_job(source_label="x", user_id=user.id, organization_id=org_id)
    job.status = "error"
    job.files = {}  # generation failed — nothing was ever written
    analyses_repo.create_analysis(
        db, user_id=user.id, organization_id=org_id, job_id=job.id, title="AO sans document", result_data={},
    )
    db.commit()

    _login(client, "nodocument@example.com")
    r = client.get(f"/api/download/{job.id}/pdf")
    assert r.status_code == 404
    assert b"%PDF" not in r.content


# ---------------------------------------------------------------------------
# T15 — job context is fixed at creation; a status query never mutates it,
# and persistence is skipped (not silently reassigned) if membership is
# revoked mid-flight
# ---------------------------------------------------------------------------

def test_status_query_string_organization_id_is_ignored(client, db):
    from src.web import jobs

    user = make_active_starter_user(db, "jobcontext@example.com")
    org_id = default_org_id(db, user)
    other_org = uuid.uuid4()
    job = jobs.create_job(source_label="x", user_id=user.id, organization_id=org_id)
    job.status = "running"

    _login(client, "jobcontext@example.com")
    r = client.get(f"/api/analyze/{job.id}/status?organization_id={other_org}")
    assert r.status_code == 200
    assert job.organization_id == org_id, "a query-string organization_id must never mutate the job's own context"


def test_mid_job_membership_revocation_skips_db_persistence(db, isolated_storage):
    from src.web import jobs
    from src.web.database.repositories import analyses as analyses_repo
    from src.web.database.repositories import memberships as memberships_repo

    user = make_active_starter_user(db, "midjob@example.com")
    org_id = default_org_id(db, user)
    job = jobs.create_job(source_label="x", user_id=user.id, organization_id=org_id)
    ao, result = _ao(), _result()
    job.files = jobs._generate_documents(job, ao, result)
    job.ao, job.result, job.status = ao, result, "done"

    # Revoke *before* the (best-effort) DB write that normally follows
    # generation in _run_analysis — simulates an admin revoking access while
    # the background job was still running.
    membership = memberships_repo.get_active(db, user_id=user.id, organization_id=org_id)
    memberships_repo.revoke(db, membership)
    db.commit()

    jobs._persist_to_database(job)  # must not raise, must not write

    assert analyses_repo.get_by_job_id(db, job.id) is None, (
        "a revoked-mid-job membership must not result in an analysis persisted "
        "under an organization the user no longer belongs to"
    )


# ---------------------------------------------------------------------------
# T16 — ambiguous membership (more than one active organization, no explicit
# selection) is refused, never defaults to "the first one"
# ---------------------------------------------------------------------------

def test_ambiguous_membership_without_selection_is_refused(client, db):
    from src.web.database.repositories import memberships as memberships_repo
    from src.web.database.repositories import organizations as organizations_repo

    user = make_active_starter_user(db, "ambiguous@example.com")
    second_org = organizations_repo.create_organization(db, name="Second Org")
    memberships_repo.create_membership(db, user_id=user.id, organization_id=second_org.id, role="viewer", status="active")
    db.commit()

    _login(client, "ambiguous@example.com")
    r = client.get("/api/capacity")
    assert r.status_code == 409  # neither organization is picked arbitrarily

    r_explicit = client.get(f"/api/capacity?organization_id={second_org.id}")
    assert r_explicit.status_code == 200  # explicit, verified selection works


# ---------------------------------------------------------------------------
# T13 (reinforced) — exclusive text markers survive in both PDF and DOCX,
# not just "the bytes differ"
# ---------------------------------------------------------------------------

def test_pdf_and_docx_carry_exclusive_markers_not_just_distinct_bytes(isolated_storage):
    from src.web import jobs

    job_a = jobs.create_job(source_label="A", user_id=uuid.uuid4())
    job_b = jobs.create_job(source_label="B", user_id=uuid.uuid4())
    ao_a = _ao("CONTENU_EXCLUSIF_A")
    ao_b = _ao("CONTENU_EXCLUSIF_B")

    files_a = jobs._generate_documents(job_a, ao_a, _result(decision="GO", score=95.0))
    files_b = jobs._generate_documents(job_b, ao_b, _result(decision="GO", score=95.0))

    pdf_text_a = "".join(p.extract_text() or "" for p in PdfReader(files_a["pdf"]).pages)
    pdf_text_b = "".join(p.extract_text() or "" for p in PdfReader(files_b["pdf"]).pages)
    assert "CONTENU_EXCLUSIF_A" in pdf_text_a and "CONTENU_EXCLUSIF_B" not in pdf_text_a
    assert "CONTENU_EXCLUSIF_B" in pdf_text_b and "CONTENU_EXCLUSIF_A" not in pdf_text_b

    docx_text_a = "\n".join(p.text for p in DocxDocument(files_a["docx"]).paragraphs)
    docx_text_b = "\n".join(p.text for p in DocxDocument(files_b["docx"]).paragraphs)
    assert "CONTENU_EXCLUSIF_A" in docx_text_a and "CONTENU_EXCLUSIF_B" not in docx_text_a
    assert "CONTENU_EXCLUSIF_B" in docx_text_b and "CONTENU_EXCLUSIF_A" not in docx_text_b
