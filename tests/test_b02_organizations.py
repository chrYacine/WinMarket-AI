"""B02 — organizations, memberships, AccessContext, permission matrix.

Exercises real authentication and authorization for every HTTP-level test
(no bypassed dependency overrides) — see tests/conftest.py's `client`
fixture, which wires FastAPI's real app with an isolated SQLite database.

T02 (migration-path: same `company` value never merges accounts into one
organization) and T03/T10 (legacy B01 data survives the backfill, twice,
atomically) are covered in tests/test_b02_migration.py instead — they need
a genuinely pre-organization_id database, which Base.metadata.create_all()
(used by the `db`/`client` fixtures here) cannot reproduce since it always
builds the current, final schema. T12 (no regression) is the rest of the
suite passing unchanged.
"""
from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest

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


def _register(client, *, email, company="Acme Corp", password="Sup3rSecret!"):
    r = client.get("/register")
    csrf = _csrf_from(r.text)
    return client.post("/register", data={
        "first_name": "Test", "last_name": "User", "email": email, "company": company,
        "job_title": "", "phone": "", "password": password, "password_confirm": password,
        "accept_terms": "on", "csrf_token": csrf,
    })


def _add_member(db, *, organization_id, email, role, password="Sup3rSecret!", capacity: bool = True, scoring: bool = True):
    """A second user joining an *existing* organization with a given role —
    unlike make_active_starter_user, which always creates a brand-new
    private organization for its user. `capacity=True` (default) also
    configures this member's own private CapacityPlan (B03) — each member
    has their own, never a shared/organization-wide one — so tests that
    launch an analysis as this member don't all need to configure one
    first; pass capacity=False to test the CAPACITY_NOT_CONFIGURED path.
    `scoring=True` (default, B06-T1) similarly activates this member's own
    synthetic ScoringPolicy + ProviderProfile — see
    tests/conftest.py::configure_synthetic_scoring_for_owner; pass
    scoring=False to test the SCORING_NOT_CONFIGURED path."""
    from src.web.auth import service as auth_service
    from src.web.database.repositories import memberships as memberships_repo
    from src.web.database.repositories import private_capacity as private_capacity_repo
    from src.web.database.repositories import subscriptions as subscriptions_repo
    from src.web.database.repositories import users as users_repo
    from tests.conftest import configure_synthetic_scoring_for_owner

    user = users_repo.create_user(
        db, email=email, password_hash=auth_service.hash_password(password),
        first_name="Test", last_name="Member", status="active",
    )
    subscription = subscriptions_repo.create_subscription(db, user_id=user.id, plan="starter", status="pending")
    subscriptions_repo.activate(db, subscription)
    memberships_repo.create_membership(db, user_id=user.id, organization_id=organization_id, role=role, status="active")
    if capacity:
        private_capacity_repo.save_for_owner(
            db, organization_id=organization_id, owner_user_id=user.id,
            charge_globale_pct=50, nombre_projets_en_cours=1, projets_en_cours=["Projet test"],
            capacites_par_pole={"Software Engineering": 40},
        )
    if scoring:
        configure_synthetic_scoring_for_owner(db, organization_id=organization_id, owner_user_id=user.id, email=email)
    db.commit()
    return user


def _ao(titre: str = "AO test B02") -> AOContext:
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
# T01 — atomic registration: pending user + private organization
# ---------------------------------------------------------------------------

def test_registration_creates_pending_user_with_private_admin_membership(client, db):
    from src.web.database.repositories import memberships as memberships_repo
    from src.web.database.repositories import users as users_repo

    r = _register(client, email="newco@example.com", company="NewCo")
    assert r.status_code == 200  # TestClient follows the 303 -> /account/pending redirect
    assert str(r.url).endswith("/account/pending")

    user = users_repo.get_by_email(db, "newco@example.com")
    assert user is not None
    assert user.status == "pending"  # still pending — registration alone never activates

    memberships = memberships_repo.list_active_for_user(db, user.id)
    assert len(memberships) == 1
    assert memberships[0].role == "organization_admin"
    assert memberships[0].status == "active"


