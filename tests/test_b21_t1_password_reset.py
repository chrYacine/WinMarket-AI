"""B21-T1 (DEFECTS confirmed): password reset hardening.

Two confirmed defects fixed here:

1. `reset_password_submit` used to SELECT every live, unexpired
   PasswordResetToken and run an Argon2 verify() against each in a Python
   loop — Argon2 is deliberately slow (the point for User.password_hash),
   so this scaled O(n) Argon2 verifications per attempt across the WHOLE
   token table. Replaced by an indexed digest lookup + a single atomic
   conditional UPDATE (src/web/database/repositories/
   password_reset_tokens.py — same idiom as
   src/web/database/repositories/analysis_jobs.py::try_claim).

2. There was NO session revocation mechanism at all: a bare signed cookie
   (itsdangerous, user_id only) with nothing server-side to invalidate an
   already-issued cookie. A password reset used to change only
   `user.password_hash` — any OTHER already-issued, still validly-signed
   cookie for that user (a second device, a stolen cookie) stayed valid
   indefinitely. Fixed with a `User.session_version` "security stamp"
   embedded in the cookie (src/web/auth/session_cookie.py) and checked on
   every read (src/web/auth/dependencies.py::get_current_user) — bumped
   atomically with the password change.

Test groups, per the ticket:
A. Full happy path: request -> (simulated) email -> consume -> old
   password rejected, new password accepted.
B. Expired / garbage / reused token: rejected with the same generic
   message, no distinguishing information leaked.
C. Concurrent consumption: exactly one of two racing attempts wins — a
   real DB-level conditional UPDATE, not timing/luck.
D. Old cookie rejected after reset — the core "révocation effective"
   proof.
E. A neighboring account is completely unaffected by someone else's reset.
F. Reset-request rate limiting triggers identically whether or not the
   requested email exists.
G. Email-adapter failure: response stays generic, token never reaches the
   HTTP response body or a log line.
H. Migration 0008 applies cleanly on a fresh AND a populated sqlite db.

Helpers are copied/adapted locally (this codebase's convention: test files
do not import from one another — see tests/test_b12_t1_job_executor.py's
own docstring for the same note). No real network, no real .env/API key
ever read (blocked at tests/conftest.py module level already), no commit
to git.

Rate-limiting note: src/web/routes_account.py calls B14-T1's (Agent A,
built in parallel) generalized `check_and_record(action, key, *,
max_attempts, window_seconds)` primitive in src/web/security/rate_limit.py,
with the action-specific thresholds read from
config.RATE_LIMIT_RESET_REQUEST_MAX_ATTEMPTS /
config.RATE_LIMIT_RESET_CONSUME_MAX_ATTEMPTS. Test F below reads those same
config constants rather than hardcoding a count, so it stays correct if
those thresholds are ever tuned.
"""
from __future__ import annotations

import re
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text

from src.core import config as core_config
from src.web import routes_account
from src.web.database.models import PasswordResetToken, User
from src.web.database.repositories import password_reset_tokens as reset_tokens_repo
from src.web.database.session import session_scope
from tests.conftest import make_active_starter_user

PROJECT_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Local helpers.
# ---------------------------------------------------------------------------

def _csrf_from(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]*)"', html)
    assert match, html
    return match.group(1)


def _login(client, email: str, password: str):
    r = client.get("/login")
    csrf = _csrf_from(r.text)
    # follow_redirects=False: a successful login is a 303 to `next` — letting
    # the client auto-follow it (the TestClient default) would report the
    # status of whatever page it lands on instead (typically 200), masking
    # exactly the signal these tests need to assert on.
    return client.post(
        "/login", data={"email": email, "password": password, "next": "/app", "csrf_token": csrf},
        follow_redirects=False,
    )


def _reset_page_csrf(client, token: str = "") -> str:
    r = client.get("/reset-password", params={"token": token})
    return _csrf_from(r.text)


def _token_from_url(url: str) -> str:
    qs = parse_qs(urlparse(url).query)
    return qs["token"][0]


