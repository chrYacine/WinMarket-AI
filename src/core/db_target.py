"""Lot 50 ter §1 — the ONE shared resolver for which database URL a migration (or any other DB-touching
operation built on `migrations/env.py`) may target.

Built after a real incident (lot 50 bis): `migrations/env.py` used to do
`DATABASE_URL or config.get_main_option("sqlalchemy.url", "")` — the real application's `.env`-derived
`DATABASE_URL` ALWAYS won over a URL a caller (a test, a scratch script) explicitly set on the Alembic
`Config` object, so a migration test believing it targeted a disposable `tmp_path` sqlite file could
silently run against the real database instead. A later, independent regression (this same lot) reproduced
the identical failure through an ORDINARY `pytest` invocation of `tests/test_lot47bis_ao_dossier.py`, not a
scratch script — proving a documentation-only fix ("remember to isolate config") is not enough; the
refusal must be structural and automatic.

Precedence for NORMAL (non-test) usage — documented here once, must never silently change deployment
behaviour:
1. `cli_url` — an explicit `-x db_url=...` passed on the `alembic` command line (rare, manual operations).
2. `explicit_url` — whatever `alembic.Config.get_main_option("sqlalchemy.url")` returns. `alembic.ini` in
   this repo deliberately never sets a default for this key (see its own comment), so a NON-EMPTY value
   here can only mean a caller explicitly called `set_main_option(...)` themselves — this is the caller
   stating intent directly and must never be silently overridden by anything else.
3. `src.core.config.DATABASE_URL` (`.env`) — the normal path when nobody overrides it: a plain
   `alembic upgrade head` from the command line, or the running application's own startup.
None resolving is a hard, explicit refusal — never a silent empty string reaching a connection attempt.

TEST MODE — detected automatically via the `PYTEST_CURRENT_TEST` environment variable (set by pytest for
the whole lifetime of every test, no configuration needed) or an explicit `WM_DB_TEST_MODE=1` (for a
scratch script that wants the exact same protection outside of pytest). Whichever URL step 1-3 above
resolves to is THEN required to pass `assert_disposable_test_target` before this function returns it — this
runs even when the URL came from step 3 (the real `DATABASE_URL`), which is exactly the case that bit us:
a test that forgot (or failed) to supply an explicit override is refused here, before any connection, file
creation, DDL or DROP is attempted, rather than silently touching the real database.
"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

from sqlalchemy.engine import make_url

# The real application's own known database filename — a hard block regardless of directory, belt-and-
# suspenders on top of the directory check below (a relative/mis-resolved path could otherwise coincidentally
# still carry this exact name outside the real repo, which is why this is a SEPARATE, unconditional check).
_FORBIDDEN_DB_FILENAMES = ("winmarket_local.db",)
_DISPOSABLE_NAME = re.compile(r"(test|ci|qualif|scratch|tmp)", re.IGNORECASE)
# Drivers may let query arguments override the authority/path of an URL.
# In test mode the target must be fully described by the checked fields.
_POSTGRES_ROUTING_OPTIONS = frozenset({
    "host", "hostaddr", "port", "database", "dbname", "unix_sock", "service", "servicefile", "dsn",
})


class DatabaseTargetRefused(RuntimeError):
    """Raised before any connection/file-creation/DDL is attempted."""


def _in_test_mode() -> bool:
    return bool(os.environ.get("PYTEST_CURRENT_TEST")) or os.environ.get("WM_DB_TEST_MODE") == "1"


def _repo_root() -> Path:
    # src/core/db_target.py -> src/core -> src -> <repo root>
    return Path(__file__).resolve().parents[2]


def _allowed_test_roots() -> list[Path]:
    """Where a disposable SQLite file may legitimately live during a test. Default: the OS temp directory
    (covers every existing test in this suite — all of them use pytest's own `tmp_path`, which lives under
    it unless `--basetemp` is pointed elsewhere). `WM_TEST_DB_EXTRA_ROOTS` (os.pathsep-separated absolute
    paths) is an explicit, deliberate opt-in for a custom `--basetemp` location outside the OS temp
    directory (e.g. a short path used to dodge Windows' MAX_PATH) — never a silent blanket allowance."""
    roots = [Path(tempfile.gettempdir()).resolve()]
    for part in os.environ.get("WM_TEST_DB_EXTRA_ROOTS", "").split(os.pathsep):
        part = part.strip()
        if part:
            roots.append(Path(part).resolve())
    return roots


def _same_target(url_a: str, url_b: str) -> bool:
    try:
        a, b = make_url(url_a), make_url(url_b)
    except Exception:
        return url_a == url_b
    if a.get_backend_name() == "sqlite" and b.get_backend_name() == "sqlite":
        pa = Path(a.database).resolve() if a.database and a.database != ":memory:" else a.database
        pb = Path(b.database).resolve() if b.database and b.database != ":memory:" else b.database
        return pa == pb
    return (a.get_backend_name(), a.host, a.port, a.database, a.query) == (
        b.get_backend_name(), b.host, b.port, b.database, b.query,
    )


def assert_unambiguous_postgresql_target(url: str) -> None:
    """Reject routing overrides before a test can connect or DROP a schema.

    Shared with tests/pg_support.py, which creates its engine directly.
    Even matching overrides in WM_POSTGRES_TEST_URL are refused: otherwise
    a harmless path name can mask a different database passed to the driver.
    Never include URL values (potential credentials) in the refusal.
    """
    try:
        parsed = make_url(url)
        valid = parsed.get_backend_name() == "postgresql" and not (
            {key.lower() for key in parsed.query} & _POSTGRES_ROUTING_OPTIONS
        )
    except Exception:
        valid = False
    if not valid:
        raise DatabaseTargetRefused(
            "REFUSED (mode test) : cible PostgreSQL ambiguë ; l'hôte, le port et la base doivent "
            "figurer dans l'URL principale, sans option de routage dans la query string."
        )


def assert_disposable_test_target(url: str) -> None:
    """Refuses (raises `DatabaseTargetRefused`) unless `url` is structurally safe to run a migration/DDL
    against DURING A TEST. Never guesses in the caller's favour — any ambiguity (malformed URL, relative
    path escaping the allowed roots, an unapproved non-SQLite target) is a refusal, not a best-effort pass.

    Deliberately structural, never a comparison against `config.DATABASE_URL`: `get_engine()` calls this
    AFTER a test fixture has already pointed `config.DATABASE_URL` AT the disposable URL being validated —
    comparing the two there would always be "equal" and wrongly refuse every legitimate test. The forbidden-
    filename / repo-root / allowed-roots rules below (sqlite) and the WM_POSTGRES_TEST_URL-exact-match rule
    (postgres) already structurally cover "this is/derives from the real application target" without needing
    that comparison."""
    try:
        parsed = make_url(url)
    except Exception as exc:
        raise DatabaseTargetRefused(f"REFUSED (mode test) : URL de base de données invalide ({type(exc).__name__}).") from exc

    if parsed.get_backend_name() == "sqlite":
        db = parsed.database
        if not db or db == ":memory:":
            return  # in-memory: never persists, always disposable
        resolved = Path(db).resolve()
        if any(marker in resolved.name for marker in _FORBIDDEN_DB_FILENAMES):
            raise DatabaseTargetRefused(f"REFUSED (mode test) : {resolved} porte le nom de la base applicative réelle.")
        repo_root = _repo_root()
        if resolved == repo_root or repo_root in resolved.parents:
            raise DatabaseTargetRefused(f"REFUSED (mode test) : {resolved} est à l'intérieur du dépôt réel — jamais une cible de test.")
        allowed = _allowed_test_roots()
        if not any(resolved == root or root in resolved.parents for root in allowed):
            raise DatabaseTargetRefused(
                f"REFUSED (mode test) : {resolved} n'est sous aucune racine jetable autorisée ({[str(r) for r in allowed]}). "
                "Utilisez tmp_path (pytest) ou déclarez WM_TEST_DB_EXTRA_ROOTS explicitement."
            )
    else:
        assert_unambiguous_postgresql_target(url)
        test_url_env = os.environ.get("WM_POSTGRES_TEST_URL", "")
        if not test_url_env or not _same_target(url, test_url_env):
            raise DatabaseTargetRefused(
                "REFUSED (mode test) : une cible non-SQLite doit provenir exactement de WM_POSTGRES_TEST_URL — "
                "jamais devinée depuis DATABASE_URL ni un nom qui contient simplement « test »."
            )
        if not _DISPOSABLE_NAME.search(parsed.database or ""):
            raise DatabaseTargetRefused(
                f"REFUSED (mode test) : le nom de base {parsed.database!r} ne contient aucun des marqueurs "
                "jetables attendus (test/ci/qualif/scratch/tmp)."
            )


def resolve_database_url(*, explicit_url: str = "", cli_url: str = "") -> str:
    """The ONE function `migrations/env.py` (and anything else that needs the same guarantee) calls to get
    a validated URL. See module docstring for the full precedence/test-mode contract."""
    from src.core.config import DATABASE_URL as APP_DATABASE_URL
    from src.web.database.session import normalize_database_url

    url = cli_url or explicit_url or APP_DATABASE_URL
    if not url:
        raise DatabaseTargetRefused(
            "Aucune URL de base de données résolue (ni -x db_url, ni Config.sqlalchemy.url explicite, ni "
            "DATABASE_URL applicatif). Refusé avant toute connexion."
        )
    url = normalize_database_url(url)
    if _in_test_mode():
        assert_disposable_test_target(url)
    else:
        # Lot 58: this is the function Alembic itself calls to run real migrations — it must accept
        # a real, remote deployment target (Render or equivalent) exactly like
        # environment_guard.validate_environment does, using the SAME single switch
        # (is_deployment_mode) rather than a second, divergent notion of "is this a deployment".
        from src.core.environment_guard import is_deployment_mode, validate_deployment_url, validate_url
        if is_deployment_mode(os.environ):
            validate_deployment_url(url)
        else:
            validate_url(url)
    return url
