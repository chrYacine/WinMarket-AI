"""B13-T2 — raw ASGI-level request-body size guard.

Closes a gap `src/web/analyze_input_service.py::read_upload_bounded` (B13-T1)
cannot: by the time that function runs, FastAPI has ALREADY handed back a
fully-received `UploadFile`. Concretely, `fastapi/routing.py` calls
`await request.form()` with no arguments, so Starlette's `MultiPartParser`
uses its hardcoded defaults (`max_part_size=1024*1024`); that limit only
applies `if self._current_part.file is None` (`starlette/formparsers.py`,
`MultiPartParser.on_part_data`) — i.e. it bounds plain text form FIELDS,
never a part carrying a `filename` (an actual file upload, spooled to disk
unconditionally, no size check at all). There is currently no limit
whatsoever on an uploaded file's size at the multipart-parsing layer: a
500 MiB file would be accepted onto disk in full — a disk-exhaustion DoS —
before any route-level bound ever runs. This module is the fix: it counts
bytes as they actually arrive off the wire and aborts before the multipart
parser (or anything else) can buffer/spool them.

Deliberately a PURE ASGI middleware class (`__call__(self, scope, receive,
send)`), NOT `@app.middleware("http")` / `Starlette.BaseHTTPMiddleware` —
that wraps the request in its own `Request` object and can end up buffering
the whole body itself before user code ever sees it, which would defeat the
entire purpose here. This class instead wraps the `receive` callable passed
to the inner app: every time an `http.request` ASGI message arrives, its
`body` length is added to a running total; the moment that total exceeds
the configured maximum, `RequestBodyTooLargeError` is raised from INSIDE
the wrapped `receive()`, before the message is ever returned to whatever
called it (Starlette's multipart reader, FastAPI's routing, etc.) — the
exception propagates up through all of that and out to the ASGI server.

IMPORTANT registration-order subtlety (found by review, see `main.py`'s own
comment at its `add_middleware(BodySizeLimitMiddleware, ...)` call for the
full trace): this middleware must be registered so it ends up INNER to
(closer to the routes than) `inject_current_user_state`'s
`@app.middleware("http")` — the OPPOSITE of "outermost", despite that
being the intuitive-sounding choice. `BaseHTTPMiddleware` relays the body
through its own internal `anyio.create_task_group()`-based receive proxy;
an exception raised while being awaited from WITHIN that task-group
context gets wrapped into an `ExceptionGroup` on the way out, which no
longer matches FastAPI's `except HTTPException: raise` in
`fastapi/routing.py` and silently degrades into a generic 400. Positioning
this middleware inner to that `BaseHTTPMiddleware` means `RequestBodyTooLargeError`
is raised outside any task-group context, propagating as itself.

No `Content-Length` header, or any other declared/claimed size, is ever
consulted anywhere in this module — only `len(message["body"])` for bytes
that have actually been received counts. A request with an absent, zero, or
understated `Content-Length` is bounded exactly the same way as one with an
honest header; an overstated `Content-Length` never triggers a rejection by
itself if the real body stays under the limit.

Scope: only requests whose path starts with one of `guarded_prefixes` are
wrapped at all — every other route's `receive` is passed through completely
unchanged (`return await self.app(scope, receive, send)`), so this is not a
global body-size policy. `scope["type"] != "http"` (e.g. `websocket`,
`lifespan`) is likewise passed through untouched.

What this closes: unbounded file-size-before-parsing (the disk-exhaustion
vector above). What this does NOT claim to close: CPU/memory exhaustion
from a legitimately-sized-but-pathologically-structured payload handled by
some parser downstream (e.g. a small PDF/DOCX crafted to be expensive to
parse) — that is a different class of attack and out of scope for this
ticket; see `src/web/knowledge/extraction.py::_extract_docx`'s bounded
real-decompression guard for the one such case this lot does address
(DOCX declared-vs-actual decompressed size).
"""
from __future__ import annotations

from typing import Awaitable, Callable, Iterable, Mapping, MutableMapping, Optional

