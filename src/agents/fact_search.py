"""Lot 52 — "Chercher dans mes documents": proposes ONE sourced, typed, citation-verified value for ONE
completion need (src/agents/completion_needs.py), from the account's OWN documents. Never a second scoring
engine, never a second extraction engine: this reuses the SAME LLM adapter (src.agents.llm_client.
ClaudeClient — no new provider), the SAME citation-verification idiom as the lot 50 bis document agents
(src.agents.document_llm_support.verify_citation/bound_for_llm — imported, never reimplemented) and the
SAME typed-value validation ao_extractor.py already uses for LLM-reported facts.

A proposal is DATA about where a value might come from, never a fact by itself: the caller
(src/web/completion_service.py) still requires an explicit user acceptance, and re-verifies the source
again at submission time — this module never writes anything.

Two subjects, two candidate sources, ONE shared LLM call (`propose_fact`):
- "prestataire" needs search the account's private knowledge base via `src.rag.hybrid_search` (the same
  index reference_evidence itself searches — never a second, parallel document access path, and never
  limited to an analysis's own already-retained `evidence_pack`: a certification attestation the reference
  scoring never picked as a project reference is still searchable here).
- "ao"/"acheteur" needs search the SAME analysis's own dossier pieces (if it was a multi-piece dossier
  analysis) or its own `texte_source` (single-file/paste mode) — NEVER the prestataire's knowledge base,
  NEVER a different/older AO, NEVER an external lookup. When neither exists, the caller must keep the plain
  manual-entry form (`status="no_source"`) rather than search anything.

The vector/lexical retrieval score is used ONLY to select which few passages the LLM is shown — it never
"certifies" a fact by itself (ticket, verbatim). The one signal treated as certifying is the citation check:
a value the model reports is discarded unless "citation" is a genuine, verifiable substring of the SPECIFIC
candidate it claims to cite (never of the whole concatenation — a model could otherwise misattribute a real
quote from one document to another passage's provenance).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from src.agents.ao_extractor import _validate_extracted_fact_value
from src.agents.document_llm_support import MAX_LLM_INPUT_CHARS, bound_for_llm, verify_citation
from src.core.config import LLM_TEMPERATURE_FACTUAL
from src.core.content_preparation import wrap_untrusted_content
from src.core.logger import get_agent_logger
from src.core.prompt_loader import load_prompt

logger = get_agent_logger("fact_search")

_PROMPTS_DIR = Path(__file__).parent / "prompts"
_SYSTEM_PATH = _PROMPTS_DIR / "fact_search_system.txt"
_USER_PATH = _PROMPTS_DIR / "fact_search_user.txt"

PROMPT_VERSION = "fact_search_v1"
# A small, fixed cap on how many candidate passages ONE search call ever shows the model — this is an
# explicit, user-triggered action for ONE need at a time (never a batch/background job), so there is no
# "repeated calls without a goal" (ticket) to bound beyond this.
MAX_CANDIDATES = 6
_WORD_RE = re.compile(r"[^\W\d_]{4,}", re.UNICODE)

ELIGIBLE_ACTIONS = ("declare_prestataire", "declare_ao", "declare_acheteur")


@dataclass(frozen=True)
class SourceCandidate:
    """One numbered passage offered to the LLM for ONE fact-search call, plus enough provenance to
    resolve — once a citation is verified against THIS candidate's OWN content only — exactly which
    document/version/chunk or dossier piece produced it. `provenance` is kind-specific, always carrying
    "kind" plus "offset_frame" (the reference frame `start_char`/`end_char` are relative to: "passage" for
    a knowledge-base chunk's own stored offsets, "piece_text" for a dossier piece's own joined text, or
    "ao_texte_source" for the AO's own frozen text — never a whole-document frame silently assumed to be
    the same as a passage's own frame)."""
    content: str
    source_label: str
    provenance: dict[str, Any]


@dataclass(frozen=True)
class FactSearchResult:
    """`status`: "proposed" (a citation-verified value was found), "absent" (the LLM read the passages and
    found nothing usable — a normal, expected outcome, never an error), "no_source" (nothing eligible to
    search at all — the caller must keep the plain manual form, never call the LLM), "no_candidates" (a
    source exists but nothing came back close enough to bother the model with), "llm_unavailable" (no
    provider configured/reachable) or "llm_invalid_response" (a malformed/unverifiable answer — treated as
    untrustworthy as a whole, never partially trusted, exactly like document_llm_support's own contract)."""
    status: str
    value: Any = None
    unit: Optional[str] = None
    citation: Optional[str] = None
    reason: Optional[str] = None
    source: Optional[dict[str, Any]] = None
    provider: Optional[str] = None
    prompt_version: Optional[str] = None
    extracted_at: Optional[str] = None
    search_mode: Optional[str] = None  # informational only: "hybrid"/"hybrid_partial"/"lexical"/"dossier"/"ao_text"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status, "value": self.value, "unit": self.unit, "citation": self.citation,
            "reason": self.reason, "source": self.source, "provider": self.provider,
            "prompt_version": self.prompt_version, "extracted_at": self.extracted_at, "search_mode": self.search_mode,
        }


