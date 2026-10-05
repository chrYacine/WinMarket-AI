# B14-T1 — CSRF verification + generalized rate limiting

Agent A ("B14-T1"). This document is the contract Agent C (B21-T1, password
reset) and the coordinator need to read — Agent C imports the rate-limiting
primitive directly and cannot wait for this agent to finish; the exact
signature below is what it calls.

## 1. Rate-limiting primitive (`src/web/security/rate_limit.py`)

```python
from src.web.security.rate_limit import RateLimitExceeded, check_and_record, reset

try:
    check_and_record(
        action,           # str — e.g. "login", "register", "reset_request", "reset_consume"
        key,               # str — caller-computed identity (see "Key conventions" below)
        max_attempts=...,  # int — read from src/core/config.py, NOT hardcoded by the caller
        window_seconds=..., # int — read from src/core/config.py
    )
except RateLimitExceeded as exc:
    # exc.retry_after_seconds: int, always >= 1 — seconds until this
    # (action, key) has room again.
    raise HTTPException(429, "...", headers={"Retry-After": str(exc.retry_after_seconds)})

# ... perform the actual action ...

reset(action, key)  # optional — e.g. call after a successful login/reset-consume
```

- `check_and_record` both checks AND records the attempt atomically (single
  lock-protected operation) — a call made while already at the limit raises
  `RateLimitExceeded` **without** incrementing the counter again (so a
  blocked caller retrying quickly doesn't keep pushing its own window
  further into the future).
- `RateLimitExceeded` is a **plain `Exception`**, not an `HTTPException`
  subclass — see the "413/429 precedent" section below for why, and why you
  should catch it immediately in your own route and raise your own
  `HTTPException(429, ..., headers={"Retry-After": str(exc.retry_after_seconds)})`
  rather than letting it propagate.
- `reset(action, key)` clears the counter — call it on success if repeated
  legitimate attempts shouldn't count against the limit (login does this;
  register does not).
- Time is read through a module-level `_clock: Callable[[], float]`
  (defaults to `time.time`) — tests do
  `monkeypatch.setattr(rate_limit, "_clock", lambda: fixed_value)` to
  deterministically cross a window boundary without a real `time.sleep()`.

### Scope honesty (read before trusting this for anything beyond a single process)

This is an **in-process, `threading.Lock`-protected dict** — atomic within
one Uvicorn process, exactly like the login-only limiter it replaces. It is
**not** cross-process/multi-worker (no Redis, no DB-backed counter). This
repo has no evidence of a multi-worker deployment (no Procfile, no
gunicorn/systemd unit, no `--workers` flag anywhere), so a single-process
limiter is an honest, reasonable choice today — but if this app is ever run
with multiple worker processes, each worker enforces its own independent
limit (the effective ceiling multiplies by worker count). No test in this
lot proves anything beyond sequential, single-process correctness, and none
claims otherwise.

### Constants added to `src/core/config.py` (additive only)

```
RATE_LIMIT_LOGIN_MAX_ATTEMPTS = 10        RATE_LIMIT_LOGIN_WINDOW_SECONDS = 60
RATE_LIMIT_REGISTER_MAX_ATTEMPTS = 5      RATE_LIMIT_REGISTER_WINDOW_SECONDS = 60
RATE_LIMIT_REGENERATE_DOCUMENT_MAX_ATTEMPTS = 20   RATE_LIMIT_REGENERATE_DOCUMENT_WINDOW_SECONDS = 60
RATE_LIMIT_RESET_REQUEST_MAX_ATTEMPTS = 5          RATE_LIMIT_RESET_REQUEST_WINDOW_SECONDS = 3600
RATE_LIMIT_RESET_CONSUME_MAX_ATTEMPTS = 10         RATE_LIMIT_RESET_CONSUME_WINDOW_SECONDS = 3600
```

The last two (`RESET_REQUEST` / `RESET_CONSUME`) are provisioned **for
Agent C** — not read by any route this agent owns. Override any of these via
env var (`RATE_LIMIT_RESET_REQUEST_MAX_ATTEMPTS=...`) the same way every
other config constant in this file works.

### Key conventions (for Agent C to follow)

