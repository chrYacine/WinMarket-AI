"""Shared pytest fixtures for the V3 SaaS test suite.

Uses an isolated, on-disk SQLite database per test (fast, no external
Postgres dependency required to run the suite) — the ORM models are
written to work identically against SQLite and PostgreSQL (see
src/web/database/models.py: portable Uuid type, JSONB.with_variant(JSON,
"sqlite")), so this exercises the real code paths, not a mock.

B04-T0 — automatic, whole-suite isolation (see docs/qa/b04_*/RAPPORT_B04.md
for why this exists). Two problems drove this:

1. Recurring accidental writes to the real data/ directory: several test
   files across B02/B03 forgot to redirect config.LOCAL_STORAGE_PATH before
   uploading a document, each time discovered only after the fact via
   `git status`. A per-file opt-in fixture is exactly the kind of thing a
   new test file can forget — so this is now autouse at the conftest.py
   level, applying to every test in the suite whether its author asked for
   it or not.

2. src/core/config.py unconditionally calls load_dotenv(ROOT_DIR / ".env",
   override=True) at import time — before any FIXTURE can run, since
   config.py gets imported (transitively, by main.py/src.web.*) the moment
   pytest first collects a test module that references it. A real .env
   with a real ANTHROPIC_API_KEY / PAPPERS_API_TOKEN was confirmed present
   in this repo.

   IMPORTANT CORRECTION (found during an independent-in-spirit review of
   this same file, 2026-09-14 — see docs/qa/validation_b04_*/): an earlier
   version of this guard did the blanking inside a session-scoped
   `@pytest.fixture(autouse=True)` — but a fixture body only runs at the
   FIRST TEST's setup, which happens AFTER pytest has already finished
   COLLECTING (importing) every test module in the whole run. Between
   "conftest.py imported (config.py loaded, real key captured)" and "first
   fixture body executes", every test module's own imports already ran
   with the real key still live in os.environ/config.ANTHROPIC_API_KEY.
   No evidence was found that anything used it in that window (nothing
   during collection constructs a real provider), but "nothing happened to
   use it" is not the same guarantee as "it structurally cannot be used" —
   see RAPPORT_B04.md's own investigation for why this distinction matters.

   The blanking below now runs as plain MODULE-LEVEL code in this file,
   immediately after importing `config` — conftest.py is always imported
   by pytest before it collects any test_*.py file in this directory, so
   this is the earliest point reachable from test infrastructure alone
   (short of editing src/core/config.py itself, out of scope for B04/this
   validation). This is still not "before config.py's own load_dotenv()
   ran" — that cannot be prevented without changing application code — but
   it is now "before any test module's own import statements run", closing
   the gap the fixture-based version left open.

   Real provider credentials are blanked (which is what actually prevents
   AnthropicProvider/OpenAICompatibleProvider from ever reporting
   `enabled=True` — see src/agents/llm_providers.py), and the two real
   transport libraries this codebase uses for outbound calls (`requests`,
   used only by src/agents/company_enrichment.py for Pappers; the
   `anthropic` SDK client class, used only by AnthropicProvider) are
   patched to raise immediately if ever invoked for real — applied once,
   for the whole session, never reverted (there is no legitimate reason
   for any test in this suite to need the real ones). `httpx` is
   deliberately NOT blocked — FastAPI's own TestClient is built on httpx
   for in-process ASGI calls, and blocking it would break every HTTP-level
   test in this suite; the credential-blanking already covers
   OpenAICompatibleProvider's httpx.post call path (it never runs because
   `enabled` is False first). A FakeProvider-based test (tests/
   test_llm_fallback.py) does no real I/O at all, so none of this affects
   it — it never touches `requests` or the real `anthropic` package.
"""
import os
os.environ["WM_DB_TEST_MODE"] = "1"
import uuid
from pathlib import Path

import pytest
import requests
from fastapi.testclient import TestClient

from src.core import config
from src.web import jobs
from src.web.database import session as db_session_module
from src.web.database.models import Base

