"""Database rows and read-back of a validated dossier. Everything a job needs is read from the database and
the private storage — never from a list held in memory — so a dossier survives a restart and a job can be
resumed on it."""
from __future__ import annotations

import uuid
from typing import Any, Optional

from sqlalchemy.orm import Session

from src.agents.dossier_consolidation import PieceText
from src.core.content_security import ModerationResult
from src.web.ao_dossier import limits, storage
from src.web.ao_dossier.intake import ReceivedDossier
from src.web.ao_dossier.scope import assess_dossier_scope
from src.web.database.models import AoDossier
from src.web.database.repositories import ao_dossiers as dossiers_repo


def describe_piece_change(before_hashes: set[str], after_hashes: set[str]) -> str:
    """Lot 54 §1 — honest wording for a documentary re-analysis's own admitted-piece set, compared to its
    origin's: NEVER assumes "pièces ajoutées" (the lot 53 wording, unconditional) when the new dossier only
    REMOVED a piece with nothing added, or when both happened at once. Compares by content hash only (the
    same identity `AoDossierPiece.content_hash`/dedup already uses elsewhere) — never a filename, which two
    genuinely different pieces could share.

    Returns one of: "pieces_ajoutees" (strict superset), "pieces_retirees" (strict subset), "pieces_modifiees"
    (both added and removed at once), "dossier_inchange" (identical admitted sets — a re-analysis with no
    net piece change, e.g. only the policy/profil/capacité differ)."""
    added = after_hashes - before_hashes
    removed = before_hashes - after_hashes
    if added and removed:
        return "pieces_modifiees"
    if added:
        return "pieces_ajoutees"
    if removed:
        return "pieces_retirees"
    return "dossier_inchange"


def admitted_piece_hashes(db: Session, *, job_id: str, organization_id: uuid.UUID, user_id: uuid.UUID) -> Optional[set[str]]:
    """The content-hash set of a job's own dossier's ADMITTED pieces, or None if that job has no dossier
    (or it is not resolvable for this account) — a caller must treat None as "nothing to compare", never as
    an empty dossier."""
    dossier = dossiers_repo.get_by_job(db, job_id=job_id, organization_id=organization_id, user_id=user_id)
    if dossier is None:
        return None
    return {p.content_hash for p in dossier.pieces if p.admitted}


def commit(db: Session, received: ReceivedDossier) -> AoDossier:
    """Record a validated dossier. The caller owns the transaction.

    Lot 50 §4: the direct-submit path never excludes a piece (no confirmation step exists there — every
    successfully-read piece is `admitted=True` by construction, see `IntakePiece`'s own default), but it can
    still legitimately be missing one or more of the 4 guided categories (e.g. annexes only) — computed here
    so its manifest/scope banner is accurate too, not just a previewed dossier's."""
    categories_missing = sorted(c for c in limits.MAIN_CATEGORIES if not any(p.category == c for p in received.pieces))
    return dossiers_repo.create_dossier(
        db, dossier_id=received.dossier_id, organization_id=received.organization_id, user_id=received.user_id,
        total_bytes=received.total_bytes, pieces=[p.row() for p in received.pieces],
        categories_missing=categories_missing, scope_limited=bool(categories_missing),
    )


def source_label(pieces: list[Any]) -> str:
    """The label shown in the history for this analysis: how many pieces, which categories."""
    cats = sorted({p.category for p in pieces}, key=limits.CATEGORIES.index)
    return f"Dossier AO — {len(pieces)} pièce{'s' if len(pieces) > 1 else ''} ({', '.join(limits.CATEGORY_SHORT[c] for c in cats)})"


def piece_payload(piece: Any, *, duplicate_name: Optional[str] = None) -> dict[str, Any]:
    """The PUBLIC description of a piece: no storage path, no internal key."""
    return {
        "id": str(piece.id), "categorie": piece.category, "categorie_libelle": limits.CATEGORY_LABEL[piece.category],
        "nom": piece.display_name, "format": piece.file_format, "taille_octets": piece.size_bytes,
        "taille": limits.format_bytes(piece.size_bytes), "empreinte": piece.content_hash, "pages": piece.page_count,
        "caracteres": piece.char_count, "doublon_de": str(piece.duplicate_of_piece_id) if piece.duplicate_of_piece_id else None,
        "doublon_de_nom": duplicate_name,
    }


def summary(dossier: AoDossier) -> dict[str, Any]:
    """Lot 50: every piece is listed here — admitted AND excluded alike, each with its manifest fields
    (`manifest_piece_payload`) — so a result/history view can show what was actually analysed alongside
    what was confirmed excluded, without a second lookup. `load_texts` below is the one that FILTERS to
    admitted pieces only, for what actually feeds the extraction."""
    names = {p.id: p.display_name for p in dossier.pieces}
    return {
        "id": str(dossier.id), "job_id": dossier.job_id, "statut": dossier.status, "taille_totale_octets": dossier.total_bytes,
        "taille_totale": limits.format_bytes(dossier.total_bytes), "nombre_pieces": dossier.piece_count,
        "pieces": [manifest_piece_payload(p, duplicate_name=names.get(p.duplicate_of_piece_id)) for p in dossier.pieces],
        "categories_manquantes": dossier.categories_missing or [], "perimetre_limite": bool(dossier.scope_limited),
        "confirme_par": str(dossier.confirmed_by_user_id) if dossier.confirmed_by_user_id else None,
        "confirme_le": dossier.confirmed_at.isoformat() if dossier.confirmed_at else None,
    }


