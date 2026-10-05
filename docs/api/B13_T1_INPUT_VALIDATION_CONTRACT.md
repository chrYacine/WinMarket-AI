# B13-T1 — AO analysis input validation contract

`src/web/analyze_input_service.py` validates every AO-analysis input **before**
any costly processing (LLM extraction, RAG, scoring, job creation). Pure
bytes-in / text-out: no DB, no LLM, no temp file.

## Signature

```python
async def resolve_analyze_input(
    *, mode: str,                 # "stock" | "upload" | "paste"
    example_id: str | None = None,
    text: str | None = None,
    file: UploadFile | None = None,
) -> str                          # plain text ready for AOExtractor().extract(...)
```

Upload chunks are joined with `"\n\n"` (the boundary
`extraction._split_paragraphs` splits on). Also exported:
`read_upload_bounded(file, max_bytes) -> bytes` (256 KiB chunks, aborts the
moment the running total exceeds the limit; no declared `size`/Content-Length
is ever trusted).

## Exceptions

All subclass `AnalyzeInputError` and carry `.error_code`, `.message`
(user-safe French, never a raw parser string) and `.http_status`.

| Class | status | `.error_code` |
|---|---|---|
| `InvalidInputError` | 400 | `INVALID_MODE`, `NO_EXAMPLE_SELECTED`, `NO_FILE`, `EMPTY_TEXT` |
| `ExampleNotFoundError` | 404 | `EXAMPLE_NOT_FOUND` |
| `InputTooLargeError` | 413 | `UPLOAD_TOO_LARGE`, `PASTE_TOO_LARGE`, `CONTENT_TOO_LARGE` |
| `UnsupportedFormatError` | 415 | `UNSUPPORTED_CONTENT` (bad extension **or** extension/content mismatch) |
| `UnusableContentError` | 422 | `EMPTY_FILE`, `EMPTY_CONTENT`, `CORRUPTED_FILE`, `OCR_REQUIRED` |

`OCR_REQUIRED` = a scanned/image-only PDF. This is not an OCR product; the
message says so plainly and asks for a text version. Behaviour change vs.
today: a bad format returns **415** (was 400).

## Config constants (`src/core/config.py`, additive)

| Constant | Default | Reasoning |
|---|---|---|
| `ANALYZE_MAX_UPLOAD_MB` | `10` | Same as `KNOWLEDGE_MAX_FILE_SIZE_MB` — same class of input. |
| `ANALYZE_MAX_PASTE_CHARS` | `500_000` | Generous for a real tender; the full untruncated string is stored as `ao.texte_source` and regex-scanned repeatedly. |
| `ANALYZE_MAX_EXTRACTED_CHARS` | `2_000_000` | Same default as `KNOWLEDGE_MAX_EXTRACTED_CHARS`. Not redundant: `extract_chunks` caps the *sum of chunk lengths*, while joining adds `"\n\n"` per boundary, so the forwarded string can exceed what extraction counted. Effective bound = `min()` of the two; keep them aligned. |
| `ANALYZE_DOCX_MAX_UNCOMPRESSED_MB` | `50` | Zip-bomb guard. Far above any real DOCX that could also pass `KNOWLEDGE_MAX_DOCX_PARAGRAPHS` (20k prose paragraphs ≈ single-digit MiB + XML), low enough to cap a malicious one at a bounded allocation. |

## Zip-bomb guard (`src/web/knowledge/extraction.py::_extract_docx`)

Before `DocxDocument(io.BytesIO(raw))`, the raw bytes are opened as a
`zipfile.ZipFile` and `sum(info.file_size for info in zf.infolist())` is
checked against `ANALYZE_DOCX_MAX_UNCOMPRESSED_MB` →
`UnsupportedContentError("CONTENT_TOO_LARGE", ...)`. A `zipfile.BadZipFile`
→ `UnsupportedContentError("CORRUPTED_FILE", ...)`. This also protects the
existing `/api/knowledge/documents` upload path.

## Wiring (coordinator)

Replace the whole inline three-way dispatch in both routes —
`routes_api.py::api_analyze` lines ~71-108 (keep `source_label`, which the
route still builds from `example_id` / `file.filename`) and
`routes_scoring_policy.py::simulate_policy` lines ~358-389 — with:

```python
from src.web import analyze_input_service

try:
    content = await analyze_input_service.resolve_analyze_input(
        mode=mode, example_id=example_id, text=text, file=file,
    )
except analyze_input_service.AnalyzeInputError as exc:
    raise HTTPException(exc.http_status, {"error_code": exc.error_code, "message": exc.message})
```

Call it **before** the capacity / scoring-policy 409 gates so an oversized
body is refused as early as possible. The `tempfile` + `finally: os.unlink`
block and the `read_document` import both disappear: extraction now happens
in memory. `file.filename` still never becomes a filesystem path — only its
suffix is read, to pick a parser.