_CREDENTIAL_ENV_VARS = ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "MISTRAL_API_KEY", "PAPPERS_API_TOKEN"]


def _blocked_call(*_args, **_kwargs):
    raise RuntimeError(
        "B04-T0 guard: a real outbound network call was attempted during a test run. "
        "This must never happen — see tests/conftest.py (module-level network block)."
    )


# --- Module-level execution: runs the moment pytest imports this
# conftest.py, before any test_*.py file in this directory is collected.
# Deliberately NOT inside a fixture — see the docstring above.
for _name in _CREDENTIAL_ENV_VARS:
    os.environ.pop(_name, None)
    setattr(config, _name, "")
del _name

requests.get = _blocked_call
requests.post = _blocked_call
requests.Session.request = _blocked_call
try:
    import anthropic
    anthropic.Anthropic = _blocked_call
except ImportError:
    pass

# Lot 50 ter §2: `src/core/db_target.py`'s test-mode guard normally activates on `PYTEST_CURRENT_TEST`, which
# pytest only sets once a SPECIFIC test's setup/call/teardown is under way — not during collection, and not
# during a bare module import that happens to touch a database before any test has started. Setting this here,
# at the SAME module-level point the credential blanking above already runs (the earliest point reachable from
# test infrastructure, before any test_*.py file in this directory is even collected), closes that window for
# the whole pytest session — never overridden if a script/CI already set it explicitly (`setdefault`).
os.environ.setdefault("WM_DB_TEST_MODE", "1")


_B04_ALLOWED_ROOTS: list[Path] = []


def _b04_assert_within_allowed_roots(path) -> None:
    resolved = Path(path).resolve()
    if not any(resolved == root or root in resolved.parents for root in _B04_ALLOWED_ROOTS):
        raise RuntimeError(
            f"B04-T0 guard: refused a write outside the allowed temporary test roots: {resolved}"
        )


@pytest.fixture(autouse=True)
def _b04_isolated_filesystem_roots(tmp_path_factory, monkeypatch):
    """Autouse for every test, no opt-in required. Redirects every
    filesystem root the application writes business data under
    (OUTPUT_DIR, LOCAL_STORAGE_PATH, DATA_DIR, LOGS_DIR) to a
    fresh, short-named directory (tmp_path_factory.mktemp("b"), e.g.
    ".../b3/") for tests that don't need to override it themselves.

    The allowed-roots registry (_B04_ALLOWED_ROOTS, checked by
    _b04_storage_write_guard below) is set to pytest's own --basetemp root
    (tmp_path_factory.getbasetemp()) rather than just this fixture's own
    subdirectory — a test that additionally needs OUTPUT_DIR isolated
    (most of the /api/analyze-calling tests) legitimately points
    LOCAL_STORAGE_PATH at its OWN tmp_path-derived directory afterward,
    which is a *sibling* temp directory just as safe as this fixture's,
    not a child of it. Trusting the whole basetemp tree (never the real
    project directory, which is never under it) is what actually catches
    the dangerous case — a misconfiguration pointing outside all temp
    directories entirely — without also rejecting every legitimate
    per-test override.

    tmp_path_factory rather than tmp_path deliberately: tmp_path embeds
    the full test id in its path, and the private knowledge storage layout
    nests 4 UUID directories (org/user/document/version) under it — long
    enough to exceed Windows' 260-char MAX_PATH for a long test name, a
    real failure mode hit while building the B03 suite."""
    _B04_ALLOWED_ROOTS[:] = [Path(tmp_path_factory.getbasetemp()).resolve()]
    root = tmp_path_factory.mktemp("b")
    data_dir = root / "data"
    data_dir.mkdir()
    outputs_dir = data_dir / "outputs"
    outputs_dir.mkdir()
    logs_dir = root / "logs"
    logs_dir.mkdir()
    historique_dir = data_dir / "historique"
    historique_dir.mkdir()
    analyses_dir = historique_dir / "analyses"
    analyses_dir.mkdir()

    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "OUTPUT_DIR", outputs_dir)
    monkeypatch.setattr(config, "LOCAL_STORAGE_PATH", data_dir)
    monkeypatch.setattr(config, "LOGS_DIR", logs_dir)

    # B04-T2: several module-level path constants are bound ONCE at import
    # time from the ORIGINAL config.DATA_DIR/config.OUTPUT_DIR — before this
    # fixture (or any fixture) ever runs, since pytest imports every test
    # module (and everything it transitively imports) during COLLECTION,
    # which happens before any fixture body executes for the whole run.
    # Patching config.DATA_DIR/config.OUTPUT_DIR above does nothing for a
    # constant that already captured the OLD value at import time.
    #
    # These three are confirmed real write leaks — before this fix,
    # tests/test_job_error_handling.py and tests/test_qa_llm_capture.py
    # (which do not redirect these by hand) wrote real PDF/DOCX/JSON files
    # into this actual project's data/outputs/ and data/historique/
    # directories every time they ran a real /api/analyze job. Two other
    # pre-existing test files (tests/test_b11_t1_persistence.py, tests/
    # test_b19_t2_routes_wiring.py) already worked around this PER-FILE by
    # manually monkeypatching the same three names by hand — exactly the
    # "a test author has to remember it" problem this whole fixture exists
    # to eliminate (see this fixture's own docstring, reason #1). Fixing it
    # here means every test gets it for free, including those two files'
    # own manual overrides, which remain harmless (they just re-point to
    # their own, equally-isolated tmp_path-derived directories afterward).
    #
    # See docs/qa/B04_T2_NOTES.md for the full inventory of every path
    # constant checked for this ticket, including the one deliberately NOT
    # patched here (src.web.examples_service.EXAMPLES_DIR is read-only in
    # every code path a test can reach) and why. (Lot 43: the global
    # historique JSON, the capacity file and the demo corpus path no longer
    # exist in the application, so there is nothing left to redirect for them.)
    monkeypatch.setattr(jobs, "ANALYSES_DIR", analyses_dir)
    monkeypatch.setattr(jobs, "ANALYSIS_FILES_DIR", outputs_dir)

    yield root


