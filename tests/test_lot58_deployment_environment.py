"""Lot 58 — a real deployment target (Render or equivalent) was structurally impossible before this
fix: environment_guard.validate_url (loopback-only, port != 5432, wm56_ role/db prefix) and
db_target.resolve_database_url's non-test branch (which called validate_url unconditionally) both
refused any real, remote PostgreSQL URL. This adds an explicit, separately-validated deployment mode
(APP_ENV=production) without weakening the existing local isolation rules, proven side by side below.
"""
from __future__ import annotations

import pytest

from src.core import environment_guard as guard


REALISTIC_RENDER_DB_URL = "postgresql://wm56_deploy_role:s3cr3t@dpg-example-a.oregon-postgres.render.com/wm56_deploy_db"
REALISTIC_RENDER_BASE_URL = "https://winmarket-ai-56.onrender.com"


def test_local_url_rules_are_unchanged_by_this_lot():
    """The existing local-only guard must keep refusing exactly what it refused before — this lot adds
    a parallel path, it does not loosen the old one."""
    with pytest.raises(guard.ProtectedTargetError):
        guard.validate_url(REALISTIC_RENDER_DB_URL)
    guard.validate_url("postgresql://wm56_app@127.0.0.1:5546/wm56_test")  # still accepted


@pytest.mark.parametrize("url", [
    REALISTIC_RENDER_DB_URL,
    "postgresql+pg8000://wm56_deploy_role@dpg-example-a/wm56_deploy_db",
])
def test_deployment_url_accepts_a_real_remote_target(url):
    assert guard.validate_deployment_url(url) == url


@pytest.mark.parametrize("url,reason", [
    ("postgresql://user@127.0.0.1/db", "loopback host"),
    ("postgresql://user@localhost:5432/db", "loopback host"),
    ("sqlite:///./app.db", "not postgresql"),
    ("postgresql://@dpg-example-a/db", "no username"),
    ("postgresql://user@dpg-example-a", "no database path"),
    ("postgresql://user@dpg-example-a/", "empty database path"),
])
def test_deployment_url_refuses_unsafe_or_incomplete_targets(url, reason):
    with pytest.raises(guard.ProtectedTargetError):
        guard.validate_deployment_url(url)


def test_deployment_base_url_requires_public_https():
    assert guard.validate_deployment_base_url(REALISTIC_RENDER_BASE_URL) == REALISTIC_RENDER_BASE_URL
    with pytest.raises(guard.ProtectedTargetError):
        guard.validate_deployment_base_url("http://winmarket-ai-56.onrender.com")  # not HTTPS
    with pytest.raises(guard.ProtectedTargetError):
        guard.validate_deployment_base_url("https://127.0.0.1:8056")  # loopback


def test_deployment_secrets_requires_a_real_session_secret():
    guard.validate_deployment_secrets({"SESSION_SECRET": "a" * 32})
    with pytest.raises(guard.ProtectedTargetError):
        guard.validate_deployment_secrets({"SESSION_SECRET": ""})
    with pytest.raises(guard.ProtectedTargetError):
        guard.validate_deployment_secrets({"SESSION_SECRET": "too-short"})
    with pytest.raises(guard.ProtectedTargetError):
        guard.validate_deployment_secrets({})


def test_validate_environment_routes_to_deployment_rules_only_under_app_env_production(tmp_path):
    deployment_values = {
        "APP_ENV": "production",
        "DATABASE_URL": REALISTIC_RENDER_DB_URL,
        "BASE_URL": REALISTIC_RENDER_BASE_URL,
        "SESSION_SECRET": "a" * 32,
    }
    guard.validate_environment(deployment_values, tmp_path)  # must not raise

    # The SAME Render-shaped values, without APP_ENV=production, must be refused by the local rules
    # instead — proving the switch is explicit, never an accidental default.
    local_values = dict(deployment_values)
    local_values.pop("APP_ENV")
    with pytest.raises(guard.ProtectedTargetError):
        guard.validate_environment(local_values, tmp_path)


def test_validate_environment_deployment_mode_still_refuses_missing_required_fields(tmp_path):
    with pytest.raises(guard.ProtectedTargetError):
        guard.validate_environment({"APP_ENV": "production"}, tmp_path)  # no DATABASE_URL at all
    with pytest.raises(guard.ProtectedTargetError):
        guard.validate_environment(
            {"APP_ENV": "production", "DATABASE_URL": REALISTIC_RENDER_DB_URL}, tmp_path
        )  # no BASE_URL


def test_get_engine_non_test_branch_accepts_render_target_under_app_env_production(monkeypatch):
    """A THIRD, independent instance of the identical blocker, and the most important one: this is the
    exact call the running application makes to open its real database connection
    (src/web/database/session.py::get_engine, via session_scope/get_db). Without this fix, the live app
    itself could never connect to a real Render PostgreSQL target regardless of what config.py or
    db_target.py validate at import/migration time."""
    import src.web.database.session as session_module
    from src.core import config as config_module

    monkeypatch.setattr(session_module, "_engine", None)
    monkeypatch.setattr(config_module, "DATABASE_URL", REALISTIC_RENDER_DB_URL)
    monkeypatch.setattr(config_module, "APP_ENV", "production")
    monkeypatch.setattr("src.core.db_target._in_test_mode", lambda: False)

    class _FakeEngine:
        dialect = type("d", (), {"name": "postgresql"})()

    monkeypatch.setattr(
        session_module, "create_engine", lambda *a, **k: _FakeEngine()
    )
    engine = session_module.get_engine()
    assert engine.dialect.name == "postgresql"

    # Without APP_ENV=production, the identical URL must still be refused (local rules unchanged).
    monkeypatch.setattr(session_module, "_engine", None)
    monkeypatch.setattr(config_module, "APP_ENV", "development")
    with pytest.raises(guard.ProtectedTargetError):
        session_module.get_engine()


def test_resolve_database_url_non_test_branch_accepts_render_target_under_app_env_production(monkeypatch):
    """The second, independent instance of the same blocker: Alembic's own resolver
    (db_target.resolve_database_url) called the strict LOCAL validator unconditionally outside test
    mode. A plain `alembic upgrade head` against a real Render DATABASE_URL must now succeed (the
    connection itself is never attempted here — only the pre-connection validation)."""
    import src.core.db_target as db_target

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("WM_DB_TEST_MODE", raising=False)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setattr(db_target, "_in_test_mode", lambda: False)

    resolved = db_target.resolve_database_url(cli_url=REALISTIC_RENDER_DB_URL)
    assert resolved.startswith("postgresql")

    # Without APP_ENV=production, the identical URL must still be refused by the local rules
    # (validate_url's own ProtectedTargetError, unwrapped — unchanged from before this lot).
    monkeypatch.delenv("APP_ENV", raising=False)
    with pytest.raises(guard.ProtectedTargetError):
        db_target.resolve_database_url(cli_url=REALISTIC_RENDER_DB_URL)
