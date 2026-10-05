"""B18-T6 (complement to B18-T5, related to B15) — centralizes the
character-budget limits for the RAG reference-selection prompt's
candidate/document-text assembly step (src/rag/semantic_rerank.py::
semantic_rerank), which used to hardcode `evidences[:6]` and
`ev.content[:800]` inline at the call site.

The gap this closes: removing B18-T5's `ev.content[:800]` re-truncation
fixed the "located passage silently lost a second time" defect, but
reopened the ORIGINAL prompt-26 budget it had implicitly enforced —
`ev.content` can now be as long as src.rag.passage_location.
MAX_PASSAGE_CHARS (3500) per candidate, and with up to 6 candidates the
assembled document block could reach 21000 characters instead of the
~4800 originally intended. This module is a CHARACTER budget, never
presented as an exact token count or a real network/compute cost.

Only the reference-SELECTION prompt's candidate assembly is bounded here
— the AO-context slice (`ao_text[:2500]`) and the fixed instructions in
src/rag/prompts/reference_selection.txt keep their own, separate, already
-existing limits, untouched by this module.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.core import config
from src.core.models import RAGEvidence
from src.rag.passage_location import MAX_PASSAGE_CHARS, fit_window_to_budget

# A header's own overhead ("--- Référence N (label) ---\n") must be
# bounded too (ticket section 4: "un nom de source ou des métadonnées
# trop longs ne doit pas [contourner la borne]") — this caps only the
# DISPLAY label used inside the prompt text, never the evidence's real
# `.source` (still used, untruncated, for citation/traceability in
# evidence_pack) nor the positional index selected_ids resolves against.
MAX_SOURCE_LABEL_CHARS = 60


class InvalidContextBudgetError(ValueError):
    """A configured budget value is not usable — surfaced loudly at the
    call site rather than silently producing an over/under-sized prompt."""


@dataclass(frozen=True)
class ContextBudget:
    max_candidates: int
    max_excerpt_chars: int
    max_document_block_chars: int


def get_context_budget() -> ContextBudget:
    """Reads and VALIDATES the three config values before use (ticket
    section 1: "valider les valeurs de configuration avant usage") — never
    trusted as already-sane just because they parsed as integers at
    process start. config.validate_config() already checks the three are
    positive integers with the block able to hold at least one excerpt;
    this adds the one cross-module check that belongs here instead of in
    src/core/config.py (a per-excerpt budget larger than the maximum
    window src.rag.passage_location can ever produce would be a
    configuration mistake, not a real constraint)."""
    max_candidates = config.RAG_SELECTION_MAX_CANDIDATES
    max_excerpt_chars = config.RAG_SELECTION_MAX_EXCERPT_CHARS
    max_document_block_chars = config.RAG_SELECTION_MAX_DOCUMENT_BLOCK_CHARS

    if not isinstance(max_candidates, int) or max_candidates <= 0:
        raise InvalidContextBudgetError(f"RAG_SELECTION_MAX_CANDIDATES must be a positive integer, got {max_candidates!r}")
    if not isinstance(max_excerpt_chars, int) or max_excerpt_chars <= 0:
        raise InvalidContextBudgetError(f"RAG_SELECTION_MAX_EXCERPT_CHARS must be a positive integer, got {max_excerpt_chars!r}")
    if not isinstance(max_document_block_chars, int) or max_document_block_chars <= 0:
        raise InvalidContextBudgetError(f"RAG_SELECTION_MAX_DOCUMENT_BLOCK_CHARS must be a positive integer, got {max_document_block_chars!r}")
    if max_document_block_chars < max_excerpt_chars:
        raise InvalidContextBudgetError(
            f"RAG_SELECTION_MAX_DOCUMENT_BLOCK_CHARS ({max_document_block_chars}) must be >= "
            f"RAG_SELECTION_MAX_EXCERPT_CHARS ({max_excerpt_chars}) — a single candidate must be able to fit"
        )
    if max_excerpt_chars > MAX_PASSAGE_CHARS:
        raise InvalidContextBudgetError(
            f"RAG_SELECTION_MAX_EXCERPT_CHARS ({max_excerpt_chars}) cannot exceed "
            f"passage_location.MAX_PASSAGE_CHARS ({MAX_PASSAGE_CHARS}) — no located passage is ever longer than that"
        )
    return ContextBudget(
        max_candidates=max_candidates, max_excerpt_chars=max_excerpt_chars,
        max_document_block_chars=max_document_block_chars,
    )


def truncate_label(source: str, max_len: int = MAX_SOURCE_LABEL_CHARS) -> str:
    """Bounds only the DISPLAY label used inside a candidate's header text
    — never the evidence's real `.source` field, and never the positional
    index selected_ids resolves against (ticket section 4: selection stays
    purely positional, "Référence N", so shortening this label can never
    change what an id resolves to)."""
    if len(source) <= max_len:
        return source
    return source[: max_len - 1] + "…"


def build_header(index: int, source: str) -> str:
    return f"--- Référence {index} ({truncate_label(source)}) ---\n"


def allocate_excerpt_budgets(candidates: list[RAGEvidence], budget: ContextBudget) -> list[int]:
    """Per-candidate character budget for the passage TEXT ALONE (headers
    already subtracted) — an even share of whatever room remains after
    every header, capped at `max_excerpt_chars`, never assuming N times
    the per-excerpt cap always fits inside the block budget (ticket
    section 1: "répartir le budget disponible en tenant compte des
    en-têtes, plutôt que supposer que six extraits de 800 caractères
    tiennent toujours"). Returns one entry per candidate, same order,
    possibly 0 for a candidate the block budget genuinely has no room for
    at all (an extreme case with a very small configured block budget or
    unusually long source names)."""
    if not candidates:
        return []
    headers_total = sum(len(build_header(i + 1, ev.source)) for i, ev in enumerate(candidates))
    remaining = max(budget.max_document_block_chars - headers_total, 0)
    equal_share = remaining // len(candidates)
    return [max(0, min(budget.max_excerpt_chars, equal_share)) for _ in candidates]


def bound_candidate_excerpt(evidence: RAGEvidence, excerpt_budget: int) -> RAGEvidence:
    """Re-locates WITHIN the evidence's own already-located `.content`
    (never re-slicing blindly, never reintroducing `content[:N]`) to fit
    `excerpt_budget`, using the evidence's own search query (the terms
    that made it relevant in the first place) as the relevance signal —
    see src.rag.passage_location.fit_window_to_budget for the clause-
    aware windowing itself.

    Returns the SAME evidence unchanged (not even a copy) when it already
    fits — ticket section 3's preference for "une seule représentation
    bornée" is honored by making this the ONLY place `.content`/
    `start_char`/`end_char` get set for what is actually sent to the LLM;
    a shrunk result carries its positions correctly translated back to
    canonical-text-relative offsets (`content == texte_canonique
    [start_char:end_char]` keeps holding), or `None`/`None` when the
    evidence had no known position to translate from (never a fabricated
    position). Never touches `content_fingerprint`/`document_version_id`/
    `duplicate_sources`/`score` — document identity and similarity are
    never recalculated on the reduced excerpt (ticket section 3 and 5)."""
    local_start, local_end = fit_window_to_budget(evidence.content, evidence.query, excerpt_budget)
    if local_start == 0 and local_end == len(evidence.content):
        return evidence

    new_start = evidence.start_char + local_start if evidence.start_char is not None else None
    new_end = evidence.start_char + local_end if evidence.start_char is not None else None
    return evidence.model_copy(update={
        "content": evidence.content[local_start:local_end],
        "start_char": new_start,
        "end_char": new_end,
    })


def build_candidates_block(candidates: list[RAGEvidence], budget: ContextBudget) -> tuple[str, list[RAGEvidence]]:
    """The single entry point src/rag/semantic_rerank.py::semantic_rerank
    calls: bounds every candidate's excerpt to its allocated share, builds
    the assembled document-block text, and enforces the overall block cap
    ONE LAST TIME right before the text is used (ticket section 4: "​
    Appliquer et vérifier la borne sur le bloc documentaire final juste
    avant l'appel") — a defensive backstop, never expected to trigger if
    the per-candidate allocation above is correct, but a rounding/edge
    case must never silently exceed the budget.

    Returns (candidates_text, bounded_candidates) — `bounded_candidates`
    is what the caller must use as its OWN candidate list from this point
    on (positional index i+1 unchanged, so selected_ids resolution is
    untouched), so that a selected reference's persisted `evidence_pack`
    entry is exactly the bounded excerpt actually shown to the LLM."""
    excerpt_budgets = allocate_excerpt_budgets(candidates, budget)
    bounded_candidates = [
        bound_candidate_excerpt(ev, excerpt_budget)
        for ev, excerpt_budget in zip(candidates, excerpt_budgets)
    ]
    candidates_text = "\n\n".join(
        f"{build_header(i + 1, ev.source)}{ev.content}"
        for i, ev in enumerate(bounded_candidates)
    )
    if len(candidates_text) > budget.max_document_block_chars:
        candidates_text = candidates_text[: budget.max_document_block_chars]
    return candidates_text, bounded_candidates
