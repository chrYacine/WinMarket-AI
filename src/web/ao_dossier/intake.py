"""Reception and validation of a WHOLE dossier, before any job exists.

Order (each step can refuse the entire submission, and a refusal always designates what is wrong):
1. structure — allowed categories only, one file per main slot, at most DOSSIER_MAX_ANNEXES annexes, at
   least one piece (no file is ever ignored silently);
2. bytes — each piece is streamed to private storage under ONE running byte budget (the SUM of the bytes
   actually received; equality allowed, one byte more refused with 413);
3. per piece — non-empty, supported format cross-checked against its magic bytes, readable, not a scan
   (no OCR), no instruction addressed to an AI assistant;
4. cumulative extracted text — bounded (413), never truncated;
5. duplicates — a byte-identical repeat is flagged and not read twice; the same name with different
   content stays a distinct piece.
Any failure removes what this call wrote (its own dossier directory only). Nothing here reads the scoring
policy or produces a score. Lot 50 bis §1: an optional `llm` (the account's configured `LLMClient`, the SAME
adapter used everywhere else) may be threaded through to the classifier/moderator/security agents for a real
semantic judgment — never required (`llm=None` keeps every existing, heuristic-only behavior byte-for-byte
unchanged, which is also exactly what happens whenever no provider is configured or enabled)."""
from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Optional

from src.agents.document_classifier_agent import DocumentClassifierAgent
from src.agents.document_moderator_agent import DocumentModeratorAgent
from src.agents.document_security_agent import DocumentSecurityAgent
from src.core import config
from src.core.content_preparation import ContentPreparer
from src.core.content_security import ContentSecurityGate
from src.web import analyze_input_service
from src.web.ao_dossier import limits, storage
from src.web.ao_dossier.scope import assess_dossier_scope
from src.web.knowledge import extraction

_UNSAFE_NAME_CHARS = re.compile(r'[\x00-\x1f\x7f"<>:*?|\\/]')
_MAX_NAME_CHARS = 120
LEGACY_FILE_FIELD = "file"


class DossierError(analyze_input_service.AnalyzeInputError):
    """A refusal with a structured body: `error_code`, `message` and optional `details` (which piece, which
    category, the limit...). The HTTP status is per instance."""

    def __init__(self, error_code: str, message: str, *, http_status: int = 400, **details: Any):
        super().__init__(error_code, message)
        self.http_status = http_status
        self.details = details


def is_file_like(value: Any) -> bool:
    return hasattr(value, "filename") and hasattr(value, "read")


def _named_files(items: Iterable[tuple[str, Any]], keys: Optional[Iterable[str]] = None) -> list[tuple[str, Any]]:
    wanted = set(keys) if keys is not None else None
    return [(k, v) for k, v in items if is_file_like(v) and (v.filename or "") != "" and (wanted is None or k in wanted)]


def dossier_files_present(items: list[tuple[str, Any]]) -> bool:
    return bool(_named_files(items, limits.FIELD_TO_CATEGORY))


def legacy_input_present(items: list[tuple[str, Any]], *, text: Optional[str], example_id: Optional[str]) -> bool:
    return bool(_named_files(items, {LEGACY_FILE_FIELD})) or bool(text and text.strip()) or bool(example_id)


def check_no_stray_files(items: list[tuple[str, Any]]) -> None:
    """Every file-like part must be one the API knows about; an unknown category is refused, never ignored."""
    known = set(limits.FIELD_TO_CATEGORY) | {LEGACY_FILE_FIELD}
    for key, value in _named_files(items):
        if key not in known:
            raise DossierError("UNKNOWN_CATEGORY", "Catégorie de pièce inconnue : les catégories acceptées sont rc, cctp, ccap, acte_engagement et annexes.",
                               field=_safe_label(key))
    if len(_named_files(items, {LEGACY_FILE_FIELD})) > 1:
        raise DossierError("TOO_MANY_FILES", "Le mode fichier unique n'accepte qu'un fichier ; utilisez le mode dossier pour plusieurs pièces.")