def _anchor_words(need: dict) -> set[str]:
    text = f"{need.get('label') or ''} {str(need.get('field_key') or '').replace('_', ' ')}"
    return {w.casefold() for w in _WORD_RE.findall(text)}


def _overlap_score(text: str, anchors: set[str]) -> int:
    if not anchors:
        return 0
    words = {w.casefold() for w in _WORD_RE.findall(text or "")}
    return len(anchors & words)


def build_prestataire_candidates(
    db, *, organization_id, owner_user_id, need: dict, top_k: int = MAX_CANDIDATES,
) -> tuple[list[SourceCandidate], Optional[str]]:
    """Reuses `src.rag.hybrid_search` — the SAME index/authorization path as every other private-knowledge
    query (never a second, parallel access to documents). Works unchanged on SQLite (declared lexical mode)
    and on PostgreSQL with or without hybrid mode active; the mode actually used is returned for display,
    never claimed as more than it was."""
    from src.rag import hybrid_search

    query = (need.get("label") or need.get("field_key") or "").strip()
    if not query:
        return [], None
    try:
        outcome = hybrid_search.search(db, organization_id=organization_id, owner_user_id=owner_user_id, query=query, top_k=top_k)
    except Exception:
        logger.exception("Fact search: hybrid_search.search failed")
        return [], None
    candidates = [
        SourceCandidate(
            content=ev.content, source_label=ev.source,
            provenance={
                "kind": "knowledge_document", "document_version_id": ev.document_version_id, "chunk_id": ev.chunk_id,
                "start_char": ev.start_char, "end_char": ev.end_char, "offset_frame": "passage",
            },
        )
        for ev in outcome.evidences
    ]
    return candidates, outcome.mode


def build_ao_candidates(db, *, job, need: dict) -> tuple[list[SourceCandidate], Optional[str]]:
    """"ao"/"acheteur" needs: reuses the SAME dossier pieces already stored for THIS job (never a
    different/older AO, never the prestataire's own knowledge base) when the analysis was a multi-piece
    dossier, else the AO's own frozen `texte_source` (single-file/paste mode). A simple shared-word overlap
    against the need's own label/field_key narrows a potentially large dossier down to a few candidate
    chunks — a one-off SELECTION heuristic only, never a scoring signal and never a second RAG engine (the
    private knowledge base's real hybrid search is untouched by this function)."""
    from src.web.ao_dossier import service as dossier_service
    from src.web.database.repositories import ao_dossiers as dossiers_repo

    dossier = dossiers_repo.get_by_job(db, job_id=job.id, organization_id=job.organization_id, user_id=job.user_id)
    if dossier is not None:
        try:
            texts, _summary = dossier_service.load_texts(db, dossier.id, job.organization_id, job.user_id)
        except LookupError:
            return [], None
        anchors = _anchor_words(need)
        scored: list[tuple[int, Any, int, dict]] = []
        for piece in texts:
            if piece.duplicate_of:
                continue
            for idx, chunk in enumerate(piece.chunks):
                score = _overlap_score(chunk.get("content", ""), anchors)
                if score > 0:
                    scored.append((score, piece, idx, chunk))
        scored.sort(key=lambda t: t[0], reverse=True)
        candidates = [
            SourceCandidate(
                content=chunk["content"], source_label=piece.name,
                provenance={
                    "kind": "dossier_piece", "dossier_id": str(dossier.id), "piece_id": piece.piece_id,
                    "chunk_index": idx, "start_char": chunk.get("start"), "end_char": chunk.get("end"),
                    "offset_frame": "piece_text",
                },
            )
            for _score, piece, idx, chunk in scored[:MAX_CANDIDATES]
        ]
        return candidates, "dossier"

    texte_source = (getattr(job.ao, "texte_source", "") or "").strip()
    if not texte_source:
        return [], None
    return [
        SourceCandidate(
            content=texte_source[:15000], source_label="Texte de l'appel d'offres fourni",
            provenance={"kind": "ao_text", "offset_frame": "ao_texte_source"},
        )
    ], "ao_text"


def _format_passages(candidates: list[SourceCandidate]) -> str:
    blocks = [
        f"[PASSAGE {i}] (source : {c.source_label})\n{wrap_untrusted_content(c.content)}\n[/PASSAGE {i}]"
        for i, c in enumerate(candidates, start=1)
    ]
    return "\n\n".join(blocks)