# ---------------------------------------------------------------------------
# T04 — organization co-membership never grants read access to another
# member's private analyses
# ---------------------------------------------------------------------------

def test_org_co_membership_does_not_grant_access_to_others_analyses(client, db, isolated_storage):
    from src.web import jobs
    from src.web.database.repositories import analyses as analyses_repo

    user_a = make_active_starter_user(db, "orga_owner@example.com")
    org_a = default_org_id(db, user_a)
    user_c = _add_member(db, organization_id=org_a, email="orga_viewer@example.com", role="viewer")
    user_b = make_active_starter_user(db, "orgb_owner@example.com")  # different organization entirely

    job_a = jobs.create_job(source_label="A", user_id=user_a.id, organization_id=org_a)
    ao, result = _ao("Dossier privé de A"), _result()
    job_a.files = jobs._generate_documents(job_a, ao, result)
    job_a.ao, job_a.result, job_a.status = ao, result, "done"
    analysis = _persist_job_to_db(job_a, ao, result, db)

    # Repository level: neither cross-org B nor same-org C can read A's analysis.
    assert analyses_repo.get_by_id_for_user(db, analysis.id, user_a.id) is not None
    assert analyses_repo.get_by_id_for_user(db, analysis.id, user_b.id) is None
    assert analyses_repo.get_by_id_for_user(db, analysis.id, user_c.id) is None

    # HTTP level: same result for both.
    for email in ("orgb_owner@example.com", "orga_viewer@example.com"):
        _login(client, email)
        r = client.get(f"/api/download/{job_a.id}/pdf")
        assert r.status_code == 404, f"{email} must not reach A's document"

    _login(client, "orga_owner@example.com")
    r = client.get(f"/api/download/{job_a.id}/pdf")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# T05 — spoofed organization_id/role grants no rights
# ---------------------------------------------------------------------------

def test_spoofed_organization_id_grants_no_access(client, db):
    user_a = make_active_starter_user(db, "spoofa@example.com")
    org_a = default_org_id(db, user_a)
    user_b = make_active_starter_user(db, "spoofb@example.com")

    _login(client, "spoofb@example.com")
    # B tries to act as A's organization by passing its id explicitly.
    r = client.get(f"/api/capacity?organization_id={org_a}")
    assert r.status_code == 403


