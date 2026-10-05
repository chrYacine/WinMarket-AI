"""Recette corpus utilisateur (2026-09-24), §4 — the reference_evidence
aggregation defect: a single document producing several passages (through
no choice of the account's — e.g. lot 51's token-based windowing splitting
one long section, or simply two relevant paragraphs) must count as ONE
reference, never several, and must never create an artificial score bonus
from chunking granularity alone. No DB, no network — a direct, precise
unit test of the evaluator itself.
"""
from __future__ import annotations

from src.agents.criteria_evaluators import EvalContext, _reference_evidence
from src.core.models import RAGEvidence
from src.core.reference_identity import group_evidences_by_reference

PARAMS = {"base_score": 30, "per_reference": 15, "similarity_weight": 40}


def _ctx(evidences: list) -> EvalContext:
    return EvalContext(ao=None, company=None, evidences=evidences, capacity=None, mastered=frozenset(), certifications_held=frozenset(), declared_facts={})


def test_two_passages_of_the_same_document_version_count_as_one_reference_not_two():
    same_version = "11111111-1111-1111-1111-111111111111"
    one_passage = [
        RAGEvidence(query="q", source="ref.md", score=0.8, content="Premier passage pertinent.", document_version_id=same_version),
    ]
    two_passages = [
        RAGEvidence(query="q", source="ref.md", score=0.8, content="Premier passage pertinent.", document_version_id=same_version),
        RAGEvidence(query="q", source="ref.md", score=0.6, content="Second passage, meme document.", document_version_id=same_version),
    ]

    out_one = _reference_evidence(PARAMS, _ctx(one_passage), {})
    out_two = _reference_evidence(PARAMS, _ctx(two_passages), {})

    assert "1 référence" in out_one.justification
    assert "1 référence" in out_two.justification, "two passages of the SAME document must still count as ONE reference"
    assert out_one.score == out_two.score, "an extra passage of the SAME document must never inflate the score"


def test_two_genuinely_different_documents_still_count_as_two_references():
    ev_a = RAGEvidence(query="q", source="a.md", score=0.8, content="Reference A.", document_version_id="aaaaaaaa-1111-1111-1111-111111111111")
    ev_b = RAGEvidence(query="q", source="b.md", score=0.7, content="Reference B, un autre document.", document_version_id="bbbbbbbb-2222-2222-2222-222222222222")
    out = _reference_evidence(PARAMS, _ctx([ev_a, ev_b]), {})
    assert "2 référence" in out.justification, "two genuinely distinct documents must still each count"


def test_the_per_reference_score_used_for_the_average_is_the_max_within_each_group_never_summed():
    same_version = "22222222-2222-2222-2222-222222222222"
    evidences = [
        RAGEvidence(query="q", source="ref.md", score=0.9, content="Meilleur passage.", document_version_id=same_version),
        RAGEvidence(query="q", source="ref.md", score=0.3, content="Passage plus faible, meme document.", document_version_id=same_version),
    ]
    out = _reference_evidence(PARAMS, _ctx(evidences), {})
    # base_score(30) + n(1)*per_reference(15) + avg(0.9)*similarity_weight(40) = 30+15+36 = 81
    assert out.score == 81.0, out.score


def test_zero_evidence_is_unaffected_base_score_only():
    out = _reference_evidence(PARAMS, _ctx([]), {})
    assert out.score == 30.0
    assert "0 référence" in out.justification


def test_evidence_without_a_document_version_id_falls_back_to_source_never_collapses_everything():
    """A synthetic/legacy RAGEvidence with no document_version_id must never
    be silently grouped with an UNRELATED reference just because both lack
    the field — falls back to `.source`, never a constant key."""
    ev_a = RAGEvidence(query="q", source="legacy_a.md", score=0.8, content="A")
    ev_b = RAGEvidence(query="q", source="legacy_b.md", score=0.7, content="B")
    groups = group_evidences_by_reference([ev_a, ev_b])
    assert len(groups) == 2


def test_group_evidences_by_reference_never_drops_a_citation():
    same_version = "33333333-3333-3333-3333-333333333333"
    evidences = [
        RAGEvidence(query="q", source="ref.md", score=0.9, content="A", document_version_id=same_version),
        RAGEvidence(query="q", source="ref.md", score=0.3, content="B", document_version_id=same_version),
    ]
    groups = group_evidences_by_reference(evidences)
    assert len(groups) == 1
    assert len(groups[0]) == 2, "both citations must remain available — only the COUNT is deduplicated, never the citations themselves"