@dataclass
class PlannedPiece:
    field: str
    category: str
    upload: Any


def plan_dossier(items: list[tuple[str, Any]]) -> list[PlannedPiece]:
    """Structure validation only (no byte is read). Deterministic order: rc, cctp, ccap, acte_engagement, then
    the annexes in the order received — independent of the order the parts arrived in."""
    check_no_stray_files(items)
    for key, value in items:
        if key in limits.FIELD_TO_CATEGORY and not is_file_like(value):
            raise DossierError("INVALID_PIECE", "Une pièce doit être envoyée comme fichier.", field=_safe_label(key))
    planned: list[PlannedPiece] = []
    for slot in limits.SLOTS:
        files = [v for k, v in _named_files(items, {slot["field"]})]
        if slot["field"] not in ("annexes", "autres"):
            if len(files) > 1:
                raise DossierError("TOO_MANY_FILES_FOR_SLOT", f"{slot['label']} : un seul fichier est accepté.", category=slot["category"], field=slot["field"], max_files=1)
        elif slot["field"] == "annexes" and len(files) > config.DOSSIER_MAX_ANNEXES:
            raise DossierError("TOO_MANY_ANNEXES", f"Annexes : {config.DOSSIER_MAX_ANNEXES} fichiers au maximum.", category="annexe", field="annexes",
                               max_files=config.DOSSIER_MAX_ANNEXES)
        # "autres" (lot 50 §1) has no sub-limit of its own — only the dossier's total DOSSIER_MAX_FILES applies
        # (checked below), so a free-form piece never gets an allocation beyond what a guided one would.
        planned.extend(PlannedPiece(slot["field"], slot["category"], f) for f in files)
    if not planned:
        raise DossierError("DOSSIER_EMPTY", "Ajoutez au moins une pièce au dossier d'appel d'offres.")
    if len(planned) > config.DOSSIER_MAX_FILES:  # cannot happen when the slots are checked above; kept as a backstop
        raise DossierError("TOO_MANY_FILES", f"{config.DOSSIER_MAX_FILES} fichiers au maximum par dossier.", max_files=config.DOSSIER_MAX_FILES)
    return planned


def _safe_label(text: str) -> str:
    return _UNSAFE_NAME_CHARS.sub("_", str(text))[:40]


def safe_display_name(filename: Optional[str], suffix: str) -> str:
    """A display-safe name: the last path component only, no control character / quote / angle bracket / colon /
    wildcard / pipe, bounded length, and the VALIDATED suffix (never a suffix taken from the text)."""
    last = re.split(r"[\\/]", filename or "")[-1]
    stem = last.rsplit(".", 1)[0] if "." in last else last
    stem = re.sub(r"\s+", " ", _UNSAFE_NAME_CHARS.sub("_", stem)).strip(" .")[:_MAX_NAME_CHARS]
    return (stem or "piece") + suffix


