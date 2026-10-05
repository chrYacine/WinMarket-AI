"""B18-T4 (DEFECT F09/F12/E-5) — test group A: engine-level guarantee that
repeating a reference (even as genuinely distinct RAGEvidence objects with
identical content) does not inflate "Références similaires"/score_global/
decision, while two REALLY different references stay fully counted.

Explicitly out of scope, per the ticket itself: any "same real-world
project, different wording" grouping — this codebase has no reliable
project identity to group on (no LLM matching, no fuzzy title matching
here). Only EXACT content duplicates are recognized; that limitation is
asserted, not glossed over.
"""
from __future__ import annotations

import math

import pytest

from src.agents.scoring_engine import ScoringEngine
from src.core.models import AOContext, CapacityResult, CompanyProfile, RAGEvidence
from src.core.rag_evidence_validation import InvalidRAGEvidenceError
from src.core.reference_identity import compute_content_fingerprint, deduplicate_evidences
from tests.synthetic_scoring import score_with_synthetic_policy


def make_ao(**overrides) -> AOContext:
    defaults = dict(
        titre="AO synthétique", client="Client Synthétique", secteur="Retail",
        budget_estime=200_000, deadline_reponse="", duree_projet_mois=None,
        technologies_demandees=[], competences_requises=[], questions_client=[],
        livrables=[], contraintes=[], certifications_obligatoires=[],
        texte_source="Marché de développement logiciel.",
    )
    defaults.update(overrides)
    return AOContext(**defaults)


def make_capacity(**overrides) -> CapacityResult:
    defaults = dict(charge_actuelle_pct=50, capacite_restante_pct=50, equipe_disponible=True, commentaire="Équipe disponible.")
    defaults.update(overrides)
    return CapacityResult(**defaults)


def criterion(result, label_display: str):
    match = next((c for c in result.criteres if c.nom == label_display), None)
    assert match is not None, f"critère {label_display!r} introuvable"
    return match


REFERENCE_TEXT = "Reference projet portail client — migration cloud AWS, stack Python/Django, secteur retail."


def test_one_reference_vs_several_exact_copies_yields_identical_values_and_decision():
    """Independent oracle: with a SINGLE unique reference at score=0.8,
    ref_score = min(100, 30 + 1*6 + 0.8*40) = min(100, 30+6+32) = 68 — hand-
    computed from the documented formula, not re-derived from the engine."""
    ao = make_ao()
    company = CompanyProfile()
    capacity = make_capacity()

    single = [RAGEvidence(query="q", source="a.md", score=0.8, content=REFERENCE_TEXT)]
    result_single = score_with_synthetic_policy(ao, company, single, capacity)
    ref_crit_single = criterion(result_single, "Références similaires")
    assert ref_crit_single.score == pytest.approx(68.0, abs=1e-6)

    # Same content, repeated 5 times, as genuinely DISTINCT RAGEvidence
    # objects (not the same Python object reused) — different `source`
    # values too, exactly what a renamed-copy upload would look like.
    repeated = [
        RAGEvidence(query="q", source=f"copy_{i}.md", score=0.8, content=REFERENCE_TEXT)
        for i in range(5)
    ]
    assert len({id(ev) for ev in repeated}) == 5, "must be 5 distinct objects, not one object aliased"
    result_repeated = score_with_synthetic_policy(ao, company, repeated, capacity)
    ref_crit_repeated = criterion(result_repeated, "Références similaires")

    assert ref_crit_repeated.score == ref_crit_single.score == pytest.approx(68.0, abs=1e-6)
    assert result_repeated.score_global == result_single.score_global
    assert result_repeated.decision == result_single.decision
    assert len(result_repeated.evidence_pack) == 1, "5 exact copies must collapse to exactly 1 in evidence_pack"


def test_two_genuinely_different_references_both_count():
    """Independent oracle: 2 DIFFERENT references at avg_sim=(0.6+0.8)/2=0.7
    -> ref_score = min(100, 30 + 2*6 + 0.7*40) = min(100, 30+12+28) = 70."""
    ao = make_ao()
    evidences = [
        RAGEvidence(query="q", source="a.md", score=0.6, content="Reference A — projet data/IA secteur banque."),
        RAGEvidence(query="q", source="b.md", score=0.8, content="Reference B — projet cybersecurite secteur sante."),
    ]
    result = score_with_synthetic_policy(ao, CompanyProfile(), evidences, make_capacity())
    assert criterion(result, "Références similaires").score == pytest.approx(70.0, abs=1e-6)
    assert len(result.evidence_pack) == 2


