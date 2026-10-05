"""B13-T1 — AO-analysis input validation (src/web/analyze_input_service.py).

Pure unit tests: no HTTP layer, no DB, no network, no API key. Every case
is bytes-in / text-out against the service function the routes will call.

Groups A-I map 1:1 to the ticket's test list.
"""
from __future__ import annotations

import asyncio
import io
import re
import time
import tokenize
import zipfile
from pathlib import Path

import pytest

from src.core import config
from src.web import analyze_input_service as svc
from src.web import examples_service
from src.web.knowledge import extraction

SERVICE_SOURCE_PATH = Path(svc.__file__)
SERVICE_SOURCE = SERVICE_SOURCE_PATH.read_text(encoding="utf-8")


def _strip_comments_and_strings(source: str) -> str:
    """Blank out comments and string literals, preserving line/column layout.

    The groups H and I assertions below are about what the module's CODE
    does, not about what its documentation is allowed to mention: the
    docstrings deliberately name AOExtractor and NamedTemporaryFile to
    explain what this module replaces and avoids.
    """
    grid = [list(line) for line in source.splitlines(keepends=True)]
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type not in (tokenize.COMMENT, tokenize.STRING):
            continue
        (start_row, start_col), (end_row, end_col) = token.start, token.end
        for row in range(start_row, end_row + 1):
            line = grid[row - 1]
            first = start_col if row == start_row else 0
            last = end_col if row == end_row else len(line)
            for col in range(first, min(last, len(line))):
                if line[col] != "\n":
                    line[col] = " "
    return "".join("".join(line) for line in grid)


# Executable code only — comments and string literals blanked out.
SERVICE_CODE = _strip_comments_and_strings(SERVICE_SOURCE)

CHUNK = 1024 * 256
MIB = 1024 * 1024


def run(coro):
    """Drive one coroutine to completion without depending on pytest-asyncio."""
    return asyncio.run(coro)


class FakeUpload:
    """Minimal UploadFile stand-in: `.filename` + awaitable `.read(size)`.

    Counts read() calls and tracks how many bytes were actually served, so a
    test can prove the reader aborted mid-stream instead of consuming
    everything. `declared_size` is deliberately allowed to LIE (group C).
    """

    def __init__(self, filename: str, data: bytes, declared_size: int | None = None):
        self.filename = filename
        self._data = data
        self._offset = 0
        self.read_calls = 0
        # Named like Starlette's UploadFile.size / a Content-Length claim.
        self.size = declared_size
        self.content_length = declared_size

    async def read(self, size: int = -1) -> bytes:
        self.read_calls += 1
        if size is None or size < 0:
            chunk = self._data[self._offset:]
        else:
            chunk = self._data[self._offset:self._offset + size]
        self._offset += len(chunk)
        return chunk

    @property
    def bytes_served(self) -> int:
        return self._offset


# ---------------------------------------------------------------------------
# A. Valid input, all three modes
# ---------------------------------------------------------------------------
def test_a_paste_valid_returns_text():
    text = "Objet du marché : refonte de l'extranet.\n\nBudget : 250 000 €."
    assert run(svc.resolve_analyze_input(mode="paste", text=text)) == text


def test_a_upload_valid_txt_returns_joined_text():
    raw = b"Objet du marche : refonte extranet.\n\nBudget : 250 000 EUR."
    upload = FakeUpload("ao_client.txt", raw)
    content = run(svc.resolve_analyze_input(mode="upload", file=upload))
    assert "Objet du marche" in content
    assert "250 000 EUR" in content
    # Paragraph boundaries preserved with the "\n\n" convention.
    assert content == "Objet du marche : refonte extranet.\n\nBudget : 250 000 EUR."


def test_a_stock_valid_returns_example_text():
    examples = examples_service.list_examples()
    if not examples:
        pytest.skip("no bundled example AO fixtures in data/ao_examples")
    example_id = examples[0]["id"]
    content = run(svc.resolve_analyze_input(mode="stock", example_id=example_id))
    assert isinstance(content, str) and len(content) > 500


def test_a_stock_fixtures_are_small_and_trusted():
    """Stock mode adds no validation on purpose — confirm the premise holds:
    server-authored fixtures, all comfortably small."""
    for example in examples_service.list_examples():
        text = examples_service.read_example(example["id"])
        assert text is not None
        assert len(text) < config.ANALYZE_MAX_PASTE_CHARS


