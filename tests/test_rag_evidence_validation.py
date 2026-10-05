"""B18-T1 — DEFECT-B04-04 correction: RAGEvidence.score is now validated
as a normalized similarity in [0, 1] — a real, finite number, never a
boolean/string/None/NaN/+-infinity. Two enforcement points share the same
rule (src/core/rag_evidence_validation.py::validate_similarity_score):
RAGEvidence's own Pydantic field_validator (construction AND assignment),
and ScoringEngine.score()'s explicit ensure_valid_evidences() call
immediately before evidences are consumed — this second point is what
still catches an evidence built through a path that skips Pydantic
entirely (RAGEvidence.model_construct(...)).

No real RAG/LLM/network involved anywhere in this file — pure model/engine
level tests with synthetic RAGEvidence instances.
"""
from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from src.agents.scoring_engine import ScoringEngine
from src.core.models import AOContext, CapacityResult, CompanyProfile, RAGEvidence
from src.core.rag_evidence_validation import InvalidRAGEvidenceError, validate_similarity_score
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


# ---------------------------------------------------------------------------
# A — parametrized domain test.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("valid_score", [0.0, 0.5, 1.0], ids=["zero", "intermediate", "one"])
def test_valid_similarity_scores_are_accepted(valid_score):
    evidence = RAGEvidence(query="q", source="ref.md", score=valid_score, content="c")
    assert evidence.score == valid_score


@pytest.mark.parametrize("invalid_value,label", [
    (-0.0001, "just_below_zero"),
    (-5.0, "clearly_negative"),
    (1.0001, "just_above_one"),
    (5.0, "clearly_above_one"),
    (math.nan, "nan"),
    (math.inf, "positive_infinity"),
    (-math.inf, "negative_infinity"),
    (True, "boolean_true"),
    (False, "boolean_false"),
    ("0.5", "numeric_string"),
    (None, "null"),
])
def test_invalid_similarity_scores_are_rejected_at_construction(invalid_value, label):
    with pytest.raises(ValidationError):
        RAGEvidence(query="q", source="ref.md", score=invalid_value, content="c")


def test_boolean_is_rejected_even_though_it_is_an_int_subclass():
    """Explicit, isolated case: Python's bool IS an int, so a naive
    `isinstance(x, (int, float))` check alone would silently accept True/
    False and let Pydantic coerce them to 1.0/0.0 — validate_similarity_score
    checks for bool BEFORE the numeric check specifically to prevent this."""
    with pytest.raises(ValueError):
        validate_similarity_score(True)


def test_invalid_score_is_refused_even_on_an_instance_that_bypassed_construction():
    """The construction-bypassing case the ticket asks for explicitly:
    RAGEvidence.model_construct() skips Pydantic validation entirely (it's
    the documented "no validation" escape hatch) — proving the ENGINE's own
    ensure_valid_evidences() call, not just the model's constructor, is
    what makes an invalid evidence unusable."""
    from src.core.rag_evidence_validation import ensure_valid_evidences

    bypassed = RAGEvidence.model_construct(query="q", source="ref.md", score=-5.0, content="c")
    assert bypassed.score == -5.0, "model_construct() really did skip validation"
    with pytest.raises(InvalidRAGEvidenceError):
        ensure_valid_evidences([bypassed])


def test_score_cannot_be_mutated_to_an_invalid_value_after_construction():
    """validate_assignment=True closes the "valid at birth, mutated later"
    path — a validator only at __init__ time would miss this."""
    evidence = RAGEvidence(query="q", source="ref.md", score=0.5, content="c")
    with pytest.raises(ValidationError):
        evidence.score = -5.0


# ---------------------------------------------------------------------------
# B — real calculation: valid set keeps its expected (independently
# computed) value; no-evidence case is unchanged; mixed valid/invalid is
# refused outright, never a partial result.
# ---------------------------------------------------------------------------

def test_valid_evidence_set_produces_the_independently_computed_reference_score():
    """Expected value computed BY HAND from the documented formula
    (ref_score = min(100, 30 + n_ev*6 + avg_sim*40)), never by calling
    ScoringEngine itself: n_ev=3, avg_sim=(0.2+0.5+0.9)/3=0.5+1/15≈0.5333
    -> 30 + 18 + 21.333... = 69.333..."""
    ao = make_ao()
    evidences = [
        RAGEvidence(query="q", source="a.md", score=0.2, content="a"),
        RAGEvidence(query="q", source="b.md", score=0.5, content="b"),
        RAGEvidence(query="q", source="c.md", score=0.9, content="c"),
    ]
    result = score_with_synthetic_policy(ao, CompanyProfile(), evidences, make_capacity())
    assert criterion(result, "Références similaires").score == pytest.approx(69.333, abs=0.01)


def test_no_evidence_case_is_unchanged_by_this_correction():
    """The empty-corpus case (n_ev=0) is untouched — ensure_valid_evidences
    on an empty list is a no-op, and the formula's own n_ev=0 branch
    (ref_score = 30) is exactly what it was before B18-T1."""
    ao = make_ao()
    result = score_with_synthetic_policy(ao, CompanyProfile(), [], make_capacity())
    assert criterion(result, "Références similaires").score == 30.0


def test_mixed_valid_and_invalid_evidences_refuses_without_a_partial_result():
    from src.core.rag_evidence_validation import ensure_valid_evidences

    valid_evidences = [
        RAGEvidence(query="q", source="a.md", score=0.4, content="a"),
        RAGEvidence.model_construct(query="q", source="bad.md", score=float("nan"), content="b"),
    ]
    with pytest.raises(InvalidRAGEvidenceError):
        ensure_valid_evidences(valid_evidences)
    # ScoringEngine.score() itself must refuse before computing anything —
    # no CriterionScore/ScoringResult is ever returned for this input.
    with pytest.raises(InvalidRAGEvidenceError):
        score_with_synthetic_policy(make_ao(), CompanyProfile(), valid_evidences, make_capacity())
