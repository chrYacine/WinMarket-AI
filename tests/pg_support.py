"""Shared guard + fixtures for the tests that need a REAL PostgreSQL (B25 / lot 46).

Three rules this module enforces for every such test:

1. SKIP or FAIL, never silently pass. Without `WM_POSTGRES_TEST_URL` the tests are skipped on a
   developer machine, but with `WM_REQUIRE_POSTGRES=1` (set by the CI job) a missing URL is a
   FAILURE — a green job can never mean "the PostgreSQL qualification was skipped". A URL that
   is set but unreachable already fails (the fixtures connect).
2. Never the application's database. The fixtures DROP the whole `public` schema, so the target
   must be an obviously disposable database (its NAME contains test / ci / qualif / scratch / tmp)
   and must not be the database the application is configured for (`config.DATABASE_URL`).
3. Migrations run against the URL under test. `migrations/env.py` prefers `config.DATABASE_URL`
   over any URL handed to Alembic, so the fixtures point `config.DATABASE_URL` at the test
   database for the duration of the test (never the reverse), and the URL given to Alembic is
   the UNMASKED one: `str(engine.url)` renders the password as `***` (SQLAlchemy 2), which made
   every Alembic call authenticate with a literal `***` on a password-protected server such as
   the CI service — reproduced on a real PostgreSQL in lot 46.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from src.core.db_target import _DISPOSABLE_NAME  # lot 50 ter §1: ONE shared naming rule, not a second copy
from src.core.db_target import assert_unambiguous_postgresql_target

ENV_URL = "WM_POSTGRES_TEST_URL"
ENV_REQUIRE = "WM_REQUIRE_POSTGRES"
ENV_DISABLE_AUTOSTART = "WM_DISABLE_PGSERVER_AUTOSTART"
MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"
_LOCAL_HOSTS = {"", "localhost", "127.0.0.1", "::1"}

# Lot 51 — module-level, lazily started EXACTLY ONCE per test process (never
# at import/collection time — only the first test that actually needs
# Postgres triggers this): a real, disposable, embedded PostgreSQL+pgvector
# (the `pgserver` test-only dependency — a binary, never a Windows service,
# never installed system-wide, never the application's own database). This
# is what lets every Postgres-guarded test in this suite (0011-0015,
# lot 51's own hybrid-search tests) run for REAL on a machine with no
# system PostgreSQL/Docker/WSL, instead of always skipping. Cleaned up via
# atexit — a per-worker instance under pytest-xdist is intentional (each
# disposable, each cleaned independently), not a bug.
_pgserver_instance = None
_pgserver_url: str | None = None


def _autostart_pgserver_url() -> str | None:
    global _pgserver_instance, _pgserver_url
    if os.environ.get(ENV_DISABLE_AUTOSTART) == "1":
        return None
    if _pgserver_url is not None:
        return _pgserver_url
    try:
        import pgserver
    except ImportError:
        return None

    import atexit
    import tempfile

    datadir = tempfile.mkdtemp(prefix="wm_pgserver_test_")
    _pgserver_instance = pgserver.get_server(datadir)
    atexit.register(_pgserver_instance.cleanup)
    base_uri = _pgserver_instance.get_uri().replace("postgresql://", "postgresql+pg8000://", 1)
    admin_engine = create_engine(base_uri, future=True, isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as conn:
            conn.execute(text("CREATE DATABASE wm_pgserver_autotest"))
    finally:
        admin_engine.dispose()
    _pgserver_url = base_uri.rsplit("/", 1)[0] + "/wm_pgserver_autotest"
    return _pgserver_url


def postgres_url_or_skip(environ=None) -> str:
    """The test database URL: an explicit WM_POSTGRES_TEST_URL always wins;
    otherwise a real disposable pgserver is auto-started (see above) unless
    WM_DISABLE_PGSERVER_AUTOSTART=1 or `pgserver` isn't installed, in which
    case this SKIPS (or FAILS under WM_REQUIRE_POSTGRES=1) exactly as
    before lot 51."""
    environ = os.environ if environ is None else environ
    url = environ.get(ENV_URL, "")
    if url:
        return url
    auto = _autostart_pgserver_url()
    if auto:
        os.environ[ENV_URL] = auto  # src/core/db_target.py reads this exact env var directly
        return auto
    if environ.get(ENV_REQUIRE) == "1":
        pytest.fail(f"{ENV_REQUIRE}=1 but {ENV_URL} is not set: the real-PostgreSQL qualification cannot be skipped here.", pytrace=False)
    pytest.skip(f"{ENV_URL} not set — no real PostgreSQL test database configured (see README, section Tests).")



def _target(url: str) -> tuple[str, int, str]:
    parsed = make_url(url)
    host = "localhost" if (parsed.host or "") in _LOCAL_HOSTS else parsed.host
    return host, parsed.port or 5432, parsed.database or ""


def assert_disposable_target(url: str, application_url: str = "") -> None:
    """Refuse (a failure, not a skip) any target that could be the application's database."""
    try:
        host, port, database = _target(url)
    except Exception as exc:  # malformed URL: never guess
        pytest.fail(f"{ENV_URL} is not a valid database URL ({type(exc).__name__}).", pytrace=False)
    try:
        assert_unambiguous_postgresql_target(url)
        if application_url and make_url(application_url).get_backend_name() == "postgresql":
            assert_unambiguous_postgresql_target(application_url)
    except Exception as exc:
        pytest.fail(f"Refusing ambiguous database routing ({type(exc).__name__}).", pytrace=False)
    if not _DISPOSABLE_NAME.search(database):
        pytest.fail(
            f"Refusing to use database {database!r}: its name must contain test / ci / qualif / scratch / tmp — these tests "
            "DROP the whole public schema.", pytrace=False)
    if application_url:
        try:
            same = _target(application_url) == (host, port, database)
        except Exception:
            same = False
        if same:
            pytest.fail("Refusing to run: the test database is the application's own DATABASE_URL.", pytrace=False)


def _alembic_config(url: str):
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    # ConfigParser treats '%' as interpolation: a percent-encoded password must be doubled.
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def upgrade(url: str, revision: str = "head") -> None:
    from alembic import command

    command.upgrade(_alembic_config(url), revision)


def downgrade(url: str, revision: str) -> None:
    from alembic import command

    command.downgrade(_alembic_config(url), revision)


def reset_public_schema(engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))


@pytest.fixture()
def pg_url(monkeypatch) -> str:
    """The guarded, UNMASKED test-database URL; `config.DATABASE_URL` points at it for the test."""
    from src.core import config
    from src.web.database import session as db_session_module
    from src.web.database.session import normalize_database_url

    url = postgres_url_or_skip()
    assert_disposable_target(url, config.DATABASE_URL)
    normalized = normalize_database_url(url)
    monkeypatch.setattr(config, "DATABASE_URL", url)
    monkeypatch.setattr(db_session_module, "_engine", None)
    monkeypatch.setattr(db_session_module, "_SessionLocal", None)
    yield normalized
    engine = db_session_module._engine
    if engine is not None:
        engine.dispose()
    monkeypatch.setattr(db_session_module, "_engine", None)
    monkeypatch.setattr(db_session_module, "_SessionLocal", None)


@pytest.fixture()
def pg_engine(pg_url):
    """An engine on an EMPTY public schema of the (guarded) test database."""
    engine = create_engine(pg_url, future=True)
    reset_public_schema(engine)
    yield engine
    engine.dispose()
