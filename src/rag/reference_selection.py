"""B18-T3 (DEFECT F12/E-4) — shared contract for the reference-SELECTION
step of RAG reranking, used by src/rag/semantic_rerank.py::SemanticReranker.
semantic_rerank (the only implementation — src/rag/private_rag_manager.py
has no reranking of its own, see that method's own docstring).

The bug this closes: the LLM was asked for a "ranking" (an ORDER over all
candidates), and a candidate simply omitted from that ranking — the
correct outcome when it's irrelevant — was silently appended back at the
end by the old code (`for ev in evidences: if id(ev) not in seen:
reranked.append(ev)`), defeating the entire point of asking the model to
exclude it. This module separates SELECTION (which references to keep —
possibly none) from ORDER (how to present the kept ones), and treats
"selected_ids": [] as a fully valid, successful selection — never a
fallback trigger.
"""
from __future__ import annotations

from pathlib import Path

from src.core.prompt_loader import load_prompt

_PROMPT_PATH = Path(__file__).parent / "prompts" / "reference_selection.txt"


def load_selection_prompt(*, ao_text: str, candidates_text: str) -> str:
    """Thin wrapper over the shared src.core.prompt_loader.load_prompt —
    this was the first ticket (B18-T3) to need a prompt in a dedicated
    text file rather than inline in Python (CLAUDE.md's own convention);
    the loader itself was generalized in B05-T2 once a second prompt
    (src/agents/prompts/ao_extraction_user.txt) needed the exact same
    mechanism, rather than writing a second ad hoc one."""
    return load_prompt(_PROMPT_PATH, ao_text=ao_text, candidates_text=candidates_text)


def validate_selection_response(data: object, num_candidates: int) -> tuple[list[int] | None, str | None]:
    """The one rule for a valid selection response — an object with a
    `selected_ids` list of integers, each naming a candidate ACTUALLY
    PRESENTED for this call (1-based, 1..num_candidates — never the size
    of some larger, unrelated list; an id valid for a different call with
    more candidates is still rejected here, ticket section 2: "si 6
    références sur 8 sont présentées, les deux autres ne peuvent pas être
    sélectionnées"). Duplicate ids are removed, keeping the first
    occurrence's position — this is NOT a stand-in for real document/
    project deduplication (B18-T4), only removing a literal repeated
    integer in this one response.

    Returns (ordered_deduped_ids, None) on success — an empty list is a
    fully valid result, distinct from failure. Returns (None, reason_code)
    on ANY validation failure, `reason_code` being one of a small, fixed,
    safe vocabulary — never a raw exception message, the model's own text,
    or document content."""
    if not isinstance(data, dict):
        return None, "invalid_response_shape"
    if "selected_ids" not in data:
        return None, "missing_selected_ids"
    raw_ids = data["selected_ids"]
    if not isinstance(raw_ids, list):
        return None, "selected_ids_not_a_list"

    seen: set[int] = set()
    ordered: list[int] = []
    for item in raw_ids:
        if isinstance(item, bool) or not isinstance(item, int):
            return None, "invalid_id_type"
        if not (1 <= item <= num_candidates):
            return None, "unknown_id"
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered, None