def _install_fake_email(monkeypatch):
    """Simulated local inbox — appends (to_email, reset_url) instead of
    ever touching real SMTP. The raw token is only ever read from HERE in
    these tests, never from an API response."""
    sent: list[tuple[str, str]] = []

    def _fake_send(to_email: str, reset_url: str) -> bool:
        sent.append((to_email, reset_url))
        return True

    monkeypatch.setattr(routes_account, "send_password_reset_email", _fake_send)
    return sent


@pytest.fixture()
def client(test_db):
    """Overrides tests/conftest.py's `client` fixture for THIS file only:
    gives every test its own unique client "IP" (Starlette TestClient's
    `client=(host, port)` constructor arg controls what
    `request.client.host` sees). B21-T1's rate limiter keys purely on IP
    and is process-wide, in-memory state (src/web/security/rate_limit.py)
    that persists for the whole test session — without this, tests in this
    file (and the dedicated rate-limit test especially, which deliberately
    exhausts its own budget) could interfere with each other or with any
    other test module hitting the same default "testclient" host."""
    import main

    host = f"198.51.100.{uuid.uuid4().int % 250}"
    with TestClient(main.app, client=(host, 51000)) as c:
        yield c


# ---------------------------------------------------------------------------
# Group A — full happy path.
# ---------------------------------------------------------------------------

def test_full_happy_path_reset_flow(client, db, monkeypatch):
    make_active_starter_user(db, "resetflow@example.com", password="OldPassw0rd!")
    sent = _install_fake_email(monkeypatch)

    csrf = _csrf_from(client.get("/forgot-password").text)
    r = client.post("/forgot-password", data={"email": "resetflow@example.com", "csrf_token": csrf})
    assert r.status_code == 200
    assert len(sent) == 1
    to_email, reset_url = sent[0]
    assert to_email == "resetflow@example.com"
    from src.core import config
    assert reset_url.startswith(config.BASE_URL)
    raw_token = _token_from_url(reset_url)

    csrf2 = _reset_page_csrf(client, raw_token)
    r2 = client.post("/reset-password", data={
        "reset_token": raw_token, "password": "NewPassw0rd!", "password_confirm": "NewPassw0rd!",
        "csrf_token": csrf2,
    })
    assert r2.status_code == 200
    assert "mis à jour" in r2.text.lower()

    r_old = _login(client, "resetflow@example.com", "OldPassw0rd!")
    assert r_old.status_code == 200  # re-renders the login form with an error, no redirect
    assert "incorrect" in r_old.text.lower()

    r_new = _login(client, "resetflow@example.com", "NewPassw0rd!")
    assert r_new.status_code == 303


def test_login_after_a_reset_stays_authenticated_on_the_next_request(client, db, monkeypatch):
    """Coordinator regression test for a real integration bug found during
    review: routes_auth.py's login handlers called
    `set_user_session(resp, user.id)` without the user's CURRENT
    session_version, defaulting to 0. For a user whose session_version has
    already been bumped once (any prior password reset), that mints an
    IMMEDIATELY-STALE cookie — get_current_user's session_version check
    would reject it on the very next request, silently logging the user
    back out right after a successful login. The happy-path test above
    only asserted the login POST's own 303 status, which this bug did NOT
    affect (the mismatch only surfaces on the FOLLOWING request) — so it
    would not have caught this. This test exercises exactly that sequence:
    reset once, log in fresh, then make a SEPARATE authenticated request
    with the resulting cookie and confirm it is still accepted."""
    make_active_starter_user(db, "reloginafterreset@example.com", password="OldPassw0rd!")
    sent = _install_fake_email(monkeypatch)

    csrf = _csrf_from(client.get("/forgot-password").text)
    client.post("/forgot-password", data={"email": "reloginafterreset@example.com", "csrf_token": csrf})
    _, reset_url = sent[0]
    raw_token = _token_from_url(reset_url)
    csrf2 = _reset_page_csrf(client, raw_token)
    client.post("/reset-password", data={
        "reset_token": raw_token, "password": "NewPassw0rd!", "password_confirm": "NewPassw0rd!",
        "csrf_token": csrf2,
    })

    r_new = _login(client, "reloginafterreset@example.com", "NewPassw0rd!")
    assert r_new.status_code == 303

    # The critical assertion: the cookie minted by THIS login must still
    # authenticate a follow-up request, not just have returned a redirect.
    r_account = client.get("/account")
    assert r_account.status_code == 200
    assert "reloginafterreset@example.com" in r_account.text


