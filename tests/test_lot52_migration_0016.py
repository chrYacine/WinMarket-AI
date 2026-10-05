"""Lot 52 §0 — migration 0016 proof, through the REAL Alembic path (migrations/env.py ->
src/core/db_target.py, never a mock), on both a disposable SQLite target and a REAL disposable
PostgreSQL (pgserver, auto-started by tests/pg_support.py). Purely additive: `analysis_complements`
gains `origin`/`source_json`, existing rows backfilled to `origin='declared_user'`.
"""
from __future__ import annotations

import uuid

from sqlalchemy import create_engine, inspect, text

from tests import pg_support
from tests.pg_support import pg_engine, pg_url  # noqa: F401  (fixtures)


def test_migration_0016_adds_origin_and_source_json_on_sqlite(tmp_path):
    url = f"sqlite:///{tmp_path / 'disposable.db'}"
    pg_support.upgrade(url, "head")
    engine = create_engine(url, future=True)
    cols = {c["name"] for c in inspect(engine).get_columns("analysis_complements")}
    assert {"origin", "source_json"} <= cols
    engine.dispose()


def _make_org_user(session):
    # Seed only columns present at 0015; current Subscription has additive 0017 fields.
    from src.web.database.repositories import users
    from src.web.auth.service import create_private_organization_for_user
    user = users.create_user(session, email=f"migration-{uuid.uuid4()}@example.test",
                             password_hash="synthetic-unused-hash", first_name="Synthetic", last_name="Migration",status="active")
    org = create_private_organization_for_user(session,user)
    session.commit()
    return org.id,user.id


def test_migration_0016_backfills_origin_for_a_pre_existing_row_on_postgresql(pg_engine, pg_url):
    from sqlalchemy.orm import sessionmaker
    from src.web.database.models import AnalysisComplement

    pg_support.upgrade(pg_url, "0015")
    session = sessionmaker(bind=pg_engine, autoflush=False, expire_on_commit=False)()
    org_id, user_id = _make_org_user(session)
    with pg_engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO analysis_complements (id, job_id, organization_id, user_id, need_id, subject, "
            "field_key, field_label, value_json, created_by_user_id, created_at) VALUES "
            "(gen_random_uuid(), 'job-1', :org, :uid, 'need-1', 'ao', 'budget_estime', 'Budget', "
            "'150000'::jsonb, :uid, now())"
        ), {"org": org_id, "uid": user_id})
    session.close()

    pg_support.upgrade(pg_url, "head")
    with pg_engine.connect() as conn:
        origin = conn.execute(text("SELECT origin FROM analysis_complements WHERE job_id = 'job-1'")).scalar()
    assert origin == "declared_user"


def test_downgrade_0016_is_refused_once_a_sourced_complement_exists(pg_engine, pg_url):
    from sqlalchemy.orm import sessionmaker
    from src.web.database.models import AnalysisComplement

    pg_support.upgrade(pg_url, "head")
    session = sessionmaker(bind=pg_engine, autoflush=False, expire_on_commit=False)()
    org_id, user_id = _make_org_user(session)
    session.add(AnalysisComplement(
        job_id="job-2", organization_id=org_id, user_id=user_id, need_id="need-1", subject="prestataire",
        field_key="frequence_nettoyage", field_label="Fréquence", value_json=5, unit="par_semaine",
        origin="llm_sourced", source_json={"kind": "knowledge_document"}, created_by_user_id=user_id,
    ))
    session.commit()
    session.close()

    try:
        pg_support.downgrade(pg_url, "0015")
        raised = None
    except Exception as exc:  # noqa: BLE001 - asserted below
        raised = exc
    assert raised is not None and "refused" in str(raised).lower()
