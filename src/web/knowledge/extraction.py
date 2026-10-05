"""Text extraction + chunking for uploaded reference documents.

Reuses the same libraries already vendored for the AO pipeline (PyMuPDF for
PDF, python-docx for DOCX — see src/agents/ao_extractor.py::read_document)
rather than adding a new dependency, but preserves page/paragraph
boundaries for chunking, which read_document's flattened string does not.

No OCR, no LLM call here — a scanned PDF with no extractable text yields
extraction_status='failed', error_code='OCR_REQUIRED', zero chunks. Never
"succeeds" with empty content (ticket B03 section 6).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from src.core import config

SUPPORTED_SUFFIXES = {".md", ".txt", ".pdf", ".docx"}


class UnsupportedContentError(Exception):
    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code


@dataclass
class ExtractedChunk:
    content: str
    page_number: int | None = None
    section: str | None = None


def detect_suffix(filename: str, raw: bytes) -> str:
    """Extension-based detection, cross-checked against a minimal magic-byte
    sniff for the two binary formats — a renamed .txt claiming to be .pdf
    (or vice versa) is rejected rather than mis-parsed."""
    from pathlib import Path

    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise UnsupportedContentError("UNSUPPORTED_CONTENT", f"Format non supporté : {suffix or '(aucune extension)'}")
    if suffix == ".pdf" and not raw.startswith(b"%PDF-"):
        raise UnsupportedContentError("UNSUPPORTED_CONTENT", "Le fichier ne commence pas par un en-tête PDF valide.")
    if suffix == ".docx" and not raw.startswith(b"PK"):
        raise UnsupportedContentError("UNSUPPORTED_CONTENT", "Le fichier ne commence pas par une signature ZIP/DOCX valide.")
    return suffix


def extract_chunks(suffix: str, raw: "bytes | Path") -> list[ExtractedChunk]:
    """`raw` is the file's bytes OR (lot 47 bis, for files of up to 100 Mo that must not be read
    into memory in one block) the Path of a private, already-validated copy: PyMuPDF and
    python-docx open it from disk, the caps and guards below are the same."""
    if suffix in (".md", ".txt"):
        return _extract_text(raw)
    if suffix == ".pdf":
        return _extract_pdf(raw)
    if suffix == ".docx":
        return _extract_docx(raw)
    raise UnsupportedContentError("UNSUPPORTED_CONTENT", f"Format non supporté : {suffix}")


def _split_paragraphs(text: str) -> list[str]:
    return [p.strip() for p in text.replace("\r\n", "\n").split("\n\n") if p.strip()]


def _extract_text(raw: "bytes | Path") -> list[ExtractedChunk]:
    if isinstance(raw, Path):
        # A UTF-8 character is at most 4 bytes: a file larger than 4x the character cap cannot fit
        # under it — refused explicitly WITHOUT reading it (never a silent truncation).
        if raw.stat().st_size > config.KNOWLEDGE_MAX_EXTRACTED_CHARS * 4:
            raise UnsupportedContentError("CONTENT_TOO_LARGE", "Texte extrait trop volumineux.")
        raw = raw.read_bytes()
    text = raw.decode("utf-8", errors="ignore")
    if len(text) > config.KNOWLEDGE_MAX_EXTRACTED_CHARS:
        raise UnsupportedContentError("CONTENT_TOO_LARGE", "Texte extrait trop volumineux.")
    paragraphs = _split_paragraphs(text)
    if not paragraphs:
        raise UnsupportedContentError("EMPTY_CONTENT", "Aucun texte exploitable dans ce fichier.")
    return [ExtractedChunk(content=p) for p in paragraphs]


def _extract_pdf(raw: "bytes | Path") -> list[ExtractedChunk]:
    import fitz  # PyMuPDF — already a project dependency

    try:
        doc = fitz.open(stream=raw, filetype="pdf") if not isinstance(raw, Path) else fitz.open(str(raw), filetype="pdf")
    except Exception as exc:
        raise UnsupportedContentError("CORRUPTED_FILE", f"PDF illisible : {exc}") from exc

    try:
        return _pdf_chunks(doc)
    finally:
        doc.close()  # a path-based document holds the file open (matters for cleanup on Windows)


def _pdf_chunks(doc) -> list[ExtractedChunk]:
    if doc.page_count > config.KNOWLEDGE_MAX_PDF_PAGES:
        raise UnsupportedContentError("CONTENT_TOO_LARGE", f"PDF de {doc.page_count} pages, limite {config.KNOWLEDGE_MAX_PDF_PAGES}.")

    chunks: list[ExtractedChunk] = []
    total_chars = 0
    for page_index in range(doc.page_count):
        page_text = doc[page_index].get_text("text") or ""
        for paragraph in _split_paragraphs(page_text):
            total_chars += len(paragraph)
            if total_chars > config.KNOWLEDGE_MAX_EXTRACTED_CHARS:
                raise UnsupportedContentError("CONTENT_TOO_LARGE", "Texte extrait trop volumineux.")
            chunks.append(ExtractedChunk(content=paragraph, page_number=page_index + 1))

    if not chunks:
        # Every page had zero extractable text — a scanned/image-only PDF.
        raise UnsupportedContentError("OCR_REQUIRED", "Aucun texte extractible — PDF probablement scanné.")
    return chunks


def _extract_docx(raw: "bytes | Path") -> list[ExtractedChunk]:
    import copy
    import io
    import zipfile
    import zlib

    from docx import Document as DocxDocument

    # bytes -> an in-memory stream; a Path -> opened from disk by zipfile / python-docx (lot 47 bis).
    def _source():
        return str(raw) if isinstance(raw, Path) else io.BytesIO(raw)

    # B13-T2 zip-bomb guard (real decompression, not declared metadata).
    # A DOCX is a ZIP archive, and DocxDocument() fully decompresses it into
    # memory BEFORE any of the paragraph/char caps below can fire — so a
    # small-on-disk, enormous-when-decompressed file would exhaust memory
    # first. The B13-T1 guard this replaces summed zf.infolist()'s declared
    # info.file_size — that value lives in the ZIP's own central directory,
    # is attacker-controlled, and zipfile never cross-checks it against what
    # decompression actually produces, so a crafted archive can misstate it.
    # Enforcement here instead streams each entry through zf.open() in
    # bounded chunks (same 256 KiB idiom as
    # analyze_input_service.read_upload_bounded), tracking both the
    # per-entry and the cumulative running total, and aborts the moment
    # either exceeds the configured bound — mid-decompression, never after
    # a full inflate. Entry count is capped first (config.ANALYZE_DOCX_MAX_ZIP_ENTRIES)
    # so a huge number of tiny/empty entries cannot cause excessive looping.
    #
    # Subtlety that makes the naive version of this guard silently wrong:
    # CPython's own zipfile.ZipExtFile._read1 does `data = data[:self._left]`
    # where `self._left` is initialised from `zinfo.file_size` (see
    # zipfile/__init__.py) — i.e. the stdlib's `.read()` API ITSELF silently
    # truncates decompressed output to the declared, attacker-controlled
    # file_size and reports EOF there, never decompressing further. An
    # UNDERSTATED file_size (e.g. 0) would therefore make `.read()` hand
    # back nothing at all for a member that really decompresses to many
    # megabytes — exactly the metadata this guard exists to distrust, still
    # deciding the outcome by the back door. To close that, each entry is
    # opened through a shallow copy of its ZipInfo with `.file_size`
    # overridden to a large sentinel BEFORE calling zf.open() — every other
    # field (header_offset, compress_size, CRC, flag_bits, orig_filename)
    # is untouched, so parsing/CRC-checking behave identically; only the
    # artificial truncation is neutralised, leaving OUR OWN chunked loop
    # (never zipfile's declared-size bookkeeping) as the sole authority on
    # when to stop.
    _DOCX_READ_CHUNK = 1024 * 256
    _NO_DECLARED_SIZE_TRUNCATION = 1 << 62
    max_uncompressed = config.ANALYZE_DOCX_MAX_UNCOMPRESSED_MB * 1024 * 1024

    try:
        with zipfile.ZipFile(_source()) as zf:
            infolist = zf.infolist()
            if len(infolist) > config.ANALYZE_DOCX_MAX_ZIP_ENTRIES:
                raise UnsupportedContentError(
                    "CONTENT_TOO_LARGE",
                    f"Archive DOCX de {len(infolist)} entrées, limite "
                    f"{config.ANALYZE_DOCX_MAX_ZIP_ENTRIES}.",
                )

            cumulative = 0
            for info in infolist:
                open_info = copy.copy(info)
                open_info.file_size = _NO_DECLARED_SIZE_TRUNCATION
                per_entry = 0
                with zf.open(open_info) as member:
                    while True:
                        piece = member.read(_DOCX_READ_CHUNK)
                        if not piece:
                            break
                        per_entry += len(piece)
                        cumulative += len(piece)
                        if per_entry > max_uncompressed or cumulative > max_uncompressed:
                            raise UnsupportedContentError(
                                "CONTENT_TOO_LARGE",
                                f"Archive DOCX décompressée au-delà de la limite de "
                                f"{config.ANALYZE_DOCX_MAX_UNCOMPRESSED_MB} Mio.",
                            )
    except UnsupportedContentError:
        raise
    except (zipfile.BadZipFile, zlib.error, EOFError, OSError) as exc:
        # A .docx that isn't a valid ZIP, or a member whose compressed data
        # is corrupt/truncated (bad CRC, truncated deflate stream, etc.) —
        # an ordinary corrupted file either way, mapped to the same code the
        # DocxDocument() parse below would otherwise raise for it.
        raise UnsupportedContentError(
            "CORRUPTED_FILE", f"DOCX illisible : archive ZIP invalide ou corrompue ({exc})."
        ) from exc

    # Same in-memory `raw` buffer used for both the check above and the
    # parse below — no disk write, no re-fetch, so there is no window where
    # checked content could differ from parsed content.
    try:
        doc = DocxDocument(_source())
    except Exception as exc:
        raise UnsupportedContentError("CORRUPTED_FILE", f"DOCX illisible : {exc}") from exc

    paragraphs = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
    if len(paragraphs) > config.KNOWLEDGE_MAX_DOCX_PARAGRAPHS:
        raise UnsupportedContentError("CONTENT_TOO_LARGE", f"{len(paragraphs)} paragraphes, limite {config.KNOWLEDGE_MAX_DOCX_PARAGRAPHS}.")

    chunks = [ExtractedChunk(content=p) for p in paragraphs]
    total_chars = sum(len(c.content) for c in chunks)
    if total_chars > config.KNOWLEDGE_MAX_EXTRACTED_CHARS:
        raise UnsupportedContentError("CONTENT_TOO_LARGE", "Texte extrait trop volumineux.")

    for table_index, table in enumerate(doc.tables):
        rows_text = []
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            if cells:
                rows_text.append(" | ".join(cells))
        if rows_text:
            chunks.append(ExtractedChunk(content="\n".join(rows_text), section=f"Tableau {table_index + 1}"))

    if not chunks:
        raise UnsupportedContentError("EMPTY_CONTENT", "Aucun texte exploitable dans ce document.")
    return chunks