@dataclass
class IntakePiece:
    id: uuid.UUID
    category: str
    display_name: str
    file_format: str
    size_bytes: int
    content_hash: str
    storage_key: str
    text_storage_key: Optional[str] = None
    page_count: Optional[int] = None
    char_count: int = 0
    chunk_count: int = 0
    duplicate_of_piece_id: Optional[uuid.UUID] = None
    # Lot 50 (§2/§3 admission manifest) — populated by the security/classifier/moderator agents (see
    # document_security_agent.py / document_classifier_agent.py / document_moderator_agent.py). Advisory
    # metadata only for the LEGACY direct-submit path (`receive_dossier`): nothing here EXCLUDES a piece
    # there — only the preview/confirm path (`receive_dossier_preview` + the confirm route) lets a
    # `moderation_verdict` or a `to_verify` security state change what is actually admitted.
    category_proposed: Optional[str] = None
    category_final: Optional[str] = None
    security_state: str = "authorized"
    security_code: Optional[str] = None
    security_reason: Optional[str] = None
    moderation_verdict: Optional[str] = None
    moderation_reason: Optional[str] = None
    admitted: bool = True
    exclusion_reason: Optional[str] = None
    user_link_note: Optional[str] = None
    # Lot 50 bis §1 — WHICH judgment path actually produced `category_proposed`/`moderation_verdict`/the
    # security reasoning: "heuristic" (no LLM call attempted or applicable), "llm" (a real, citation-verified
    # LLM answer), "heuristic_llm_unavailable" (no provider configured/enabled/reachable — fell back) or
    # "heuristic_llm_invalid" (the provider answered but the shape/enum/citation could not be trusted — fell
    # back). Never invented after the fact: set exactly once, at the same time as the field it describes.
    classification_source: str = "heuristic"
    moderation_source: str = "heuristic"
    security_review_source: str = "heuristic"

    def __post_init__(self) -> None:
        if self.category_final is None:
            self.category_final = self.category

    def row(self) -> dict[str, Any]:
        return {
            "id": self.id, "category": self.category, "display_name": self.display_name, "file_format": self.file_format,
            "size_bytes": self.size_bytes, "content_hash": self.content_hash, "storage_key": self.storage_key,
            "text_storage_key": self.text_storage_key, "page_count": self.page_count, "char_count": self.char_count,
            "chunk_count": self.chunk_count, "duplicate_of_piece_id": self.duplicate_of_piece_id,
            "category_proposed": self.category_proposed, "category_final": self.category_final,
            "security_state": self.security_state, "security_code": self.security_code, "security_reason": self.security_reason,
            "moderation_verdict": self.moderation_verdict, "moderation_reason": self.moderation_reason,
            "admitted": self.admitted, "exclusion_reason": self.exclusion_reason, "user_link_note": self.user_link_note,
            "classification_source": self.classification_source, "moderation_source": self.moderation_source,
            "security_review_source": self.security_review_source,
        }


@dataclass
class ReceivedDossier:
    dossier_id: uuid.UUID
    organization_id: uuid.UUID
    user_id: uuid.UUID
    pieces: list[IntakePiece] = field(default_factory=list)
    total_bytes: int = 0
    total_chars: int = 0
    texts: list[str] = field(default_factory=list)  # the prepared text of each READ piece (never stored twice, never persisted here)
    pieces_with_text: list[IntakePiece] = field(default_factory=list)  # parallel to `texts` (lot 50 §2.C moderation)

    def discard(self) -> None:
        storage.remove_dossier_dir(self.organization_id, self.user_id, self.dossier_id)


def _piece_error(planned: PlannedPiece, name: str, error_code: str, message: str) -> dict[str, Any]:
    return {"category": planned.category, "category_label": limits.CATEGORY_LABEL[planned.category], "piece": name,
            "error_code": error_code, "message": message}


def _attach_moderation(received: "ReceivedDossier", *, llm=None) -> None:
    """Lot 50 §2.C — each piece's relevance is judged against the OTHERS' text (never itself, never a
    2-keyword gate — see document_moderator_agent.py). Advisory only here (never excludes a piece by
    itself): only the preview/confirm path lets a 'hors_sujet' verdict change what is actually admitted."""
    moderator = DocumentModeratorAgent()
    texts = received.texts
    for i, piece in enumerate(received.pieces_with_text):
        others = "\n\n".join(t for j, t in enumerate(texts) if j != i)
        result = moderator.assess_relevance(texts[i], other_pieces_text=others, llm=llm)
        piece.moderation_verdict, piece.moderation_reason, piece.moderation_source = result.verdict, result.reason, result.source