- **Never trust `X-Forwarded-For`.** Use `request.client.host if request.client else "unknown"` — the same pattern login already used and still uses.
- Anonymous/pre-auth action (e.g. `reset_request`, the "forgot password" form) → key on IP alone: `check_and_record("reset_request", client_host, ...)`.
- An action with a known target identity but still pre-auth (e.g. `reset_consume`, the "set new password" form, which has a token but no verified session) → also keyed by IP by convention (there is no authenticated user id yet, and keying on the raw token itself would mean a new key per attempt, defeating the limiter).
- An authenticated, per-account costly action → key on `str(user.id)`, not IP (more precise, matches what `regenerate_document` in this lot does).

### Where it's used today (this agent's routes)

| Route | Action name | Key | Response on exceed |
|---|---|---|---|
| `POST /login` | `"login"` | `f"{email}:{client_host}"` | **Unchanged** — same 200 + inline "Trop de tentatives..." message as before this ticket (deliberately not changed to 429, to preserve existing UX/tests) |
| `POST /register` | `"register"` | `client_host` | **New** — real `429` + `Retry-After` header, rendered via the app's existing generic `error.html` (same convention as any other HTML-route `HTTPException`) |
| `POST /api/analyze/{job_id}/documents/{kind}/regenerate` | `"regenerate_document"` | `str(current_user.id)` | **New** — real `429` + `Retry-After`, JSON body `{"error_code": "RATE_LIMITED", "message": "..."}` (same JSON-error convention as every other `/api/*` route) |

`/api/analyze` itself is not additionally rate-limited by this ticket — it
already has a structurally different 429 path via
`job_executor.JobQueueSaturatedError` (bounded worker-pool queue
saturation, pre-existing, B12-T1). Adding a second, independent rate limiter
on top was judged unnecessary for "at least one costly action."

## 2. CSRF verification added

Convention chosen for every route below (**one convention, applied
consistently**, per the ticket's own instruction): a `X-CSRF-Token` request
header, read via `request.headers.get("X-CSRF-Token")` and passed to the
existing `require_csrf(request, submitted_token)` — reused byte-for-byte,
signature unchanged. This applies uniformly to JSON-body routes, multipart
upload routes, and even the no-body `POST /analyze/{job_id}/documents/{kind}/regenerate`
route. `require_csrf` is called as the **first line of business logic** in
every route below — before any input validation, repository call, or LLM
call — so a bad/missing token is refused before any side effect.

| File | Route | Notes |
|---|---|---|
| `routes_api.py` | `POST /api/analyze` | header check before `analyze_input_service.resolve_analyze_input` |
| `routes_api.py` | `POST /api/analyze/{job_id}/documents/{kind}/regenerate` | header check + rate limit, both before ownership/kind validation |
| `routes_api.py` | `POST /api/capacity` | header check before `private_capacity_repo.save_for_owner` |
| `routes_api.py` | `POST /api/knowledge/reload` | header check before `private_rag_manager.invalidate` |
| `routes_knowledge_documents.py` | `POST /api/knowledge/documents` (upload) | header check before `documents_service.read_upload_with_limit` |
| `routes_knowledge_documents.py` | `POST /api/knowledge/documents/{id}/versions` | header check before ownership lookup |
| `routes_knowledge_documents.py` | `DELETE /api/knowledge/documents/{id}` | header check before ownership lookup |
| `routes_scoring_policy.py` | `PUT /api/scoring-config/profile` | header check before `provider_profile_repo.save_for_owner` |
| `routes_scoring_policy.py` | `PUT /api/scoring-config/policy` | header check before draft persistence |
| `routes_scoring_policy.py` | `POST /api/scoring-config/policy/validate` | header check added (this route never persists, but the ticket lists it explicitly — cheap defense in depth) |
| `routes_scoring_policy.py` | `POST /api/scoring-config/policy/activate` | header check before validation/activation |
| `routes_scoring_policy.py` | `POST /api/scoring-config/simulate` | header check before input resolution/scoring |

`POST /api/contact` already had its own CSRF check (a `csrf_token` JSON
field, checked before this ticket) — left untouched, different convention,
pre-existing and correct.

**GET-never-mutates audit**: every GET route in `routes_api.py`,
`routes_knowledge_documents.py` and `routes_scoring_policy.py` was read —
none of them perform a write. Two are explicitly tested in
`tests/test_b14_t1_csrf_and_rate_limits.py` (`GET /api/capacity`,
`GET /api/knowledge/documents`) by monkeypatching their file's write
functions to raise if called.

## 3. Client-side (JS) wiring

- `templates/layout.html` now embeds
  `<meta name="csrf-token" content="{{ csrf_token|default('') }}">` in
  `<head>` (this is the base template every page extends, including
  `app_shell.html`) — the token is the exact same one already rendered into
  every server-side form's hidden `csrf_token` field. Empty on pages that
  never receive `csrf_token` in their context (landing, pricing — no
  mutating fetch calls there).
- `static/js/main.js` defines a shared `window.wmCsrfToken()` helper that
  reads that meta tag (the underlying cookie is `httponly`, unreadable from
  JS directly — this is the whole reason the meta tag exists).
- `static/js/analyze.js`: both `fetch("/api/capacity", ...)` and
  `fetch("/api/analyze", ...)` now send `"X-CSRF-Token": window.wmCsrfToken()`.
- `static/js/knowledge.js`: `fetch("/api/knowledge/reload", ...)` now sends
  the same header.

### Frontend follow-up still needed (out of scope for this ticket, flagging honestly)

`routes_knowledge_documents.py`'s upload/add-version/delete routes
(`POST /api/knowledge/documents`, `POST .../{id}/versions`,
`DELETE /api/knowledge/documents/{id}`) are now CSRF- and
permission-protected server-side, but **there is currently no JS in this
codebase that calls them at all** — `templates/app_knowledge.html` /
`static/js/knowledge.js` only wire up "reload" and "search", not
upload/versions/delete (confirmed by reading both files; no upload form
exists in the template today). Whoever builds that UI needs to send the
same `X-CSRF-Token: window.wmCsrfToken()` header on those three calls —
nothing else in the current design needs to change for that.

## 4. The 413/429-through-middleware precedent (why RateLimitExceeded is a plain Exception)

`src/web/body_limit_middleware.py::RequestBodyTooLargeError` documents a
real, previously-fixed bug: an `HTTPException` raised from **inside**
Starlette's `BaseHTTPMiddleware` task-group-based receive relay gets wrapped
into an `ExceptionGroup` by `anyio`, which no longer matches FastAPI's
`except HTTPException: raise`, and silently degrades into a generic 400.

`RateLimitExceeded` avoids this whole class of risk differently:
`check_and_record()` is **always** called synchronously, directly inside a
route function's own call stack (never from the ASGI layer or a
middleware), so it is caught in the very same frame and converted into a
plain `HTTPException(429, ..., headers={"Retry-After": ...})` right there —
it never propagates through any middleware at all. This is the "simpler,
avoids the whole class of risk" option the ticket names explicitly.

