"""B20-T1 — centralized subscription-access decision.

`src/web/auth/dependencies.py::user_has_active_starter_subscription` used to
check ONLY `subscription.status == "active"` and never looked at
`started_at`/`expires_at` at all (DEFECT confirmed). This file exercises its
replacement, `evaluate_subscription_access()`, which:

  - grants only inside the half-open window [started_at, expires_at) —
    `now >= started_at` and `now < expires_at` — documented and pinned at
    the exact boundary instant below (group B);
  - normalizes DB datetimes to UTC-aware before comparing (SQLite silently
    drops tzinfo on a `DateTime(timezone=True)` column round-trip — see
    group C, which proves this is real, not hypothetical, on this project's
    own test database);
  - refuses (never grants) a genuinely incoherent state
    (`expires_at <= started_at`) with a distinguishable reason code
    ("ambiguous_dates"), rather than defaulting to access (group D);
  - applies IDENTICALLY through the API gate (require_active_starter_user)
    and the page gate (resolve_app_access) — group A's parametrization
    checks both for every scenario;
  - is re-read fresh on every request, including from an already-issued
    session cookie (group E) and at job-claim time (group F, which also
    proves zero LLM construction for a subscription that lapsed between
    submission and claim).

No real network, no real .env/API key ever read (relies on tests/conftest.py's
existing credential-blanking), no commit.
"""
from __future__ import annotations

import queue
import re
from datetime import datetime, timedelta, timezone

import pytest

from src.web import job_executor, jobs
from src.web.auth import dependencies as deps
from src.web.database.repositories import analysis_jobs as analysis_jobs_repo
from src.web.database.repositories import subscriptions as subscriptions_repo
from tests.conftest import default_org_id, make_active_starter_user

VALID_AO_TEXT = (
    "Appel d'offres - Portail client\n"
    "Acheteur : Collectivite Exemple\n"
    "Budget : 250 000 euros. Date limite : 30/11/2026.\n"
    "Exigences : Python et Django.\n"
)