@pytest.fixture(autouse=True)
def _b04_storage_write_guard(monkeypatch, _b04_isolated_filesystem_roots):
    """Wraps src.web.knowledge.storage.write_upload with a path check
    performed BEFORE the real function ever touches disk — a test whose
    LOCAL_STORAGE_PATH somehow still points outside this test's allowed
    root (a misconfigured override, a forgotten monkeypatch order, a
    caller that captured a real default argument before isolation applied
    — exactly the class of bug the original data/ leaks and the
    default-argument incident of the (since removed) CapacityRepository was) is refused before
    a single byte is written, not discovered afterward via git status."""
    from src.web.knowledge import storage as knowledge_storage

    real_write_upload = knowledge_storage.write_upload

    def _guarded_write_upload(**kwargs):
        _b04_assert_within_allowed_roots(Path(config.LOCAL_STORAGE_PATH))
        return real_write_upload(**kwargs)

    monkeypatch.setattr(knowledge_storage, "write_upload", _guarded_write_upload)


@pytest.fixture(autouse=True)
def _b04_historique_jobs_write_guard(monkeypatch, _b04_isolated_filesystem_roots):
    """B04-T2: same idiom as _b04_storage_write_guard above, extended to
    the other real disk-writing function identified by this ticket's gap
    analysis — jobs._persist (writes ANALYSES_DIR/<job_id>.json). (Lot 43:
    the second one, historique_service.append_to_historique, no longer
    exists — the global JSON history is not written any more.)

    This is defense in depth, not a replacement for the redirection done
    in _b04_isolated_filesystem_roots above: that redirection is what
    actually makes every test isolated by default; this guard exists so a
    FUTURE regression — someone reverting that redirection, a new caller
    bypassing it, a monkeypatch order mistake in a test that overrides
    these paths itself — is refused loudly, before a single byte is
    written, rather than discovered afterward via git status (the exact
    failure mode this whole ticket investigated)."""
    real_persist = jobs._persist

    def _guarded_persist(job):
        _b04_assert_within_allowed_roots(Path(jobs.ANALYSES_DIR))
        return real_persist(job)

    monkeypatch.setattr(jobs, "_persist", _guarded_persist)