def test_a_invalid_mode_and_missing_inputs_are_400():
    for kwargs in (
        {"mode": "nope"},
        {"mode": "stock"},
        {"mode": "paste", "text": "   "},
        {"mode": "upload", "file": None},
    ):
        with pytest.raises(svc.InvalidInputError) as excinfo:
            run(svc.resolve_analyze_input(**kwargs))
        assert excinfo.value.http_status == 400


def test_a_unknown_example_is_404():
    with pytest.raises(svc.ExampleNotFoundError) as excinfo:
        run(svc.resolve_analyze_input(mode="stock", example_id="ceci_n_existe_pas"))
    assert excinfo.value.http_status == 404


# ---------------------------------------------------------------------------
# B. Size boundary + proof the reader aborts mid-stream
# ---------------------------------------------------------------------------
def test_b_upload_exactly_at_limit_succeeds(monkeypatch):
    monkeypatch.setattr(config, "ANALYZE_MAX_UPLOAD_MB", 1)
    raw = b"x" * MIB
    upload = FakeUpload("pile_a_la_limite.txt", raw)
    content = run(svc.resolve_analyze_input(mode="upload", file=upload))
    assert len(content) == MIB


def test_b_upload_one_byte_over_limit_is_413(monkeypatch):
    monkeypatch.setattr(config, "ANALYZE_MAX_UPLOAD_MB", 1)
    raw = b"x" * (MIB + 1)
    upload = FakeUpload("un_octet_de_trop.txt", raw)
    with pytest.raises(svc.InputTooLargeError) as excinfo:
        run(svc.resolve_analyze_input(mode="upload", file=upload))
    assert excinfo.value.http_status == 413
    assert excinfo.value.error_code == "UPLOAD_TOO_LARGE"


def test_b_reader_aborts_mid_stream_and_never_buffers_the_whole_body():
    """The bounded reader must stop calling read() once the running total
    exceeds the limit — not consume a 10 MiB stream to then check."""
    data = b"y" * (10 * MIB)
    upload = FakeUpload("enorme.txt", data)
    with pytest.raises(svc.InputTooLargeError):
        run(svc.read_upload_bounded(upload, MIB))

    # 4 chunks reach exactly 1 MiB, the 5th trips the limit.
    assert upload.read_calls == 5
    assert upload.bytes_served == 5 * CHUNK
    assert upload.bytes_served < len(data)  # the full body was never read

    # And it genuinely stops: no further reads happen after the raise.
    calls_after_abort = upload.read_calls
    assert calls_after_abort * CHUNK < len(data)


def test_b_reader_at_exact_limit_reads_to_eof_and_returns_all_bytes():
    data = b"z" * MIB
    upload = FakeUpload("limite.txt", data)
    assert run(svc.read_upload_bounded(upload, MIB)) == data


# ---------------------------------------------------------------------------
# C. A lying declared size changes nothing
# ---------------------------------------------------------------------------
def test_c_declared_size_is_ignored_actual_bytes_decide():
    """The reader never trusts a declared length. FakeUpload claims 10 bytes
    via `.size`/`.content_length` but streams 5 MiB; the limit must still
    fire on the bytes ACTUALLY received.

    Note: a "streams more than declared" scenario is structurally impossible
    to sneak past this design, because no declared length is ever read — the
    reader only ever sums len() of the chunks it received. The assertions
    below pin that down: the attributes are untouched by the call, and the
    service source contains no reference to them.
    """
    upload = FakeUpload("menteur.txt", b"w" * (5 * MIB), declared_size=10)
    with pytest.raises(svc.InputTooLargeError):
        run(svc.read_upload_bounded(upload, MIB))
    assert upload.size == 10  # never consulted, never corrected
    assert upload.bytes_served > 10

    assert ".size" not in SERVICE_CODE
    assert "content_length" not in SERVICE_CODE.lower()
    assert "content-length" not in SERVICE_CODE.lower()


def test_c_undersized_claim_on_a_small_file_still_succeeds_normally():
    upload = FakeUpload("honnete.txt", b"Contenu court.", declared_size=999_999_999)
    assert run(svc.resolve_analyze_input(mode="upload", file=upload)) == "Contenu court."