### A second, real bug found and fixed while proving the 429 path end-to-end

`main.py`'s generic `@app.exception_handler(HTTPException)` handler
(`html_http_exception_handler`) constructed its `JSONResponse`/
`TemplateResponse` **without** forwarding `exc.headers` — so any header set
on a raised `HTTPException` (concretely: `Retry-After` on the new 429s) was
silently dropped before reaching the client, even though `status_code` and
`detail` came through correctly. This was only caught by actually asserting
on the header in a real `TestClient` call
(`tests/test_b14_t1_csrf_and_rate_limits.py::test_register_rate_limit_429_then_recovers_after_clock_advances`
failed with `KeyError: 'Retry-After'` before this fix) — exactly the kind
of gap a purely unit-level test of `rate_limit.py` alone would never catch.
Fixed by adding `headers=exc.headers` to both branches in `main.py`; a
no-op for every pre-existing caller that never set `.headers` (defaults to
`None`, which `JSONResponse`/`TemplateResponse` both accept fine).

**This is the one shared file (`main.py`) this agent touched outside its
owned file list** — flagging it per the lot's own instructions. The change
is a strict two-line addition (`headers=exc.headers` on each of the two
existing `return` statements) with no other behavior change; it does not
touch the middleware-ordering logic the B13-T2 lot fixed.

## 5. Known, expected fallout: pre-existing tests that never sent a CSRF token

Adding real CSRF verification to `/api/analyze`, `/api/capacity`,
`/api/knowledge/documents*`, `/api/knowledge/reload` and every
`/api/scoring-config/*` mutating route breaks **every pre-existing test that
calls one of those routes without a token** — because none of them ever
sent one (confirmed by exhaustive grep before writing this fix: zero
`csrf_token`/`X-CSRF-Token` usage anywhere these routes are called in the
existing suite). This is the direct, unavoidable, and correct consequence of
closing a real vulnerability — not a regression introduced by accident.

