# B13-T2 — reception-time bounds (raw body size + real DOCX decompression)

Complements `docs/api/B13_T1_INPUT_VALIDATION_CONTRACT.md`. B13-T1 bounded a
*second* read of an already-fully-received `UploadFile`; this ticket closes
the gap in front of it: nothing previously bounded the file as it was first
received off the wire, and the B13-T1 DOCX zip-bomb guard trusted the
archive's own declared (attacker-controlled) uncompressed size.

## 1. Raw ASGI body-size guard (`src/web/body_limit_middleware.py`)

**Why it's needed**: `fastapi/routing.py` calls `await request.form()` with
no arguments, so Starlette's `MultiPartParser` uses hardcoded defaults
(`max_part_size=1024*1024`). That limit only applies
`if self._current_part.file is None` (`starlette/formparsers.py`,
`on_part_data`) — i.e. it bounds plain text form fields, **never** a part
carrying a `filename`. An uploaded file therefore has no size limit
whatsoever at the multipart-parsing layer today; a file far beyond any
configured limit would be spooled to disk in full — a disk-exhaustion DoS
vector — before any route-level check ever runs.

**Fix**: `BodySizeLimitMiddleware`, a pure ASGI middleware (not
`@app.middleware("http")`/`BaseHTTPMiddleware`, which would buffer the body
itself). It wraps the `receive` callable passed to the inner app; each
`http.request` message's `body` length is added to a running total, and the
moment the total exceeds the configured max, `RequestBodyTooLargeError` is
raised from inside the wrapped `receive()` — before that message is ever
handed to Starlette's multipart reader. `http.disconnect` is passed through
unchanged (never counted as overflow). No header (`Content-Length` or
otherwise) is ever consulted — only bytes actually received count in either
direction (an absent/understated header does not let an oversized body
through; an overstated header does not reject an undersized one).

Scoped by path prefix (constructor `guarded_prefixes`); every other route's
`receive` is passed through completely untouched.

### Config (`src/core/config.py`, additive)

| Constant | Default | Reasoning |
|---|---|---|
| `MAX_REQUEST_BODY_MB` | `32` | The largest legitimate guarded body is one file (`ANALYZE_MAX_UPLOAD_MB` / `KNOWLEDGE_MAX_FILE_SIZE_MB` = 10 MiB) plus small multipart framing overhead plus a few text fields. 32 MiB gives >3x headroom while still capping any one request to a bounded allocation instead of "however much disk is free." `validate_config()` rejects a value below the largest configured single-file bound. |

### Actual wiring in `main.py` (coordinator-integrated; corrected after review)

