"""Generic in-memory fixed-window rate limiter.

B14-T1: generalized from the previous login-only, hardcoded-threshold
version. A caller now picks an **action name** (a short string identifying
what is being limited — "login", "register", "regenerate_document",
"reset_request", "reset_consume", ...) and a **key** (the identity being
limited — an email+IP composite, a bare IP for an anonymous action, or an
authenticated user's id for a per-account costly action). Each (action, key)
pair gets its own independent fixed window.

Call contract (this is what Agent C / any other caller imports and uses
directly — see docs/api/B14_T1_CSRF_RATE_LIMIT_CONTRACT.md for the full
write-up):

    from src.web.security.rate_limit import RateLimitExceeded, check_and_record, reset

    try:
        check_and_record(
            "reset_request", key,
            max_attempts=config.RATE_LIMIT_RESET_REQUEST_MAX_ATTEMPTS,
            window_seconds=config.RATE_LIMIT_RESET_REQUEST_WINDOW_SECONDS,
        )
    except RateLimitExceeded as exc:
        raise HTTPException(429, "...", headers={"Retry-After": str(exc.retry_after_seconds)})

    # ... perform the action ...

    reset("reset_request", key)  # optional — e.g. call on a successful login

`max_attempts`/`window_seconds` are NOT hardcoded in this module — every
caller passes its own numbers, which should be read from
src/core/config.py's per-action constants (RATE_LIMIT_<ACTION>_MAX_ATTEMPTS /
RATE_LIMIT_<ACTION>_WINDOW_SECONDS) so a threshold can be tuned per action
without touching this file.

Testability ("horloge contrôlée"): time is read through the module-level
`_clock` callable, not `time.time()` inlined into the logic — a test does
`monkeypatch.setattr(rate_limit, "_clock", lambda: fixed_value)` to
deterministically advance past a window boundary without a real
`time.sleep()`.

Scope/honesty note on "compteurs partagés/atomiques" (explicit ticket
requirement — do not claim more than is proven): this is a single
`threading.Lock`-protected in-process dict, exactly like the previous
login-only version it replaces. It is atomic *within one process* — two
threads of the SAME Uvicorn worker contending for the same (action, key)
observe a consistent, correctly-incremented count. It is NOT a
cross-process/multi-worker limiter (no shared external store, no database
transaction, no Redis). As far as this codebase's own deployment
configuration goes (checked: no Procfile, no gunicorn/systemd unit, no
`--workers` flag anywhere in this repo — main.py's own module docstring
documents a single `uvicorn main:app` invocation), this app runs as a single
Uvicorn process today, which is what makes an in-process limiter a
reasonable, honest choice rather than a shortcut. If this application is
ever deployed with multiple worker processes, this module's guarantee
degrades to "each worker enforces its own independent limit" (effectively
multiplying the real ceiling by the worker count) — a real limitation, not
something to paper over. Tests exercising this module prove sequential,
single-process correctness only; they do not and cannot prove
multi-process/concurrent correctness, and no test or docstring in this
codebase claims otherwise.
"""
from __future__ import annotations

import math
import threading
import time
from typing import Callable, Dict, List, Tuple

# Injectable clock — a test monkeypatches this module attribute directly
# (`monkeypatch.setattr(rate_limit, "_clock", lambda: ...)`) rather than
# needing a real time.sleep() to cross a window boundary.
_clock: Callable[[], float] = time.time

_attempts: Dict[Tuple[str, str], List[float]] = {}
_lock = threading.Lock()


class RateLimitExceeded(Exception):
    """Raised by check_and_record() when (action, key) is already at its
    configured limit. Deliberately a PLAIN Exception, not an HTTPException
    subclass — see this lot's precedent in
    src/web/body_limit_middleware.py::RequestBodyTooLargeError (an
    HTTPException raised from deep inside the ASGI/BaseHTTPMiddleware stack
    got silently reclassified from 413 to a generic 400 by an
    anyio-task-group/ExceptionGroup interaction). check_and_record() is
    always called synchronously, directly inside a route function's own
    call stack — never from inside the ASGI layer or a middleware — so a
    caller catches this immediately in the same frame and raises its own
    `HTTPException(429, ..., headers={"Retry-After": ...})` right there.
    This sidesteps that whole class of risk entirely rather than relying on
    RateLimitExceeded itself being exception-hierarchy-safe to propagate
    through framework internals.
    """

    def __init__(self, retry_after_seconds: int):
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"Rate limit exceeded; retry after {retry_after_seconds}s")


def _key(action: str, key: str) -> Tuple[str, str]:
    return (action, key)


def check_and_record(action: str, key: str, *, max_attempts: int, window_seconds: int) -> None:
    """Raises RateLimitExceeded(retry_after_seconds=...) if this (action,
    key) pair has already recorded `max_attempts` attempts within the last
    `window_seconds`; otherwise records this attempt (extending/creating the
    window) and returns None.

    `retry_after_seconds` is computed from the oldest still-in-window
    attempt — the number of whole seconds until that attempt ages out and
    the window has room again — rounded UP (math.ceil) and floored at 1, so
    a caller can always put a truthful, non-zero `Retry-After` header on a
    429 response.
    """
    if max_attempts <= 0:
        raise ValueError("max_attempts must be a positive integer")
    if window_seconds <= 0:
        raise ValueError("window_seconds must be a positive integer")

    now = _clock()
    full_key = _key(action, key)
    with _lock:
        timestamps = [t for t in _attempts.get(full_key, []) if now - t < window_seconds]
        if len(timestamps) >= max_attempts:
            _attempts[full_key] = timestamps
            oldest = min(timestamps)
            retry_after = max(1, math.ceil(window_seconds - (now - oldest)))
            raise RateLimitExceeded(retry_after_seconds=retry_after)
        timestamps.append(now)
        _attempts[full_key] = timestamps


def reset(action: str, key: str) -> None:
    """Clear recorded attempts for (action, key) — e.g. call after a
    successful login so a legitimate user who mistyped a password a few
    times isn't left partway toward the limit."""
    with _lock:
        _attempts.pop(_key(action, key), None)
