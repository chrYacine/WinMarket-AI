# B20-T1 — Centralized subscription-access decision

## Defect this replaces

`src/web/auth/dependencies.py::user_has_active_starter_subscription` checked
**only** `subscription.status == "active"` and never looked at
`started_at`/`expires_at` at all. Today no writer in the codebase sets
`expires_at` (`subscriptions_repo.activate()` only sets `started_at`), so
nothing currently expires through the normal flow — but the check must still
defensively handle an `expires_at` in the past (a future billing feature, an
admin script, or seeded data) and a `started_at` in the future (a
subscription scheduled to start later), since ignoring either is exactly the
confirmed gap.

## The decision function

```python
@dataclass(frozen=True)
class SubscriptionAccessDecision:
    granted: bool
    reason: str | None  # None iff granted

def evaluate_subscription_access(db: Session, user: User | None) -> SubscriptionAccessDecision: ...

def user_has_active_starter_subscription(db: Session, user: User | None) -> bool:
    return evaluate_subscription_access(db, user).granted
```

`user_has_active_starter_subscription` keeps its exact name and signature —
**zero edits needed in any caller** (`require_active_starter_user`,
`resolve_app_access`, `main.py`'s template-context middleware if any, or any
other existing call site).

### Reason codes (checked in this order; first match wins)

| Order | Condition | Reason code |
|---|---|---|
| 1 | `user is None` | `no_user` |
| 2 | `user.status != "active"` | `user_not_active` |
| 3 | no subscription row for the user | `no_subscription` |
| 4 | `subscription.plan != "starter"` | `wrong_plan` |
| 5 | `subscription.status != "active"` | `not_active_status` |
| 6 | both `started_at`/`expires_at` set and `expires_at <= started_at` | `ambiguous_dates` |
| 7 | `started_at` set and `now < started_at` | `not_yet_started` |
| 8 | `expires_at` set and `now >= expires_at` | `expired` |
| — | none of the above | granted, reason `None` |

`get_latest_for_user` (orders by `created_at DESC LIMIT 1`) is unchanged —
confirmed this is the right scope: `Subscription` rows are never updated in
place to represent a NEW plan/period (the only writers are `create_subscription`
+ `activate`, both operating on one row), so "most recently created" and "the
one that matters" coincide in this schema. No scenario requiring "consider an
older-but-currently-valid row" was found.

## UTC boundary convention (chosen and pinned)

The access window is **half-open**: `[started_at, expires_at)`.

- `now >= started_at` **grants** — a subscription "starting now" is already
  usable, not usable "one tick later".
- `now < expires_at` **grants**; `now == expires_at` **denies** — at the
  exact expiry instant the subscription is already expired, not "valid for
  one more tick".

This is symmetric: the boundary instant always belongs to the state that is
**starting**, never to the state that is **ending**. Both fields are
nullable and a null value never itself denies access (no invented trial/grace
value is substituted for a missing date).

Pinned by tests in `tests/test_b20_t1_subscription_access.py`:
- `test_boundary_started_at_exact_instant_is_granted`
- `test_boundary_expires_at_exact_instant_is_denied`
- `test_boundary_one_microsecond_before_expiry_is_still_granted`

## Naive-vs-aware datetime handling (real defect, confirmed against this project's own SQLite test DB)

