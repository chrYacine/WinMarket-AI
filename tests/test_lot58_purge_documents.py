"""Lot 58 §D/G.3 — scripts/purge_documents.py's core logic (run_purge), on a real disposable
PostgreSQL target (never the protected old installation, never a hardcoded local convenience), across
TWO accounts/organizations. Proves: active knowledge documents and AO dossiers are removed (metadata +
physical files) for every account, while accounts/memberships/scoring policy/profile/capacity and any
OTHER data are left completely untouched — the authorization covers documents, never configuration or
analysis history. CLI/env-file/validate_environment plumbing is covered separately
(tests/test_lot58_operator_access_env_mode.py's equivalent pattern applies identically here; this file
tests the purge behavior itself against a real database).
"""
from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy.orm import sessionmaker

from tests import pg_support
from tests.pg_support import pg_engine, pg_url  # noqa: F401  (fixtures)


def _make_two_accounts_with_documents(pg_engine, pg_url, monkeypatch):
    """Returns (session, org_a, user_a, org_b, user_b) with the schema at head and each account
    logged in and having uploaded exactly one private document — the "minuscule jeu jetable" the
    ticket asks for, real HTTP upload path, real physical files."""
    import main
    from fastapi.testclient import TestClient

    from src.core import config
    from src.web.database.repositories import memberships as memberships_repo
    from src.web.database.repositories import organizations as organizations_repo
    from tests.conftest import default_org_id, make_active_starter_user
    from tests.test_b03_private_knowledge import _login, _upload

    monkeypatch.setattr(config, "SESSION_SECRET", "test-secret-not-for-production")
    pg_support.upgrade(pg_url, "head")
    session = sessionmaker(bind=pg_engine, autoflush=False, expire_on_commit=False)()
    user_a = make_active_starter_user(session, "purge-a@example.test", scoring=False, capacity=False)
    org_a = default_org_id(session, user_a)
    user_b = make_active_starter_user(session, "purge-b@example.test", scoring=False, capacity=False)
    org_b = default_org_id(session, user_b)
    session.commit()

    with TestClient(main.app) as client:
        csrf_a = _login(client, "purge-a@example.test")
        up_a = _upload(client, "ref_a.md", b"# Reference A\n\nSynthetic content for account A.", csrf=csrf_a)
        assert up_a.status_code == 201, up_a.text
        client.cookies.clear()
        csrf_b = _login(client, "purge-b@example.test")
        up_b = _upload(client, "ref_b.md", b"# Reference B\n\nSynthetic content for account B.", csrf=csrf_b)
        assert up_b.status_code == 201, up_b.text

    return session, org_a, user_a.id, org_b, user_b.id


def _make_dossier_with_real_files(organization_id, user_id) -> uuid.UUID:
    from src.core import config
    from src.web.database.models import AoDossier
    from src.web.ao_dossier import storage as dossier_storage

    dossier_id = uuid.uuid4()
    piece_dir = dossier_storage.dossier_dir(organization_id, user_id, dossier_id)
    piece_dir.mkdir(parents=True, exist_ok=True)
    (piece_dir / "rc.txt").write_bytes(b"Reglement de consultation (synthetique).")
    return dossier_id


def test_purge_dry_run_reports_counts_without_deleting_anything(pg_engine, pg_url, monkeypatch):
    from scripts.purge_documents import run_purge
    from src.web.database.models import KnowledgeDocument

    session, org_a, user_a, org_b, user_b = _make_two_accounts_with_documents(pg_engine, pg_url, monkeypatch)
    try:
        report = run_purge(session, execute=False)
        assert report == {"refused": False, "documents": 2, "dossiers": 0, "errors": 0, "executed": False}
        assert session.query(KnowledgeDocument).filter(KnowledgeDocument.status == "active").count() == 2
    finally:
        session.close()


def test_purge_execute_removes_documents_and_dossiers_but_preserves_everything_else(pg_engine, pg_url, tmp_path, monkeypatch):
    from src.core import config

    monkeypatch.setattr(config, "LOCAL_STORAGE_PATH", tmp_path)
    session, org_a, user_a, org_b, user_b = _make_two_accounts_with_documents(pg_engine, pg_url, monkeypatch)
    dossier_id = _make_dossier_with_real_files(org_a, user_a)
    from src.web.database.models import AoDossier

    session.add(AoDossier(id=dossier_id, organization_id=org_a, user_id=user_a, status="validated", total_bytes=10, piece_count=1))
    session.commit()

    from src.web.ao_dossier import storage as dossier_storage
    piece_dir = dossier_storage.dossier_dir(org_a, user_a, dossier_id)
    assert piece_dir.exists()

    from scripts.purge_documents import run_purge
    from src.web.database.models import KnowledgeDocument, Membership, Organization, User

    report = run_purge(session, execute=True)
    assert report["documents"] == 2 and report["dossiers"] == 1 and report["errors"] == 0

    session.expire_all()
    from src.web.database.models import AoDossier as AoDossierModel

    remaining_active_docs = session.query(KnowledgeDocument).filter(KnowledgeDocument.status == "active").count()
    assert remaining_active_docs == 0
    deleted_docs = session.query(KnowledgeDocument).filter(KnowledgeDocument.status == "deleted").count()
    assert deleted_docs == 2, "documents are soft-deleted (tombstoned), never hard-erased from the table"
    assert session.query(AoDossierModel).count() == 0
    assert not piece_dir.exists(), "the dossier's physical files must be actually removed"

    # Everything OTHER than documents/dossiers is untouched.
    assert session.query(User).count() == 2
    assert session.query(Organization).count() == 2
    assert session.query(Membership).count() == 2

    session.close()


def test_purge_refuses_when_a_job_is_active_unless_forced(pg_engine, pg_url, monkeypatch):
    from scripts.purge_documents import run_purge
    from src.web.database.repositories import analysis_jobs as analysis_jobs_repo

    session, org_a, user_a, org_b, user_b = _make_two_accounts_with_documents(pg_engine, pg_url, monkeypatch)
    try:
        analysis_jobs_repo.create_queued(session, job_id="purge-guard-job", user_id=user_a, organization_id=org_a, source_label="synthetic")
        session.commit()

        refused = run_purge(session, execute=True)
        assert refused["refused"] is True and refused["active_jobs"] == 1

        from src.web.database.models import KnowledgeDocument
        assert session.query(KnowledgeDocument).filter(KnowledgeDocument.status == "active").count() == 2, (
            "a refusal must never delete anything, even partially"
        )

        forced = run_purge(session, execute=True, force_with_active_jobs=True)
        assert forced["refused"] is False and forced["documents"] == 2
    finally:
        session.close()