def test_spoofed_role_via_client_side_field_is_ignored(client, db):
    """There is no client-writable role field anywhere (no profile-form
    field, no request body field consulted for role) — role always comes
    from the Membership row read fresh from the database."""
    user = make_active_starter_user(db, "noro@example.com", role="viewer")
    csrf = _login(client, "noro@example.com")
    r = client.post(
        "/api/analyze",
        data={"mode": "paste", "text": "Un texte d'AO quelconque.", "role": "organization_admin"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 403  # the extraneous "role" form field changes nothing


# ---------------------------------------------------------------------------
# T06 — permission matrix: viewer cannot launch an analysis, analyst/admin can
# ---------------------------------------------------------------------------

def test_permission_matrix_for_launching_analysis(client, db):
    admin = make_active_starter_user(db, "roleadmin@example.com")
    org_id = default_org_id(db, admin)
    _add_member(db, organization_id=org_id, email="roleviewer@example.com", role="viewer")
    _add_member(db, organization_id=org_id, email="roleanalyst@example.com", role="analyst")

    cases = [("roleviewer@example.com", 403), ("roleanalyst@example.com", 200), ("roleadmin@example.com", 200)]
    for email, expected in cases:
        csrf = _login(client, email)
        r = client.post(
            "/api/analyze",
            data={"mode": "paste", "text": "Texte d'appel d'offres pour test."},
            headers={"X-CSRF-Token": csrf},
        )
        assert r.status_code == expected, f"{email}: expected {expected}, got {r.status_code}"


# ---------------------------------------------------------------------------
# T07 — revoked membership / suspended organization blocks access with the
# existing session cookie, no new login
# ---------------------------------------------------------------------------

def test_revoked_membership_blocks_existing_session_including_download(client, db, isolated_storage):
    from src.web import jobs
    from src.web.database.repositories import memberships as memberships_repo

    user = make_active_starter_user(db, "revoke@example.com")
    org_id = default_org_id(db, user)

    job = jobs.create_job(source_label="x", user_id=user.id, organization_id=org_id)
    ao, result = _ao(), _result()
    job.files = jobs._generate_documents(job, ao, result)
    job.ao, job.result, job.status = ao, result, "done"
    _persist_job_to_db(job, ao, result, db)

    csrf = _login(client, "revoke@example.com")  # TestClient follows the redirect; session cookie is retained regardless

    r_before = client.get(f"/api/download/{job.id}/pdf")
    assert r_before.status_code == 200

    membership = memberships_repo.get_active(db, user_id=user.id, organization_id=org_id)
    memberships_repo.revoke(db, membership)
    db.commit()

    r_after = client.get(f"/api/download/{job.id}/pdf")
    assert r_after.status_code == 404, "revoked membership must cut off a download that worked moments ago"

    r_status = client.get(f"/api/analyze/{job.id}/status")
    assert r_status.status_code == 404

    r_new_analysis = client.post("/api/analyze", data={"mode": "paste", "text": "texte"}, headers={"X-CSRF-Token": csrf})
    assert r_new_analysis.status_code == 403


def test_suspended_organization_blocks_access(client, db, isolated_storage):
    from src.web import jobs
    from src.web.database.repositories import organizations as organizations_repo

    user = make_active_starter_user(db, "suspend@example.com")
    org_id = default_org_id(db, user)
    job = jobs.create_job(source_label="x", user_id=user.id, organization_id=org_id)
    ao, result = _ao(), _result()
    job.files = jobs._generate_documents(job, ao, result)
    job.ao, job.result, job.status = ao, result, "done"
    _persist_job_to_db(job, ao, result, db)

    _login(client, "suspend@example.com")
    assert client.get(f"/api/download/{job.id}/pdf").status_code == 200

    org = organizations_repo.get_by_id(db, org_id)
    organizations_repo.set_status(db, org, "suspended")
    db.commit()

    assert client.get(f"/api/download/{job.id}/pdf").status_code == 404


# ---------------------------------------------------------------------------
# T08 — role change takes effect without a new login
# ---------------------------------------------------------------------------

def test_role_upgrade_takes_effect_on_next_request_same_session(client, db):
    from src.web.database.repositories import memberships as memberships_repo

    admin = make_active_starter_user(db, "upgradeadmin@example.com")
    org_id = default_org_id(db, admin)
    viewer = _add_member(db, organization_id=org_id, email="upgradee@example.com", role="viewer")

    csrf = _login(client, "upgradee@example.com")
    assert client.post("/api/analyze", data={"mode": "paste", "text": "texte"}, headers={"X-CSRF-Token": csrf}).status_code == 403

    membership = memberships_repo.get_active(db, user_id=viewer.id, organization_id=org_id)
    memberships_repo.update_role(db, membership, "analyst")
    db.commit()

    # Same client (same session cookie, no re-login) — role must be re-read.
    assert client.post("/api/analyze", data={"mode": "paste", "text": "texte"}, headers={"X-CSRF-Token": csrf}).status_code == 200


# ---------------------------------------------------------------------------
# T09 — DB-level consistency, bypassing the repository entirely
# ---------------------------------------------------------------------------

def test_document_organization_must_match_its_analysis(db):
    from sqlalchemy.exc import IntegrityError

    from src.web.database.models import Analysis, AnalysisDocument, Organization

    org1 = Organization(name="Org 1")
    org2 = Organization(name="Org 2")
    db.add_all([org1, org2])
    db.flush()

    user = make_active_starter_user(db, "dbconstraint@example.com")
    analysis = Analysis(user_id=user.id, organization_id=org1.id, result_data={})
    db.add(analysis)
    db.flush()

    # Direct ORM insert, no repository involved: organization_id disagrees
    # with the parent analysis's own organization_id.
    bad_doc = AnalysisDocument(
        analysis_id=analysis.id, user_id=user.id, organization_id=org2.id,
        filename="x.pdf", storage_path="local://x.pdf",
    )
    db.add(bad_doc)
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


# ---------------------------------------------------------------------------
# T11 — a job with no organization at all (genuinely pre-B02) is refused,
# never silently served through the legacy fallback
# ---------------------------------------------------------------------------

def test_job_without_organization_id_is_refused_not_fallback(client, db, isolated_storage):
    from src.web import jobs

    user = make_active_starter_user(db, "nulllegacy@example.com")
    job = jobs.create_job(source_label="x", user_id=user.id)  # organization_id left as None
    ao, result = _ao(), _result()
    job.files = jobs._generate_documents(job, ao, result)
    job.ao, job.result, job.status = ao, result, "done"
    # No DB persistence at all — this reproduces a job created before B02
    # existed, not merely one whose best-effort DB write failed.

    _login(client, "nulllegacy@example.com")
    r = client.get(f"/api/download/{job.id}/pdf")
    assert r.status_code == 404
    assert b"%PDF" not in r.content


# ---------------------------------------------------------------------------
# T17 (B03) — the historical MULTI_CLIENT_MODE/corpus_access flags are now
# vestigial: /api/capacity, /api/knowledge and /api/analyze read exclusively
# from a private, per-(organization, owner) corpus/capacity — no flag
# combination can make them reach a shared/global resource, because there
# is no longer a code path that reads one for these routes at all.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("multi_client_mode, corpus_access", [
    (False, False), (False, True), (True, False), (True, True),
])
def test_legacy_flags_cannot_reach_shared_corpus_in_any_combination(client, db, monkeypatch, multi_client_mode, corpus_access):
    from src.core import config
    from src.web.database.repositories import organizations as organizations_repo

    monkeypatch.setattr(config, "MULTI_CLIENT_MODE", multi_client_mode)

    user = make_active_starter_user(db, f"legacyflags{multi_client_mode}{corpus_access}@example.com")
    org_id = default_org_id(db, user)
    org = organizations_repo.get_by_id(db, org_id)
    org.corpus_access = corpus_access
    db.commit()

    csrf = _login(client, f"legacyflags{multi_client_mode}{corpus_access}@example.com")

    # Every combination behaves identically: the account reaches its own
    # (empty, private) corpus/capacity and nothing else — never a 503
    # gate, and never the old global demo data (there is nothing global to
    # read from these routes any more).
    r_capacity = client.get("/api/capacity")
    assert r_capacity.status_code == 200
    assert r_capacity.json()["status"] == "configured"  # make_active_starter_user pre-configures it

    r_knowledge = client.get("/api/knowledge")
    assert r_knowledge.status_code == 200
    assert r_knowledge.json()["total_documents"] == 0  # this account's own corpus, genuinely empty

    r_analyze = client.post("/api/analyze", data={"mode": "paste", "text": "texte"}, headers={"X-CSRF-Token": csrf})
    assert r_analyze.status_code == 200