# ---------------------------------------------------------------------------
# D. Extension / content mismatch -> 415
# ---------------------------------------------------------------------------
def test_d_text_bytes_named_pdf_is_rejected_as_unsupported():
    upload = FakeUpload("faux_ao.pdf", b"Ceci n'est pas un PDF, juste du texte.")
    with pytest.raises(svc.UnsupportedFormatError) as excinfo:
        run(svc.resolve_analyze_input(mode="upload", file=upload))
    assert excinfo.value.http_status == 415
    assert excinfo.value.error_code == "UNSUPPORTED_CONTENT"


def test_d_text_bytes_named_docx_is_rejected_as_unsupported():
    upload = FakeUpload("faux_ao.docx", b"Ceci n'est pas un DOCX.")
    with pytest.raises(svc.UnsupportedFormatError):
        run(svc.resolve_analyze_input(mode="upload", file=upload))


def test_d_disallowed_extension_is_rejected():
    upload = FakeUpload("payload.exe", b"MZ\x90\x00 binaire")
    with pytest.raises(svc.UnsupportedFormatError):
        run(svc.resolve_analyze_input(mode="upload", file=upload))


def test_d_no_extension_is_rejected():
    upload = FakeUpload("sans_extension", b"du texte")
    with pytest.raises(svc.UnsupportedFormatError):
        run(svc.resolve_analyze_input(mode="upload", file=upload))


# ---------------------------------------------------------------------------
# E. Empty / corrupted / unexploitable -> 422
# ---------------------------------------------------------------------------
def test_e_empty_file_is_422():
    upload = FakeUpload("vide.txt", b"")
    with pytest.raises(svc.UnusableContentError) as excinfo:
        run(svc.resolve_analyze_input(mode="upload", file=upload))
    assert excinfo.value.http_status == 422
    assert excinfo.value.error_code == "EMPTY_FILE"


def test_e_whitespace_only_file_is_422():
    upload = FakeUpload("blanc.txt", b"   \n\n   \t  \n")
    with pytest.raises(svc.UnusableContentError):
        run(svc.resolve_analyze_input(mode="upload", file=upload))


def test_e_truncated_pdf_is_422():
    pytest.importorskip("fitz")
    upload = FakeUpload("tronque.pdf", b"%PDF-1.4\n" + b"\x00garbage" * 50)
    with pytest.raises(svc.UnusableContentError) as excinfo:
        run(svc.resolve_analyze_input(mode="upload", file=upload))
    assert excinfo.value.http_status == 422
    assert excinfo.value.error_code in {"CORRUPTED_FILE", "EMPTY_CONTENT", "OCR_REQUIRED"}


def test_e_corrupted_docx_not_a_valid_zip_is_422():
    upload = FakeUpload("corrompu.docx", b"PK\x03\x04" + b"n'importe quoi" * 20)
    with pytest.raises(svc.UnusableContentError) as excinfo:
        run(svc.resolve_analyze_input(mode="upload", file=upload))
    assert excinfo.value.http_status == 422
    assert excinfo.value.error_code == "CORRUPTED_FILE"


def test_e_scanned_pdf_surfaces_ocr_required_without_a_real_fixture(monkeypatch):
    """A text-less (scanned) PDF reaches extraction's own OCR_REQUIRED path;
    the service must surface that exact code as a 422, with an honest
    message saying this is not an OCR product."""
    def fake_extract_chunks(suffix, raw):
        raise extraction.UnsupportedContentError("OCR_REQUIRED", "Aucun texte extractible — PDF probablement scanné.")

    monkeypatch.setattr(extraction, "extract_chunks", fake_extract_chunks)
    upload = FakeUpload("scan.pdf", b"%PDF-1.4\n" + b"contenu image")
    with pytest.raises(svc.UnusableContentError) as excinfo:
        run(svc.resolve_analyze_input(mode="upload", file=upload))
    assert excinfo.value.http_status == 422
    assert excinfo.value.error_code == "OCR_REQUIRED"
    assert "OCR" in excinfo.value.message or "reconnaissance de caractères" in excinfo.value.message
    # Never a raw internal parser string leaked to the client.
    assert "Traceback" not in excinfo.value.message