@pytest.fixture(autouse=True)
def _b14_reset_rate_limits():
    """B14-T1: src/web/security/rate_limit.py keeps its `_attempts` counter
    dict at MODULE scope — deliberately, so it's shared/atomic across
    threads of one process (see that module's own docstring on the honest
    single-process scope of that guarantee). Left unreset, that same
    module-level dict persists across every test FUNCTION in a whole pytest
    run, not just within one test — and FastAPI's TestClient reports a
    constant `request.client.host` ("testclient") for every request from
    every test in this suite, so an IP-keyed action (e.g. "register", used
    by src/web/routes_auth.py::register_submit) would accumulate attempts
    ACROSS unrelated test files and eventually make some later, otherwise
    correct test's registration/login call fail with a bogus 429 purely
    because of test execution order/count — a real cross-test contamination
    bug, not a hypothetical one (confirmed: RATE_LIMIT_REGISTER_MAX_ATTEMPTS
    defaults to 5, and more than 5 tests across this suite call /register).
    Autouse, like the B04-T0 filesystem-isolation fixtures above, so no
    individual test file has to remember to opt in."""
    from src.web.security import rate_limit

    rate_limit._attempts.clear()
    yield
    rate_limit._attempts.clear()


@pytest.fixture()
def test_db(tmp_path, monkeypatch):
    db_path = tmp_path / f"test_{uuid.uuid4().hex}.db"
    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setattr(config, "SESSION_SECRET", "test-secret-not-for-production")
    monkeypatch.setattr(db_session_module, "_engine", None)
    monkeypatch.setattr(db_session_module, "_SessionLocal", None)

    engine = db_session_module.get_engine()
    Base.metadata.create_all(engine)
    yield engine

    engine.dispose()
    monkeypatch.setattr(db_session_module, "_engine", None)
    monkeypatch.setattr(db_session_module, "_SessionLocal", None)


@pytest.fixture()
def db(test_db):
    """A plain SQLAlchemy Session for repository-level tests."""
    session = db_session_module.get_session_factory()()
    yield session
    session.close()


@pytest.fixture()
def client(test_db):
    """FastAPI TestClient wired to the isolated test database."""
    import main
    with TestClient(main.app) as c:
        yield c


def configure_synthetic_scoring_for_owner(db, *, organization_id, owner_user_id, email: str = "test") -> None:
    """Shared by make_active_starter_user and any test helper that adds a
    SECOND member to an existing organization (e.g. test_b02_organizations.
    py::_add_member) — capacity AND scoring are owner-scoped, never
    organization-wide, so each member needs their own row, not just the
    org's first user. Builds a synthetic ScoringPolicy + ProviderProfile
    that reproduce ScoringEngine's pre-B06-T1 GLOBAL
    weights/thresholds/mastered/certs_ok exactly, so pre-existing
    B01-B04 tests keep seeing identical scores/decisions after policy
    injection became mandatory for /api/analyze (ticket B06-T1 section 5
    explicitly permits a test fixture doing this: "les fixtures de tests
    peuvent créer une politique synthétique explicite")."""
    from tests.synthetic_scoring import (
        SYNTHETIC_CERTS, SYNTHETIC_MASTERED, SYNTHETIC_THRESHOLD_GO, SYNTHETIC_THRESHOLD_SOUS_RESERVE,
        SYNTHETIC_WEIGHTS,
    )
    from src.web.database.repositories import provider_profile as provider_profile_repo
    from src.web.database.repositories import scoring_policy as scoring_policy_repo

    provider_profile_repo.save_for_owner(
        db, organization_id=organization_id, owner_user_id=owner_user_id,
        raison_sociale=f"ESN de test ({email})", effectif=None,
        competences=sorted(SYNTHETIC_MASTERED),
        certifications=[{"nom": c, "statut": "declaree", "preuve_reference": None} for c in sorted(SYNTHETIC_CERTS)],
    )
    scoring_policy_repo.save_draft(
        db, organization_id=organization_id, owner_user_id=owner_user_id, created_by_user_id=owner_user_id,
        weights=dict(SYNTHETIC_WEIGHTS),
        threshold_go=SYNTHETIC_THRESHOLD_GO, threshold_sous_reserve=SYNTHETIC_THRESHOLD_SOUS_RESERVE,
    )
    scoring_policy_repo.activate_draft(db, organization_id=organization_id, owner_user_id=owner_user_id, expected_active_version=None)


