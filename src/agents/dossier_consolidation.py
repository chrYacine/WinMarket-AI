"""Lot 47 bis — ONE consolidated extraction over the pieces of an AO dossier, with sourced observations.

This is NOT a second pipeline and NOT a score per file: it feeds the existing `AOExtractor` (LLM or local
fallback) one bounded WINDOW of one piece at a time, keeps every observation with its source (piece id, page
when the format has pages, window and character range), and only then builds the single `AOContext` the
existing scoring chain consumes. Nothing here reads the account's policy or computes a score.

Consolidation rules (documented limits, not a promise of exhaustive understanding):
- an ABSENCE in a piece never erases a value found in another;
- two DIFFERENT values for the same field stay AMBIGUOUS: no value is retained — no "last file wins", no
  favourable pick, no legal priority assumed between RC / CCTP / CCAP / acte / annexes. The scoring then treats
  the field like any missing/ambiguous datum (INCOMPLET, never a favourable certain conclusion);
- values that concern different lots or periods are NOT told apart automatically: they are reported as a
  conflict rather than guessed (prudent by design);
- local-fallback scalar candidates (used when no AI provider answers) take, per piece, only the FIRST match,
  as the historical single-file extraction did — the fallback is a keyword/regex reading, not a semantic one;
- lists are the (bounded) union of what the pieces list; a doubt (`ambiguous`) in any piece stays a doubt;
- a document is DATA: no piece content is ever an instruction or an access right.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from src.agents.certification_scope import resolve_mandatory_certifications
from src.core import config
from src.core.models import AOContext, ExtractedFact

SCALAR_FIELDS = ("budget_estime", "duree_projet_mois", "deadline_reponse")
DESCRIPTIVE_FIELDS = ("titre", "client", "secteur")
LIST_FIELDS = ("technologies_demandees", "competences_requises", "questions_client", "livrables", "contraintes")
LIMIT_NOTES = (
    "Deux valeurs différentes pour un même champ restent ambiguës : aucune valeur n'est retenue et aucune priorité n'est supposée entre RC, CCTP, CCAP, acte d'engagement et annexes.",
    "Des valeurs relatives à des lots ou des périodes différents ne sont pas distinguées automatiquement : elles sont signalées comme conflit, jamais tranchées.",
    "Sans fournisseur d'IA, la lecture est un repli à mots-clés : un montant en euros quelconque est un candidat de budget (premier candidat de chaque pièce).",
    "La provenance indique la pièce, la page quand le format en a (PDF) et le passage analysé — pas la phrase exacte.",
)


@dataclass
class PieceText:
    piece_id: str
    position: int
    category: str
    category_label: str
    name: str
    duplicate_of: Optional[str] = None
    chunks: list[dict] = field(default_factory=list)  # {content, page, section, start, end}

    def text(self) -> str:
        return "\n\n".join(c["content"] for c in self.chunks)


@dataclass
class Window:
    piece_id: str
    index: int  # 0-based inside its piece
    text: str
    char_start: int
    char_end: int
    page_start: Optional[int]
    page_end: Optional[int]


# ---------------------------------------------------------------------------
# windows + consolidated text
# ---------------------------------------------------------------------------

def _split_long(text: str, size: int) -> list[tuple[int, str]]:
    """Consecutive slices of at most `size` characters (cut at a blank when one is close), covering the WHOLE text —
    nothing is dropped."""
    out, start = [], 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            cut = text.rfind(" ", start + size // 2, end)
            end = cut + 1 if cut > start else end
        out.append((start, text[start:end]))
        start = end
    return out


def build_windows(pieces: list[PieceText], window_chars: Optional[int] = None) -> list[Window]:
    size = window_chars or config.DOSSIER_WINDOW_CHARS
    windows: list[Window] = []
    for piece in sorted(pieces, key=lambda p: p.position):
        if piece.duplicate_of or not piece.chunks:
            continue
        index, buffer, pages, first_start, last_end = 0, [], [], None, 0

        def flush() -> None:
            nonlocal index, buffer, pages, first_start, last_end
            if buffer:
                windows.append(Window(piece.piece_id, index, "\n\n".join(buffer), first_start or 0, last_end,
                                      min(pages) if pages else None, max(pages) if pages else None))
                index += 1
            buffer, pages, first_start = [], [], None

        for chunk in piece.chunks:
            content, page = chunk["content"], chunk.get("page")
            if len(content) > size:
                flush()
                for offset, slice_text in _split_long(content, size):
                    windows.append(Window(piece.piece_id, index, slice_text, chunk["start"] + offset, chunk["start"] + offset + len(slice_text), page, page))
                    index += 1
                continue
            projected = sum(len(b) for b in buffer) + 2 * len(buffer) + len(content)
            if buffer and projected > size:
                flush()
            if first_start is None:
                first_start = chunk["start"]
            buffer.append(content)
            last_end = chunk["end"]
            if page is not None:
                pages.append(page)
        flush()
    return windows


def plain_text(pieces: list[PieceText]) -> str:
    """The pieces' text WITHOUT the boundary tags: what a content-scope check must judge (a tag such as "CCTP" is
    ours, not evidence that the documents are a tender). Duplicates are not read twice."""
    return "\n\n".join(p.text() for p in sorted(pieces, key=lambda p: p.position) if not p.duplicate_of)


def consolidated_text(pieces: list[PieceText]) -> str:
    """The text of the whole dossier with EXPLICIT boundaries and piece identifiers (the file names are not in it:
    a name is display data, and must not steer keyword detections)."""
    parts = []
    for piece in sorted(pieces, key=lambda p: p.position):
        tag = f"PIÈCE {piece.position} · {piece.category_label} · id {piece.piece_id[:8]}"
        if piece.duplicate_of:
            parts.append(f"===== {tag} : contenu identique à une autre pièce, non relu =====")
            continue
        parts.append(f"===== {tag} =====\n{piece.text()}\n===== FIN DE LA PIÈCE {piece.position} =====")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# sourced observations + consolidation
# ---------------------------------------------------------------------------

def _source(piece: PieceText, window: Window, windows_of_piece: int) -> dict[str, Any]:
    return {
        "piece_id": piece.piece_id, "categorie": piece.category, "categorie_libelle": piece.category_label, "nom": piece.name,
        "passage": {"fenetre": window.index + 1, "sur": windows_of_piece,
                    "pages": [window.page_start, window.page_end] if window.page_start is not None else None,
                    "caracteres": [window.char_start, window.char_end]},
    }


def _canon(name: str, value: Any) -> Any:
    if name == "budget_estime":
        return round(float(value), 2)
    if name == "duree_projet_mois":
        return int(value)
    return re.sub(r"[.\-]", "/", " ".join(str(value).split())).casefold()


def _dedupe_keep_order(values: list[str]) -> list[str]:
    seen, out = set(), []
    for v in values:
        key = str(v).casefold().strip()
        if key and key not in seen:
            seen.add(key)
            out.append(v)
    return out


def extract_dossier_ao(
    pieces: list[PieceText], extractor: Any, *, requested_facts: Optional[dict] = None, allow_llm: bool = True,
    summary: Optional[dict] = None, window_chars: Optional[int] = None,
) -> AOContext:
    windows = build_windows(pieces, window_chars)
    observed = [(w, extractor.extract(w.text, requested_facts=requested_facts or None, allow_llm=allow_llm)) for w in windows]
    return consolidate(pieces, observed, requested_facts=requested_facts or {}, summary=summary)


def consolidate(pieces: list[PieceText], observed: list[tuple[Window, AOContext]], *, requested_facts: dict, summary: Optional[dict] = None) -> AOContext:
    by_id = {p.piece_id: p for p in pieces}
    position = {p.piece_id: p.position for p in pieces}
    observed = sorted(observed, key=lambda wa: (position.get(wa[0].piece_id, 0), wa[0].index))
    per_piece_windows: dict[str, int] = {}
    for w, _ in observed:
        per_piece_windows[w.piece_id] = per_piece_windows.get(w.piece_id, 0) + 1

    def src(w: Window) -> dict[str, Any]:
        return _source(by_id[w.piece_id], w, per_piece_windows[w.piece_id])

    observations: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    provenance: dict[str, str] = {}
    values: dict[str, Any] = {}

    # ---- scalar fields that can decide a score: a second, different value is a conflict
    for name in SCALAR_FIELDS:
        candidates, first_fallback_seen = [], set()
        for w, ao in observed:
            code, value = ao.field_provenance.get(name), getattr(ao, name)
            if code not in ("llm", "fallback") or value is None or value == "":
                continue
            if code == "fallback":  # a keyword reading: the first match of each piece, as in the single-file path
                if w.piece_id in first_fallback_seen:
                    continue
                first_fallback_seen.add(w.piece_id)
            candidates.append((value, code, w))
        distinct: dict[Any, list[tuple[Any, str, Window]]] = {}
        for item in candidates:
            distinct.setdefault(_canon(name, item[0]), []).append(item)
        for group in distinct.values():
            observations.append({"champ": name, "valeur": group[0][0], "source": src(group[0][2]), "autres_sources": [src(g[2]) for g in group[1:]]})
        if len(distinct) > 1:
            provenance[name] = "conflict"
            values[name] = None
            conflicts.append({"champ": name, "valeurs": [{"valeur": g[0][0], "sources": [src(x[2]) for x in g]} for g in distinct.values()],
                              "resolution": "ambigu — aucune valeur retenue"})
        elif candidates:
            values[name] = candidates[0][0]
            provenance[name] = "llm" if any(c[1] == "llm" for c in candidates) else "fallback"
        else:
            values[name] = None
            provenance[name] = "absent"

    # ---- descriptive fields: display only, first found in the fixed piece order (recorded, never a conflict)
    for name in DESCRIPTIVE_FIELDS:
        chosen, code = None, "absent"
        for w, ao in observed:
            if name == "titre" and w.index != 0:
                continue
            value, c = getattr(ao, name), ao.field_provenance.get(name)
            if c in ("llm", "fallback") and value:
                observations.append({"champ": name, "valeur": value, "source": src(w), "autres_sources": []})
                if chosen is None:
                    chosen, code = value, c
        values[name] = chosen or ""
        provenance[name] = code

    # ---- lists: bounded union, in piece order
    limits_notes = list(LIMIT_NOTES)
    for name in LIST_FIELDS + ("certifications_obligatoires",):
        merged: list[str] = []
        codes = set()
        for w, ao in observed:
            items = list(getattr(ao, name) or [])
            if items:
                codes.add(ao.field_provenance.get(name, "absent"))
            merged.extend(items)
        values[name] = _dedupe_keep_order(merged)
        provenance[name] = "llm" if "llm" in codes else ("fallback" if "fallback" in codes else "absent")

    # ---- certifications: contradictions are computed on ALL mentions of ALL pieces (same rule as one document)
    mentions = [m for _, ao in observed for m in ao.certification_mentions]
    _, contradictions = resolve_mandatory_certifications(mentions)
    blocked = {c.casefold() for c in contradictions}
    values["certifications_obligatoires"] = [c for c in values["certifications_obligatoires"] if c.casefold() not in blocked]
    for cert in values["certifications_obligatoires"]:
        for w, ao in observed:
            if cert.casefold() in {c.casefold() for c in ao.certifications_obligatoires}:
                observations.append({"champ": "certifications_obligatoires", "valeur": cert, "source": src(w), "autres_sources": []})
                break
    for cert in contradictions:
        conflicts.append({"champ": "certifications_obligatoires", "valeurs": [{"valeur": cert, "sources": []}],
                          "resolution": "ambigu — exclue des certifications obligatoires (mentions contradictoires dans le dossier)"})

    for name in LIST_FIELDS:
        if len(values[name]) > config.DOSSIER_MAX_LIST_ITEMS:
            limits_notes.append(f"Liste « {name} » limitée à {config.DOSSIER_MAX_LIST_ITEMS} éléments ({len(values[name])} relevés).")
            values[name] = values[name][:config.DOSSIER_MAX_LIST_ITEMS]

    # ---- requested business facts (the ones the account's OWN criteria ask for): merged fact by fact
    facts: dict[str, ExtractedFact] = {}
    for key, spec in (requested_facts or {}).items():
        per_window = [(w, ao.extracted_facts.get(key)) for w, ao in observed if ao.extracted_facts.get(key) is not None]
        merged, fact_obs, fact_conflict = _merge_fact(key, spec, per_window)
        facts[key] = merged
        for value, unit, group in fact_obs:
            observations.append({"champ": key, "libelle": spec.get("label") or key, "valeur": value, "unite": unit, "source": src(group[0]),
                                 "autres_sources": [src(x) for x in group[1:]]})
        if fact_conflict:
            conflicts.append({"champ": key, "libelle": spec.get("label") or key,
                              "valeurs": [{"valeur": v, "unite": u, "sources": [src(x) for x in g]} for v, u, g in fact_obs],
                              "resolution": "ambigu — aucune valeur retenue"})

    if len(observations) > config.DOSSIER_MAX_OBSERVATIONS:
        limits_notes.append(f"Observations limitées à {config.DOSSIER_MAX_OBSERVATIONS} ({len(observations)} relevées).")
        observations = observations[:config.DOSSIER_MAX_OBSERVATIONS]

    statuses = {ao.extraction_status for _, ao in observed}
    status = "fallback_local" if statuses <= {"fallback_local"} else ("llm_full" if statuses <= {"llm_full"} else "llm_partial")
    reason = next((ao.extraction_reason for _, ao in observed if ao.extraction_reason), None)
    soft_conflicts = [c for _, ao in observed for c in ao.extraction_conflicts]
    duplicates = [{"piece_id": p.piece_id, "nom": p.name, "doublon_de": p.duplicate_of} for p in pieces if p.duplicate_of]

    payload = {
        "version": 1,
        **{k: v for k, v in (summary or {}).items() if k in (
            "id", "pieces", "nombre_pieces", "taille_totale", "taille_totale_octets",
            # Lot 50 §4/§5 — the confirmed admission scope, frozen into this analysis's own manifest: which of
            # the 4 guided categories are absent from the ADMITTED set, and who/when confirmed it. Never
            # recomputed later from whatever the dossier's current documents happen to be (a manifest is a
            # record of what was confirmed, not a live view — see AoDossier's own docstring).
            "categories_manquantes", "perimetre_limite", "confirme_par", "confirme_le",
        )},
        "fenetres_analysees": len(observed), "caracteres_analyses": sum(len(w.text) for w, _ in observed),
        "observations": observations, "conflits": conflicts, "doublons": duplicates, "limites": limits_notes,
    }
    return AOContext(
        titre=values["titre"], client=values["client"], secteur=values["secteur"], budget_estime=values["budget_estime"],
        deadline_reponse=values["deadline_reponse"], duree_projet_mois=values["duree_projet_mois"],
        technologies_demandees=values["technologies_demandees"], competences_requises=values["competences_requises"],
        questions_client=values["questions_client"], livrables=values["livrables"], contraintes=values["contraintes"],
        certifications_obligatoires=values["certifications_obligatoires"], texte_source=consolidated_text(pieces),
        field_provenance=provenance, extraction_conflicts=sorted(set(soft_conflicts) | {c["champ"] for c in conflicts if c["champ"] in SCALAR_FIELDS}),
        extraction_status=status, extraction_reason=reason, certification_mentions=mentions,
        certification_contradictions=list(contradictions), extracted_facts=facts, dossier=payload,
    )


def _merge_fact(key: str, spec: dict, per_window: list[tuple[Window, ExtractedFact]]):
    """(merged fact, observations [(value, unit, [windows])], conflict?) for ONE requested fact."""
    found = [(w, f) for w, f in per_window if f.status == "found"]
    ambiguous = [(w, f) for w, f in per_window if f.status == "ambiguous"]
    if ambiguous:
        reason = next((f.reason for _, f in ambiguous if f.reason), None) or "ambiguous_in_dossier"
        return ExtractedFact(status="ambiguous", provenance=ambiguous[0][1].provenance, reason=reason), [], False
    if not found:
        return ExtractedFact(status="absent", provenance="absent"), [], False
    provenance = "llm" if any(f.provenance == "llm" for _, f in found) else found[0][1].provenance
    if spec.get("type") == "list":
        merged, groups = [], []
        for w, f in found:
            items = list(f.value or [])
            merged.extend(items)
            groups.append((items, f.unit, [w]))
        return ExtractedFact(value=_dedupe_keep_order(merged), unit=found[0][1].unit, status="found", provenance=provenance), groups, False

    def canon(value: Any) -> Any:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return round(float(value), 6)
        return " ".join(str(value).split()).casefold()

    groups: dict[Any, list[tuple[Window, ExtractedFact]]] = {}
    for w, f in found:
        groups.setdefault(canon(f.value), []).append((w, f))
    units = {f.unit.strip().casefold() for _, f in found if isinstance(f.unit, str) and f.unit.strip()}
    obs = [(g[0][1].value, next((f.unit for _, f in g if f.unit), None), [w for w, _ in g]) for g in groups.values()]
    if len(groups) > 1 or len(units) > 1:
        return ExtractedFact(status="ambiguous", provenance=provenance, reason="conflicting_values"), obs, True
    return ExtractedFact(value=found[0][1].value, unit=next((f.unit for _, f in found if f.unit), None), status="found", provenance=provenance), obs, False