from fastapi import HTTPException


class RequestBodyTooLargeError(HTTPException):
    """Raised from inside the wrapped `receive()` the moment the running
    total of ACTUALLY-received bytes for a guarded request exceeds
    `max_bytes`. Never raised because of a header claim — only real bytes
    counted as they arrive.

    DELIBERATELY an `HTTPException` subclass, not a plain `Exception`
    (coordinator-integration fix, found by review): every guarded route
    (`/api/analyze`, `/api/scoring-config/simulate`,
    `/api/knowledge/documents*`) has a form/file body, and
    `fastapi/routing.py`'s request-handling wraps `await request.form()` in
    `except HTTPException: raise / except Exception as e: raise
    HTTPException(400, "There was an error parsing the body") from e` — a
    plain `Exception` subclass raised from inside the multipart read (as
    this originally was) gets silently RECLASSIFIED into that generic 400
    before it can ever reach a registered
    `@app.exception_handler(RequestBodyTooLargeError)`, no matter how that
    handler is written. Being an `HTTPException` makes FastAPI's own
    `except HTTPException: raise` fire first, so this propagates unchanged
    with the real status/detail set below, and is then handled by the
    application's existing generic `@app.exception_handler(HTTPException)`
    (`main.py::html_http_exception_handler`) via ordinary MRO-based handler
    lookup — no bespoke handler needs registering for this class at all.

    Carries `.max_bytes`/`.received_bytes` for logging in addition to the
    standard `.status_code`/`.detail`.
    """

    def __init__(self, max_bytes: int, received_bytes: int):
        super().__init__(
            status_code=413,
            detail={
                "error_code": "REQUEST_BODY_TOO_LARGE",
                "message": "La taille de la requête dépasse la limite autorisée.",
            },
        )
        self.max_bytes = max_bytes
        self.received_bytes = received_bytes


Scope = MutableMapping[str, object]
Message = MutableMapping[str, object]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]


class BodySizeLimitMiddleware:
    """Pure ASGI middleware — `app(scope, receive, send)`, no framework base
    class. Wraps `receive` for guarded paths only; every other request is
    forwarded completely untouched.
    """

    def __init__(self, app, *, max_bytes: int, guarded_prefixes: Iterable[str], prefix_limits: Optional[Mapping[str, int]] = None):
        if max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        self.app = app
        self.max_bytes = max_bytes
        # Tuple: str.startswith() accepts a tuple of prefixes directly.
        self.guarded_prefixes = tuple(guarded_prefixes)
        # Lot 47 bis: a route family with its own, larger bound (the AO dossier: 100 Mo of
        # files) — ONLY the listed prefixes; every other guarded path keeps `max_bytes`.
        # Longest matching prefix wins. A listed prefix is guarded even if it is not in
        # `guarded_prefixes`.
        self.prefix_limits = {p: int(v) for p, v in (prefix_limits or {}).items()}
        if any(v <= 0 for v in self.prefix_limits.values()):
            raise ValueError("every prefix limit must be a positive integer")

    def _limit_for(self, path: str) -> Optional[int]:
        """The byte bound that applies to `path`, or None when it is not guarded at all."""
        specific = [p for p in self.prefix_limits if path.startswith(p)]
        if specific:
            return self.prefix_limits[max(specific, key=len)]
        return self.max_bytes if path.startswith(self.guarded_prefixes) else None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        max_bytes = self._limit_for(scope.get("path", "") or "") if scope.get("type") == "http" else None
        if max_bytes is None:
            await self.app(scope, receive, send)
            return

        total = 0

        async def guarded_receive() -> Message:
            nonlocal total
            message = await receive()
            # A mid-stream client disconnect is not a size overflow — pass
            # it through unchanged so normal disconnect handling elsewhere
            # in Starlette/FastAPI still runs.
            if message.get("type") != "http.request":
                return message
            total += len(message.get("body") or b"")
            if total > max_bytes:
                raise RequestBodyTooLargeError(max_bytes, total)
            return message

        await self.app(scope, guarded_receive, send)
