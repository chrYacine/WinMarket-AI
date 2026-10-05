"""B13-T1 — validation of AO-analysis inputs BEFORE any costly processing.

Single entry point (`resolve_analyze_input`) for the three-way input
dispatch that `/api/analyze` (routes_api.py::api_analyze) and
`/api/scoring-config/simulate` (routes_scoring_policy.py::simulate_policy)
currently duplicate inline. It takes the raw request inputs and returns the
validated plain-text AO content, or raises a typed `AnalyzeInputError`
carrying the HTTP status the caller should map it to.

Deliberately pure bytes-in / text-out. This module must NEVER import or
call the LLM, RAG, scoring or job machinery: a rejected input has to be
refused before any of that is touched, and
tests/test_b13_t1_input_validation.py asserts that property at the source
level so a future edit cannot quietly reintroduce it.

It also uses no temporary file — unlike the route code it replaces, which
wrote the upload to a NamedTemporaryFile just to hand a path to
ao_extractor.read_document. Everything here stays in memory, so there is no
descriptor or temp file to leak on any failure path.

Upload handling reuses src/web/knowledge/extraction.py (magic-byte format
check, page/paragraph/char caps, and the B13-T1 DOCX zip-bomb guard) rather
than ao_extractor.read_document, which has none of those protections.
"""
from __future__ import annotations

from typing import Any, Optional

from src.core import config
from src.web import examples_service
from src.web.knowledge import extraction

# Matches documents_service.read_upload_with_limit's chunk size.
_UPLOAD_CHUNK_SIZE = 1024 * 256

VALID_MODES = ("stock", "upload", "paste")


# ---------------------------------------------------------------------------
# Exceptions — same idiom as documents_service.DocumentTooLargeError /
# CorpusFullError (a plain Exception subclass per case), plus the
# error_code carried by extraction.UnsupportedContentError.
#
# Every instance exposes .error_code, .message (safe to show a user — never
# a raw internal exception string) and .http_status, so a route needs a
# single handler:
#
#     except analyze_input_service.AnalyzeInputError as exc:
#         raise HTTPException(exc.http_status, {"error_code": exc.error_code,
#                                               "message": exc.message})
# ---------------------------------------------------------------------------
class AnalyzeInputError(Exception):
    """Base class for every rejection produced by this module."""

    http_status: int = 400

    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code
        self.message = message


class InvalidInputError(AnalyzeInputError):
    """Malformed request: unknown mode, missing example id, no file, empty paste."""

    http_status = 400


class ExampleNotFoundError(AnalyzeInputError):
    """Stock mode: the requested example id does not exist."""

    http_status = 404


class InputTooLargeError(AnalyzeInputError):
    """Upload, pasted text or extracted content beyond the configured bound."""

    http_status = 413


class UnsupportedFormatError(AnalyzeInputError):
    """Extension not allowed, or extension/content mismatch (a .pdf that is not a PDF)."""

    http_status = 415


class UnusableContentError(AnalyzeInputError):
    """Empty, corrupted, or producing no exploitable text (e.g. a scanned PDF)."""

    http_status = 422


# extraction.UnsupportedContentError error_code -> (our class, safe message).
# The originating error_code is preserved (notably OCR_REQUIRED) but the
# message is replaced: extraction.py interpolates raw parser exceptions
# (PyMuPDF/python-docx internals) into some of its own messages, which must
# not reach an API client.
_EXTRACTION_ERROR_MAP: dict[str, tuple[type[AnalyzeInputError], str]] = {
    "UNSUPPORTED_CONTENT": (
        UnsupportedFormatError,
        "Format non supporté ou contenu incohérent avec l'extension. "
        "Formats acceptés : PDF, DOCX, TXT, MD.",
    ),
    "CONTENT_TOO_LARGE": (
        InputTooLargeError,
        "Le contenu extrait de ce fichier dépasse la limite autorisée.",
    ),
    "CORRUPTED_FILE": (
        UnusableContentError,
        "Fichier illisible ou corrompu — impossible d'en extraire le texte.",
    ),
    "EMPTY_CONTENT": (
        UnusableContentError,
        "Aucun texte exploitable dans ce fichier.",
    ),
    "OCR_REQUIRED": (
        UnusableContentError,
        "Ce document ne contient aucun texte sélectionnable (PDF scanné ou image). "
        "WinMarket AI ne pratique pas la reconnaissance de caractères : "
        "fournissez une version texte du document (PDF natif, DOCX, TXT).",
    ),
}