`RequestBodyTooLargeError` is an `HTTPException` subclass (not a plain
`Exception` — see the class's own docstring), carrying
`status_code=413`/`detail={"error_code": "REQUEST_BODY_TOO_LARGE", ...}`
directly, so it is handled by the app's existing generic
`@app.exception_handler(HTTPException)` (`main.py::html_http_exception_handler`)
via ordinary MRO-based lookup — **no bespoke exception handler is
registered for it**. An earlier version of this doc showed a dedicated
`@app.exception_handler(RequestBodyTooLargeError)` and a plain-`Exception`
base class; both were wrong and have been corrected (see "Integration
defect found and fixed" below).

```python
from src.core import config
from src.web.body_limit_middleware import BodySizeLimitMiddleware

app.add_middleware(
    BodySizeLimitMiddleware,
    max_bytes=config.MAX_REQUEST_BODY_MB * 1024 * 1024,
    guarded_prefixes=("/api/analyze", "/api/scoring-config/simulate", "/api/knowledge/documents"),
)
```

**Registration order is load-bearing and counter-intuitive**: this call is
placed *before* the existing `@app.middleware("http") inject_current_user_state`
definition — i.e. this middleware ends up INNER to (closer to the routes
than) that `BaseHTTPMiddleware`, not outermost. See "Integration defect
found and fixed" below for why.

### Integration defect found and fixed (post-delivery review)

The first integration attempt registered this middleware *after*
`inject_current_user_state` (reasoning: "outermost sees raw bytes first",
which is true but not the relevant property) and kept
`RequestBodyTooLargeError` as a plain `Exception` with a dedicated
`@app.exception_handler`. A real oversized upload through the full app
(not a hand-built ASGI scope) then returned a generic `400 {"detail":
"There was an error parsing the body"}` instead of the documented 413.

Root cause, traced end-to-end: Starlette's `BaseHTTPMiddleware`
(`inject_current_user_state`'s decorator) relays the request body through
its own internal `anyio.create_task_group()`-based receive proxy. Any
exception raised while being awaited *from within* that task-group context
is wrapped by `anyio` into an `ExceptionGroup` on the way out — no longer
`isinstance(exc, HTTPException)`, so `fastapi/routing.py`'s
`except HTTPException: raise` no longer matches it and it falls through to
the generic `except Exception: raise HTTPException(400, "There was an
error parsing the body")`, silently discarding the intended 413/error_code
regardless of what handler was registered for the original exception type.

Fixed two ways together: (1) `RequestBodyTooLargeError` is now an
`HTTPException` subclass itself, and (2) the middleware is registered
*inner* to `inject_current_user_state`'s `BaseHTTPMiddleware` so the
exception is raised outside any task-group context and propagates as
itself. Verified with a real oversized multipart POST through
`main.app` via `TestClient` (not just a hand-built ASGI scope) returning
`413 {"detail": {"error_code": "REQUEST_BODY_TOO_LARGE", ...}}`, and a
normal-sized request still returning `200` unaffected by the reorder.
The underlying DoS protection (bytes never fully spooled past the limit)
was correct throughout — only the client-visible HTTP contract was wrong.

## 2. Real-decompression DOCX bound (`src/web/knowledge/extraction.py::_extract_docx`)

The B13-T1 guard summed `zf.infolist()`'s declared `info.file_size` — data
from the ZIP's own central directory, attacker-controlled, never
cross-checked by `zipfile` against what decompression actually produces.

**Fix**: each entry is streamed through `zf.open()` in 256 KiB chunks (same
idiom as `analyze_input_service.read_upload_bounded`), tracking both the
**per-entry** and **cumulative** running total of real decompressed bytes,
aborting the instant either exceeds `ANALYZE_DOCX_MAX_UNCOMPRESSED_MB`.
Entry count is capped first (`ANALYZE_DOCX_MAX_ZIP_ENTRIES`) before any
iteration. Corruption during actual decompression (`zipfile.BadZipFile`,
`zlib.error`, `EOFError`, `OSError`) maps to `CORRUPTED_FILE`, same as an
invalid ZIP outright. Everything operates on the same in-memory `raw: bytes`
buffer already used for the eventual `DocxDocument(io.BytesIO(raw))` parse —
no disk write, no re-fetch, no window for checked content to differ from
parsed content.

**Non-obvious subtlety found and fixed while implementing this**: CPython's
own `zipfile.ZipExtFile._read1` does `data = data[:self._left]`, where
`self._left` is initialised from `zinfo.file_size` — i.e. the stdlib's own
`.read()` API *itself* silently truncates decompressed output to the
declared (attacker-controlled) size and reports EOF there, regardless of how
much more the compressed stream could actually yield. A naive
"stream through `zf.open(info).read()`" guard would therefore still be
gated by the very metadata it's meant to distrust: an **understated**
`file_size` (e.g. `0`) makes `.read()` hand back nothing for a member that
truly decompresses to megabytes, which surfaced in testing as a
`CORRUPTED_FILE` (CRC mismatch on the truncated read) rather than the
correct `CONTENT_TOO_LARGE`. Fixed by opening each entry through a shallow
`copy.copy(info)` with `.file_size` overridden to a large sentinel
(`1 << 62`) before calling `zf.open()` — every other field (`header_offset`,
`compress_size`, `CRC`, `flag_bits`, `orig_filename`) is untouched, so
parsing/CRC-checking behave identically; only the artificial truncation is
neutralised, leaving this module's own chunked loop as the sole authority
on when to stop.

### Config (`src/core/config.py`, additive)

| Constant | Default | Reasoning |
|---|---|---|
| `ANALYZE_DOCX_MAX_ZIP_ENTRIES` | `2000` | A real `.docx` has ~10-40 internal XML parts. 2000 is generously above any legitimate document while bounding the cost of iterating an archive crafted with huge numbers of tiny/empty entries. |

`ANALYZE_DOCX_MAX_UNCOMPRESSED_MB` (B13-T1, unchanged default `50`) is now
enforced against real bytes rather than declared metadata.

## Knowledge-upload path — verified, not assumed

`src/web/routes_knowledge_documents.py`'s `POST /api/knowledge/documents`
and `POST /api/knowledge/documents/{id}/versions` both call
`documents_service.read_upload_with_limit(file, KNOWLEDGE_MAX_FILE_SIZE_MB)`
(a bounded-chunk reader, same idiom, already existing) and then
`documents_service.upload_document` / `add_version` →
`_ingest_version` → `extraction.extract_chunks` → `_extract_docx`. Both
knowledge-upload routes therefore already funnel through the exact function
fixed here — no separate DOCX extraction path exists for the knowledge
corpus. The ASGI middleware also covers these two routes by path prefix
(`/api/knowledge/documents`) independently of this.

## What this closes / does not close

**Closes**: unbounded file-size-before-parsing at the multipart layer
(disk-exhaustion DoS via an oversized upload reaching the request body
guard unmodified regardless of Content-Length); the DOCX
declared-vs-actual-decompressed-size mismatch (memory exhaustion via a
small-on-disk, huge-when-decompressed `.docx`), for both the AO-analyze and
knowledge-document upload paths.

**Does not close**: CPU or memory exhaustion from a legitimately-sized but
pathologically structured payload handled by some parser downstream (e.g. a
small PDF/DOCX crafted to be algorithmically expensive to parse) — a
different class of attack, out of scope for this ticket. Nor does it provide
isolation for every parser in the codebase; it addresses exactly the two
gaps described above.
