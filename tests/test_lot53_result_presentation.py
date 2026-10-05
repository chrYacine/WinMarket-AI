"""Lot 53 — direct, no-DB unit tests of src/web/result_presentation.py (the shared, typed presentation
projection reused by the result page AND the PDF/DOCX generator — see that module's own docstring)."""
from __future__ import annotations

from src.core.models import CriterionScore, RAGEvidence, ScoringResult
from src.web.result_presentation import PolicyLabel, build_result_view, build_revision_diff


def _result(**kw) -> ScoringResult:
    base = dict(decision="GO", score_global=80.0, criteres=[])
    base.update(kw)
    return ScoringResult(**base)


def _crit(cid, nom, score, etat=None) -> CriterionScore:
    return CriterionScore(nom=nom, poids=50, score=score, justification="x", critere_id=cid, etat=etat)


class _Complement:
    def __init__(self, subject, field_label, value_json, unit, created_at, origin="declared_user", source_json=None):
        self.subject, self.field_label, self.value_json, self.unit = subject, field_label, value_json, unit
        self.created_at, self.origin, self.source_json = created_at, origin, source_json


def test_policy_label_text_names_legacy_vs_user_origin():
    assert PolicyLabel(version=3, origin="user").text == "v3 (votre politique)"
    assert PolicyLabel(version=1, origin="legacy").text == "v1 (politique historique migrée)"
    assert PolicyLabel(version=None, origin="user").text is None, "no version ever fabricates a placeholder"


def test_reference_count_uses_group_evidences_by_reference_not_raw_passage_count():
    same_version = "11111111-1111-1111-1111-111111111111"
    evidences = [
        RAGEvidence(query="q", source="ref.md", score=0.8, content="A", document_version_id=same_version),
        RAGEvidence(query="q", source="ref.md", score=0.6, content="B", document_version_id=same_version),
    ]
    view = build_result_view(_result(evidence_pack=evidences), [], scoring_policy_version=1, parent_result=None)
    assert view.reference_count == 1, "two passages of the SAME document must count as ONE reference in the display too"
    assert view.reference_passage_count == 2


def test_revision_diff_lists_only_criteria_that_actually_changed():
    parent = _result(decision="INCOMPLET", score_global=40.0, criteres=[_crit("a", "A", 50.0), _crit("b", "B", 80.0)])
    current = _result(decision="GO", score_global=90.0, criteres=[_crit("a", "A", 100.0), _crit("b", "B", 80.0)])
    diff = build_revision_diff(parent, current)
    assert diff.decision_before == "INCOMPLET" and diff.decision_after == "GO"
    assert len(diff.criteria_changed) == 1 and diff.criteria_changed[0].critere_id == "a"
    assert diff.criteria_changed[0].score_before == 50.0 and diff.criteria_changed[0].score_after == 100.0


def test_revision_diff_none_for_an_ordinary_non_revision_analysis():
    assert build_revision_diff(None, _result()) is None


def test_revision_diff_handles_a_criterion_added_or_removed_between_policy_versions():
    parent = _result(criteres=[_crit("a", "A", 50.0)])
    current = _result(criteres=[_crit("a", "A", 50.0), _crit("c", "C", 70.0)])
    diff = build_revision_diff(parent, current)
    added = next(d for d in diff.criteria_changed if d.critere_id == "c")
    assert added.score_before is None and added.score_after == 70.0


def test_complement_view_exposes_citation_only_for_llm_sourced_origin():
    c1 = _Complement("prestataire", "Fréquence", 5, "par_semaine", "2026-01-01",
                      origin="llm_sourced", source_json={"kind": "knowledge_document", "citation": "5 fois par semaine", "source_label": "doc.md"})
    c2 = _Complement("ao", "Budget", 150000, None, "2026-01-01", origin="declared_user", source_json=None)
    view = build_result_view(_result(), [c1, c2], scoring_policy_version=1, parent_result=None)
    sourced, declared = view.complements
    assert sourced.origin_label == "Proposition documentaire acceptée (citation retrouvée)"
    assert sourced.citation == "5 fois par semaine" and sourced.source_label == "doc.md"
    assert declared.origin_label == "Déclaré par vous" and declared.citation is None
