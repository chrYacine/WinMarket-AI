"""B13-T2 — reception-time bounds: raw ASGI body-size guard
(src/web/body_limit_middleware.py) + real-decompression DOCX bound
(src/web/knowledge/extraction.py::_extract_docx).

Two independent concerns tested here:

1. BodySizeLimitMiddleware — a pure-ASGI middleware, tested by driving it
   directly with hand-built ASGI `scope`/`receive`/`send` (no TestClient
   needed: this class only touches the ASGI layer, never HTTPException/
   Starlette Request objects).

2. _extract_docx's real-decompression zip-bomb guard — B13-T1 trusted
   ZipInfo.file_size (attacker-controlled archive metadata); this fixes
   that by streaming actual decompressed bytes in bounded chunks. Tested
   both directly (extraction.extract_chunks) and end-to-end through
   analyze_input_service.resolve_analyze_input, the same pattern
   tests/test_b13_t1_input_validation.py uses.

Pure unit-level: no DB, no HTTP client, no network, no API key.
"""
from __future__ import annotations

import asyncio
import io
import re
import zipfile
from pathlib import Path

import pytest

from src.core import config
from src.web import analyze_input_service as svc
from src.web import body_limit_middleware
from src.web.body_limit_middleware import BodySizeLimitMiddleware, RequestBodyTooLargeError
from src.web.knowledge import extraction

MIB = 1024 * 1024
DOCX_CHUNK = 1024 * 256  # must match _extract_docx's internal read chunk


# ---------------------------------------------------------------------------
# Local fixtures — self-contained, no cross-import from another agent's file.
# ---------------------------------------------------------------------------
class _FakeUpload:
    """Minimal UploadFile stand-in: `.filename` + awaitable `.read(size)`."""

    def __init__(self, filename: str, data: bytes):
        self.filename = filename
        self._data = data
        self._offset = 0

    async def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            chunk = self._data[self._offset:]
        else:
            chunk = self._data[self._offset:self._offset + size]
        self._offset += len(chunk)
        return chunk


def run(coro):
    return asyncio.run(coro)