# ---------------------------------------------------------------------------
# Group B — expired / garbage / reused tokens: same generic rejection.
# ---------------------------------------------------------------------------

def test_expired_invalid_and_reused_tokens_all_rejected_identically(client, db):
    user = make_active_starter_user(db, "tokenstates@example.com", password="Sup3rSecret1!")

    expired_token = "expired-" + uuid.uuid4().hex
    reset_tokens_repo.create_token(db, user_id=user.id, raw_token=expired_token, ttl_seconds=-10)
    db.commit()

    csrf = _reset_page_csrf(client)
    r_expired = client.post("/reset-password", data={
        "reset_token": expired_token, "password": "NewPassw0rd!", "password_confirm": "NewPassw0rd!",
        "csrf_token": csrf,
    })
    assert routes_account._GENERIC_INVALID_TOKEN_MESSAGE in r_expired.text

    csrf2 = _reset_page_csrf(client)
    r_garbage = client.post("/reset-password", data={
        "reset_token": "totally-made-up-token-" + uuid.uuid4().hex,
        "password": "NewPassw0rd!", "password_confirm": "NewPassw0rd!", "csrf_token": csrf2,
    })
    assert routes_account._GENERIC_INVALID_TOKEN_MESSAGE in r_garbage.text

    raw_token = "reuse-" + uuid.uuid4().hex
    reset_tokens_repo.create_token(db, user_id=user.id, raw_token=raw_token, ttl_seconds=3600)
    db.commit()

    csrf3 = _reset_page_csrf(client, raw_token)
    r_first_use = client.post("/reset-password", data={
        "reset_token": raw_token, "password": "NewPassw0rd!", "password_confirm": "NewPassw0rd!",
        "csrf_token": csrf3,
    })
    assert "mis à jour" in r_first_use.text.lower()

    csrf4 = _reset_page_csrf(client, raw_token)
    r_second_use = client.post("/reset-password", data={
        "reset_token": raw_token, "password": "AnotherPassw0rd!", "password_confirm": "AnotherPassw0rd!",
        "csrf_token": csrf4,
    })
    assert routes_account._GENERIC_INVALID_TOKEN_MESSAGE in r_second_use.text

    # The three rejections are byte-for-byte the same message — nothing
    # distinguishes "expired" from "garbage" from "already used".
    assert r_expired.text.count(routes_account._GENERIC_INVALID_TOKEN_MESSAGE) == \
        r_garbage.text.count(routes_account._GENERIC_INVALID_TOKEN_MESSAGE) == \
        r_second_use.text.count(routes_account._GENERIC_INVALID_TOKEN_MESSAGE) == 1


# ---------------------------------------------------------------------------
# Group C — concurrent consumption: exactly one real winner.
# ---------------------------------------------------------------------------

def test_concurrent_consumption_exactly_one_winner(db):
    user = make_active_starter_user(db, "racetoken@example.com", password="Sup3rSecret1!")
    raw_token = "race-" + uuid.uuid4().hex
    reset_tokens_repo.create_token(db, user_id=user.id, raw_token=raw_token, ttl_seconds=3600)
    db.commit()

    barrier = threading.Barrier(2)
    results: dict[str, bool] = {}

    def _attempt(name: str) -> None:
        barrier.wait(timeout=5)
        try:
            with session_scope() as s:
                results[name] = reset_tokens_repo.try_consume(s, raw_token=raw_token)
        except Exception:
            results[name] = False

    t1 = threading.Thread(target=_attempt, args=("worker-a",))
    t2 = threading.Thread(target=_attempt, args=("worker-b",))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert set(results) == {"worker-a", "worker-b"}
    winners = [name for name, won in results.items() if won]
    assert len(winners) == 1, f"expected exactly one winner, got {winners}"

    db.expire_all()
    row = db.execute(select(PasswordResetToken).where(PasswordResetToken.user_id == user.id)).scalar_one()
    assert row.used_at is not None