def _csrf_from(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "csrf token not found in rendered page"
    return match.group(1)


def _login(client, email, password="Sup3rSecret!"):
    r = client.get("/login")
    csrf = _csrf_from(r.text)
    return client.post(
        "/login", data={"email": email, "password": password, "next": "/app", "csrf_token": csrf}
    )


def _set_subscription(db, user, *, status: str = "active", started_at=None, expires_at=None):
    """Directly overwrite the user's latest subscription row's
    status/started_at/expires_at for test scenarios — bypasses
    subscriptions_repo.activate() (which never sets expires_at today) since
    several scenarios below need to construct states no current writer
    produces (a future-billing expires_at, an admin-seeded inconsistency)."""
    sub = subscriptions_repo.get_latest_for_user(db, user.id)
    sub.status = status
    sub.started_at = started_at
    sub.expires_at = expires_at
    db.commit()
    return sub


def _assert_page_and_api_agree(client, *, expected_granted: bool):
    """The "même décision page/API" requirement: /api/examples
    (require_active_starter_user-gated) and /app/analyser
    (resolve_app_access-gated) must reach the same verdict for the same
    account state."""
    api_r = client.get("/api/examples")
    page_r = client.get("/app/analyser", follow_redirects=False)
    if expected_granted:
        assert api_r.status_code == 200, api_r.text
        assert page_r.status_code == 200, page_r.text
    else:
        assert api_r.status_code == 403, api_r.text
        assert page_r.status_code == 303, page_r.text
        assert page_r.headers["location"] == "/account/pending"


# ---------------------------------------------------------------------------
# Group A — parametrized scenarios, unit-level decision + reason code, AND
# (for the account-state-reachable-via-HTTP ones) same page/API decision.
# ---------------------------------------------------------------------------

NOW = datetime.now(timezone.utc)

SCENARIOS = [
    # (label, status, started_at, expires_at, expected_granted, expected_reason)
    ("active_no_dates", "active", None, None, True, None),
    ("active_valid_window", "active", NOW - timedelta(days=10), NOW + timedelta(days=20), True, None),
    ("expired_correct_status", "expired", NOW - timedelta(days=40), NOW - timedelta(days=10), False, "not_active_status"),
    ("expired_stale_active_status", "active", NOW - timedelta(days=40), NOW - timedelta(days=1), False, "expired"),
    ("future_start", "active", NOW + timedelta(days=5), None, False, "not_yet_started"),
    ("pending_status", "pending", None, None, False, "not_active_status"),
    ("cancelled_status", "cancelled", NOW - timedelta(days=1), None, False, "not_active_status"),
]


@pytest.mark.parametrize("label,status,started_at,expires_at,expected_granted,expected_reason", SCENARIOS)
def test_scenarios_unit_level(db, label, status, started_at, expires_at, expected_granted, expected_reason):
    user = make_active_starter_user(db, f"{label}@example.com")
    _set_subscription(db, user, status=status, started_at=started_at, expires_at=expires_at)

    decision = deps.evaluate_subscription_access(db, user)
    assert decision.granted is expected_granted, f"{label}: expected granted={expected_granted}, got {decision}"
    assert decision.reason == expected_reason, f"{label}: expected reason={expected_reason}, got {decision.reason}"
    # boolean-compatible wrapper stays consistent with the detailed decision.
    assert deps.user_has_active_starter_subscription(db, user) is expected_granted


@pytest.mark.parametrize("label,status,started_at,expires_at,expected_granted,expected_reason", SCENARIOS)
def test_scenarios_page_and_api_agree(client, db, label, status, started_at, expires_at, expected_granted, expected_reason):
    user = make_active_starter_user(db, f"http-{label}@example.com")
    _set_subscription(db, user, status=status, started_at=started_at, expires_at=expires_at)
    _login(client, f"http-{label}@example.com")

    _assert_page_and_api_agree(client, expected_granted=expected_granted)


def test_no_subscription_row_at_all(db):
    """A user row with zero subscriptions ever created — make_active_starter_user
    always creates one, so this constructs the account by hand instead."""
    from src.web.auth import service as auth_service
    from src.web.database.repositories import users as users_repo

    user = users_repo.create_user(
        db, email="nosub@example.com", password_hash=auth_service.hash_password("Sup3rSecret!"),
        first_name="No", last_name="Sub", status="active",
    )
    db.commit()

    decision = deps.evaluate_subscription_access(db, user)
    assert decision.granted is False
    assert decision.reason == "no_subscription"


# ---------------------------------------------------------------------------
# Group B — UTC boundary convention, pinned at exactly the chosen instant.
#
# Convention (see evaluate_subscription_access's docstring): half-open
# window [started_at, expires_at) — now == started_at GRANTS, now ==
# expires_at DENIES (already expired at the exact instant, not "one more
# tick").
# ---------------------------------------------------------------------------

def _freeze_now(monkeypatch, fixed):
    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed

    monkeypatch.setattr(deps, "datetime", _FrozenDatetime)


def test_boundary_started_at_exact_instant_is_granted(db, monkeypatch):
    fixed_now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    _freeze_now(monkeypatch, fixed_now)

    user = make_active_starter_user(db, "boundary-start@example.com")
    _set_subscription(db, user, status="active", started_at=fixed_now, expires_at=None)

    decision = deps.evaluate_subscription_access(db, user)
    assert decision.granted is True, decision


def test_boundary_expires_at_exact_instant_is_denied(db, monkeypatch):
    fixed_now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    _freeze_now(monkeypatch, fixed_now)

    user = make_active_starter_user(db, "boundary-expire@example.com")
    _set_subscription(
        db, user, status="active", started_at=fixed_now - timedelta(days=30), expires_at=fixed_now,
    )

    decision = deps.evaluate_subscription_access(db, user)
    assert decision.granted is False
    assert decision.reason == "expired"


def test_boundary_one_microsecond_before_expiry_is_still_granted(db, monkeypatch):
    fixed_now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    _freeze_now(monkeypatch, fixed_now)

    user = make_active_starter_user(db, "boundary-almost-expire@example.com")
    _set_subscription(
        db, user, status="active",
        started_at=fixed_now - timedelta(days=30),
        expires_at=fixed_now + timedelta(microseconds=1),
    )

    decision = deps.evaluate_subscription_access(db, user)
    assert decision.granted is True, decision


# ---------------------------------------------------------------------------
# Group C — naive-vs-aware datetime never crashes (real SQLite defect: a
# DateTime(timezone=True) column round-trips as a NAIVE datetime on SQLite).
# ---------------------------------------------------------------------------

def test_naive_datetime_from_db_round_trip_does_not_crash(db):
    user = make_active_starter_user(db, "naive-roundtrip@example.com")
    sub = subscriptions_repo.get_latest_for_user(db, user.id)
    sub.status = "active"
    sub.expires_at = datetime.now(timezone.utc) + timedelta(days=5)
    db.commit()

    # Force a real round trip through SQLite (new identity map) rather than
    # reusing the in-session Python object, which could still carry its
    # original tzinfo. This reproduces the exact condition confirmed against
    # this project's own SQLite test database: DateTime(timezone=True)
    # values come back naive.
    db.expire_all()
    reloaded_user = user
    reloaded_sub = subscriptions_repo.get_latest_for_user(db, reloaded_user.id)
    assert reloaded_sub.expires_at.tzinfo is None, (
        "test premise: SQLite must round-trip this column as naive for this "
        "test to actually exercise the guard"
    )

    # Must not raise TypeError: can't compare offset-naive and offset-aware datetimes
    decision = deps.evaluate_subscription_access(db, reloaded_user)
    assert decision.granted is True, decision


def test_naive_started_at_in_the_future_still_denies(db):
    """Same naive-round-trip condition, but on started_at, and on the
    denying side — proves the normalization is applied symmetrically, not
    just on the path that happens to grant."""
    user = make_active_starter_user(db, "naive-future@example.com")
    sub = subscriptions_repo.get_latest_for_user(db, user.id)
    sub.status = "active"
    sub.started_at = datetime.now(timezone.utc) + timedelta(days=5)
    sub.expires_at = None
    db.commit()
    db.expire_all()

    reloaded_sub = subscriptions_repo.get_latest_for_user(db, user.id)
    assert reloaded_sub.started_at.tzinfo is None

    decision = deps.evaluate_subscription_access(db, user)
    assert decision.granted is False
    assert decision.reason == "not_yet_started"


# ---------------------------------------------------------------------------
# Group D — ambiguous/incoherent data: refuse, never invent a right.
# ---------------------------------------------------------------------------

def test_ambiguous_expires_before_started_is_refused_with_distinct_code(db):
    user = make_active_starter_user(db, "ambiguous-reversed@example.com")
    now = datetime.now(timezone.utc)
    _set_subscription(db, user, status="active", started_at=now, expires_at=now - timedelta(days=1))

    decision = deps.evaluate_subscription_access(db, user)
    assert decision.granted is False
    assert decision.reason == "ambiguous_dates"


def test_ambiguous_expires_equal_started_is_refused(db):
    """A zero-length window (ends at exactly the instant it starts) is just
    as incoherent as a reversed one — treated the same way."""
    user = make_active_starter_user(db, "ambiguous-equal@example.com")
    now = datetime.now(timezone.utc)
    _set_subscription(db, user, status="active", started_at=now, expires_at=now)

    decision = deps.evaluate_subscription_access(db, user)
    assert decision.granted is False
    assert decision.reason == "ambiguous_dates"


# ---------------------------------------------------------------------------
# Group E — a valid session cookie for a user whose subscription expires
# AFTER the cookie was issued is refused on the very next request.
# ---------------------------------------------------------------------------

def test_session_cookie_reflects_lapse_on_next_request_no_caching(client, db):
    user = make_active_starter_user(db, "lapse-midsession@example.com")
    _login(client, "lapse-midsession@example.com")

    # Access granted before the lapse.
    r_before = client.get("/api/examples")
    assert r_before.status_code == 200, r_before.text

    # Subscription expires — same session cookie, no re-login.
    _set_subscription(
        db, user, status="active",
        started_at=datetime.now(timezone.utc) - timedelta(days=30),
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )

    r_after = client.get("/api/examples")
    assert r_after.status_code == 403, r_after.text

    page_after = client.get("/app/analyser", follow_redirects=False)
    assert page_after.status_code == 303
    assert page_after.headers["location"] == "/account/pending"


# ---------------------------------------------------------------------------
# Group F — job-executor claim-time re-check: a job queued while the
# subscription was active, then cancelled/expired BEFORE a worker claims it,
# must never reach the pipeline (zero LLM construction) and must end up in a
# clear failed state.
# ---------------------------------------------------------------------------

def test_claim_time_check_refuses_a_job_whose_subscription_lapsed_after_submission(db, monkeypatch):
    user = make_active_starter_user(db, "lapse-before-claim@example.com")
    org_id = default_org_id(db, user)

    # Disable the real worker pool so this test drives _process() by hand,
    # same idiom as tests/test_b12_t1_job_executor.py's saturation test.
    monkeypatch.setattr(job_executor, "_QUEUE", queue.Queue(maxsize=10))
    monkeypatch.setattr(job_executor, "_workers_started", True)

    job = jobs.create_job(source_label="will-lapse", user_id=user.id, organization_id=org_id)
    job_executor.submit(job, VALID_AO_TEXT)  # subscription is active at submission time

    # Subscription lapses AFTER submission, BEFORE a worker claims it.
    _set_subscription(db, user, status="cancelled")

    class _ExplodingClaudeClient:
        """Same idiom as B12-T1/B19-T2's own regeneration tests: a
        ClaudeClient-shaped fake that raises the moment it is constructed,
        proving the pipeline never even got as far as building an LLM
        client — no LLM call, no scoring, no document generation."""

        def __init__(self, *a, **k):
            raise RuntimeError("must never construct an LLM client for a job whose subscription lapsed before claim")

    monkeypatch.setattr("src.agents.llm_client.ClaudeClient", _ExplodingClaudeClient)

    item = job_executor._QUEUE.get_nowait()
    job_executor._process(item)

    assert job.status == "error", job.error
    assert job.error_code == job_executor.SUBSCRIPTION_NO_LONGER_ACTIVE_ERROR_CODE
    assert job.result is None

    db.expire_all()
    row = analysis_jobs_repo.get_by_id(db, job.id)
    assert row is not None
    assert row.status == "error"
    assert row.error_code == job_executor.SUBSCRIPTION_NO_LONGER_ACTIVE_ERROR_CODE


def test_claim_time_check_does_not_touch_an_already_done_job(db, monkeypatch):
    """The new check only applies to a job about to START running — it must
    never re-evaluate, fail, or otherwise touch a job that already reached a
    terminal state. Simulated directly against _finalize/mark_done's own
    idempotency (a conditional UPDATE excluding done/error/interrupted) since
    _process() itself only ever calls the new check on the not-yet-claimed
    path."""
    user = make_active_starter_user(db, "already-done@example.com")
    org_id = default_org_id(db, user)
    analysis_jobs_repo.create_queued(db, job_id="already-done-job-01", user_id=user.id, organization_id=org_id, source_label="x")
    db.commit()
    assert analysis_jobs_repo.try_claim(db, job_id="already-done-job-01", worker_instance_id="w1")
    db.commit()
    assert analysis_jobs_repo.mark_done(db, job_id="already-done-job-01", analysis_id=None)
    db.commit()

    # Subscription lapses AFTER the job already finished.
    _set_subscription(db, user, status="cancelled")

    # mark_error (what the claim-time check would call) must be a no-op
    # against an already-terminal row — proving a lapsed subscription can
    # never retroactively flip an already-done job to an error state.
    changed = analysis_jobs_repo.mark_error(db, job_id="already-done-job-01", error_code="should_not_apply")
    assert changed is False

    db.expire_all()
    row = analysis_jobs_repo.get_by_id(db, "already-done-job-01")
    assert row.status == "done"
    assert row.error_code is None


# ---------------------------------------------------------------------------
# Group G — regression: third-party access and ownership checks are
# unaffected by this change (a correctly-subscribed user still cannot read
# someone else's analysis; subscription is additive, never a substitute for
# membership/ownership).
# ---------------------------------------------------------------------------

def test_third_party_access_still_refused_regardless_of_own_subscription_state(client, db):
    owner = make_active_starter_user(db, "owner-b20@example.com")
    owner_org = default_org_id(db, owner)
    intruder = make_active_starter_user(db, "intruder-b20@example.com")  # correctly subscribed

    analysis_jobs_repo.create_queued(db, job_id="owned-by-owner-01", user_id=owner.id, organization_id=owner_org, source_label="x")
    db.commit()

    assert analysis_jobs_repo.get_by_id_for_user(db, "owned-by-owner-01", owner.id) is not None
    assert analysis_jobs_repo.get_by_id_for_user(db, "owned-by-owner-01", intruder.id) is None

    _login(client, "intruder-b20@example.com")
    r = client.get("/api/analyze/owned-by-owner-01/status")
    assert r.status_code == 404
