"""Lot 51 §1 — migration 0015 proof, through the REAL Alembic path
(migrations/env.py -> src/core/db_target.py, never a mock), on both a
disposable SQLite target and a REAL disposable PostgreSQL+pgvector
(pgserver, auto-started by tests/pg_support.py). Dialect-conditioned DDL:
`knowledge_passages` exists on both, its `embedding` vector column and the
`vector` extension exist ONLY on PostgreSQL.
"""
from __future__ import annotations

from sqlalchemy import create_engine, inspect, text

from tests import pg_support
from tests.pg_support import pg_engine, pg_url  # noqa: F401  (fixtures)


def test_migration_0015_on_sqlite_is_portable_but_never_creates_a_vector_column(tmp_path):
    url = f"sqlite:///{tmp_path / 'disposable.db'}"
    pg_support.upgrade(url, "head")
    engine = create_engine(url, future=True)
    insp = inspect(engine)
    assert "knowledge_passages" in insp.get_table_names()
    cols = {c["name"] for c in insp.get_columns("knowledge_passages")}
    assert "embedding" not in cols
    version_cols = {c["name"] for c in insp.get_columns("knowledge_document_versions")}
    assert {"embedding_status", "embedding_model_id", "embedding_model_revision", "embedding_dimension", "embedding_error_code", "embedding_indexed_at"} <= version_cols
    engine.dispose()


def test_migration_0015_on_postgresql_creates_the_extension_and_the_vector_column(pg_engine, pg_url):
    pg_support.upgrade(pg_url, "head")
    insp = inspect(pg_engine)
    cols = {c["name"] for c in insp.get_columns("knowledge_passages")}
    assert "embedding" in cols
    with pg_engine.connect() as conn:
        ext = conn.execute(text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")).scalar()
        assert ext is not None


def test_an_orphaned_chunk_id_is_rejected_by_the_composite_foreign_key_on_postgresql(pg_engine, pg_url):
    pg_support.upgrade(pg_url, "head")
    with pg_engine.connect() as conn:
        try:
            conn.execute(text(
                "INSERT INTO knowledge_passages (id, chunk_id, document_version_id, organization_id, owner_user_id, "
                "start_char, end_char, content, content_fingerprint, embedding_model_id, embedding_model_revision, "
                "embedding_dimension, created_at) VALUES (gen_random_uuid(), gen_random_uuid(), gen_random_uuid(), "
                "gen_random_uuid(), gen_random_uuid(), 0, 3, 'abc', 'fp', 'm', 'r', 3, now())"
            ))
            raised = None
        except Exception as exc:  # noqa: BLE001 - asserted below
            raised = exc
        finally:
            conn.rollback()
    assert raised is not None and "fk_knowledge_passages_chunk_scope" in str(raised)


def test_downgrade_0015_is_refused_once_any_embedding_data_exists(pg_engine, pg_url, monkeypatch):
    from sqlalchemy.orm import sessionmaker

    from src.core import config
    from tests.conftest import default_org_id, make_active_starter_user

    pg_support.upgrade(pg_url, "head")
    monkeypatch.setattr(config, "RAG_HYBRID_MODE_ENABLED", True)
    session = sessionmaker(bind=pg_engine, autoflush=False, expire_on_commit=False)()
    from src.web.knowledge import documents_service
    user = make_active_starter_user(session, "lot51-downgrade@example.com", scoring=False)
    org = default_org_id(session, user)
    documents_service.upload_document(session, organization_id=org, owner_user_id=user.id, original_filename="a.md", raw=b"Reference pour le test de downgrade.")
    session.commit()
    session.close()

    try:
        pg_support.downgrade(pg_url, "0014")
        raised = None
    except Exception as exc:  # noqa: BLE001 - asserted below
        raised = exc
    assert raised is not None and "refused" in str(raised).lower()