def test_e_rejections_never_return_a_placeholder_text():
    """Every rejection raises — no call path returns a fabricated or empty
    string that could become a bogus AO downstream."""
    cases = [
        {"mode": "upload", "file": FakeUpload("vide.txt", b"")},
        {"mode": "upload", "file": FakeUpload("faux.pdf", b"pas un pdf")},
        {"mode": "paste", "text": ""},
        {"mode": "stock", "example_id": "inconnu"},
        {"mode": "bidon"},
    ]
    for kwargs in cases:
        with pytest.raises(svc.AnalyzeInputError):
            run(svc.resolve_analyze_input(**kwargs))


# ---------------------------------------------------------------------------
# F. Pasted-text length bound
# ---------------------------------------------------------------------------
def test_f_paste_exactly_at_limit_succeeds():
    text = "a" * config.ANALYZE_MAX_PASTE_CHARS
    assert len(run(svc.resolve_analyze_input(mode="paste", text=text))) == config.ANALYZE_MAX_PASTE_CHARS


def test_f_paste_one_char_over_limit_is_413():
    text = "a" * (config.ANALYZE_MAX_PASTE_CHARS + 1)
    with pytest.raises(svc.InputTooLargeError) as excinfo:
        run(svc.resolve_analyze_input(mode="paste", text=text))
    assert excinfo.value.http_status == 413
    assert excinfo.value.error_code == "PASTE_TOO_LARGE"


def test_f_paste_limit_is_read_at_call_time(monkeypatch):
    monkeypatch.setattr(config, "ANALYZE_MAX_PASTE_CHARS", 10)
    assert run(svc.resolve_analyze_input(mode="paste", text="0123456789")) == "0123456789"
    with pytest.raises(svc.InputTooLargeError):
        run(svc.resolve_analyze_input(mode="paste", text="01234567890"))


# ---------------------------------------------------------------------------
# G. DOCX zip-bomb guard (extraction.py, benefits knowledge upload too)
# ---------------------------------------------------------------------------
def _zip_bomb(uncompressed_bytes: int) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", b"0" * uncompressed_bytes)
    return buffer.getvalue()


def test_g_zip_bomb_rejected_before_any_docx_parsing(monkeypatch):
    """The guard must fire on the SUM of uncompressed sizes from the archive
    directory, BEFORE python-docx is ever handed the bytes."""
    docx_mod = pytest.importorskip("docx")
    monkeypatch.setattr(config, "ANALYZE_DOCX_MAX_UNCOMPRESSED_MB", 1)

    def never_call_me(*args, **kwargs):
        raise AssertionError("python-docx full parse was reached — the zip-bomb guard did not fire first")

    monkeypatch.setattr(docx_mod, "Document", never_call_me)

    bomb = _zip_bomb(4 * MIB)
    assert len(bomb) < 64 * 1024  # tiny on the wire, 4 MiB decompressed

    with pytest.raises(extraction.UnsupportedContentError) as excinfo:
        extraction.extract_chunks(".docx", bomb)
    assert excinfo.value.error_code == "CONTENT_TOO_LARGE"


def test_g_zip_bomb_rejected_at_the_real_default_bound_and_fast():
    """60 MiB of zeros against the shipped 50 MiB default — and it must be
    rejected quickly, i.e. from the archive directory, not after a full
    decompression."""
    pytest.importorskip("docx")
    assert config.ANALYZE_DOCX_MAX_UNCOMPRESSED_MB == 50
    bomb = _zip_bomb(60 * MIB)

    started = time.perf_counter()
    with pytest.raises(extraction.UnsupportedContentError) as excinfo:
        extraction.extract_chunks(".docx", bomb)
    elapsed = time.perf_counter() - started

    assert excinfo.value.error_code == "CONTENT_TOO_LARGE"
    assert elapsed < 2.0, f"guard took {elapsed:.2f}s — it likely decompressed first"


def test_g_zip_bomb_through_the_service_is_413(monkeypatch):
    pytest.importorskip("docx")
    monkeypatch.setattr(config, "ANALYZE_DOCX_MAX_UNCOMPRESSED_MB", 1)
    upload = FakeUpload("bombe.docx", _zip_bomb(4 * MIB))
    with pytest.raises(svc.InputTooLargeError) as excinfo:
        run(svc.resolve_analyze_input(mode="upload", file=upload))
    assert excinfo.value.http_status == 413
    assert excinfo.value.error_code == "CONTENT_TOO_LARGE"