def manifest_piece_payload(piece: Any, *, duplicate_name: Optional[str] = None) -> dict[str, Any]:
    """Lot 50 §3 / lot 50 bis §1 — the admission table's per-file row: what the user declared, what the
    classifier/moderator/security agents proposed, and (once confirmed) what was finally retained. Never a
    storage path. `*_origine` names WHICH path (heuristic / llm / a fallback from one) actually produced each
    proposal — never presented as an LLM validation that did not actually happen."""
    base = piece_payload(piece, duplicate_name=duplicate_name)
    base.update({
        "categorie_proposee": piece.category_proposed, "categorie_finale": piece.category_final or piece.category,
        "categorie_origine": piece.classification_source,
        "securite": piece.security_state, "securite_code": piece.security_code, "securite_motif": piece.security_reason,
        "securite_origine": piece.security_review_source,
        "pertinence": piece.moderation_verdict, "pertinence_motif": piece.moderation_reason,
        "pertinence_origine": piece.moderation_source,
        "sera_pris_en_compte": bool(piece.admitted), "raison_exclusion": piece.exclusion_reason,
        "lien_declare": piece.user_link_note,
    })
    return base


def preview_summary(dossier: AoDossier, *, rejected: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
    """The admission table for a STAGING dossier (§3) — one row per stored piece plus the (never stored)
    structurally-unsupported ones, so nothing is ever silently missing from what the user sees."""
    names = {p.id: p.display_name for p in dossier.pieces}
    usable = [p for p in dossier.pieces if p.admitted and p.duplicate_of_piece_id is None and p.security_state != "blocked"]
    return {
        "dossier_id": str(dossier.id), "expires_at": dossier.staging_expires_at.isoformat() if dossier.staging_expires_at else None,
        "pieces": [manifest_piece_payload(p, duplicate_name=names.get(p.duplicate_of_piece_id)) for p in dossier.pieces],
        "rejetees": list(rejected or []),
        "au_moins_une_piece_exploitable": bool(usable),
    }


def load_texts(db: Session, dossier_id: uuid.UUID, organization_id: uuid.UUID, user_id: uuid.UUID) -> tuple[list[PieceText], dict[str, Any]]:
    """The pieces and their extracted chunks, READ BACK from the database and the private storage, plus the public
    summary. Duplicates are returned (flagged) but carry no chunks: they are not read twice.

    Lot 50: only `admitted` pieces (never a security-`blocked` one, which can never be `admitted`) are fed to
    extraction — a confirmed exclusion (hors-sujet, technical failure, security concern the user chose to
    drop) must never reach the score, even though `summary()` above still LISTS it, for traceability. Each
    piece's `category_final` (falling back to `category` for a pre-lot-50 row) is what tags its text — a
    corrected classification is honoured, not the original upload slot."""
    dossier = dossiers_repo.get_for_owner(db, dossier_id=dossier_id, organization_id=organization_id, user_id=user_id)
    if dossier is None:
        raise LookupError("dossier introuvable")
    texts: list[PieceText] = []
    for piece in dossier.pieces:
        if not piece.admitted:
            continue
        category = piece.category_final or piece.category
        chunks = storage.read_json(piece.text_storage_key)["chunks"] if piece.text_storage_key and not piece.duplicate_of_piece_id else []
        texts.append(PieceText(
            piece_id=str(piece.id), position=piece.position, category=category, category_label=limits.CATEGORY_SHORT.get(category, category),
            name=piece.display_name, duplicate_of=str(piece.duplicate_of_piece_id) if piece.duplicate_of_piece_id else None, chunks=chunks,
        ))
    return texts, summary(dossier)


def check_admitted_scope(dossier: AoDossier) -> ModerationResult:
    """Lot 50 bis §1 / lot 50 ter §2 — the whole-dossier scope decision (`assess_dossier_scope`) judged on the
    FINAL admitted set, at CONFIRM time, exactly as `receive_dossier` (the legacy direct-submit route) judges
    it at intake — the same shared rule, not two drifting copies (see `scope.py`). Before lot 50 bis, a
    dossier admitted through preview/confirm (which never ran this whole-text check — only each piece's OWN
    security/relevance) could pass confirmation with a 200 and then have its job fail LATER, silently, at
    analysis time. Running the SAME check HERE, before any job exists, closed that gap. Lot 50 ter further
    demoted the lexical "≥2 termes de marché" heuristic from an absolute veto to a fallback signal (see
    `scope.py`'s own docstring) — this function only needed to start passing the admitted pieces alongside
    their combined text so that fallback can defer to an established per-piece relevance judgment."""
    parts, admitted_pieces = [], []
    for piece in sorted(dossier.pieces, key=lambda p: p.position):
        if not piece.admitted or piece.duplicate_of_piece_id is not None or not piece.text_storage_key:
            continue
        admitted_pieces.append(piece)
        chunks = storage.read_json(piece.text_storage_key)["chunks"]
        parts.append("\n\n".join(c["content"] for c in chunks))
    return assess_dossier_scope("\n\n".join(parts), admitted_pieces)