Confirmed by running `tests/test_b06_scoring_config.py` standalone after
this change: **17 of 23 tests fail**, every single failure a `403` from
`require_csrf` (`"Requête invalide (jeton de sécurité manquant ou
expiré)..."`), never a different error. The same pattern affects roughly
20+ other test files across the suite (`test_b02_organizations.py`,
`test_b03_private_knowledge.py`, `test_b04_t2_write_isolation.py`,
`test_b11_t1_persistence.py`, `test_b12_t1_job_executor.py`,
`test_b19_t2_routes_wiring.py`, `test_b19_t2_document_rendering.py`,
`test_certification_scope.py`, `test_context_budget*.py`,
`test_job_error_handling.py`, `test_passage_location*.py`,
`test_qa_gaps_20260913.py`, `test_qa_llm_capture.py`,
`test_rag_producer_validation.py`, `test_reference_identity*.py`,
`test_reference_selection_job_path.py`, `test_ao_extraction_field_resolution.py`,
`test_b02_qa_validation.py` — most via duplicated helper functions named
`_configure_capacity`, `_save_profile`, `_save_draft`, `_activate`,
`_run_analysis_to_completion`/`_run_analysis_to_terminal`, `_upload`, all
independently copy-pasted rather than shared from `conftest.py`).

**This was a deliberate scope decision, not an oversight**: fixing ~25 files
(no single shared helper module to patch once — each duplicates its own
copy) is a large, mechanical, cross-cutting change that risks colliding
with Agents B and C editing the same or adjacent files in parallel, and
touches far more files than this agent's ownership grant covers. Per this
lot's own instructions ("Do NOT run the full suite yourself... the
coordinator runs the full suite once everyone is done"), this is reported
here for the coordinator to route rather than silently patched or — the
alternative that was explicitly forbidden — silently skipped by weakening
the CSRF check itself.

**The fix pattern, for whoever picks this up**, is mechanical and identical
everywhere: each affected test file's login helper should return the CSRF
token from the login page's hidden field (`get_or_create_csrf_token` keeps
it valid for the rest of the session — see
`tests/test_b13_t2_middleware_wiring.py::_login` in this lot for the exact
pattern applied), and every subsequent mutating `client.post/put/delete(...)`
call in that file should add `headers={"X-CSRF-Token": csrf}` (merging with
any existing `headers=` argument, e.g. the raw-JSON test helper in
`test_b06_scoring_config.py::_save_draft_raw_json`).

**Two files were fixed as part of this ticket's own required regression
check** (both pass): `tests/test_b13_t2_middleware_wiring.py` (its `_login`
helper now returns the token; its `/api/capacity` and `/api/analyze` calls
attach it) and `tests/conftest.py` (see next section — unrelated to CSRF,
but also required for this ticket's tests to pass reliably).

## 6. The other shared-file change: `tests/conftest.py` rate-limit reset fixture

`rate_limit.py`'s `_attempts` dict is module-level by design (see the
"Scope honesty" section above). Left unreset between tests, it persists
across **every test function in the whole pytest session**, not just within
one test file — and FastAPI's `TestClient` reports a constant
`request.client.host` (`"testclient"`) for every request from every test in
the suite. An IP-keyed action like `"register"` (`RATE_LIMIT_REGISTER_MAX_ATTEMPTS`
defaults to 5) would accumulate attempts across unrelated test files run in
the same session and eventually make some later, otherwise-correct test's
registration call fail with a bogus 429 purely from test execution
order/count — a real cross-test contamination bug, confirmed by the fact
that more than 5 tests across this suite call `/register`.

Added an autouse fixture to `tests/conftest.py` (same pattern as the
existing B04-T0 autouse isolation fixtures already there):

```python
@pytest.fixture(autouse=True)
def _b14_reset_rate_limits():
    from src.web.security import rate_limit
    rate_limit._attempts.clear()
    yield
    rate_limit._attempts.clear()
```

This is the second (and last) shared file this agent touched outside its
owned file list, flagged here for the same reason as `main.py` above. It is
additive-only and low-risk: it resets private test-infrastructure state,
touches no application behavior, and directly benefits Agent C's own
reset_request/reset_consume rate-limit tests too (same module-level dict,
same contamination risk).

## 7. Test file

`tests/test_b14_t1_csrf_and_rate_limits.py` — 14 tests, all passing:
valid-CSRF-succeeds and missing/mismatched-CSRF-refused-before-write for one
route per touched file (6 tests), "two tabs" token reuse (1), register
429→Retry-After→clock-advance→recovery via real `TestClient` (1), sequential
shared-counter proof via login (1), and two GET-never-mutates checks (2).