async def receive_dossier(items: list[tuple[str, Any]], *, organization_id: uuid.UUID, user_id: uuid.UUID, llm=None) -> ReceivedDossier:
    """Validate and store a whole dossier. Returns it (its files stay in private storage until the caller commits
    the rows or calls `discard()`), or raises `DossierError` after removing everything this call wrote."""
    planned = plan_dossier(items)
    received = ReceivedDossier(dossier_id=uuid.uuid4(), organization_id=organization_id, user_id=user_id)
    directory = storage.dossier_dir(organization_id, user_id, received.dossier_id)
    budget = storage.ByteBudget(config.DOSSIER_MAX_TOTAL_BYTES)
    problems: list[dict[str, Any]] = []
    first_by_hash: dict[str, uuid.UUID] = {}
    preparer, gate = ContentPreparer(), ContentSecurityGate()
    try:
        for planned_piece in planned:
            piece = await _receive_piece(planned_piece, directory, budget, received, first_by_hash, problems, preparer, gate, llm=llm)
            if piece is not None:
                received.pieces.append(piece)
        if problems:
            raise DossierError(
                "DOSSIER_PIECE_INVALID",
                "Le dossier est refusé : " + "; ".join(f"{p['category_label']} « {p['piece']} » — {p['message']}" for p in problems),
                http_status=422, pieces=problems,
            )
        _attach_moderation(received, llm=llm)
        # Lot 50 ter §2: the lexical "≥2 market terms" heuristic is a FALLBACK signal here, never a veto
        # against a piece the moderator already judged relevant (shared client/site/reference/lot, or a real
        # LLM reading) — see src/web/ao_dossier/scope.py for the full rationale. Injection detection is
        # untouched and stays an absolute block regardless of any piece's relevance.
        verdict = assess_dossier_scope("\n\n".join(received.texts), received.pieces_with_text)
        if not verdict.allowed:
            raise DossierError(
                "CONTENT_BLOCKED",
                "Le contenu du dossier n'a pas passé les contrôles de sécurité : aucune pièce n'a été reconnue "
                "comme liée à un appel d'offres (ni par son vocabulaire, ni par un élément commun avec les autres "
                "pièces), ou une instruction parasite a été détectée.",
                http_status=422, reasons=list(verdict.reason_codes),
            )
        received.total_bytes = budget.used
        return received
    except BaseException:
        received.discard()  # this call's own directory only
        raise


