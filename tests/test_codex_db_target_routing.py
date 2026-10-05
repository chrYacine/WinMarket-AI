"""Routing overrides must be refused before Alembic or fixtures create an engine.

Only synthetic URLs are used. The real pg8000 dialect translates the URL but
never opens a connection; engine factories are tripwires for the entry points.
"""
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.dialects.postgresql.pg8000 import PGDialect_pg8000
from sqlalchemy.engine import make_url

from src.core import config
from src.core.db_target import DatabaseTargetRefused, assert_disposable_test_target
from src.web.database import session
from tests import pg_support


APPROVED = "postgresql+pg8000://synthetic:fake@localhost:5433/approved_test"


@pytest.mark.parametrize("query,key,value", [
    ("database=protected", "database", "protected"),
    ("host=other.invalid", "host", "other.invalid"),
    ("port=5432", "port", "5432"),
    ("unix_sock=/tmp/other.sock", "unix_sock", "/tmp/other.sock"),
])
@pytest.mark.parametrize("approve_override", [False, True])
def test_actual_driver_routing_overrides_are_refused(monkeypatch, query, key, value, approve_override):
    candidate = APPROVED + "?" + query
    # Proof of the mismatch, using the installed driver's actual translation.
    _, arguments = PGDialect_pg8000().create_connect_args(make_url(candidate))
    assert arguments[key] == value
    monkeypatch.setenv("WM_POSTGRES_TEST_URL", candidate if approve_override else APPROVED)
    with pytest.raises(DatabaseTargetRefused):
        assert_disposable_test_target(candidate)


@pytest.mark.parametrize("entry", ["alembic", "session", "postgres_fixture"])
def test_entry_points_refuse_rerouting_before_engine_creation(monkeypatch, entry):
    candidate = APPROVED + "?database=protected"
    monkeypatch.setenv("WM_POSTGRES_TEST_URL", candidate)
    monkeypatch.setattr(config, "DATABASE_URL", candidate)
    calls = []

    def tripwire(*args, **kwargs):
        calls.append(True)
        raise AssertionError("An unapproved engine was requested")

    if entry == "alembic":
        import sqlalchemy
        monkeypatch.setattr(sqlalchemy, "engine_from_config", tripwire)
        cfg = Config()
        cfg.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "migrations"))
        cfg.set_main_option("sqlalchemy.url", candidate)
        with pytest.raises(DatabaseTargetRefused):
            command.upgrade(cfg, "head")
    elif entry == "session":
        monkeypatch.setattr(session, "_engine", None)
        monkeypatch.setattr(session, "create_engine", tripwire)
        with pytest.raises(DatabaseTargetRefused):
            session.get_engine()
    else:
        monkeypatch.setattr(config, "DATABASE_URL", "")
        monkeypatch.setattr(pg_support, "create_engine", tripwire)
        with pytest.raises(pytest.fail.Exception, match="Refusing"):
            next(pg_support.pg_url.__wrapped__(monkeypatch))
    assert not calls


def test_plain_normalized_postgresql_target_remains_allowed(monkeypatch):
    monkeypatch.setenv("WM_POSTGRES_TEST_URL", APPROVED.replace("+pg8000", ""))
    assert_disposable_test_target(APPROVED)


def test_explicit_non_routing_option_remains_allowed_but_must_match(monkeypatch):
    monkeypatch.setenv("WM_POSTGRES_TEST_URL", APPROVED + "?timeout=5")
    assert_disposable_test_target(APPROVED + "?timeout=5")
    with pytest.raises(DatabaseTargetRefused):
        assert_disposable_test_target(APPROVED + "?timeout=9")


@pytest.mark.parametrize("option", ["service=hidden", "hostaddr=127.0.0.2", "dbname=protected", "dsn=hidden"])
def test_alternate_driver_routing_options_fail_closed(monkeypatch, option):
    candidate = APPROVED + "?" + option
    monkeypatch.setenv("WM_POSTGRES_TEST_URL", candidate)
    with pytest.raises(DatabaseTargetRefused):
        assert_disposable_test_target(candidate)


def test_application_routing_alias_is_refused_by_destructive_fixture():
    with pytest.raises(pytest.fail.Exception, match="Refusing"):
        pg_support.assert_disposable_target(APPROVED, APPROVED + "?database=protected")


def test_regular_application_also_refuses_routing_overrides(monkeypatch):
    from src.core.db_target import resolve_database_url
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("WM_DB_TEST_MODE", "0")
    candidate = APPROVED + "?database=protected"
    from src.core.environment_guard import ProtectedTargetError
    with pytest.raises(ProtectedTargetError):
        resolve_database_url(explicit_url=candidate)