def propose_fact(llm, *, need: dict, candidates: list[SourceCandidate], search_mode: Optional[str]) -> FactSearchResult:
    """The one LLM call shared by every subject — see module docstring. Never raises: any failure mode
    (disabled provider, malformed JSON, an unverifiable citation, a type-mismatched value, an out-of-range
    passage reference) is reported as an explicit, safe status, exactly like
    document_llm_support.run_structured_judgment's own contract."""
    if not candidates:
        return FactSearchResult(status="no_candidates", search_mode=search_mode)
    if llm is None or not getattr(llm, "enabled", False):
        return FactSearchResult(status="llm_unavailable", reason="no_provider_configured", search_mode=search_mode)

    field_type = need.get("type") or "text"
    bounded: list[SourceCandidate] = []
    total = 0
    for c in candidates[:MAX_CANDIDATES]:
        text, _was_truncated = bound_for_llm(c.content)
        if bounded and total + len(text) > MAX_LLM_INPUT_CHARS:
            break
        bounded.append(SourceCandidate(content=text, source_label=c.source_label, provenance=c.provenance))
        total += len(text)
    if not bounded:
        return FactSearchResult(status="no_candidates", search_mode=search_mode)

    prompt = load_prompt(
        _USER_PATH, field_key=need.get("field_key") or need.get("id") or "", label=need.get("label") or "",
        type=field_type, unit=need.get("unit") or "aucune", passages=_format_passages(bounded),
    )
    try:
        system = load_prompt(_SYSTEM_PATH)
        data = llm.json_complete(prompt, system=system, temperature=LLM_TEMPERATURE_FACTUAL, max_tokens=600)
    except Exception:
        logger.exception("Fact search LLM call raised unexpectedly")
        return FactSearchResult(status="llm_unavailable", reason="provider_exception", search_mode=search_mode)

    if data is None:
        return FactSearchResult(status="llm_unavailable", reason="no_content_or_unparseable", search_mode=search_mode)
    if not isinstance(data, dict):
        return FactSearchResult(status="llm_invalid_response", reason="not_an_object", search_mode=search_mode)
    if not isinstance(data.get("found"), bool):
        return FactSearchResult(status="llm_invalid_response", reason="missing_found", search_mode=search_mode)
    reason = data.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return FactSearchResult(status="llm_invalid_response", reason="missing_reason", search_mode=search_mode)
    if not data["found"]:
        return FactSearchResult(status="absent", reason=reason, search_mode=search_mode)

    passage_number = data.get("passage_number")
    if isinstance(passage_number, bool) or not isinstance(passage_number, int) or not (1 <= passage_number <= len(bounded)):
        return FactSearchResult(status="llm_invalid_response", reason="invalid_passage_number", search_mode=search_mode)
    candidate = bounded[passage_number - 1]

    citation = data.get("citation", "")
    if not isinstance(citation, str) or not verify_citation(candidate.content, citation):
        logger.warning("Fact search citation could not be verified against the referenced passage")
        return FactSearchResult(status="llm_invalid_response", reason="citation_not_verified", search_mode=search_mode)

    value, ok = _validate_extracted_fact_value(data.get("value"), field_type)
    if not ok:
        return FactSearchResult(status="llm_invalid_response", reason="value_type_mismatch", search_mode=search_mode)

    reported_unit = data.get("unit")
    unit = reported_unit if isinstance(reported_unit, str) and reported_unit.strip() else None

    return FactSearchResult(
        status="proposed", value=value, unit=unit, citation=citation.strip(), reason=reason,
        source={**candidate.provenance, "source_label": candidate.source_label},
        provider=getattr(llm, "last_provider_used", None), prompt_version=PROMPT_VERSION,
        extracted_at=datetime.now(timezone.utc).isoformat(), search_mode=search_mode,
    )


def search_fact_for_need(db, *, job, need: dict, llm, organization_id, owner_user_id) -> FactSearchResult:
    """Orchestrator: dispatches to the right candidate source by the need's own `action` (never guessed
    from `subject` alone — a need not of kind "declarable" or not one of ELIGIBLE_ACTIONS is refused
    outright, never silently searched)."""
    if need.get("kind") != "declarable" or need.get("action") not in ELIGIBLE_ACTIONS:
        return FactSearchResult(status="no_source", reason="need_not_searchable")
    if need["action"] == "declare_prestataire":
        candidates, mode = build_prestataire_candidates(db, organization_id=organization_id, owner_user_id=owner_user_id, need=need)
    else:
        candidates, mode = build_ao_candidates(db, job=job, need=need)
    if not candidates:
        return FactSearchResult(status="no_source", search_mode=mode)
    return propose_fact(llm, need=need, candidates=candidates, search_mode=mode)
