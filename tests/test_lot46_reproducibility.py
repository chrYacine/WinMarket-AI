"""Lot 46 — what protects the reproducible install and the PostgreSQL qualification WITHOUT needing a PostgreSQL.

A. the target guard of the real-PostgreSQL tests (disposable database only; skip vs FAIL when required);
B. Alembic is given the UNMASKED URL and the URL under test wins over `config.DATABASE_URL` (env.py precedence);
C. the CI workflow keeps the guarantees this lot established (job fails without its database, same Python and
   lock as the demonstrated install, real `alembic upgrade head`, both PostgreSQL files);
D. the lock still satisfies requirements*.txt and does not bring Streamlit back.

The install itself (fresh venv, `pip check`, exact lock, imports, real start) is verified by
scripts/check_lock_consistency.py in a CLEAN environment — see the lot 46 report; it cannot be asserted from a
long-lived development environment.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
import yaml
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from sqlalchemy import create_engine, inspect
from _pytest.outcomes import Failed, Skipped

from tests import pg_support

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# A — target guard
# ---------------------------------------------------------------------------

def test_the_target_must_be_an_obviously_disposable_database():
    for accepted in ("postgresql://u:p@localhost:5432/winmarket_ci", "postgresql://u@127.0.0.1/winmarket_lot46_test",
                     "postgresql+pg8000://u:p@db.example:6543/scratch_db"):
        pg_support.assert_disposable_target(accepted)
    for refused in ("postgresql://u:p@localhost:5432/winmarket", "postgresql://u:p@localhost/postgres", "postgresql://u:p@localhost:5432/"):
        with pytest.raises(Failed, match="Refusing to use database"):
            pg_support.assert_disposable_target(refused)


def test_the_application_database_is_never_an_accepted_target():
    application = "postgresql://app:secret@localhost:5432/winmarket_test"   # even a "test-named" application database
    for same in ("postgresql+pg8000://other:pw@127.0.0.1:5432/winmarket_test", "postgresql://x@localhost/winmarket_test"):
        with pytest.raises(Failed, match="application's own DATABASE_URL"):
            pg_support.assert_disposable_target(same, application)
    pg_support.assert_disposable_target("postgresql://x@localhost:5433/winmarket_test", application)  # another port = another server
    pg_support.assert_disposable_target("postgresql://x@localhost/winmarket_ci", application)


def test_a_missing_database_url_skips_locally_but_fails_when_it_is_required(monkeypatch):
    # Lot 51: postgres_url_or_skip() now auto-starts a real, disposable pgserver instead of
    # skipping when `pgserver` is installed (see tests/pg_support.py::_autostart_pgserver_url) —
    # this is what lets every Postgres-guarded test in this suite actually RUN on a machine with
    # no system PostgreSQL/Docker/WSL. The pre-lot-51 "always skips without a URL" contract only
    # still holds with the auto-start explicitly disabled (WM_DISABLE_PGSERVER_AUTOSTART=1) or
    # when `pgserver` genuinely isn't installed — both exercised below.
    monkeypatch.setenv(pg_support.ENV_DISABLE_AUTOSTART, "1")
    with pytest.raises(Skipped):
        pg_support.postgres_url_or_skip({})
    with pytest.raises(Failed, match="cannot be skipped"):
        pg_support.postgres_url_or_skip({pg_support.ENV_REQUIRE: "1"})
    assert pg_support.postgres_url_or_skip({pg_support.ENV_URL: "postgresql://u@h/winmarket_ci", pg_support.ENV_REQUIRE: "1"}) == "postgresql://u@h/winmarket_ci"
    with pytest.raises(Failed, match="not a valid database URL"):
        pg_support.assert_disposable_target("not a url at all ://")


@pytest.mark.skipif(os.name != "nt", reason="Embedded pgserver Linux Unix-socket adaptation deferred; CI qualifies its explicit PostgreSQL service")
def test_a_missing_database_url_auto_starts_a_real_disposable_pgserver_when_available(monkeypatch):
    """Lot 51 — the new default (WM_DISABLE_PGSERVER_AUTOSTART unset, `pgserver` installed): no URL
    provided still resolves to a REAL, reachable PostgreSQL, never a skip, never a mock."""
    monkeypatch.delenv(pg_support.ENV_DISABLE_AUTOSTART, raising=False)
    monkeypatch.delenv(pg_support.ENV_URL, raising=False)
    url = pg_support.postgres_url_or_skip(dict(os.environ))
    from sqlalchemy import create_engine, text as sa_text
    engine = create_engine(url, future=True)
    try:
        with engine.connect() as conn:
            assert conn.execute(sa_text("SELECT 1")).scalar() == 1
    finally:
        engine.dispose()
    pg_support.assert_disposable_target(url)  # the auto-started database's own name passes the disposable-name check


# ---------------------------------------------------------------------------
# B — Alembic gets the real URL, and the URL under test wins
# ---------------------------------------------------------------------------

def test_alembic_receives_the_unmasked_url_including_a_percent_encoded_password():
    """Regression (reproduced on a real PostgreSQL): `str(engine.url)` renders the password as `***`, so every
    Alembic call authenticated with a literal `***` on a password-protected server such as the CI service."""
    url = "postgresql+pg8000://winmarket_ci:ci%40pass%25word@localhost:5432/winmarket_ci"
    assert "***" in str(create_engine(url).url), "documents WHY: SQLAlchemy masks the password in str(url) — never hand THAT to Alembic"
    assert pg_support._alembic_config(url).get_main_option("sqlalchemy.url") == url


def test_the_url_under_test_wins_over_config_database_url_in_env_py(tmp_path, monkeypatch):
    """UPDATED lot 50 ter: `migrations/env.py::_database_url()` used to prefer `config.DATABASE_URL` over any
    URL explicitly handed to Alembic — a test that only passed its own URL could therefore migrate the
    APPLICATION's database (or whatever `.env` names) instead. This was fixed AT THE SOURCE by
    src/core/db_target.py::resolve_database_url (lot 50 ter §1): an explicit `sqlalchemy.url` on the Alembic
    `Config` (which `pg_support.upgrade()`/`_alembic_config()` always sets) now wins over
    `config.DATABASE_URL` unconditionally — the `pg_url` fixture's own `monkeypatch.setattr(config,
    "DATABASE_URL", ...)` is no longer what makes this safe, it is redundant defense-in-depth (belt-and-
    suspenders, still worth keeping: it also satisfies db_target's own test-mode structural checks). This test
    was NOT re-run after the lot 50 ter fix landed (not in that lot's own targeted test list) and kept
    asserting the OLD, now-false behavior until lot 51's pgserver auto-start made the full file execute again
    and exposed it — rewritten here to assert the CURRENT, correct contract instead of the pre-fix one."""
    from src.core import config

    application_db, test_db = tmp_path / "application.db", tmp_path / "under_test.db"
    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite:///{application_db}")
    pg_support.upgrade(f"sqlite:///{test_db}", "head")
    assert "users" in inspect(create_engine(f"sqlite:///{test_db}")).get_table_names(), (
        "the explicit URL handed to Alembic wins, EVEN with a different config.DATABASE_URL set")
    assert not application_db.exists() or "users" not in inspect(create_engine(f"sqlite:///{application_db}")).get_table_names(), (
        "config.DATABASE_URL must NEVER be touched when an explicit URL was provided")

    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite:///{test_db}")  # what the pg_url fixture ALSO does (defense in depth, not what makes this safe)
    pg_support.upgrade(f"sqlite:///{test_db}", "head")
    assert "users" in inspect(create_engine(f"sqlite:///{test_db}")).get_table_names()


# ---------------------------------------------------------------------------
# C — the CI workflow
# ---------------------------------------------------------------------------

def _workflow():
    return yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))


def _steps_text(job) -> str:
    return "\n".join(str(step.get("run", "")) + " " + str(step.get("uses", "")) for step in job["steps"])


def test_the_postgresql_job_cannot_succeed_without_its_database():
    job = _workflow()["jobs"]["qualification"]
    assert job["env"]["WM_REQUIRE_POSTGRES"] == "1"
    url = job["env"]["WM_POSTGRES_TEST_URL"]
    assert url.endswith("/wm56_ci_test") and "@127.0.0.1:5547/" in url
    assert job["services"]["postgres"]["image"] == "pgvector/pgvector:pg16"
    run = _steps_text(job)
    assert "alembic upgrade head" in run
    assert "test_lot56_migration.py" in run and "test_lot56_manual_access.py" in run
    assert "test_lot50ter_db_target_guard.py" in run
    assert "check_lock_consistency.py" in run and "requirements.lock.txt" in run


def test_both_jobs_use_the_demonstrated_python_and_the_lock():
    jobs = _workflow()["jobs"]
    for name, job in jobs.items():
        run = _steps_text(job)
        setups = [s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/setup-python")]
        assert setups and str(setups[0]["with"]["python-version"]) == "3.12", name
        assert "requirements.lock.txt" in run, name
    qualification = _steps_text(jobs["qualification"])
    assert "pip install -r requirements.lock.txt" in qualification
    assert "check_lock_consistency.py" in qualification  # Includes pip check.
    assert "pip_audit" in _steps_text(jobs["dependency-audit"])


# ---------------------------------------------------------------------------
# D — the lock against requirements*.txt
# ---------------------------------------------------------------------------

def _pins() -> dict[str, str]:
    pins = {}
    for line in (ROOT / "requirements.lock.txt").read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            name, _, version = line.strip().partition("==")
            pins[canonicalize_name(name)] = version
    return pins


def test_every_requirement_is_pinned_by_the_lock_within_its_specifier():
    pins = _pins()
    for source in ("requirements.txt", "requirements-test.txt"):
        for line in (ROOT / source).read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            req = Requirement(line)
            version = pins.get(canonicalize_name(req.name))
            assert version is not None, f"{req.name} ({source}) is not pinned by the lock"
            assert req.specifier.contains(version, prereleases=True), f"{req.name}=={version} violates {req.specifier} ({source})"


def test_the_lock_does_not_bring_streamlit_or_its_dependencies_back():
    pins = _pins()
    # Lot 51: `protobuf` re-entered the lock legitimately, as a REAL transitive
    # dependency of onnxruntime (fastembed's local-embeddings backend) — not
    # streamlit, which never returned (still checked by name below and via the
    # requirements*.txt declared-set assertion). It is deliberately NOT in this
    # blacklist anymore; it stayed blacklisted here only as a streamlit-era
    # artifact, and would need to come back if fastembed/onnxruntime were ever
    # removed without another package needing it.
    for name in ("streamlit", "pandas", "altair", "pyarrow", "pydeck", "watchdog", "jsonschema", "blinker", "toml"):
        assert name not in pins, name
    declared = {canonicalize_name(Requirement(line.split("#", 1)[0].strip()).name)
                for source in ("requirements.txt", "requirements-test.txt")
                for line in (ROOT / source).read_text(encoding="utf-8").splitlines() if line.split("#", 1)[0].strip()}
    assert "streamlit" not in declared, "comments may mention it; a requirement line may not"
    assert not (ROOT / "src" / "ui").exists()