async def _receive_piece(planned: PlannedPiece, directory: Path, budget: storage.ByteBudget, received: ReceivedDossier,
                         first_by_hash: dict[str, uuid.UUID], problems: list[dict[str, Any]], preparer: ContentPreparer,
                         gate: ContentSecurityGate, *, llm=None) -> Optional[IntakePiece]:
    upload = planned.upload
    original_name = upload.filename or ""
    suffix = PurePosixPath(re.split(r"[\\/]", original_name)[-1]).suffix.lower()
    label = safe_display_name(original_name, suffix)  # what the user recognises: "cctp.pdf", never a path
    if suffix not in extraction.SUPPORTED_SUFFIXES:
        problems.append(_piece_error(planned, label, "UNSUPPORTED_CONTENT", "Format non supporté. Formats acceptés : PDF, DOCX, TXT, MD."))
        return None

    piece_id = uuid.uuid4()
    target = directory / f"{piece_id.hex}{suffix}"  # the file name on disk never comes from the user
    try:
        size, digest, head = await storage.stream_to_file(upload, target, budget)
    except storage.BudgetExceeded as exc:
        raise DossierError(
            "DOSSIER_TOO_LARGE",
            f"Le dossier dépasse la limite de {limits.dossier_limits()['max_total_label']} (somme des fichiers reçus).",
            http_status=413, limit_bytes=exc.limit, piece=label, category=planned.category,
        ) from exc
    if size == 0:
        target.unlink(missing_ok=True)
        problems.append(_piece_error(planned, label, "EMPTY_FILE", "Le fichier fourni est vide."))
        return None
    try:
        extraction.detect_suffix(original_name, head)
    except extraction.UnsupportedContentError as exc:
        problems.append(_piece_error(planned, label, *_error_pair(exc)))
        return None

    piece = IntakePiece(
        id=piece_id, category=planned.category, display_name=safe_display_name(original_name, suffix), file_format=suffix,
        size_bytes=size, content_hash=digest, storage_key=storage.relative_key(target),
    )
    if digest in first_by_hash:
        # A byte-identical repeat: signalled, kept as a row, NOT read a second time (its text would be counted twice).
        piece.duplicate_of_piece_id = first_by_hash[digest]
        return piece
    try:
        chunks = extraction.extract_chunks(suffix, target)
    except extraction.UnsupportedContentError as exc:
        problems.append(_piece_error(planned, label, *_error_pair(exc)))
        return None

    prepared = [(preparer.prepare(c.content).text, c) for c in chunks]
    prepared = [(text, c) for text, c in prepared if text.strip()]
    if not prepared:
        problems.append(_piece_error(planned, label, "EMPTY_CONTENT", "Aucun texte exploitable dans ce fichier."))
        return None
    joined = "\n\n".join(text for text, _ in prepared)
    # Lot 50 §2.A: the SAME underlying pattern as before (ContentSecurityGate's injection regex) — the tri-state
    # agent's 'blocked' is exactly the old hard-reject condition, so this is not a behaviour change for the
    # existing (legacy, no-confirmation-step) direct-submit path; its 'to_verify'/'authorized' states are pure,
    # new, advisory metadata (never gating here — only the preview/confirm path of §3 lets them change admission).
    security = DocumentSecurityAgent(gate).assess(joined, display_name=label, llm=llm)
    if security.state == "blocked":
        problems.append(_piece_error(planned, label, "CONTENT_BLOCKED", "Ce fichier contient des instructions adressées à un assistant IA : il est refusé."))
        return None
    piece.security_state, piece.security_code, piece.security_reason = security.state, security.code, security.reason
    piece.security_review_source = security.source
    classification = DocumentClassifierAgent().classify(joined, filename=original_name, declared_category=planned.category, llm=llm)
    piece.category_proposed = classification.category_proposed
    piece.classification_source = classification.source

    received.total_chars += len(joined)
    received.texts.append(joined)
    received.pieces_with_text.append(piece)
    if received.total_chars > config.DOSSIER_MAX_EXTRACTED_CHARS:
        limit_text = f"{config.DOSSIER_MAX_EXTRACTED_CHARS:,}".replace(",", " ")
        raise DossierError(
            "DOSSIER_TEXT_TOO_LARGE",
            f"Le texte cumulé du dossier dépasse {limit_text} caractères analysables (dépassement atteint avec « {label} ») : "
            "allégez le dossier. Aucun texte n'est tronqué en silence.",
            http_status=413, limit_chars=config.DOSSIER_MAX_EXTRACTED_CHARS, piece=label, category=planned.category,
        )
    offset, records = 0, []
    for text, chunk in prepared:
        records.append({"content": text, "page": chunk.page_number, "section": chunk.section, "start": offset, "end": offset + len(text)})
        offset += len(text) + 2
    pages = [r["page"] for r in records if r["page"] is not None]
    text_path = directory / f"{piece_id.hex}.chunks.json"
    storage.write_json(text_path, {"chunks": records})
    piece.text_storage_key = storage.relative_key(text_path)
    piece.char_count, piece.chunk_count = len(joined), len(records)
    piece.page_count = max(pages) if pages else None
    first_by_hash[digest] = piece_id
    return piece


def _error_pair(exc: extraction.UnsupportedContentError) -> tuple[str, str]:
    translated = analyze_input_service._translate_extraction_error(exc)
    return translated.error_code, translated.message


# ---------------------------------------------------------------------------
# Lot 50 §3 — preview/admission staging: TOLERANT of a single bad/blocked/irrelevant piece (a structural
# problem — wrong slot, too many files, the total byte budget — still refuses the WHOLE submission outright,
# same as `receive_dossier`; nothing here relaxes those). A piece-level problem no longer aborts everything:
# it is recorded as an EXCLUDED-BY-DEFAULT row (never silently dropped, §3) that the user reviews and either
# confirms excluded or, where legitimate (an unresolved 'to_verify'/'hors_sujet', never a security 'blocked'
# one), overrides at confirmation. Structurally UNSUPPORTED formats are never even stored (exactly like
# `receive_dossier` — the suffix is rejected before any byte is streamed) and so cannot become a row; they
# are reported back as plain dicts, informational only, nothing to confirm later.
# ---------------------------------------------------------------------------

