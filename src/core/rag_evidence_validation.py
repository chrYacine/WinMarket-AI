"""B18-T1 — centralized validation for RAGEvidence.score (DEFECT-B04-04).

A negative, >1, NaN or +/-infinite similarity score used to leak straight
through ScoringEngine.score() into CriterionScore.score/score_global,
capable of producing an artificially favorable decision (see
docs/qa/validation_b04_20260914/BACKLOG_CORRECTIONS.md). This module is the
single place the acceptance rule is written; both enforcement points below
call the same function so the rule can never quietly drift into two
different definitions.
"""
from __future__ import annotations

import math

MIN_SIMILARITY = 0.0
MAX_SIMILARITY = 1.0


def validate_similarity_score(value: object) -> float:
    """The one rule for a valid RAGEvidence.score: a real, finite number in
    [0, 1] — a normalized cosine similarity. Never a boolean (Python's
    `bool` is an `int` subclass and would otherwise silently coerce to 0.0/
    1.0), a string, `None`, `NaN`, or +/-infinity.

    Deliberately does not accept a distance or a differently-scaled
    reranker output. This codebase's only two producers today
    (src/rag/private_rag_manager.py; the global src/rag/rag_manager.py was removed in lot 43) both compute a
    cosine similarity between non-negative TF-IDF vectors, which is
    mathematically bounded to [0, 1] — a future producer emitting a
    genuinely different scale must convert explicitly before ever building
    a RAGEvidence, never be implicitly reinterpreted as this similarity by
    this function.

    Used by BOTH:
    - RAGEvidence's own Pydantic field_validator (src/core/models.py),
      covering normal construction AND later attribute assignment (the
      model declares `validate_assignment=True` for exactly this reason —
      "a validator at construction time is not enough if an instance can
      be mutated afterward").
    - ensure_valid_evidences() below, the explicit re-check
      ScoringEngine.score() runs immediately before consuming evidences —
      this is what still catches a RAGEvidence built through a path that
      skips Pydantic validation entirely (e.g. `RAGEvidence.model_construct
      (...)`) or a duck-typed stand-in that was never a real RAGEvidence at
      all.

    Raises ValueError with a description safe to surface (it never quotes
    the caller's own data back) — callers needing a stable, public error
    code wrap this in InvalidRAGEvidenceError instead of relaying this
    message directly.
    """
    if isinstance(value, bool):
        raise ValueError("RAGEvidence.score must be a real number, not a boolean")
    if not isinstance(value, (int, float)):
        raise ValueError("RAGEvidence.score must be a real number")
    if not math.isfinite(value):
        raise ValueError("RAGEvidence.score must be finite (not NaN or +/-infinity)")
    if not (MIN_SIMILARITY <= value <= MAX_SIMILARITY):
        raise ValueError(f"RAGEvidence.score must be between {MIN_SIMILARITY} and {MAX_SIMILARITY} inclusive")
    return float(value)


class InvalidRAGEvidenceError(ValueError):
    """Raised by ensure_valid_evidences() — a controlled, terminal data
    error for the job/pipeline layer (src/web/jobs.py), reusing the same
    "catch a specific exception, set a safe job.error, stop" mechanism
    already used for ContentSecurityError. `error_code` is stable and
    intended to be surfaced to callers/clients; `user_message` never
    includes evidence content, source names, or any other raw document
    material — only this fixed, generic sentence. This is a DATA error
    (malformed RAG evidence), never an `enrichment_status="failed"` (which
    is reserved for the LLM enrichment step specifically, see B06-T3) —
    the two must not be conflated."""

    error_code = "invalid_rag_evidence"
    user_message = (
        "Une preuve de référence interne présente une valeur numérique invalide "
        "et empêche le calcul fiable du score. Réessayez ; si cela persiste, "
        "contactez le support."
    )

    def __init__(self) -> None:
        super().__init__(self.user_message)


def ensure_valid_evidences(evidences) -> None:
    """Explicit gate — call immediately before `evidences` is consumed by
    the scoring calculation. Raises InvalidRAGEvidenceError on the FIRST
    invalid evidence found: one invalid evidence among otherwise-valid ones
    must interrupt the whole calculation, never be silently dropped so a
    decision still gets produced (ticket B18-T1 sections 2-3) — dropping it
    would itself be a silent, undocumented change to which evidence the
    score is based on."""
    for evidence in evidences:
        try:
            validate_similarity_score(evidence.score)
        except (ValueError, TypeError, AttributeError):
            raise InvalidRAGEvidenceError() from None


# B18-T2 — the producer-side floating-point noise adapter. Deliberately a
# SEPARATE function from validate_similarity_score above: that one is the
# strict, epsilon-free acceptance gate used everywhere a score is actually
# consumed (RAGEvidence's own validator, ensure_valid_evidences); this one
# is used ONLY by the RAG producer (src/rag/private_rag_manager.py; the
# global src/rag/rag_manager.py was removed in lot 43), immediately after computing a raw
# cosine_similarity value and BEFORE any threshold filtering or sorting —
# a bug found in the first B18-T1 delivery let `min(1.0, max(0.0, x))`
# silently correct ANY out-of-range value (2.0 -> 1.0, -5.0 -> 0.0, even
# NaN comparisons behaving unpredictably) instead of only absorbing
# genuine floating-point rounding noise at the boundary.
SIMILARITY_ROUNDING_EPSILON = 1e-12


def sanitize_producer_similarity(value: object) -> float:
    """Type and finiteness are checked FIRST — a boolean, a string, `None`,
    or a non-finite float is rejected immediately, never reaching the
    epsilon logic below (no implicit "string/bool coerced to a valid
    number" path exists here, same as validate_similarity_score).

    A value already within [0, 1] passes through unchanged. A value that
    overshoots that range by no more than SIMILARITY_ROUNDING_EPSILON —
    the documented, tiny rounding noise `cosine_similarity` can produce
    right at the 0 or 1 boundary (e.g. 1.0000000000000002) — is clamped to
    the nearest bound. Any other out-of-range value (2.0, -5.0, an
    overshoot larger than the epsilon, ...) raises InvalidRAGEvidenceError
    directly here, at the producer, before it can be used in a `>
    threshold` comparison or a sort and silently vanish from the
    candidate list without ever being flagged."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidRAGEvidenceError()
    value = float(value)
    if not math.isfinite(value):
        raise InvalidRAGEvidenceError()
    if MIN_SIMILARITY <= value <= MAX_SIMILARITY:
        return value
    if MIN_SIMILARITY - SIMILARITY_ROUNDING_EPSILON <= value <= MAX_SIMILARITY + SIMILARITY_ROUNDING_EPSILON:
        return min(MAX_SIMILARITY, max(MIN_SIMILARITY, value))
    raise InvalidRAGEvidenceError()