# ---------------------------------------------------------------------------
# Group D — old cookie rejected after reset (core revocation proof).
# ---------------------------------------------------------------------------

def test_old_cookie_rejected_after_reset(client, db):
    make_active_starter_user(db, "revokeme@example.com", password="OldPassw0rd1!")
    r_login = _login(client, "revokeme@example.com", "OldPassw0rd1!")
    assert r_login.status_code == 303

    r_before = client.get("/account")
    assert r_before.status_code == 200, "sanity check: the freshly-issued cookie must work before any reset"

    user = db.execute(select(User).where(User.email == "revokeme@example.com")).scalar_one()
    raw_token = "revoke-" + uuid.uuid4().hex
    reset_tokens_repo.create_token(db, user_id=user.id, raw_token=raw_token, ttl_seconds=3600)
    db.commit()

    # A separate request context performs the reset — the client's cookie
    # jar (holding the attacker's still-valid old cookie) is never touched
    # by this flow, exactly like "attacker still has the old cookie".
    csrf = _reset_page_csrf(client, raw_token)
    r_reset = client.post("/reset-password", data={
        "reset_token": raw_token, "password": "BrandNewPassw0rd!", "password_confirm": "BrandNewPassw0rd!",
        "csrf_token": csrf,
    })
    assert "mis à jour" in r_reset.text.lower()

    # The OLD cookie (still automatically sent by this same client) must
    # now be refused: session_version mismatch, treated exactly like "no
    # session" — redirected to /login, never served the protected page.
    r_after = client.get("/account", follow_redirects=False)
    assert r_after.status_code in (302, 303)
    assert r_after.headers["location"].startswith("/login")

    # And logging in again with the NEW password issues a cookie that
    # works again.
    r_relogin = _login(client, "revokeme@example.com", "BrandNewPassw0rd!")
    assert r_relogin.status_code == 303
    assert client.get("/account").status_code == 200


# ---------------------------------------------------------------------------
# Group E — a neighboring account is untouched.
# ---------------------------------------------------------------------------

def test_neighboring_account_unaffected_by_someone_elses_reset(client, db, monkeypatch):
    victim = make_active_starter_user(db, "victim@example.com", password="VictimPass1!")
    neighbor = make_active_starter_user(db, "neighbor@example.com", password="NeighborPass1!")
    neighbor_hash_before = neighbor.password_hash
    neighbor_version_before = neighbor.session_version

    neighbor_token = "neighbor-" + uuid.uuid4().hex
    reset_tokens_repo.create_token(db, user_id=neighbor.id, raw_token=neighbor_token, ttl_seconds=3600)
    db.commit()

    sent = _install_fake_email(monkeypatch)
    csrf = _csrf_from(client.get("/forgot-password").text)
    client.post("/forgot-password", data={"email": "victim@example.com", "csrf_token": csrf})
    assert len(sent) == 1
    raw_token = _token_from_url(sent[0][1])

    csrf2 = _reset_page_csrf(client, raw_token)
    r = client.post("/reset-password", data={
        "reset_token": raw_token, "password": "VictimNewPass1!", "password_confirm": "VictimNewPass1!",
        "csrf_token": csrf2,
    })
    assert "mis à jour" in r.text.lower()

    db.expire_all()
    neighbor_after = db.execute(select(User).where(User.email == "neighbor@example.com")).scalar_one()
    assert neighbor_after.password_hash == neighbor_hash_before
    assert neighbor_after.session_version == neighbor_version_before

    neighbor_row = db.execute(
        select(PasswordResetToken).where(PasswordResetToken.user_id == neighbor.id)
    ).scalar_one()
    assert neighbor_row.used_at is None, "an unrelated user's own pending reset token must stay usable"