async def read_upload_bounded(file: Any, max_bytes: int) -> bytes:
    """Read an UploadFile in bounded chunks, aborting the moment the running
    total exceeds `max_bytes` — the entire body is never buffered first.

    Deliberately a near-identical copy of
    documents_service.read_upload_with_limit (same 256 KiB chunk, same
    abort-mid-stream semantics), raising this module's InputTooLargeError
    instead of DocumentTooLargeError. Copied rather than imported so this
    validation layer stays free of the DB/storage/SQLAlchemy dependency
    graph that documents_service pulls in; keep the two in sync.

    No declared size is trusted: any `size` or Content-Length-like attribute
    on `file` is ignored entirely. Only the bytes actually received count.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        piece = await file.read(_UPLOAD_CHUNK_SIZE)
        if not piece:
            break
        total += len(piece)
        if total > max_bytes:
            raise InputTooLargeError(
                "UPLOAD_TOO_LARGE",
                f"Fichier supérieur à la limite de {max_bytes // (1024 * 1024)} Mio.",
            )
        chunks.append(piece)
    return b"".join(chunks)


async def resolve_analyze_input(
    *,
    mode: str,
    example_id: Optional[str] = None,
    text: Optional[str] = None,
    file: Any = None,
) -> str:
    """Validate one AO-analysis input and return the plain text to analyse.

    Args mirror the three form fields both routes already accept. `file` is
    a fastapi.UploadFile (typed Any so any object exposing `.filename` and
    an awaitable `.read(size)` works, which is what the tests use).

    Returns a plain string ready for `AOExtractor().extract(...)` — for an
    upload, the extracted chunks joined with "\\n\\n", matching the
    paragraph-boundary convention extraction._split_paragraphs splits on.

    Raises AnalyzeInputError (see the subclasses above for the HTTP status).
    """
    if mode == "stock":
        return _resolve_stock(example_id)
    if mode == "upload":
        return await _resolve_upload(file)
    if mode == "paste":
        return _resolve_paste(text)
    raise InvalidInputError("INVALID_MODE", "Mode de source invalide.")


def _resolve_stock(example_id: Optional[str]) -> str:
    """Server-authored fixtures under data/ao_examples (11-18 KiB, vetted at
    build time, not user-supplied) — no size/format validation is added
    here on purpose; it would be pure overhead on trusted content.
    examples_service.read_example already refuses any id that escapes that
    directory."""
    if not example_id:
        raise InvalidInputError("NO_EXAMPLE_SELECTED", "Aucun exemple sélectionné.")
    content = examples_service.read_example(example_id)
    if content is None:
        raise ExampleNotFoundError("EXAMPLE_NOT_FOUND", "Exemple introuvable.")
    return content


def _resolve_paste(text: Optional[str]) -> str:
    if not text or not text.strip():
        raise InvalidInputError("EMPTY_TEXT", "Le texte de l'appel d'offres est vide.")
    # Measured on the raw, untruncated string: that is what gets stored as
    # ao.texte_source and re-scanned by the scoring regexes.
    if len(text) > config.ANALYZE_MAX_PASTE_CHARS:
        raise InputTooLargeError(
            "PASTE_TOO_LARGE",
            f"Texte trop long ({len(text)} caractères) — limite "
            f"{config.ANALYZE_MAX_PASTE_CHARS} caractères.",
        )
    return text


async def _resolve_upload(file: Any) -> str:
    if file is None or not getattr(file, "filename", None):
        raise InvalidInputError("NO_FILE", "Aucun fichier fourni.")

    # Bounded read FIRST: an oversized upload never reaches the parser,
    # whatever its content.
    max_bytes = config.ANALYZE_MAX_UPLOAD_MB * 1024 * 1024
    raw = await read_upload_bounded(file, max_bytes)

    if not raw:
        raise UnusableContentError("EMPTY_FILE", "Le fichier fourni est vide.")

    try:
        # Only the suffix of the filename is ever used, and only to select a
        # parser — never to build a filesystem path (B13-T1 point 5).
        suffix = extraction.detect_suffix(file.filename, raw)
        chunks = extraction.extract_chunks(suffix, raw)
    except extraction.UnsupportedContentError as exc:
        raise _translate_extraction_error(exc) from exc

    content = "\n\n".join(chunk.content for chunk in chunks)
    if not content.strip():
        raise UnusableContentError("EMPTY_CONTENT", "Aucun texte exploitable dans ce fichier.")

    # extract_chunks caps the SUM of chunk lengths; the joined string adds a
    # separator per boundary, so bound what we actually forward.
    if len(content) > config.ANALYZE_MAX_EXTRACTED_CHARS:
        raise InputTooLargeError(
            "CONTENT_TOO_LARGE",
            "Le contenu extrait de ce fichier dépasse la limite autorisée.",
        )
    return content


def _translate_extraction_error(exc: extraction.UnsupportedContentError) -> AnalyzeInputError:
    error_class, message = _EXTRACTION_ERROR_MAP.get(
        exc.error_code,
        (UnusableContentError, "Fichier inexploitable — impossible d'en extraire le texte."),
    )
    return error_class(exc.error_code, message)