@dataclass
class ReceivedPreview:
    dossier_id: uuid.UUID
    organization_id: uuid.UUID
    user_id: uuid.UUID
    pieces: list[IntakePiece] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)  # unsupported formats only — never stored, nothing to confirm
    total_bytes: int = 0

    def discard(self) -> None:
        storage.remove_dossier_dir(self.organization_id, self.user_id, self.dossier_id)


async def receive_dossier_preview(
    items: list[tuple[str, Any]], *, organization_id: uuid.UUID, user_id: uuid.UUID, llm=None,
    carried_over: Optional[list["IntakePiece"]] = None,
) -> ReceivedPreview:
    """Structural validation stays exactly as strict as `receive_dossier` (same `plan_dossier`, same total byte
    budget) — only what happens to an INDIVIDUAL piece once its bytes are safely stored changes: a technical
    read failure, a security concern or a moderation doubt becomes a row the user reviews, never an immediate
    422 for the whole dossier. Returns the preview (files already in private storage; the caller persists a
    'staging' `AoDossier` row over it, or calls `discard()` on any later failure).

    Lot 50 bis §3 ("Ajouter les pièces restantes"): `carried_over` (optional) are ALREADY-STORED, already
    hash-verified pieces of an EXISTING dossier being extended — their bytes are never re-uploaded (they still
    live under the original dossier's own storage directory; only the reference is reused, never copied) but
    they count toward the SAME shared byte/char budgets as the new pieces, and are RE-vetted (security,
    classification, and — crucially — relevance against the now-COMBINED set of old and new pieces together)
    exactly like a freshly-uploaded piece would be. `items` may legitimately be empty here (adding zero new
    files, only dropping some old ones) as long as at least one carried-over piece exists."""
    carried_over = list(carried_over or [])
    # `items` may still contain non-file form fields (e.g. `keep_piece_ids` on the add-pieces route) even when
    # zero files were uploaded — a raw `bool(items)` would wrongly be True then and send an all-carried-over,
    # no-new-file request through `plan_dossier` (which only sees files and would raise DOSSIER_EMPTY).
    if dossier_files_present(items) or not carried_over:
        planned = plan_dossier(items)
    else:
        check_no_stray_files(items)
        planned = []
    received = ReceivedPreview(dossier_id=uuid.uuid4(), organization_id=organization_id, user_id=user_id)
    directory = storage.dossier_dir(organization_id, user_id, received.dossier_id)
    budget = storage.ByteBudget(config.DOSSIER_MAX_TOTAL_BYTES)
    first_by_hash: dict[str, uuid.UUID] = {}
    preparer, gate = ContentPreparer(), ContentSecurityGate()
    texts_by_piece: dict[uuid.UUID, str] = {}
    total_chars = 0

    try:
        for piece in carried_over:
            budget.used += piece.size_bytes
            if budget.used > budget.limit:
                raise DossierError(
                    "DOSSIER_TOO_LARGE",
                    f"Le dossier dépasse la limite de {limits.dossier_limits()['max_total_label']} (somme des fichiers, pièces reprises incluses).",
                    http_status=413, limit_bytes=budget.limit, piece=piece.display_name, category=piece.category,
                )
            first_by_hash.setdefault(piece.content_hash, piece.id)
            # Reset any PRIOR admission decision — this is a fresh table for a fresh combined set, never a
            # stale carry-over of "was excluded last time" without a fresh reason.
            piece.admitted, piece.exclusion_reason = True, None
            piece.category_final = piece.category
            if piece.duplicate_of_piece_id or not piece.text_storage_key:
                received.pieces.append(piece)
                continue
            try:
                chunks = storage.read_json(piece.text_storage_key)["chunks"]
                joined = "\n\n".join(c["content"] for c in chunks)
            except Exception:
                # The original text file is gone/unreadable even though the piece itself re-verified moments
                # ago (routes_api.py) — never reconstructed: excluded, with an honest reason.
                piece.admitted, piece.exclusion_reason = False, "Le texte de cette pièce n'a pas pu être relu : non reprise."
                received.pieces.append(piece)
                continue
            security = DocumentSecurityAgent(gate).assess(joined, display_name=piece.display_name, llm=llm)
            piece.security_state, piece.security_code, piece.security_reason = security.state, security.code, security.reason
            piece.security_review_source = security.source
            classification = DocumentClassifierAgent().classify(joined, filename=piece.display_name, declared_category=piece.category, llm=llm)
            piece.category_proposed = classification.category_proposed
            piece.classification_source = classification.source
            if security.state == "blocked":
                piece.admitted, piece.exclusion_reason = False, security.reason
            texts_by_piece[piece.id] = joined
            total_chars += len(joined)
            received.pieces.append(piece)
        for planned_piece in planned:
            upload = planned_piece.upload
            original_name = upload.filename or ""
            suffix = PurePosixPath(re.split(r"[\\/]", original_name)[-1]).suffix.lower()
            label = safe_display_name(original_name, suffix)
            if suffix not in extraction.SUPPORTED_SUFFIXES:
                # No file is stored for a format we do not even attempt to read — nothing to confirm later,
                # only reported so the user understands why it is absent from the table (never silent).
                received.rejected.append(_piece_error(planned_piece, label, "UNSUPPORTED_CONTENT", "Format non supporté. Formats acceptés : PDF, DOCX, TXT, MD."))
                continue
            piece_id = uuid.uuid4()
            target = directory / f"{piece_id.hex}{suffix}"
            try:
                size, digest, head = await storage.stream_to_file(upload, target, budget)
            except storage.BudgetExceeded as exc:
                raise DossierError(
                    "DOSSIER_TOO_LARGE",
                    f"Le dossier dépasse la limite de {limits.dossier_limits()['max_total_label']} (somme des fichiers reçus).",
                    http_status=413, limit_bytes=exc.limit, piece=label, category=planned_piece.category,
                ) from exc
            piece = IntakePiece(
                id=piece_id, category=planned_piece.category, display_name=safe_display_name(original_name, suffix),
                file_format=suffix, size_bytes=size, content_hash=digest, storage_key=storage.relative_key(target),
            )
            if size == 0:
                target.unlink(missing_ok=True)
                piece.storage_key = ""  # nothing was actually kept — an empty placeholder key, never a dangling one
                piece.admitted, piece.exclusion_reason = False, "Fichier vide : aucun contenu à analyser."
                received.pieces.append(piece)
                continue
            try:
                extraction.detect_suffix(original_name, head)
                chunks = extraction.extract_chunks(suffix, target)
            except extraction.UnsupportedContentError as exc:
                code, message = _error_pair(exc)
                piece.admitted, piece.exclusion_reason = False, message
                received.pieces.append(piece)
                continue
            prepared = [(preparer.prepare(c.content).text, c) for c in chunks]
            prepared = [(t, c) for t, c in prepared if t.strip()]
            if not prepared:
                piece.admitted, piece.exclusion_reason = False, "Aucun texte exploitable dans ce fichier."
                received.pieces.append(piece)
                continue
            joined = "\n\n".join(t for t, _ in prepared)
            if digest in first_by_hash:
                piece.duplicate_of_piece_id = first_by_hash[digest]
                received.pieces.append(piece)
                continue  # a byte-identical repeat is never re-read (its text would be counted/judged twice)
            if total_chars + len(joined) > config.DOSSIER_MAX_EXTRACTED_CHARS:
                piece.admitted = False
                piece.exclusion_reason = "Le texte cumulé du dossier dépasserait la limite analysable avec cette pièce : exclue par défaut."
                received.pieces.append(piece)
                continue
            security = DocumentSecurityAgent(gate).assess(joined, display_name=label, llm=llm)
            piece.security_state, piece.security_code, piece.security_reason = security.state, security.code, security.reason
            piece.security_review_source = security.source
            classification = DocumentClassifierAgent().classify(joined, filename=original_name, declared_category=planned_piece.category, llm=llm)
            piece.category_proposed = classification.category_proposed
            piece.classification_source = classification.source
            offset, records = 0, []
            for t, chunk in prepared:
                records.append({"content": t, "page": chunk.page_number, "section": chunk.section, "start": offset, "end": offset + len(t)})
                offset += len(t) + 2
            pages = [r["page"] for r in records if r["page"] is not None]
            text_path = directory / f"{piece_id.hex}.chunks.json"
            storage.write_json(text_path, {"chunks": records})
            piece.text_storage_key = storage.relative_key(text_path)
            piece.char_count, piece.chunk_count = len(joined), len(records)
            piece.page_count = max(pages) if pages else None
            first_by_hash[digest] = piece_id
            total_chars += len(joined)
            texts_by_piece[piece.id] = joined
            if security.state == "blocked":
                piece.admitted, piece.exclusion_reason = False, security.reason
            received.pieces.append(piece)

        moderator = DocumentModeratorAgent()
        for piece in received.pieces:
            text = texts_by_piece.get(piece.id)
            if text is None:
                continue
            others = "\n\n".join(t for pid, t in texts_by_piece.items() if pid != piece.id)
            result = moderator.assess_relevance(text, other_pieces_text=others, llm=llm)
            piece.moderation_verdict, piece.moderation_reason, piece.moderation_source = result.verdict, result.reason, result.source
            if result.verdict == "hors_sujet" and piece.admitted:
                piece.admitted = False
                piece.exclusion_reason = "Proposition d'exclusion (pertinence incertaine) : " + result.reason

        received.total_bytes = budget.used
        return received
    except BaseException:
        received.discard()
        raise