# ---------------------------------------------------------------------------
# Group F — reset-request rate limiting, identical for a real vs. fake email.
# ---------------------------------------------------------------------------

def test_reset_request_rate_limit_identical_whether_or_not_email_exists(db, monkeypatch):
    import main

    make_active_starter_user(db, "rlreal@example.com", password="Sup3rSecret1!")
    monkeypatch.setattr(routes_account, "send_password_reset_email", lambda *a, **k: True)

    max_attempts = core_config.RATE_LIMIT_RESET_REQUEST_MAX_ATTEMPTS

    def _exhaust(email: str) -> tuple[list, "TestClient"]:
        host = f"198.51.100.{uuid.uuid4().int % 250}"
        c = TestClient(main.app, client=(host, 51000))
        csrf = _csrf_from(c.get("/forgot-password").text)
        responses = []
        for _ in range(max_attempts + 1):
            responses.append(c.post("/forgot-password", data={"email": email, "csrf_token": csrf}))
        return responses, c

    real_responses, real_client = _exhaust("rlreal@example.com")
    fake_responses, fake_client = _exhaust("this-account-does-not-exist@example.com")

    # The first `max_attempts` attempts succeed (generic "submitted" shape)
    # for BOTH; the next one is rate-limited for BOTH, with the exact same
    # message — the limiter's behavior never depends on whether the email
    # is real (see config.RATE_LIMIT_RESET_REQUEST_MAX_ATTEMPTS).
    for responses in (real_responses, fake_responses):
        for resp in responses[:max_attempts]:
            assert resp.status_code == 200
            assert routes_account._GENERIC_RATE_LIMITED_MESSAGE not in resp.text
        assert responses[max_attempts].status_code == 200
        assert routes_account._GENERIC_RATE_LIMITED_MESSAGE in responses[max_attempts].text

    real_client.close()
    fake_client.close()


# ---------------------------------------------------------------------------
# Group G — email adapter failure: generic response, token never leaked.
# ---------------------------------------------------------------------------

def test_email_adapter_failure_stays_generic_and_never_leaks_the_token(client, db, monkeypatch, caplog):
    make_active_starter_user(db, "emailfail@example.com", password="Sup3rSecret1!")
    captured: dict[str, str] = {}

    def _boom(to_email: str, reset_url: str) -> bool:
        captured["token"] = _token_from_url(reset_url)
        raise RuntimeError("simulated SMTP explosion")

    monkeypatch.setattr(routes_account, "send_password_reset_email", _boom)

    # routes_account.logger (like every get_agent_logger logger in this
    # codebase — see src/core/logger.py::setup_logger) has propagate=False
    # by design (it manages its own console/file handlers), and so does its
    # ancestor "winmarket" logger (src/core/logger.py's own module-level
    # `logger = setup_logger("winmarket", ...)`) — pytest's caplog attaches
    # its capture handler to the ROOT logger only, so a record has to climb
    # past BOTH before it can reach it. Test-only; monkeypatch reverts both
    # automatically.
    import logging as _logging
    monkeypatch.setattr(routes_account.logger, "propagate", True)
    monkeypatch.setattr(_logging.getLogger("winmarket"), "propagate", True)

    csrf = _csrf_from(client.get("/forgot-password").text)
    with caplog.at_level("ERROR"):
        r = client.post("/forgot-password", data={"email": "emailfail@example.com", "csrf_token": csrf})

    assert r.status_code == 200
    assert "captured" in captured or "token" in captured
    assert captured["token"] not in r.text, "the raw reset token must never reach the HTTP response body"

    # Still the same generic, submitted-shaped response — a delivery
    # failure is invisible to the caller.
    assert routes_account._GENERIC_RATE_LIMITED_MESSAGE not in r.text
    assert routes_account._GENERIC_INVALID_TOKEN_MESSAGE not in r.text

    # A safe internal trace exists...
    assert any("password reset email" in rec.getMessage().lower() for rec in caplog.records)
    # ...but never contains the raw token either.
    for rec in caplog.records:
        assert captured["token"] not in rec.getMessage()


