"""SQLAlchemy engine/session wiring for the SaaS layer.

The only coupling to a Postgres provider anywhere in this codebase is
`DATABASE_URL` (local Postgres, Docker, Supabase, Neon, ...). No connection
info is ever hardcoded here.

Two access patterns are provided:
- `get_db()` — a FastAPI dependency yielding one Session per request.
- `session_scope()` — a plain context manager for code that does NOT run
  inside a FastAPI request, most importantly the background analysis
  threads in src/web/jobs.py. A Session obtained via Depends() must never be
  reused across a thread boundary; each thread opens and closes its own.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Generator, Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from src.core import config
from src.core.logger import get_agent_logger

logger = get_agent_logger("web_database")

_engine: Engine | None = None
_SessionLocal: sessionmaker | None = None


def normalize_database_url(raw_url: str) -> str:
    """Pick a driver automatically so DATABASE_URL alone stays sufficient.

    Providers like Supabase/Neon hand out plain `postgresql://` URLs. We
    route those through pg8000 (pure Python — no compiled driver to install,
    which also sidesteps DLL-signing restrictions some environments enforce)
    unless the URL already names a driver explicitly.
    """
    if raw_url.startswith("postgresql://"):
        return "postgresql+pg8000://" + raw_url[len("postgresql://"):]
    return raw_url


def is_database_configured() -> bool:
    return bool(config.DATABASE_URL)


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        if not config.DATABASE_URL:
            raise RuntimeError(
                "DATABASE_URL n'est pas configuré. Renseigne-le dans .env "
                "(voir .env.example) avant d'utiliser une fonctionnalité "
                "nécessitant la base de données."
            )
        url = normalize_database_url(config.DATABASE_URL)
        # Lot 50 ter §1 — defense in depth alongside migrations/env.py's own guard: during a test
        # (PYTEST_CURRENT_TEST) or an explicitly-declared test-mode script, refuse BEFORE creating an
        # engine if a test forgot to redirect config.DATABASE_URL away from the real application target.
        # A no-op for the real running application (never in test mode there).
        from src.core.db_target import _in_test_mode, assert_disposable_test_target
        if _in_test_mode():
            assert_disposable_test_target(url)
        if not _in_test_mode():
            # Lot 58: a real deployment (APP_ENV=production, Render or equivalent) needs a real,
            # remote PostgreSQL target — the SAME single switch (is_deployment_mode) used by
            # src.core.config and src.core.db_target, never a third, divergent notion of "is this
            # a deployment" at the one place that actually opens the connection.
            from src.core.environment_guard import is_deployment_mode, validate_deployment_url, validate_url
            if is_deployment_mode({"APP_ENV": config.APP_ENV}):
                validate_deployment_url(url)
            else:
                validate_url(url)
        _engine = create_engine(url, pool_pre_ping=True, future=True)
        if _engine.dialect.name == "sqlite":
            # SQLite ignores foreign keys (including the composite
            # analysis_documents -> analyses(id, organization_id) constraint
            # B02 relies on) unless this pragma is set per connection.
            # Postgres (production) enforces FKs unconditionally already.
            @event.listens_for(_engine, "connect")
            def _enable_sqlite_fk(dbapi_connection, connection_record):
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()
        logger.info("Database engine created dialect=%s", _engine.dialect.name)
    return _engine


def get_session_factory() -> sessionmaker:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)
    return _SessionLocal


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency: one Session per request, always closed."""
    session_factory = get_session_factory()
    db = session_factory()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Context manager for non-request code (background threads, scripts).

    Commits on success, rolls back on error, always closes. Never share a
    Session created here — or via Depends(get_db) — across threads.
    """
    session_factory = get_session_factory()
    db = session_factory()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
