# B21-T1 — Password reset hardening contract

Scope: `src/web/routes_account.py` (reset-password flow), `src/web/auth/session_cookie.py`,
`src/web/services/email_service.py`, `src/web/database/models.py`,
`src/web/database/repositories/password_reset_tokens.py`,
`migrations/versions/0008_b21t1_password_reset_hardening.py`,
`tests/test_b21_t1_password_reset.py`.

## 1. Defects fixed

1. **Argon2 full-scan on every reset attempt.** `reset_password_submit` used
   to `SELECT` every live, unexpired `PasswordResetToken` and run an Argon2
   `verify()` against each in a Python loop. Argon2 is deliberately slow
   (the whole point for `User.password_hash`) — this scaled O(n) Argon2
   verifications per attempt across the WHOLE token table.
2. **No session revocation mechanism existed.** The session cookie was a
   bare signed `user_id` with no server-side way to invalidate an
   already-issued cookie. A password reset changed only
   `user.password_hash`; any other already-issued, still validly-signed
   cookie for that user (second device, stolen cookie) stayed valid until
   its own `max_age` expired.

## 2. Fast token lookup (replaces the Argon2 loop)

- New column `password_reset_tokens.token_digest` (String(64), unique,
  indexed). Digest = `hmac.new(config.SESSION_SECRET.encode(), raw_token.encode(), hashlib.sha256).hexdigest()`
  (see `src/web/database/repositories/password_reset_tokens.py::compute_digest`).
  Chosen over plain `sha256(raw_token)` as defense-in-depth: a stolen DB
  dump alone (without `SESSION_SECRET`) can't be used to test candidate
  tokens against a precomputed table. This is secondary — the raw token's
  own 256 bits of entropy (`secrets.token_urlsafe(32)`) is what actually
  makes it unguessable.
- Verification is a direct `WHERE token_digest = :digest` lookup — no
  loop, no Argon2 anywhere in this path. `User.password_hash` is
  completely untouched; Argon2 (`src/web/auth/service.py`) stays exactly
  where it was, for passwords only. Confirmed by grep: `token_hash` is
  never read anywhere for verification (only declared, nullable, and
  explicitly set to `None` on new writes).
- `password_reset_tokens.token_hash` (the old Argon2 hash column) is kept
  but made nullable — additive/non-destructive rather than dropped.

## 3. Atomic single-use consumption

`password_reset_tokens_repo.try_consume(db, raw_token=..., now=...)`:
a single conditional `UPDATE password_reset_tokens SET used_at = :now
WHERE token_digest = :digest AND used_at IS NULL AND expires_at > :now`,
`rowcount == 1` is the sole "I won" signal — same idiom as
`analysis_jobs_repo.try_claim`. No preceding `SELECT` decides the outcome.
Proven by `tests/test_b21_t1_password_reset.py::
test_concurrent_consumption_exactly_one_winner` (two real threads, two
separate `session_scope()` sessions, `threading.Barrier`).

## 4. Session revocation — `User.session_version`

- New column `users.session_version` (Integer, NOT NULL, default 0).
- **Cookie shape** (`src/web/auth/session_cookie.py`): the itsdangerous
  payload is now `{"uid": str(user_id), "v": int(session_version)}`
  (previously a bare `str(user_id)`).
  - `set_user_session(response, user_id, session_version: int = 0)` —
    signature changed, `session_version` added (default 0 for any caller
    not yet updated).
  - `get_session_identity(request) -> tuple[str, int] | None` — new
    function, returns `(user_id_str, session_version)` or `None`.
  - `get_session_user_id(request) -> str | None` — kept for back-compat,
    now implemented via `get_session_identity` (ignores the version).
- **Comparison logic** (`src/web/auth/dependencies.py::get_current_user`):
  reads `get_session_identity`, then rejects (returns `None`, exactly like
  "no session") if `user.session_version != session_version` from the
  cookie.
- **Bump on reset**: `reset_password_submit` sets
  `user.session_version = (user.session_version or 0) + 1` in the SAME
  transaction as the password change and the token consumption/
  invalidation — one commit, all-or-nothing.
- **Pre-migration cookie compatibility (explicit decision):** a cookie
  minted before this shipped was a bare string payload, not a dict. The
  new `get_session_identity` treats any payload that isn't a dict with
  both `"uid"` and `"v"` as **invalid** (returns `None`) — it is NOT
  treated as `session_version == 0`. Consequence: every user with an
  old-shaped cookie must log in again once this ships. This is the
  deliberate, safe choice (fail closed), not an oversight.
- Proven by `tests/test_b21_t1_password_reset.py::
  test_old_cookie_rejected_after_reset`: log in for a real cookie, reset
  that same account's password through a separate request while the old
  cookie stays in the client's jar, then confirm the old cookie is
  refused (redirected to `/login`) on a protected route.

## 5. Sibling token invalidation

After a successful consumption, `invalidate_other_tokens_for_user(db,
user_id=..., exclude_id=consumed_token.id, now=now)` bulk-updates every
OTHER still-unused token for the same user to `used_at = now`, in the
same transaction/commit as the password change and session bump.

## 6. Rate limiting

Reuses Agent A/B14-T1's `src/web/security/rate_limit.py::check_and_record`
(real, landed contract — not the placeholder originally assumed):

```python
from src.web.security.rate_limit import RateLimitExceeded, check_and_record

check_and_record(
    "reset_request", client_ip,
    max_attempts=config.RATE_LIMIT_RESET_REQUEST_MAX_ATTEMPTS,   # 5
    window_seconds=config.RATE_LIMIT_RESET_REQUEST_WINDOW_SECONDS,  # 3600
)
check_and_record(
    "reset_consume", client_ip,
    max_attempts=config.RATE_LIMIT_RESET_CONSUME_MAX_ATTEMPTS,   # 10
    window_seconds=config.RATE_LIMIT_RESET_CONSUME_WINDOW_SECONDS,  # 3600
)
```