def test_g_a_normal_small_zip_still_passes_the_guard(monkeypatch):
    """The guard must not reject an ordinary archive — it should fail later,
    as a normal corrupted/unsupported DOCX, not as CONTENT_TOO_LARGE."""
    pytest.importorskip("docx")
    small = _zip_bomb(1000)
    with pytest.raises(extraction.UnsupportedContentError) as excinfo:
        extraction.extract_chunks(".docx", small)
    assert excinfo.value.error_code != "CONTENT_TOO_LARGE"


# ---------------------------------------------------------------------------
# H. No LLM / job machinery reachable from this module
# ---------------------------------------------------------------------------
def test_h_service_module_imports_no_llm_or_job_machinery():
    """A rejected input must be refused before any expensive machinery is
    touched. Source-level assertion so a future edit cannot reintroduce it
    silently."""
    forbidden_import = re.compile(
        r"^\s*(?:from|import)\s+.*(?:ao_extractor|llm_client|scoring_engine|rag_manager|"
        r"capacity_analyzer|company_enrichment|document_generator|anthropic|openai|mistralai|"
        r"src\.web\.jobs|src\.agents|src\.rag|src\.livrables)",
        re.MULTILINE,
    )
    offenders = forbidden_import.findall(SERVICE_CODE)
    assert not offenders, f"forbidden import(s) in analyze_input_service.py: {offenders}"


def test_h_service_module_never_instantiates_an_llm_client():
    for call in ("AOExtractor(", "ClaudeClient(", "LLMClient(", "Anthropic(", "start_analysis(", "create_job("):
        assert call not in SERVICE_CODE, f"{call} must not appear in analyze_input_service.py code"


def test_h_service_namespace_exposes_no_llm_attribute():
    for name in ("AOExtractor", "ClaudeClient", "LLMClient", "jobs", "AnalysisPipeline"):
        assert not hasattr(svc, name), f"analyze_input_service unexpectedly exposes {name}"


# ---------------------------------------------------------------------------
# I. No temp files / descriptors leaked
# ---------------------------------------------------------------------------
def test_i_service_uses_no_temporary_file_at_all():
    """The implementation is bytes-in/text-out via extraction.extract_chunks,
    so there is no temp file to leak on any path — unlike the route code it
    replaces (tempfile.NamedTemporaryFile + finally: os.unlink)."""
    for token in ("tempfile", "NamedTemporaryFile", "mkstemp", "os.unlink"):
        assert token not in SERVICE_CODE, f"{token} must not appear in analyze_input_service.py code"


def test_i_temp_directory_is_untouched_on_success_and_on_failure(tmp_path, monkeypatch):
    """Belt and braces: point every tempfile default at an empty directory and
    confirm nothing lands there across a success and several failure paths."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("TEMP", str(tmp_path))
    monkeypatch.setenv("TMP", str(tmp_path))

    run(svc.resolve_analyze_input(mode="upload", file=FakeUpload("ok.txt", b"Contenu valide.")))
    for kwargs in (
        {"mode": "upload", "file": FakeUpload("vide.txt", b"")},
        {"mode": "upload", "file": FakeUpload("faux.pdf", b"pas un pdf")},
        {"mode": "upload", "file": FakeUpload("corrompu.docx", b"PK\x03\x04sale")},
    ):
        with pytest.raises(svc.AnalyzeInputError):
            run(svc.resolve_analyze_input(**kwargs))

    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# Cross-cutting: every exception carries what the coordinator's handler needs
# ---------------------------------------------------------------------------
def test_every_exception_carries_error_code_message_and_status():
    cases = [
        ({"mode": "bidon"}, 400),
        ({"mode": "stock", "example_id": "inconnu"}, 404),
        ({"mode": "paste", "text": "a" * (config.ANALYZE_MAX_PASTE_CHARS + 1)}, 413),
        ({"mode": "upload", "file": FakeUpload("faux.pdf", b"pas un pdf")}, 415),
        ({"mode": "upload", "file": FakeUpload("vide.txt", b"")}, 422),
    ]
    for kwargs, expected_status in cases:
        with pytest.raises(svc.AnalyzeInputError) as excinfo:
            run(svc.resolve_analyze_input(**kwargs))
        exc = excinfo.value
        assert exc.http_status == expected_status
        assert isinstance(exc.error_code, str) and exc.error_code
        assert isinstance(exc.message, str) and exc.message
        assert exc.error_code.isupper()