def test_duplicate_with_an_invalid_score_still_raises():
    """Validated BEFORE deduplication — an invalid duplicate must never
    hide behind a valid twin with the same content."""
    ao = make_ao()
    valid = RAGEvidence(query="q", source="a.md", score=0.8, content=REFERENCE_TEXT)
    invalid_duplicate = RAGEvidence.model_construct(query="q", source="b.md", score=float("nan"), content=REFERENCE_TEXT, duplicate_sources=[])
    with pytest.raises(InvalidRAGEvidenceError):
        score_with_synthetic_policy(ao, CompanyProfile(), [valid, invalid_duplicate], make_capacity())


def test_best_score_among_duplicates_is_kept_never_averaged_or_summed():
    """Independent oracle: best of {0.4, 0.9} is 0.9 -> ref_score =
    min(100, 30 + 1*6 + 0.9*40) = 72. NOT the average (0.65 -> 30+6+26=62),
    NOT the sum."""
    ao = make_ao()
    evidences = [
        RAGEvidence(query="q", source="a.md", score=0.4, content=REFERENCE_TEXT),
        RAGEvidence(query="q", source="b.md", score=0.9, content=REFERENCE_TEXT),
    ]
    result = score_with_synthetic_policy(ao, CompanyProfile(), evidences, make_capacity())
    assert criterion(result, "Références similaires").score == pytest.approx(72.0, abs=1e-6)
    assert result.evidence_pack[0].score == 0.9
    assert result.evidence_pack[0].duplicate_sources == ["b.md"] or result.evidence_pack[0].duplicate_sources == ["a.md"]


def test_deduplicate_evidences_unit_level_preserves_order_and_traceability():
    a = RAGEvidence(query="q", source="a.md", score=0.5, content="X")
    b = RAGEvidence(query="q", source="b.md", score=0.9, content="X")  # exact duplicate content of a
    c = RAGEvidence(query="q", source="c.md", score=0.3, content="Y")  # genuinely different
    result = deduplicate_evidences([a, b, c])
    assert len(result) == 2
    assert result[0].score == 0.9  # best of the a/b duplicate group
    assert set(result[0].duplicate_sources) == {"a.md"} or set(result[0].duplicate_sources) == {"b.md"}
    assert result[1].source == "c.md"


def test_content_fingerprint_distinguishes_shared_prefix_from_different_ending():
    """Explicit, isolated proof of the documented safety property: two
    texts sharing a long identical prefix but differing at the end must
    fingerprint DIFFERENTLY — never merged just because of the prefix."""
    shared_prefix = "Reference projet portail client, stack technique identique, "
    text_a = shared_prefix + "livré en 2024 pour le secteur banque."
    text_b = shared_prefix + "livré en 2025 pour le secteur sante, budget different."
    assert compute_content_fingerprint(text_a) != compute_content_fingerprint(text_b)

    ao = make_ao()
    evidences = [
        RAGEvidence(query="q", source="a.md", score=0.7, content=text_a),
        RAGEvidence(query="q", source="b.md", score=0.7, content=text_b),
    ]
    result = score_with_synthetic_policy(ao, CompanyProfile(), evidences, make_capacity())
    assert len(result.evidence_pack) == 2, "same-prefix, different-ending documents must NOT be merged"


def test_project_level_grouping_is_explicitly_not_implemented():
    """B18-T4 section 2's documented limitation: no reliable project
    identity exists in this schema, so two DIFFERENT documents that a
    human might recognize as "the same project, described twice" are
    NOT merged — only byte-for-byte (after minimal normalization) content
    identity is. This test exists to make that boundary explicit and
    testable, not to claim project-level dedup works."""
    ao = make_ao()
    # Same underlying "project" in spirit, worded differently — must NOT
    # be recognized as one project by this ticket's mechanism.
    evidences = [
        RAGEvidence(query="q", source="a.md", score=0.7, content="Portail client livré pour Acme Corp en 2024, stack Python."),
        RAGEvidence(query="q", source="b.md", score=0.7, content="Nous avons realise le portail web d'Acme Corp l'an dernier avec Python."),
    ]
    result = score_with_synthetic_policy(ao, CompanyProfile(), evidences, make_capacity())
    assert len(result.evidence_pack) == 2, (
        "documents describing the same real project in different words are "
        "NOT deduplicated by this ticket — only exact content matches are"
    )