# ---------------------------------------------------------------------------
# Group H — migration 0008 on a fresh AND a populated sqlite database.
# ---------------------------------------------------------------------------

def test_migration_0008_applies_on_fresh_and_populated_sqlite(tmp_path):
    from alembic import command
    from alembic.config import Config

    migrations_dir = PROJECT_ROOT / "migrations"

    # --- Fresh, empty database. ---
    fresh_db = tmp_path / "fresh_0008.db"
    cfg = Config()
    cfg.set_main_option("script_location", str(migrations_dir))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{fresh_db}")
    command.upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{fresh_db}")
    with engine.connect() as conn:
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        user_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(users)"))}
        token_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(password_reset_tokens)"))}
    # B06-T5 added migration 0009 after this test was written — "head" now
    # legitimately resolves past 0008; this test's actual intent is "the
    # columns this ticket added are present after a full upgrade", not
    # "head must forever stop at exactly 0008".
    assert version == "0017"  # lot 56: additive manual access
    assert "session_version" in user_cols
    assert "token_digest" in token_cols
    assert "token_hash" in token_cols
    engine.dispose()

    # --- Populated database: 0001-0007 first, seed a user AND a
    # legacy-shaped reset-token row (token_hash populated, no
    # session_version/token_digest columns existed yet at that revision)
    # via RAW SQL matching the pre-0008 schema exactly — the ORM's current
    # User/PasswordResetToken models already declare the new columns, so
    # inserting through the ORM against a database still at revision 0007
    # would itself fail ("no such column"). THEN apply 0008 and confirm
    # both existing rows are preserved untouched.
    seeded_db = tmp_path / "seeded_0008.db"
    cfg2 = Config()
    cfg2.set_main_option("script_location", str(migrations_dir))
    cfg2.set_main_option("sqlalchemy.url", f"sqlite:///{seeded_db}")
    command.upgrade(cfg2, "0007")

    engine2 = create_engine(f"sqlite:///{seeded_db}")
    now_iso = datetime.now(timezone.utc).isoformat()
    user_id_raw = uuid.uuid4().hex
    with engine2.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (id, email, password_hash, first_name, last_name, status, created_at, updated_at) "
                "VALUES (:id, :email, :password_hash, :first_name, :last_name, :status, :created_at, :updated_at)"
            ),
            {
                "id": user_id_raw, "email": "migrated0008@example.com",
                "password_hash": "argon2-placeholder-hash", "first_name": "Test", "last_name": "User",
                "status": "active", "created_at": now_iso, "updated_at": now_iso,
            },
        )
        conn.execute(
            text(
                "INSERT INTO password_reset_tokens (id, user_id, token_hash, expires_at, created_at) "
                "VALUES (:id, :user_id, :token_hash, :expires_at, :created_at)"
            ),
            {
                "id": uuid.uuid4().hex,
                "user_id": user_id_raw,
                "token_hash": "legacy-argon2-hash-placeholder",
                "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                "created_at": now_iso,
            },
        )
    engine2.dispose()

    command.upgrade(cfg2, "0008")

    engine3 = create_engine(f"sqlite:///{seeded_db}")
    with engine3.connect() as conn:
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        user_count = conn.execute(text("SELECT COUNT(*) FROM users")).scalar()
        session_versions = [row[0] for row in conn.execute(text("SELECT session_version FROM users"))]
        token_rows = conn.execute(text("SELECT token_hash, token_digest FROM password_reset_tokens")).fetchall()
    assert version == "0008"
    assert user_count == 1
    assert session_versions == [0], "an existing user must be backfilled to session_version=0, not left NULL"
    assert len(token_rows) == 1
    assert token_rows[0][0] == "legacy-argon2-hash-placeholder", "pre-migration token_hash must be preserved"
    assert token_rows[0][1] is None, "a pre-migration row has no digest and must not be silently invented one"
    engine3.dispose()

    # A second `upgrade head` from the fresh db is a pure no-op, same
    # convention as test_b02_qa_validation.py / test_b12_t1's own group F.
    command.upgrade(cfg, "head")