def _zip_with_entry(name: str, content: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(name, content)
    return buffer.getvalue()


def _make_receive(messages: list[dict]):
    it = iter(messages)

    async def receive():
        return next(it)

    return receive


async def _consume_body_app(scope, receive, send):
    """Minimal downstream ASGI app: reads the body to completion (like
    Starlette's multipart parser would) and echoes the total byte count."""
    total = 0
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            break
        total += len(message.get("body", b"") or b"")
        if not message.get("more_body", False):
            break
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": str(total).encode()})


def _sent_body(sent: list[dict]) -> int:
    message = next(m for m in sent if m["type"] == "http.response.body")
    return int(message["body"])


# ===========================================================================
# 1. BodySizeLimitMiddleware — real ASGI events, fragmented
# ===========================================================================
def test_fragmented_body_across_multiple_messages_is_summed_correctly():
    fragments = [b"a" * 1000, b"b" * 2000, b"c" * 500]
    messages = [
        {"type": "http.request", "body": frag, "more_body": (i < len(fragments) - 1)}
        for i, frag in enumerate(fragments)
    ]
    scope = {"type": "http", "path": "/api/analyze", "headers": []}
    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    middleware = BodySizeLimitMiddleware(_consume_body_app, max_bytes=10_000, guarded_prefixes=("/api/analyze",))
    run(middleware(scope, _make_receive(messages), send))

    assert _sent_body(sent) == sum(len(f) for f in fragments)


def test_overflow_on_a_later_fragment_stops_before_excess_reaches_the_app():
    """The multipart parser downstream must never receive the bytes that
    push the request over the limit."""
    first = b"a" * 6000
    second = b"b" * 6000  # cumulative 12000 > max 10000
    messages = [
        {"type": "http.request", "body": first, "more_body": True},
        {"type": "http.request", "body": second, "more_body": False},
    ]
    received_by_app: list[bytes] = []

    async def app(scope, receive, send):
        while True:
            message = await receive()  # second call raises inside guarded_receive
            received_by_app.append(message["body"])
            if not message.get("more_body", False):
                break

    scope = {"type": "http", "path": "/api/analyze", "headers": []}

    async def send(message):
        pass

    middleware = BodySizeLimitMiddleware(app, max_bytes=10_000, guarded_prefixes=("/api/analyze",))
    with pytest.raises(RequestBodyTooLargeError):
        run(middleware(scope, _make_receive(messages), send))

    assert received_by_app == [first], "the second, over-limit fragment must never reach the downstream app"


# ===========================================================================
# 2. At the limit / one byte over
# ===========================================================================
def test_exactly_at_the_limit_passes_through():
    data = b"x" * 10_000
    messages = [{"type": "http.request", "body": data, "more_body": False}]
    scope = {"type": "http", "path": "/api/analyze", "headers": []}
    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    middleware = BodySizeLimitMiddleware(_consume_body_app, max_bytes=10_000, guarded_prefixes=("/api/analyze",))
    run(middleware(scope, _make_receive(messages), send))
    assert _sent_body(sent) == 10_000


def test_one_byte_over_the_limit_is_rejected():
    data = b"x" * 10_001
    messages = [{"type": "http.request", "body": data, "more_body": False}]
    scope = {"type": "http", "path": "/api/analyze", "headers": []}
    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    middleware = BodySizeLimitMiddleware(_consume_body_app, max_bytes=10_000, guarded_prefixes=("/api/analyze",))
    with pytest.raises(RequestBodyTooLargeError) as excinfo:
        run(middleware(scope, _make_receive(messages), send))
    assert excinfo.value.max_bytes == 10_000
    assert excinfo.value.received_bytes == 10_001
    assert sent == [], "downstream app must never get a chance to respond to an over-limit request"


# ===========================================================================
# 3. No reliable / lying Content-Length — real bytes decide, header never consulted
# ===========================================================================
def test_understated_content_length_does_not_prevent_rejection():
    """Content-Length claims 10 bytes; the real body is 20000 (over the
    10000 max). Must still be rejected on real bytes received."""
    scope = {"type": "http", "path": "/api/analyze", "headers": [(b"content-length", b"10")]}
    data = b"z" * 20_000
    messages = [{"type": "http.request", "body": data, "more_body": False}]
    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    middleware = BodySizeLimitMiddleware(_consume_body_app, max_bytes=10_000, guarded_prefixes=("/api/analyze",))
    with pytest.raises(RequestBodyTooLargeError) as excinfo:
        run(middleware(scope, _make_receive(messages), send))
    assert excinfo.value.received_bytes > 10  # the tiny declared header changed nothing


def test_overstated_content_length_does_not_cause_a_false_rejection():
    """Content-Length claims ~1 GiB; the real body is 500 bytes (well under
    the 10000 max). Must NOT be rejected just because the header looked
    huge — only real bytes received are ever counted."""
    scope = {"type": "http", "path": "/api/analyze", "headers": [(b"content-length", b"999999999")]}
    data = b"z" * 500
    messages = [{"type": "http.request", "body": data, "more_body": False}]
    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    middleware = BodySizeLimitMiddleware(_consume_body_app, max_bytes=10_000, guarded_prefixes=("/api/analyze",))
    run(middleware(scope, _make_receive(messages), send))  # must not raise
    assert _sent_body(sent) == 500


def test_absent_content_length_header_is_still_bounded_correctly():
    scope = {"type": "http", "path": "/api/analyze", "headers": []}  # no Content-Length at all
    data = b"z" * 10_001
    messages = [{"type": "http.request", "body": data, "more_body": False}]

    async def send(message):
        pass

    middleware = BodySizeLimitMiddleware(_consume_body_app, max_bytes=10_000, guarded_prefixes=("/api/analyze",))
    with pytest.raises(RequestBodyTooLargeError):
        run(middleware(scope, _make_receive(messages), send))


# ===========================================================================
# Scope / prefix / disconnect behaviour
# ===========================================================================
def test_unguarded_path_is_never_wrapped_and_never_rejected():
    huge = b"x" * 50_000  # far over max_bytes, but path is not guarded
    messages = [{"type": "http.request", "body": huge, "more_body": False}]
    scope = {"type": "http", "path": "/unrelated/route", "headers": []}
    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    middleware = BodySizeLimitMiddleware(_consume_body_app, max_bytes=10_000, guarded_prefixes=("/api/analyze",))
    run(middleware(scope, _make_receive(messages), send))  # must not raise
    assert _sent_body(sent) == 50_000


def test_non_http_scope_type_is_passed_through_untouched():
    calls = []

    async def app(scope, receive, send):
        calls.append(scope["type"])

    middleware = BodySizeLimitMiddleware(app, max_bytes=10, guarded_prefixes=("/api/analyze",))

    async def send(message):
        pass

    run(middleware({"type": "lifespan"}, _make_receive([]), send))
    assert calls == ["lifespan"]


def test_http_disconnect_mid_stream_is_not_treated_as_overflow():
    messages = [
        {"type": "http.request", "body": b"a" * 100, "more_body": True},
        {"type": "http.disconnect"},
    ]
    received_types = []

    async def app(scope, receive, send):
        while True:
            message = await receive()
            received_types.append(message["type"])
            if message["type"] == "http.disconnect":
                break

    scope = {"type": "http", "path": "/api/analyze", "headers": []}

    async def send(message):
        pass

    middleware = BodySizeLimitMiddleware(app, max_bytes=1000, guarded_prefixes=("/api/analyze",))
    run(middleware(scope, _make_receive(messages), send))  # must not raise
    assert received_types == ["http.request", "http.disconnect"]


def test_middleware_module_imports_no_job_or_llm_machinery():
    """Mirrors test_b13_t1_input_validation.py's group H: this is a pure
    ASGI primitive and must stay free of job/LLM/DB coupling."""
    source = Path(body_limit_middleware.__file__).read_text(encoding="utf-8")
    forbidden_import = re.compile(
        r"^\s*(?:from|import)\s+.*(?:ao_extractor|llm_client|scoring_engine|rag_manager|"
        r"capacity_analyzer|company_enrichment|document_generator|anthropic|openai|mistralai|"
        r"src\.web\.jobs|src\.agents|src\.rag|src\.livrables|sqlalchemy)",
        re.MULTILINE,
    )
    assert not forbidden_import.findall(source)


# ===========================================================================
# 4. Real valid DOCX — end to end, not just unit-tested in isolation
# ===========================================================================
def test_real_docx_parses_successfully_through_extract_chunks():
    pytest.importorskip("docx")
    from docx import Document as DocxDocument

    doc = DocxDocument()
    doc.add_paragraph("Objet du marché : refonte de l'extranet client.")
    doc.add_paragraph("Budget prévisionnel : 250 000 euros.")
    buffer = io.BytesIO()
    doc.save(buffer)
    raw = buffer.getvalue()

    chunks = extraction.extract_chunks(".docx", raw)
    joined = "\n\n".join(c.content for c in chunks)
    assert "refonte de l'extranet" in joined
    assert "250 000" in joined


def test_real_docx_end_to_end_through_resolve_analyze_input():
    pytest.importorskip("docx")
    from docx import Document as DocxDocument

    doc = DocxDocument()
    doc.add_paragraph("Objet du marché : refonte de l'extranet client.")
    doc.add_paragraph("Budget prévisionnel : 250 000 euros.")
    buffer = io.BytesIO()
    doc.save(buffer)
    raw = buffer.getvalue()

    upload = _FakeUpload("ao_reel.docx", raw)
    content = run(svc.resolve_analyze_input(mode="upload", file=upload))
    assert "refonte de l'extranet" in content
    assert "250 000" in content


# ===========================================================================
# 5. DOCX with excessive REAL decompressed size — early stop, not a full inflate
# ===========================================================================
def test_docx_excessive_real_decompressed_size_is_rejected_with_an_early_stop(monkeypatch):
    pytest.importorskip("docx")
    monkeypatch.setattr(config, "ANALYZE_DOCX_MAX_UNCOMPRESSED_MB", 1)

    payload = b"\x00" * (20 * MIB)  # 20x over the 1 MiB test limit, tiny on the wire
    bomb = _zip_with_entry("word/document.xml", payload)
    assert len(bomb) < 64 * 1024

    read_calls: list[int] = []
    original_read = zipfile.ZipExtFile.read

    def counting_read(self, n=-1, *a, **kw):
        read_calls.append(n)
        return original_read(self, n, *a, **kw)

    monkeypatch.setattr(zipfile.ZipExtFile, "read", counting_read)

    with pytest.raises(extraction.UnsupportedContentError) as excinfo:
        extraction.extract_chunks(".docx", bomb)
    assert excinfo.value.error_code == "CONTENT_TOO_LARGE"

    # Bounded by ceil(limit / chunk), never anywhere near ceil(full_size /
    # chunk) — proof the loop aborted mid-decompression rather than
    # inflating the full 20 MiB before checking.
    max_expected_calls = (1 * MIB) // DOCX_CHUNK + 2
    assert len(read_calls) <= max_expected_calls, (
        f"{len(read_calls)} read() calls — guard did not abort early "
        f"(full inflate would need ~{(20 * MIB) // DOCX_CHUNK} calls)"
    )


def test_docx_zip_bomb_rejected_before_docxdocument_is_ever_called(monkeypatch):
    docx_mod = pytest.importorskip("docx")
    monkeypatch.setattr(config, "ANALYZE_DOCX_MAX_UNCOMPRESSED_MB", 1)

    def never_call_me(*args, **kwargs):
        raise AssertionError("python-docx full parse was reached — the real-decompression guard did not fire first")

    monkeypatch.setattr(docx_mod, "Document", never_call_me)

    bomb = _zip_with_entry("word/document.xml", b"\x00" * (4 * MIB))
    with pytest.raises(extraction.UnsupportedContentError) as excinfo:
        extraction.extract_chunks(".docx", bomb)
    assert excinfo.value.error_code == "CONTENT_TOO_LARGE"


def test_docx_zip_bomb_through_the_service_is_413(monkeypatch):
    pytest.importorskip("docx")
    monkeypatch.setattr(config, "ANALYZE_DOCX_MAX_UNCOMPRESSED_MB", 1)
    upload = _FakeUpload("bombe.docx", _zip_with_entry("word/document.xml", b"\x00" * (4 * MIB)))
    with pytest.raises(svc.InputTooLargeError) as excinfo:
        run(svc.resolve_analyze_input(mode="upload", file=upload))
    assert excinfo.value.http_status == 413
    assert excinfo.value.error_code == "CONTENT_TOO_LARGE"


def test_docx_cumulative_limit_across_multiple_entries_is_enforced(monkeypatch):
    """No single entry exceeds the limit alone, but their sum does — the
    cumulative running total (not just per-entry) must catch it."""
    pytest.importorskip("docx")
    monkeypatch.setattr(config, "ANALYZE_DOCX_MAX_UNCOMPRESSED_MB", 1)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for i in range(5):
            zf.writestr(f"part{i}.xml", b"\x00" * (300 * 1024))  # 300 KiB each, 1.5 MiB total > 1 MiB
    bomb = buffer.getvalue()

    with pytest.raises(extraction.UnsupportedContentError) as excinfo:
        extraction.extract_chunks(".docx", bomb)
    assert excinfo.value.error_code == "CONTENT_TOO_LARGE"


def test_docx_too_many_zip_entries_is_rejected_before_iterating(monkeypatch):
    monkeypatch.setattr(config, "ANALYZE_DOCX_MAX_ZIP_ENTRIES", 5)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for i in range(10):
            zf.writestr(f"part{i}.xml", b"x")
    bomb = buffer.getvalue()

    with pytest.raises(extraction.UnsupportedContentError) as excinfo:
        extraction.extract_chunks(".docx", bomb)
    assert excinfo.value.error_code == "CONTENT_TOO_LARGE"


def test_a_normal_small_zip_still_fails_later_not_as_content_too_large():
    """The guard must not reject an ordinary tiny archive — it fails later
    as a normal corrupted/unsupported DOCX, never CONTENT_TOO_LARGE."""
    pytest.importorskip("docx")
    small = _zip_with_entry("word/document.xml", b"0" * 1000)
    with pytest.raises(extraction.UnsupportedContentError) as excinfo:
        extraction.extract_chunks(".docx", small)
    assert excinfo.value.error_code != "CONTENT_TOO_LARGE"


# ===========================================================================
# 6. Metadata-lying fixtures — confirm ZipInfo.file_size is never trusted
# ===========================================================================
def test_docx_overstated_declared_file_size_does_not_cause_a_false_rejection(monkeypatch):
    """A ZipInfo lying that content is huge, when the REAL content is tiny,
    must not cause a CONTENT_TOO_LARGE rejection — proof the declared field
    is never consulted for the decision. This is a synthetic metadata
    fixture, not a demonstrated exploit against real-world zip tooling."""
    pytest.importorskip("docx")
    original_infolist = zipfile.ZipFile.infolist

    def lying_huge(self):
        infos = original_infolist(self)
        for info in infos:
            info.file_size = 10**9  # lies: claims ~1 GiB
        return infos

    monkeypatch.setattr(zipfile.ZipFile, "infolist", lying_huge)
    small_zip = _zip_with_entry("word/document.xml", b"hello")

    with pytest.raises(extraction.UnsupportedContentError) as excinfo:
        extraction.extract_chunks(".docx", small_zip)
    # Fails for an unrelated reason (not a real docx package) but never
    # because of the lying declared size.
    assert excinfo.value.error_code != "CONTENT_TOO_LARGE"


def test_docx_understated_declared_file_size_cannot_hide_a_real_oversized_entry(monkeypatch):
    """A ZipInfo lying that content is empty, when the REAL content is
    oversized, must still be rejected — proof enforcement uses actual
    decompressed bytes, never the declared field, in either direction."""
    pytest.importorskip("docx")
    monkeypatch.setattr(config, "ANALYZE_DOCX_MAX_UNCOMPRESSED_MB", 1)
    original_infolist = zipfile.ZipFile.infolist

    def lying_zero(self):
        infos = original_infolist(self)
        for info in infos:
            info.file_size = 0  # lies: claims empty
        return infos

    monkeypatch.setattr(zipfile.ZipFile, "infolist", lying_zero)
    bomb = _zip_with_entry("word/document.xml", b"\x00" * (5 * MIB))  # 5x over the 1 MiB test limit

    with pytest.raises(extraction.UnsupportedContentError) as excinfo:
        extraction.extract_chunks(".docx", bomb)
    assert excinfo.value.error_code == "CONTENT_TOO_LARGE"


# ===========================================================================
# 7. Corruption during real decompression maps to CORRUPTED_FILE
# ===========================================================================
def test_docx_not_a_valid_zip_is_corrupted_file():
    with pytest.raises(extraction.UnsupportedContentError) as excinfo:
        extraction.extract_chunks(".docx", b"PK\x03\x04" + b"n'importe quoi" * 20)
    assert excinfo.value.error_code == "CORRUPTED_FILE"


def test_docx_truncated_member_data_is_corrupted_file():
    """A ZIP whose central directory is intact but whose compressed member
    bytes are truncated/mangled — CRC or deflate-stream failure during the
    real read loop must map to CORRUPTED_FILE, not propagate raw."""
    raw = _zip_with_entry("word/document.xml", b"hello world" * 100)
    # Corrupt the tail of the archive (compressed data region) without
    # touching the ZIP end-of-central-directory signature enough to make
    # zipfile refuse to open it outright — this targets the read()-time
    # corruption path inside the per-entry loop.
    mutated = bytearray(raw)
    # Flip bytes roughly in the middle of the local file header/data area.
    for i in range(20, min(40, len(mutated))):
        mutated[i] ^= 0xFF
    mutated_bytes = bytes(mutated)

    with pytest.raises(extraction.UnsupportedContentError) as excinfo:
        extraction.extract_chunks(".docx", mutated_bytes)
    assert excinfo.value.error_code == "CORRUPTED_FILE"


# ===========================================================================
# 8. Cleanup — no fd/member leak on success or abort path
# ===========================================================================
def test_docx_zip_members_are_closed_on_the_abort_path(monkeypatch):
    pytest.importorskip("docx")
    monkeypatch.setattr(config, "ANALYZE_DOCX_MAX_UNCOMPRESSED_MB", 1)
    bomb = _zip_with_entry("word/document.xml", b"\x00" * (5 * MIB))

    opened_members = []
    original_open = zipfile.ZipFile.open

    def tracking_open(self, name, *a, **kw):
        member = original_open(self, name, *a, **kw)
        opened_members.append(member)
        return member

    monkeypatch.setattr(zipfile.ZipFile, "open", tracking_open)

    with pytest.raises(extraction.UnsupportedContentError):
        extraction.extract_chunks(".docx", bomb)

    assert opened_members, "no member was ever opened"
    assert all(m.closed for m in opened_members), "a ZipExtFile was left open on the abort path"


def test_docx_zip_members_are_closed_on_the_success_path(monkeypatch):
    pytest.importorskip("docx")
    from docx import Document as DocxDocument

    doc = DocxDocument()
    doc.add_paragraph("Contenu de test.")
    buffer = io.BytesIO()
    doc.save(buffer)
    raw = buffer.getvalue()

    opened_members = []
    original_open = zipfile.ZipFile.open

    def tracking_open(self, name, *a, **kw):
        member = original_open(self, name, *a, **kw)
        opened_members.append(member)
        return member

    monkeypatch.setattr(zipfile.ZipFile, "open", tracking_open)

    chunks = extraction.extract_chunks(".docx", raw)
    assert any("Contenu de test" in c.content for c in chunks)
    assert opened_members, "no member was ever opened"
    assert all(m.closed for m in opened_members), "a ZipExtFile was left open on the success path"


# ===========================================================================
# 9. 413 / 415 / 422 consistency after the B13-T2 changes
# ===========================================================================
def test_413_415_422_consistency_is_unchanged_by_the_b13_t2_docx_fix():
    # Valid input still succeeds.
    assert run(svc.resolve_analyze_input(mode="upload", file=_FakeUpload("ok.txt", b"Contenu valide."))) == "Contenu valide."

    # Size boundary -> 413.
    with pytest.raises(svc.InputTooLargeError) as size_exc:
        run(svc.resolve_analyze_input(mode="paste", text="a" * (config.ANALYZE_MAX_PASTE_CHARS + 1)))
    assert size_exc.value.http_status == 413

    # Extension/content mismatch -> 415 (B13-T1 behaviour, unaffected here).
    with pytest.raises(svc.UnsupportedFormatError) as fmt_exc:
        run(svc.resolve_analyze_input(mode="upload", file=_FakeUpload("faux.pdf", b"pas un pdf")))
    assert fmt_exc.value.http_status == 415

    # Corrupted/unusable content -> 422.
    with pytest.raises(svc.UnusableContentError) as corrupt_exc:
        run(svc.resolve_analyze_input(mode="upload", file=_FakeUpload("corrompu.docx", b"PK\x03\x04sale")))
    assert corrupt_exc.value.http_status == 422


# ===========================================================================
# 10. No job/LLM machinery reachable — extends test_b13_t1's source-level guard
# ===========================================================================
def test_no_job_or_llm_call_for_any_rejected_input_in_this_suite():
    """Every rejection case exercised above raises before returning any
    fabricated content; combined with test_middleware_module_imports_no_job_or_llm_machinery
    and the existing tests/test_b13_t1_input_validation.py::test_h_* source
    guards, no path here can reach job/LLM machinery."""
    rejected_cases = [
        {"mode": "upload", "file": _FakeUpload("faux.pdf", b"pas un pdf")},
        {"mode": "upload", "file": _FakeUpload("corrompu.docx", b"PK\x03\x04sale")},
        {"mode": "upload", "file": _FakeUpload("bombe.docx", _zip_with_entry("word/document.xml", b"\x00" * (2 * MIB)))},
    ]
    for kwargs in rejected_cases:
        with pytest.raises(svc.AnalyzeInputError):
            run(svc.resolve_analyze_input(**kwargs))