def reverify_pieces_for_carry_over(original_pieces: Iterable[Any]) -> tuple[list[IntakePiece], list[dict[str, Any]]]:
    """Lot 50 bis §3 ("Ajouter les pièces restantes") — for each ADMITTED piece of an existing, already-
    validated dossier (an ORM `AoDossierPiece` row or anything with the same attributes — this module never
    imports the ORM itself, staying a plain service layer), re-verifies its stored file STILL exists and
    still hashes to the RECORDED `content_hash` before it may be reused. A piece that fails either check is
    NEVER reconstructed or guessed at — it is reported as excluded, with an honest reason, and the caller
    must ask the user to re-supply it if it is still needed. A duplicate/non-admitted original piece is
    skipped entirely (nothing useful to carry: a duplicate has no text of its own, and a piece the account
    already excluded is not silently re-admitted here).

    Returns (carried_over `IntakePiece` list — position/manifest fields deliberately RESET to sensible
    "not yet re-decided" defaults by the caller (`receive_dossier_preview`), never a stale prior verdict —,
    rejected reasons list, same shape as `receive_dossier_preview`'s own `rejected`)."""
    carried: list[IntakePiece] = []
    rejected: list[dict[str, Any]] = []
    for original in original_pieces:
        if original.duplicate_of_piece_id is not None or not original.admitted:
            continue
        label = original.display_name
        try:
            path = storage.resolve(original.storage_key)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except FileNotFoundError:
            rejected.append({"category": original.category, "category_label": limits.CATEGORY_LABEL.get(original.category, original.category),
                             "piece": label, "error_code": "ORIGINAL_PIECE_MISSING", "message": "Le fichier d'origine n'existe plus sur le serveur : non repris."})
            continue
        if digest != original.content_hash:
            rejected.append({"category": original.category, "category_label": limits.CATEGORY_LABEL.get(original.category, original.category),
                             "piece": label, "error_code": "ORIGINAL_PIECE_MODIFIED", "message": "Le contenu de ce fichier a changé depuis l'analyse d'origine : non repris."})
            continue
        carried.append(IntakePiece(
            id=uuid.uuid4(), category=original.category, display_name=original.display_name, file_format=original.file_format,
            size_bytes=original.size_bytes, content_hash=original.content_hash, storage_key=original.storage_key,
            text_storage_key=original.text_storage_key, page_count=original.page_count, char_count=original.char_count,
            chunk_count=original.chunk_count,
        ))
    return carried, rejected
