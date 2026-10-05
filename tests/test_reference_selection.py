"""B18-T3 (DEFECT F12/E-4) — reference SELECTION (not just reordering) for
RAG evidences. The bug: the LLM answered `ranking=[1]` (excluding an
off-topic second reference), and the old code re-appended every candidate
missing from the ranking anyway — the excluded reference still reached
scoring. This file exercises test groups A and B directly against
src/rag/semantic_rerank.py::SemanticReranker.semantic_rerank (the ONLY
implementation — src/rag/private_rag_manager.py has none of its own;
lot 43 moved it out of the removed LocalRAGManager unchanged) and
src/rag/reference_selection.py::validate_selection_response in isolation.

No real LLM/network anywhere — a minimal fake `llm` object exposing only
`.enabled`/`.json_complete(...)`, same idiom as
tests/test_validation_b04_scoring_defects.py.
"""
from __future__ import annotations

import pytest

from src.core.models import RAGEvidence
from src.rag.reference_selection import validate_selection_response
from src.rag.semantic_rerank import SemanticReranker


def make_evidence(source: str, score: float = 0.5) -> RAGEvidence:
    return RAGEvidence(query="q", source=source, score=score, content=f"Contenu de {source}.")


class _FakeLLM:
    def __init__(self, payload=None, exc: Exception | None = None, enabled: bool = True):
        self.enabled = enabled
        self._payload = payload
        self._exc = exc
        self.calls = 0

    def json_complete(self, *_args, **_kwargs):
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return self._payload


@pytest.fixture()
def manager():
    """A corpus-free SemanticReranker — semantic_rerank only ever reads the
    `evidences` list passed to it."""
    return SemanticReranker()


# ---------------------------------------------------------------------------
# A — selection vs. order, exactly what the audited bug was about.
# ---------------------------------------------------------------------------

def test_selecting_one_of_two_excludes_the_other_for_real(manager):
    """THE regression this ticket exists for: ranking=[1] (only A) must
    result in ONLY A — B must never reappear."""
    a, b = make_evidence("a.md"), make_evidence("b.md")
    llm = _FakeLLM({"selected_ids": [1], "synthese": "A est pertinente."})
    result, synthese = manager.semantic_rerank("AO texte", [a, b], llm)
    assert result == [a]
    assert "A est pertinente." == synthese
    assert manager.last_selection_status == "applied"


def test_empty_selection_is_valid_and_returns_no_evidence(manager):
    a, b = make_evidence("a.md"), make_evidence("b.md")
    llm = _FakeLLM({"selected_ids": [], "synthese": "ignorée, doit être vidée"})
    result, synthese = manager.semantic_rerank("AO texte", [a, b], llm)
    assert result == []
    assert synthese == "", "a synthesis must never accompany an empty selection, even if the LLM sent one"
    assert manager.last_selection_status == "applied", "an empty selection is a SUCCESSFUL outcome, not a fallback"


def test_order_is_preserved_and_repeated_ids_are_deduplicated(manager):
    a, b, c = make_evidence("a.md"), make_evidence("b.md"), make_evidence("c.md")
    llm = _FakeLLM({"selected_ids": [3, 1, 3, 1], "synthese": "C puis A."})
    result, _ = manager.semantic_rerank("AO texte", [a, b, c], llm)
    assert result == [c, a], "order from selected_ids preserved, repeats collapsed to first occurrence"


def test_id_beyond_the_presented_candidates_is_invalid_even_if_more_evidences_exist(manager):
    """8 evidences retrieved, only the first 6 are ever presented — an id
    of 7 or 8 must be rejected, not silently resolved against the full
    underlying list (ticket section 2)."""
    evidences = [make_evidence(f"{i}.md") for i in range(1, 9)]
    llm = _FakeLLM({"selected_ids": [7], "synthese": "invalide"})
    result, synthese = manager.semantic_rerank("AO texte", evidences, llm)
    assert manager.last_selection_status == "fallback"
    assert manager.last_selection_reason == "unknown_id"
    assert result == evidences, "fallback must return the ORIGINAL full candidate list untouched"
    assert synthese == ""


