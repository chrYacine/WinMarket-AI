"""Shared, corpus-free RAG services used by the SaaS analysis path.

Lot 43: extracted from the former `src/rag/rag_manager.py::LocalRAGManager`
(a process-wide TF-IDF index over the demo corpus in `data/reg_docs`, only
ever consumed by the removed Streamlit demo). Only what the SaaS still needs
lives here:

- `FRENCH_STOP_WORDS`, reused by `src/rag/private_rag_manager.py`'s
  per-account vectorizer;
- `SemanticReranker.semantic_rerank`, the stateless LLM selection step applied
  to evidences produced by the account's own private corpus.

No corpus is loaded here and nothing is read from disk at construction.
"""
import threading
from pathlib import Path
from typing import List, Tuple

from src.core.config import LLM_TEMPERATURE_FACTUAL
from src.core.models import RAGEvidence
from src.core.prompt_loader import load_prompt
from src.rag.context_budget import build_candidates_block, get_context_budget
from src.rag.reference_selection import load_selection_prompt, validate_selection_response

# B16-T1: same shared loader already used for the reference-selection user
# prompt (src/rag/reference_selection.py::load_selection_prompt) and
# src/livrables/document_generator.py's prompts — the system prompt is a
# static string with no per-call variables.
_RAG_SYSTEM_PATH = Path(__file__).parent / "prompts" / "reference_selection_system.txt"

# French stop words for better TF-IDF results
FRENCH_STOP_WORDS = [
    "le","la","les","de","du","des","un","une","et","en","au","aux","par","pour",
    "sur","dans","avec","est","sont","a","ont","ou","mais","donc","car","si",
    "que","qui","quoi","dont","où","ce","se","sa","son","ses","leur","leurs",
    "nous","vous","ils","elles","je","tu","il","elle","on","mon","ton","ma",
    "ta","mes","tes","cette","cet","ces","plus","très","aussi","comme","tout",
    "tous","bien","peut","être","fait","faire","avoir","notre","votre","leur"
]


class SemanticReranker:
    def __init__(self):
        # B18-T3: set by semantic_rerank() as a side effect of its last call
        # on THIS thread — a caller that needs to persist rag_selection_status
        # on a ScoringResult (src/web/jobs.py) reads these right after calling
        # semantic_rerank(), rather than semantic_rerank() itself returning a
        # 3-tuple.
        #
        # threading.local(), not a plain instance attribute: a reranker may be
        # shared by concurrent job threads — a plain attribute would let two
        # analyses launched at the same time overwrite each other's status
        # before either reads it back. Each thread gets its own isolated slot
        # on the same object instead.
        self._selection_state = threading.local()

    @property
    def last_selection_status(self) -> str:
        return getattr(self._selection_state, "status", "not_attempted")

    @property
    def last_selection_reason(self) -> str | None:
        return getattr(self._selection_state, "reason", None)

    def _set_selection_state(self, status: str, reason: str | None) -> None:
        self._selection_state.status = status
        self._selection_state.reason = reason

    def _rag_system_prompt(self) -> str:
        """B16-T1: loaded from src/rag/prompts/reference_selection_system.txt
        (same shared load_prompt loader as the rest of this codebase's
        externalized prompts) — a static string, no per-call variables,
        loaded lazily here (never at import/class-definition time) so a
        missing file only ever surfaces when a reranking actually happens,
        not merely on import — the "not_attempted" fast path in
        semantic_rerank (llm disabled / no evidences) never touches it."""
        return load_prompt(_RAG_SYSTEM_PATH)

    def semantic_rerank(self, ao_text: str, evidences: List[RAGEvidence], llm) -> Tuple[List[RAGEvidence], str]:
        """Selects (never merely reorders) which evidences to keep, using
        the model's `selected_ids` — see src/rag/reference_selection.py
        for the full contract this closes (DEFECT F12/E-4): a candidate
        NOT in `selected_ids` is genuinely excluded, never silently
        appended back. Sets self.last_selection_status/last_selection_reason
        as a side effect (see __init__ for why this isn't a 3-tuple return).

        Only the first `context_budget.get_context_budget().max_candidates`
        evidences (6 by default) are ever PRESENTED as candidates — an id
        is only ever resolved against THESE, never the full `evidences`
        list; anything beyond that position is never a candidate and can
        neither be selected nor re-added afterward, success or fallback."""
        if not llm.enabled or not evidences:
            # Zero LLM calls either way — "not_attempted" covers both an
            # explicitly disabled LLM and the trivial empty-input case.
            self._set_selection_state("not_attempted", None)
            return evidences, ""

        # B18-T6 (complement to B18-T5, related to B15): src/rag/
        # context_budget.py centralizes and validates the three limits
        # (candidate count, per-excerpt cap, total block cap) and
        # re-localizes WITHIN each candidate's own already-located passage
        # (never a blind slice) to fit its allocated share — see that module
        # for the full allocation/fitting logic. `bounded_candidates` (not the
        # original `candidates`) is what selected_ids resolves against below,
        # so a selected reference's persisted evidence is exactly the bounded
        # excerpt actually shown to the LLM (a single bounded representation,
        # never a second parallel "what was sent" record — ticket section 3).
        budget = get_context_budget()
        candidates = evidences[:budget.max_candidates]
        candidates_text, bounded_candidates = build_candidates_block(candidates, budget)
        prompt = load_selection_prompt(ao_text=ao_text[:2500], candidates_text=candidates_text)

        try:
            data = llm.json_complete(prompt, system=self._rag_system_prompt(), temperature=LLM_TEMPERATURE_FACTUAL)
        except Exception:
            # Fallback: keep the INITIAL candidates exactly as received —
            # never a partial selection, never the rejected response's own
            # synthesis (ticket section 4).
            self._set_selection_state("fallback", "provider_exception")
            return evidences, ""
        if not data:
            self._set_selection_state("fallback", "no_content")
            return evidences, ""

        selected_ids, error_reason = validate_selection_response(data, len(candidates))
        if error_reason is not None:
            self._set_selection_state("fallback", error_reason)
            return evidences, ""

        # Success — an empty selected_ids is a fully valid, deliberate
        # "keep nothing" outcome, not a fallback trigger. Resolved against
        # `bounded_candidates` (same order/positions as `candidates`), so
        # the persisted evidence_pack entry matches what the LLM actually
        # saw (ticket section 3).
        selected = [bounded_candidates[i - 1] for i in selected_ids]
        self._set_selection_state("applied", None)
        synthese = data.get("synthese") if selected_ids else ""
        if not isinstance(synthese, str):
            synthese = ""
        return selected, synthese