def test_knowledge_page_never_reveals_another_organizations_documents(client, db, monkeypatch):
    """B02-C2, closed: the HTML page and its JSON equivalent both resolve
    through the same private AccessContext-scoped corpus — B is never able
    to see A's uploaded documents through either surface, regardless of the
    historical flags' values."""
    from src.core import config
    from src.web.database.repositories import knowledge as knowledge_repo

    monkeypatch.setattr(config, "MULTI_CLIENT_MODE", True)

    user_a = make_active_starter_user(db, "knowpagea@example.com")
    org_a = default_org_id(db, user_a)
    corpus_a = knowledge_repo.get_or_create_corpus(db, organization_id=org_a, owner_user_id=user_a.id)
    doc = knowledge_repo.create_document(db, corpus=corpus_a, organization_id=org_a, owner_user_id=user_a.id, original_filename="SECRET_DE_A.md")
    db.commit()

    user_b = make_active_starter_user(db, "knowpageb@example.com")
    _login(client, "knowpageb@example.com")

    r_page = client.get("/app/base-connaissances")
    assert r_page.status_code == 200
    assert "SECRET_DE_A" not in r_page.text

    r_json = client.get("/api/knowledge")
    assert r_json.status_code == 200
    assert r_json.json()["total_documents"] == 0
    assert "SECRET_DE_A" not in r_json.text
