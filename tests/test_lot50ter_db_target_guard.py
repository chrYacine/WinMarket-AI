"""Lot 50 ter §1 — proof that `migrations/env.py`'s target resolution (`src/core/db_target.py`) actually
protects a database from an ORDINARY `alembic`/pytest invocation, not a mocked resolver. Every test below
exercises the REAL `command.upgrade()` -> `migrations/env.py` -> `resolve_database_url()` chain; none of them
monkeypatch `resolve_database_url` or `assert_disposable_test_target` themselves (that would only prove the
mock works, not the protection). "The real application" is always simulated by a disposable sentinel file
under `tmp_path` — this file itself is never the real `data/winmarket_local.db`, only a stand-in used to
prove the resolver would have refused/ignored it had it been real.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from src.core.db_target import DatabaseTargetRefused, assert_disposable_test_target

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"


def _cfg(url: str | None = None) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    if url:
        cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def _empty_sqlite(path: Path) -> None:
    sqlite3.connect(path).close()


def _tables(path: Path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


# ---------------------------------------------------------------------------------------------------------
# End-to-end, real alembic.command.upgrade() through the real (fixed) migrations/env.py
# ---------------------------------------------------------------------------------------------------------

def test_explicit_config_url_wins_over_application_database_url_real_alembic_path(tmp_path, monkeypatch):
    """THE regression test for the actual lot-50-bis incident: an explicitly-set Config URL must be the one
    migrated; the application's DATABASE_URL (here a disposable SENTINEL standing in for "the real app",
    never the real database) must come out byte-for-byte untouched — verified on disk, not via a mock."""
    from src.core import config

    sentinel_path = tmp_path / "sentinel_app.db"
    _empty_sqlite(sentinel_path)
    before_bytes = sentinel_path.read_bytes()
    before_mtime = sentinel_path.stat().st_mtime_ns

    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite:///{sentinel_path}")

    target_path = tmp_path / "explicit_target.db"
    command.upgrade(_cfg(f"sqlite:///{target_path}"), "head")  # the REAL alembic path

    assert sentinel_path.read_bytes() == before_bytes, "the sentinel 'application' database was touched"
    assert sentinel_path.stat().st_mtime_ns == before_mtime, "the sentinel 'application' database was written to"
    assert target_path.exists()
    assert "organizations" in _tables(target_path), "the EXPLICIT target was never actually migrated"


def test_a_forbidden_fallback_target_is_refused_before_any_write(tmp_path, monkeypatch):
    """No explicit Config URL given at all (the exact shape of the real bug): the resolver falls through to
    `config.DATABASE_URL` — when THAT itself carries the real application's known filename (reproduced here
    on a disposable tmp_path copy, never the real file), it must be refused, never migrated, never written."""
    from src.core import config

    forbidden = tmp_path / "winmarket_local.db"
    _empty_sqlite(forbidden)
    before = forbidden.read_bytes()

    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite:///{forbidden}")
    with pytest.raises(Exception, match="REFUSED"):
        command.upgrade(_cfg(), "head")  # no explicit URL: falls through, on purpose, to prove the refusal

    assert forbidden.read_bytes() == before, "a forbidden fallback target was written to despite the refusal"


def test_missing_target_everywhere_is_refused_not_silently_empty(monkeypatch):
    from src.core import config

    monkeypatch.setattr(config, "DATABASE_URL", "")
    with pytest.raises(Exception):
        command.upgrade(_cfg(), "head")


def test_a_path_inside_the_real_repository_is_refused_even_if_not_the_real_filename(monkeypatch, tmp_path):
    """A fallback DATABASE_URL that resolves inside the real repository tree — under a DIFFERENT filename
    than the known one — must still be refused: the repo-root check is independent of the filename check."""
    from src.core import config
    from src.core.db_target import _repo_root

    sneaky = _repo_root() / "data" / "not_the_usual_name_but_still_inside_the_repo.db"
    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite:///{sneaky}")
    with pytest.raises(Exception, match="REFUSED"):
        command.upgrade(_cfg(), "head")
    assert not sneaky.exists(), "a file was actually created inside the real repository by this test"


# ---------------------------------------------------------------------------------------------------------
# Direct unit tests of assert_disposable_test_target — path/URL variants, no alembic involved
# ---------------------------------------------------------------------------------------------------------

def test_in_memory_sqlite_is_always_disposable():
    assert_disposable_test_target("sqlite:///:memory:")  # must not raise


def test_a_tmp_path_sqlite_file_is_disposable(tmp_path):
    assert_disposable_test_target(f"sqlite:///{tmp_path / 'ok.db'}")  # must not raise


def test_a_relative_path_that_resolves_outside_any_allowed_root_is_refused(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path.parent)  # cwd is itself allowed (under the OS temp dir)... construct one that is not
    outside = Path(tmp_path.anchor) / "wm50ter_never_created_by_this_test" / "x.db"
    with pytest.raises(DatabaseTargetRefused):
        assert_disposable_test_target(f"sqlite:///{outside}")
    assert not outside.exists()


def test_wm_test_db_extra_roots_explicitly_opts_a_custom_basetemp_in(monkeypatch, tmp_path):
    monkeypatch.delenv("WM_TEST_DB_EXTRA_ROOTS", raising=False)  # independent of whatever the CI/shell set
    # tmp_path normally lives under the OS temp root, hence is already allowed.
    # Model distinct system/custom roots so the initial refusal is meaningful.
    from src.core import db_target
    monkeypatch.setattr(db_target.tempfile, "gettempdir", lambda: str(tmp_path / "system_temp"))
    custom_root = tmp_path / "custom_basetemp_like_root"
    custom_root.mkdir()
    candidate = custom_root / "db.sqlite"
    with pytest.raises(DatabaseTargetRefused):
        assert_disposable_test_target(f"sqlite:///{candidate}")  # not allowed yet
    monkeypatch.setenv("WM_TEST_DB_EXTRA_ROOTS", str(custom_root))
    assert_disposable_test_target(f"sqlite:///{candidate}")  # now explicitly opted in, must not raise


def test_postgres_target_must_come_from_the_dedicated_env_var_exactly(monkeypatch):
    monkeypatch.delenv("WM_POSTGRES_TEST_URL", raising=False)
    with pytest.raises(DatabaseTargetRefused):
        assert_disposable_test_target("postgresql://user:pw@localhost/some_test_db")


def test_postgres_target_matching_the_env_var_but_not_disposably_named_is_refused(monkeypatch):
    monkeypatch.setenv("WM_POSTGRES_TEST_URL", "postgresql://user:pw@localhost/production")
    with pytest.raises(DatabaseTargetRefused):
        assert_disposable_test_target("postgresql://user:pw@localhost/production")


def test_postgres_target_matching_the_env_var_and_disposably_named_is_accepted(monkeypatch):
    monkeypatch.setenv("WM_POSTGRES_TEST_URL", "postgresql://user:pw@localhost/app_scratch_db")
    assert_disposable_test_target("postgresql://user:pw@localhost/app_scratch_db")  # must not raise


def test_a_postgres_url_merely_containing_test_but_not_from_the_env_var_is_still_refused(monkeypatch):
    """Ticket, verbatim: 'un nom contenant simplement test n'est pas une garantie suffisante' — a name
    match alone (without also being the exact, explicitly-approved URL) must never be enough."""
    monkeypatch.setenv("WM_POSTGRES_TEST_URL", "postgresql://user:pw@localhost/the_approved_one_test")
    with pytest.raises(DatabaseTargetRefused):
        assert_disposable_test_target("postgresql://user:pw@localhost/a_different_test_db")