def test_validate_selection_response_unit_level_matrix():
    """Direct unit coverage of the shared validator, complementing the
    end-to-end cases above."""
    assert validate_selection_response({"selected_ids": [2, 1]}, num_candidates=3) == ([2, 1], None)
    assert validate_selection_response({"selected_ids": []}, num_candidates=3) == ([], None)
    assert validate_selection_response([1, 2], num_candidates=3) == (None, "invalid_response_shape")
    assert validate_selection_response({}, num_candidates=3) == (None, "missing_selected_ids")
    assert validate_selection_response({"selected_ids": "1,2"}, num_candidates=3) == (None, "selected_ids_not_a_list")
    assert validate_selection_response({"selected_ids": [True]}, num_candidates=3) == (None, "invalid_id_type")
    assert validate_selection_response({"selected_ids": ["1"]}, num_candidates=3) == (None, "invalid_id_type")
    assert validate_selection_response({"selected_ids": [0]}, num_candidates=3) == (None, "unknown_id")
    assert validate_selection_response({"selected_ids": [4]}, num_candidates=3) == (None, "unknown_id")


# ---------------------------------------------------------------------------
# B — malformed responses: explicit fallback, original candidates
# untouched, no reused synthesis, zero calls when disabled/no input.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("payload,expected_reason", [
    pytest.param([1, 2], "invalid_response_shape", id="non_object_response"),
    pytest.param({"synthese": "texte qui ne doit jamais être réutilisé"}, "missing_selected_ids", id="missing_field"),
    pytest.param({"selected_ids": "oops"}, "selected_ids_not_a_list", id="wrong_type_field"),
    pytest.param({"selected_ids": [99], "synthese": "texte qui ne doit jamais être réutilisé"}, "unknown_id", id="unknown_id"),
    pytest.param({"selected_ids": [True]}, "invalid_id_type", id="boolean_id"),
    pytest.param(None, "no_content", id="empty_response"),
])
def test_malformed_or_empty_responses_fall_back_to_original_candidates(manager, payload, expected_reason):
    a, b = make_evidence("a.md"), make_evidence("b.md")
    llm = _FakeLLM(payload)
    result, synthese = manager.semantic_rerank("AO texte", [a, b], llm)
    assert result == [a, b], "fallback must keep the initial candidates exactly, never a partial selection"
    assert synthese == "", "a rejected response's own synthesis must never be reused"
    assert manager.last_selection_status == "fallback"
    assert manager.last_selection_reason == expected_reason
    assert llm.calls == 1, "no retry to repair the response — one call only"


def test_provider_exception_falls_back_to_original_candidates(manager):
    a, b = make_evidence("a.md"), make_evidence("b.md")
    llm = _FakeLLM(exc=RuntimeError("simulated provider failure — must never leak into reason"))
    result, synthese = manager.semantic_rerank("AO texte", [a, b], llm)
    assert result == [a, b]
    assert synthese == ""
    assert manager.last_selection_status == "fallback"
    assert manager.last_selection_reason == "provider_exception"


def test_disabled_llm_makes_zero_calls(manager):
    a, b = make_evidence("a.md"), make_evidence("b.md")
    llm = _FakeLLM(enabled=False)
    result, synthese = manager.semantic_rerank("AO texte", [a, b], llm)
    assert result == [a, b]
    assert synthese == ""
    assert llm.calls == 0
    assert manager.last_selection_status == "not_attempted"
    assert manager.last_selection_reason is None


def test_empty_evidence_list_makes_zero_calls(manager):
    llm = _FakeLLM(payload={"selected_ids": []})
    result, synthese = manager.semantic_rerank("AO texte", [], llm)
    assert result == []
    assert llm.calls == 0, "no candidates at all -> no LLM call, per contract"
    assert manager.last_selection_status == "not_attempted"