`Subscription.started_at`/`expires_at` are `DateTime(timezone=True)`. Every
writer uses `datetime.now(timezone.utc)`. **Confirmed by direct
reproduction**: on SQLite (this project's local/test database), a value
written as UTC-aware round-trips as **naive** (`tzinfo is None`) on read —
SQLite has no native tz-aware datetime storage. Comparing that naive value
directly against `datetime.now(timezone.utc)` raises `TypeError: can't
compare offset-naive and offset-aware datetimes`. On PostgreSQL (production)
the same column stays aware, so this bug would otherwise surface
unpredictably and only in production.

Fix: a small `_as_utc()` normalizer is applied to both `started_at` and
`expires_at` before any comparison — a naive value is assumed to already be
UTC (true given the writer convention above) and given `tzinfo=utc`; an aware
value is converted (`.astimezone(timezone.utc)`), never blindly reinterpreted.

Covered by:
- `test_naive_datetime_from_db_round_trip_does_not_crash` (asserts the
  round-tripped value really is naive — proving the test exercises the real
  condition, not a hypothetical — then asserts no crash and correct grant)
- `test_naive_started_at_in_the_future_still_denies` (same condition, denying side)

## Ambiguous/incoherent data — refuse, never invent a right

If both `started_at` and `expires_at` are set and `expires_at <= started_at`
(reversed, or a degenerate zero-length window), this is treated as the one
case the schema/data genuinely doesn't decide, and access is **refused**
with `reason="ambiguous_dates"` — never granted by falling through to
whatever the individual `not_yet_started`/`expired` checks would otherwise
say. No other combination was found to be ambiguous: one date present and
the other absent is fully decidable by the ordinary per-field checks.

No new subscription status, no trial, no grace period was added anywhere in
this change — the ticket's constraint against inventing any of these is
respected.

## Job-executor claim-time check

`src/web/job_executor.py::_process` now re-checks subscription access with a
**fresh** `session_scope()` read, immediately after `try_claim` succeeds and
strictly before `jobs._run_analysis` is called:

```python
if not claimed:
    ...
    return

if not _user_subscription_still_active(job.user_id):
    jobs._fail(job, message=_SUBSCRIPTION_NO_LONGER_ACTIVE_MESSAGE,
               error_code=SUBSCRIPTION_NO_LONGER_ACTIVE_ERROR_CODE)
    _finalize(job)
    return

stop_heartbeat = threading.Event()
...
```

`_user_subscription_still_active(user_id)` opens its own `session_scope()`,
loads the user fresh, and calls `evaluate_subscription_access` — never a
cached/stale value from submission time. It fails **closed**: any exception
while checking (DB hiccup, missing user) is treated as "not active", never as
"granted" — the same "never invent a right" principle applied one level up.

The new error code (`subscription_no_longer_active`) is defined **in
`job_executor.py`** (which this lot owns), not in `jobs.py` (owned elsewhere
this round) — `jobs._fail`'s `error_code` parameter is a plain string with no
requirement to live in `jobs.py`'s own namespace; confirmed by reading
`_fail`'s signature (`error_code: Optional[str]`) before adding this.

This only runs on the **not-yet-claimed → about-to-run** transition. It never
re-evaluates, fails, or touches a job that already reached `done`/`error`
(`_mark_terminal`'s own conditional UPDATE — `status.notin_(("done", "error",
"interrupted"))` — makes a later `mark_error` call against an already-`done`
row a structural no-op regardless; see
`test_claim_time_check_does_not_touch_an_already_done_job`). No already-
computed `analyses` row, generated document, or historical data is ever
touched by this check.

Test: `test_claim_time_check_refuses_a_job_whose_subscription_lapsed_after_submission`
submits a job while the subscription is active, cancels the subscription,
then drives `_process()` directly with a `ClaudeClient` stand-in that raises
if ever constructed — proving zero LLM calls, zero scoring, zero document
generation — and asserts the job and its durable row both end up `error` /
`subscription_no_longer_active`.

## Applies identically to pages, API, and the job-claim path

`require_active_starter_user` (every `/api/*` route) and `resolve_app_access`
(every `/app/*` page route) both already call
`user_has_active_starter_subscription` — unchanged, so fixing the shared
function covers both automatically. Verified with
`test_scenarios_page_and_api_agree` (parametrized across every scenario:
active/valid, expired via correct status, expired via stale `active` status +
past `expires_at`, future-start, `pending`, `cancelled`), asserting `/api/examples`
and `/app/analyser` reach the identical granted/denied verdict for every one.

`test_session_cookie_reflects_lapse_on_next_request_no_caching` proves a
single, already-issued session cookie sees the lapse on the very next
request (grant → verify 200 → expire the subscription mid-session → verify
403/redirect), with no re-login and no session-level caching involved.

## Auth-adjacent routes — verified, not assumed

Read `routes_auth.py` and `routes_account.py` directly. None of `/login`
(GET/POST), `/register` (GET/POST), `/logout`, `/account/pending`,
`/forgot-password` (GET/POST), or `/reset-password` (GET/POST) depend on
`require_active_starter_user` or call `resolve_app_access` — they only take
`Depends(get_db)` (plus form fields). **No bug found here**; these flows are
correctly unblocked by subscription state today. (`/account` itself does call
`get_current_user`/status checks inline but not the subscription gate — out
of scope to re-verify further since it isn't in the ticket's named list and I
don't own `routes_account.py`.)

## Never creates a job or calls an LLM on refusal — confirmed, not assumed

`POST /api/analyze` depends on `require_permission("analysis:create")` →
`get_access_context` → `Depends(require_active_starter_user)`
(`src/web/auth/access_context.py:88-89`). FastAPI resolves dependencies
before the route body runs, so a subscription refusal raises `HTTPException`
inside `require_active_starter_user` **before** `get_access_context` even
reaches its own body, and therefore long before the route body's
`jobs.create_job(...)` call on line 112 of `routes_api.py`. Confirmed by
reading the dependency chain directly (not re-tested — this is existing,
unmodified wiring; group A/E's HTTP-level tests exercise the same dependency
and would fail if it stopped raising before the body ran).

## Download / historical-access — open question for the coordinator, NOT fixed here

Read `routes_api.py` and `routes_pages.py` directly:

- `GET /api/download/{job_id}/{kind}` depends on `require_active_starter_user`
  (`routes_api.py:154-158`).
- `GET /app/resultats/{job_id}` calls `resolve_app_access`
  (`routes_pages.py:80-82`).

**Both are gated by the live subscription check**, same as every other route.
This means: a user whose subscription later lapses is blocked from
re-downloading or re-viewing an analysis they already paid to generate —
already-existing behavior, not introduced by this change (the gate was
already there; this ticket only fixed WHAT it correctly detects, not WHERE
it's applied). I did not touch `routes_api.py`/`routes_pages.py` (out of
ownership this lot) and did not change this behavior. Flagging precisely per
the ticket's instruction, since the ticket's own data-preservation principle
("ne pas modifier sans décision la politique de consultation... des données
historiques") reads as in tension with this: the underlying `analyses`
row/documents are never deleted or hidden in storage, but they become
unreachable through these two routes once the gate denies. This is a product
decision (loosen these two routes to a lighter "was ever a customer" check,
vs. leave as-is) that the coordinator should make explicitly — I have not
guessed at it.

## Constraints respected

- No new `Subscription.status` value added (schema's
  `ck_subscriptions_status` — `pending/active/cancelled/expired` — is
  unchanged; no `'suspended'` value exists in this schema and none was
  added). No migration, no `models.py` edit.
- No billing/payment logic, no trial, no grace period invented anywhere.
- Owner/membership checks (`AccessContext`, `require_active_membership`) are
  untouched — regression-tested via `test_third_party_access_still_refused_regardless_of_own_subscription_state`
  and the existing `test_b12_t1_job_executor.py`/`test_b11_t1_persistence.py`
  passes above.

## Exact diff for the coordinator's manual merge

`src/web/auth/dependencies.py` — as of this report the file **already
contains both this change and Agent C's own `get_current_user`/session-
version edit**, merged cleanly with no conflict (confirmed: the full test
suite listed below passes against the live file). If a future merge ever
needs to redo this by hand, the shape is:

1. Add imports: `from dataclasses import dataclass` and
   `from datetime import datetime, timezone` (alongside the existing
   `import uuid`).
2. Insert `SubscriptionAccessDecision`, `_as_utc()`, and
   `evaluate_subscription_access()` as new top-level definitions, placed
   just before the existing `user_has_active_starter_subscription`.
3. Replace `user_has_active_starter_subscription`'s body (the 4 lines
   currently doing the `user is None or user.status != "active"` /
   `get_latest_for_user` / `bool(...)` check) with a single line:
   `return evaluate_subscription_access(db, user).granted`.
4. **Do not touch** `get_current_user`, `require_authenticated_user`,
   `require_active_starter_user`, or `resolve_app_access` — all four are
   unmodified by this change and call the same function names as before.

`src/web/job_executor.py`:

1. Add imports: `from src.web.auth.dependencies import evaluate_subscription_access`
   and `from src.web.database.repositories import users as users_repo`.
2. Add module-level constants `SUBSCRIPTION_NO_LONGER_ACTIVE_ERROR_CODE` and
   `_SUBSCRIPTION_NO_LONGER_ACTIVE_MESSAGE` near the top (after the logger).
3. In `_process()`, insert a new block between the existing
   `if not claimed: ... return` and `stop_heartbeat = threading.Event()`
   that calls `_user_subscription_still_active(job.user_id)` and, on
   `False`, calls `jobs._fail(...)` + `_finalize(job)` + `return`.
4. Add the new `_user_subscription_still_active()` helper function (placed
   just before `_heartbeat_loop`).

## Test command and results

```
python -m pytest tests/test_b20_t1_subscription_access.py -q
# 26 passed

python -m pytest tests/test_saas_auth.py tests/test_b12_t1_job_executor.py tests/test_b11_t1_persistence.py -q
# 32 passed, 1 failed:
#   FAILED tests/test_b12_t1_job_executor.py::test_migration_0007_applies_on_fresh_and_populated_sqlite
#   AssertionError: assert '0008' == '0007'
```

That one failure is **pre-existing and unrelated to this change**: it
hardcodes `alembic upgrade head` landing on version `"0007"`, and Agent C's
`migrations/versions/0008_b21t1_password_reset_hardening.py` (outside this
lot's ownership) made `head` become `"0008"`. Nothing in this diff touches
migrations or that test file.