def make_active_starter_user(
    db, email: str, password: str = "Sup3rSecret!", *, role: str = "organization_admin",
    capacity: bool = True, scoring: bool = True,
):
    """Test helper: an already-active Starter user, ready to log in, with
    B02's registration side-effect reproduced — a private Organization and
    an active Membership (role configurable, default 'organization_admin'
    to match what real registration grants the account owner).

    `capacity=True` (default) also configures a private CapacityPlan (B03)
    for this account, since most tests care about something other than the
    CAPACITY_NOT_CONFIGURED precondition and would otherwise all need to
    configure one before /api/analyze stops refusing with 409 — pass
    `capacity=False` for a test that specifically wants a fresh account
    with nothing configured yet.

    `scoring=True` (default, B06-T1) similarly activates a synthetic
    ScoringPolicy + ProviderProfile — deliberately built to reproduce
    ScoringEngine's pre-B06-T1 GLOBAL weights/thresholds/mastered/certs_ok
    exactly (see _SYNTHETIC_POLICY_* below), so every pre-existing
    B01-B04 test that asserts a specific score/decision continues to see
    the identical numbers after policy injection became mandatory for
    /api/analyze — this is a test fixture creating an explicit synthetic
    policy (ticket B06-T1 section 5 permits this explicitly: "les fixtures
    de tests peuvent créer une politique synthétique explicite ; ce n'est
    pas un défaut de configuration des nouveaux comptes"), never a
    real account's default. Pass `scoring=False` for a test that
    specifically wants a fresh account with no active policy yet."""
    from src.web.auth import service as auth_service
    from src.web.database.repositories import memberships as memberships_repo
    from src.web.database.repositories import organizations as organizations_repo
    from src.web.database.repositories import private_capacity as private_capacity_repo
    from src.web.database.repositories import subscriptions as subscriptions_repo
    from src.web.database.repositories import users as users_repo

    user = users_repo.create_user(
        db, email=email, password_hash=auth_service.hash_password(password),
        first_name="Test", last_name="User", status="active",
    )
    subscription = subscriptions_repo.create_subscription(db, user_id=user.id, plan="starter", status="pending")
    subscriptions_repo.activate(db, subscription)
    org = organizations_repo.create_organization(db, name=f"Organisation de {email}")
    memberships_repo.create_membership(db, user_id=user.id, organization_id=org.id, role=role, status="active")
    if capacity:
        private_capacity_repo.save_for_owner(
            db, organization_id=org.id, owner_user_id=user.id,
            charge_globale_pct=50, nombre_projets_en_cours=1, projets_en_cours=["Projet test"],
            capacites_par_pole={"Software Engineering": 40},
        )
    if scoring:
        configure_synthetic_scoring_for_owner(db, organization_id=org.id, owner_user_id=user.id, email=email)
    db.commit()
    return user


def default_org_id(db, user):
    """The single active organization a `make_active_starter_user` account
    belongs to — for tests that need to pass `organization_id` explicitly
    to a repository call rather than going through AccessContext/HTTP."""
    from src.web.database.repositories import memberships as memberships_repo

    memberships = memberships_repo.list_active_for_user(db, user.id)
    assert len(memberships) == 1, f"expected exactly one active membership for {user.email}, got {len(memberships)}"
    return memberships[0].organization_id