Applied in both `forgot_password_submit` and `reset_password_submit`.
Keyed **only** by requester IP (`request.client.host`) — never by the
target email — so the limiter's own behavior can never reveal whether an
email exists. Proven by `test_reset_request_rate_limit_identical_
whether_or_not_email_exists` (an existing and a non-existent email, each
on its own dedicated IP, both blocked after exactly
`config.RATE_LIMIT_RESET_REQUEST_MAX_ATTEMPTS` attempts, same message).

## 7. Trusted origin for the reset link

The reset URL is built exclusively from `config.BASE_URL`
(`f"{config.BASE_URL.rstrip('/')}/reset-password?token={raw_token}"`),
never from the incoming request's `Host` header — closes the
host-header-poisoning-into-reset-link attack.

## 8. Email adapter interface

`src/web/services/email_service.py::send_password_reset_email(to_email: str, reset_url: str) -> bool`
— same convention as the existing `send_admin_notification`: returns
`True` only on an actual SMTP send, `False` if skipped (SMTP not
configured) or failed, **never raises**. Never logs `to_email` or
`reset_url` (which embeds the raw token) — only that an attempt
succeeded/failed. Explicitly documented as NOT production-ready email
delivery (no retries, no bounce handling, no deliverability tuning).

`forgot_password_submit` wraps the call in a belt-and-suspenders
`try/except Exception` too, so even an adapter that violates its own
never-raise contract can't change the HTTP response or leak the token.
Proven by `test_email_adapter_failure_stays_generic_and_never_leaks_the_token`:
simulated adapter raises, response stays the generic "submitted" shape,
the raw token is confirmed absent from both the response body and every
captured log record.

## 9. Migration 0008 — column list

File: `migrations/versions/0008_b21t1_password_reset_hardening.py`
(`down_revision = "0007"`).

- `users.session_version` — `Integer`, `NOT NULL`, `server_default='0'`
  (backfills every existing row to 0).
- `password_reset_tokens.token_hash` — altered to `nullable=True` (was
  `NOT NULL`), via `batch_alter_table` (SQLite-portable).
- `password_reset_tokens.token_digest` — new `String(64)`, `nullable=True`
  at the DB level (pre-migration rows have none), unique index
  `uq_password_reset_tokens_token_digest`.

Tested against both a fresh empty SQLite db and one seeded with a raw
(pre-0008-shaped) user + reset-token row inserted via raw SQL matching the
schema as it existed at revision 0007 — see
`test_migration_0008_applies_on_fresh_and_populated_sqlite`. The seed
deliberately does NOT go through the current ORM models for the
pre-migration row (those models already declare the new columns, which
would fail an insert against a database still at 0007).

## 10. Exact changes needed in files this ticket does not own

### `src/web/auth/dependencies.py` (Agent B also edits this file this lot)

Already applied directly (this file was edited in place, not a
worktree — Agent B's own subscription-access changes, e.g.
`SubscriptionAccessDecision`/`evaluate_subscription_access`, were already
on disk when this edit landed, and this diff applied cleanly alongside
them with no conflict). For the coordinator's record, the diff is exactly:

```diff
-from src.web.auth.session_cookie import get_session_user_id
+from src.web.auth.session_cookie import get_session_identity

 def get_current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
-    """Best-effort lookup — returns None for anonymous or invalid sessions."""
-    raw_user_id = get_session_user_id(request)
-    if not raw_user_id:
-        return None
-    try:
-        user_id = uuid.UUID(raw_user_id)
-    except ValueError:
-        return None
-    return users_repo.get_by_id(db, user_id)
+    """Best-effort lookup — returns None for anonymous, invalid, or revoked
+    (stale session_version — B21-T1, e.g. after a password reset) sessions."""
+    identity = get_session_identity(request)
+    if identity is None:
+        return None
+    raw_user_id, session_version = identity
+    try:
+        user_id = uuid.UUID(raw_user_id)
+    except ValueError:
+        return None
+    user = users_repo.get_by_id(db, user_id)
+    if user is None or user.session_version != session_version:
+        return None
+    return user
```

Nothing else in that file was touched — `require_authenticated_user`,
`SubscriptionAccessDecision`, `evaluate_subscription_access`,
`user_has_active_starter_subscription`, `require_active_starter_user`,
`resolve_app_access` (all Agent B's) are untouched by this diff.

### `src/web/routes_auth.py` (Agent A's file this lot)

Two call sites (currently lines 108 and 194 — both post-login/
post-register redirects) need `user.session_version` added as the third
positional argument, since `set_user_session`'s signature changed:

```diff
-    set_user_session(resp, user.id)
+    set_user_session(resp, user.id, user.session_version)
```

Applied verbatim at both call sites. `set_user_session`'s new third
parameter defaults to `0` so this is not a hard breakage if missed, but a
cookie minted without it would be forever pinned to `session_version=0`
and could outlive that account's later revocations — this change is
required for the revocation guarantee to actually hold for freshly
logged-in sessions.

## 11. Verification performed

- `python -m pytest tests/test_b21_t1_password_reset.py -v` — 8/8 passed,
  run 4 times total (including 3 back-to-back reruns) with no flakiness,
  concurrency test included.
- `grep -rn "verify_password\|hash_password\|PasswordHasher" src/web/routes_account.py` →
  only `auth_service.hash_password(password)` for the NEW password
  (Argon2, correct and expected) — no Argon2 call anywhere in the
  token-matching path.
- `grep -rn "token_hash" src/` → only declared (nullable) and set to
  `None` on write; never read for comparison/verification anywhere.
