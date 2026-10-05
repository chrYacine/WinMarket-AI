"""B25-T1 — real PostgreSQL qualification: migrations on a POPULATED
database, private constraints, and a transaction/savepoint behavior this
project's own SQLite test database cannot qualify (SQLite enforces some
constraints more loosely, and does not exhibit PostgreSQL's
transaction-aborts-on-failed-statement semantics that
scripts/migrate_history_to_postgresql.py's SAVEPOINT fix — B23-T1 — exists
specifically to guard against).

Needs a real PostgreSQL test database via the WM_POSTGRES_TEST_URL environment
variable (see tests/pg_support.py for the guard rules). Without it the tests are
SKIPPED on a developer machine and FAIL when WM_REQUIRE_POSTGRES=1 (CI). To run
for real locally against a disposable database:

    WM_POSTGRES_TEST_URL=postgresql://user:pass@localhost:5432/winmarket_ci \\
        python -m pytest tests/test_b25_t1_postgresql_qualification.py -q

History: written at B25-T1 without a PostgreSQL to run it on. First executed on a
real PostgreSQL 16 in lot 46, which reproduced two defects in THIS file (not in the
product): the Alembic URL was rendered with a masked password (`***`), and the
duplicate-job_id test expected the IntegrityError one statement too late
(`create_analysis` flushes by itself). Both are fixed below.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from tests import pg_support
from tests.pg_support import pg_engine, pg_url  # noqa: F401  (fixtures)


def _apply_all_migrations(engine, url) -> None:
    pg_support.upgrade(url, "head")


def test_migrations_apply_cleanly_and_are_idempotent_on_real_postgresql(pg_engine, pg_url):
    _apply_all_migrations(pg_engine, pg_url)
    with pg_engine.connect() as conn:
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        tables = {row[0] for row in conn.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        ))}
    assert version is not None
    assert {"users", "organizations", "analyses", "analysis_jobs", "password_reset_tokens"} <= tables

    # Idempotent: running "upgrade head" again against an already-current
    # database must be a pure no-op, never an error.
    _apply_all_migrations(pg_engine, pg_url)


def test_migrations_apply_on_a_populated_database_without_data_loss(pg_engine, pg_url):
    _apply_all_migrations(pg_engine, pg_url)
    Session = sessionmaker(bind=pg_engine)
    session = Session()
    from tests.conftest import make_active_starter_user
    user = make_active_starter_user(session, "pgqualif@example.com")
    user_id = user.id
    session.close()

    # Re-running the full chain against a database that already has real
    # rows must leave them untouched (every migration here is additive).
    _apply_all_migrations(pg_engine, pg_url)
    session2 = Session()
    from src.web.database.repositories import users as users_repo
    still_there = users_repo.get_by_id(session2, user_id)
    assert still_there is not None
    assert still_there.email == "pgqualif@example.com"
    session2.close()


def test_unique_job_id_constraint_is_really_enforced_by_postgresql(pg_engine, pg_url):
    """A constraint SQLite enforces too, but real PostgreSQL enforcement —
    and specifically its transaction-abort-on-violation behavior — is what
    B23-T1's SAVEPOINT fix (scripts/migrate_history_to_postgresql.py) exists
    to isolate. This proves the constraint itself is real on this engine,
    and that a violation aborts the transaction (confirming the failure
    mode the SAVEPOINT fix guards against genuinely exists here)."""
    _apply_all_migrations(pg_engine, pg_url)
    Session = sessionmaker(bind=pg_engine)
    session = Session()
    from tests.conftest import make_active_starter_user, default_org_id
    from src.web.database.repositories import analyses as analyses_repo

    user = make_active_starter_user(session, "pgconstraint@example.com")
    org_id = default_org_id(session, user)
    analyses_repo.create_analysis(session, user_id=user.id, organization_id=org_id, job_id="dup-1", result_data={})
    session.commit()

    # create_analysis flushes by itself: the violation surfaces inside it, so the
    # whole duplicate insert (not only a following flush) belongs in the block.
    with pytest.raises(IntegrityError):
        analyses_repo.create_analysis(session, user_id=user.id, organization_id=org_id, job_id="dup-1", result_data={})
        session.flush()

    # PostgreSQL-specific: the transaction is now ABORTED — even an
    # unrelated SELECT fails until rollback. This is exactly the "why" the
    # per-row SAVEPOINT in migrate_history_to_postgresql.py is necessary.
    with pytest.raises(Exception):
        session.execute(text("SELECT 1"))
    session.rollback()
    session.close()


def test_savepoint_isolates_a_failed_row_without_aborting_the_outer_transaction(pg_engine, pg_url):
    """The direct proof that B23-T1's fix works on the engine it was
    written for: with a SAVEPOINT around the failing insert, the outer
    transaction survives and a subsequent good row still commits."""
    _apply_all_migrations(pg_engine, pg_url)
    Session = sessionmaker(bind=pg_engine)
    session = Session()
    from tests.conftest import make_active_starter_user, default_org_id
    from src.web.database.repositories import analyses as analyses_repo

    user = make_active_starter_user(session, "pgsavepoint@example.com")
    org_id = default_org_id(session, user)
    analyses_repo.create_analysis(session, user_id=user.id, organization_id=org_id, job_id="sp-dup", result_data={})
    session.commit()

    try:
        with session.begin_nested():
            analyses_repo.create_analysis(session, user_id=user.id, organization_id=org_id, job_id="sp-dup", result_data={})
    except IntegrityError:
        pass

    # The outer transaction is still usable — the SAVEPOINT confined the
    # failure, exactly as scripts/migrate_history_to_postgresql.py relies on.
    analyses_repo.create_analysis(session, user_id=user.id, organization_id=org_id, job_id="sp-after", result_data={})
    session.commit()

    assert analyses_repo.get_by_job_id(session, "sp-after") is not None
    session.close()
