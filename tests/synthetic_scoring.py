"""Lot 43 — the former process-wide scoring demo values, kept as TEST DATA.

Until lot 43, `ScoringEngine` carried class-level weights / mastered
technologies / held certifications and read global thresholds from the
environment whenever `policy=None`. Those values are gone from the product;
the many existing tests that assert concrete scores and decisions keep their
assertions unchanged by passing THE SAME numbers explicitly, through the
account-style `ScoringPolicySnapshot` the engine now requires. Nothing here is
imported by application code, and no test relies on a hidden default.

`tests/fixtures/lot43_scoring_golden_before_cleanup.json` (recorded with the
pre-cleanup code) proves that this snapshot reproduces the legacy results
byte for byte (tests/test_lot43_cleanup.py).
"""
from __future__ import annotations

from src.agents.scoring_engine import ScoringEngine, ScoringPolicySnapshot

SYNTHETIC_WEIGHTS = {
    "Adequation expertise":     20,
    "References similaires":    15,
    "Disponibilite equipe":     10,
    "Rentabilite estimee":      10,
    "Faisabilite delai":        10,
    "Certifications requises":  10,
    "Complexite technique":      5,
    "Connaissance secteur":      5,
    "Potentiel commercial":      5,
    "Risque contractuel":        5,
    "Solidite client":           3,
    "Valeur strategique":        2,
}
SYNTHETIC_MASTERED = frozenset({
    "react", "angular", "vue", "java", "spring", "python", "django", "fastapi",
    ".net", "c#", "azure", "aws", "gcp", "docker", "kubernetes", "power bi",
    "postgresql", "oracle", "sql server", "ia", "rag", "llm", "node", "nodejs",
    "typescript", "devops", "ci/cd", "terraform", "ansible",
})
SYNTHETIC_CERTS = frozenset({"iso 27001", "rgpd", "qualiopi", "iso27001"})
SYNTHETIC_THRESHOLD_GO = 88
SYNTHETIC_THRESHOLD_SOUS_RESERVE = 60
SYNTHETIC_BUSINESS_RULES = {
    "budget_minimum_eur": 50_000, "max_charge_pct": 95, "max_unmastered_technologies": 4,
    "certification_penalty_score": 20,
}


def synthetic_policy(**overrides) -> ScoringPolicySnapshot:
    """A fully explicit IT-flavoured snapshot (every rule configured), the
    stand-in for the removed global defaults. Any field can be overridden."""
    fields = dict(
        weights=dict(SYNTHETIC_WEIGHTS),
        threshold_go=SYNTHETIC_THRESHOLD_GO,
        threshold_sous_reserve=SYNTHETIC_THRESHOLD_SOUS_RESERVE,
        mastered_technologies=SYNTHETIC_MASTERED,
        certifications_held=SYNTHETIC_CERTS,
        **SYNTHETIC_BUSINESS_RULES,
    )
    fields.update(overrides)
    return ScoringPolicySnapshot.from_legacy(**fields)


def score_with_synthetic_policy(ao, company, evidences, capacity, **overrides):
    """`ScoringEngine().score(...)` with the synthetic snapshot above."""
    return ScoringEngine().score(ao, company, evidences, capacity, policy=synthetic_policy(**overrides))
