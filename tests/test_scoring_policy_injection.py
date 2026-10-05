"""B06-T1 — ScoringEngine.score(..., policy=...) contract: a
ScoringPolicySnapshot is the ONLY source of weights, mastered technologies,
held certifications and thresholds. Lot 43: the class-level
weights/mastered/certs_ok and the config.py global thresholds it used to
REPLACE no longer exist, and `policy=None` is refused instead of falling back
to them (see test_policy_none_is_refused_...). No DB, no web layer here —
pure engine-level tests, same style as tests/test_scoring_engine.py."""
from __future__ import annotations

import pytest

from src.agents.scoring_engine import ScoringEngine, ScoringPolicySnapshot
from src.core.models import AOContext, CapacityResult, CompanyProfile, RAGEvidence
from src.core.private_configuration import PrivateConfigurationRequired
from tests.synthetic_scoring import score_with_synthetic_policy


def make_ao(**overrides) -> AOContext:
    defaults = dict(
        titre="AO synthétique", client="Client Synthétique", secteur="Retail",
        budget_estime=None, deadline_reponse="", duree_projet_mois=None,
        technologies_demandees=[], competences_requises=[], questions_client=[],
        livrables=[], contraintes=[], certifications_obligatoires=[], texte_source="",
    )
    defaults.update(overrides)
    return AOContext(**defaults)


def make_capacity(**overrides) -> CapacityResult:
    defaults = dict(charge_actuelle_pct=50, capacite_restante_pct=50, equipe_disponible=True, commentaire="Équipe disponible.")
    defaults.update(overrides)
    return CapacityResult(**defaults)


def criterion(result, label_display: str):
    match = next((c for c in result.criteres if c.nom == label_display), None)
    assert match is not None, f"critère {label_display!r} introuvable parmi {[c.nom for c in result.criteres]}"
    return match


ALL_EQUAL_WEIGHTS = {
    "Adequation expertise": 20, "References similaires": 15, "Disponibilite equipe": 10,
    "Rentabilite estimee": 10, "Faisabilite delai": 10, "Certifications requises": 10,
    "Complexite technique": 5, "Connaissance secteur": 5, "Potentiel commercial": 5,
    "Risque contractuel": 5, "Solidite client": 3, "Valeur strategique": 2,
}


def test_the_explicit_synthetic_policy_reproduces_the_pre_b06_hand_computed_result():
    """The former policy=None behavior (80.2, GO SOUS RESERVE) is reproduced —
    byte for byte, see tests/test_lot43_cleanup.py's golden fixture — by
    passing the same values explicitly."""
    ao = make_ao(budget_estime=250_000, technologies_demandees=["Python", "Django"])
    company = CompanyProfile(secteur="Public", solidite_financiere="Bonne")
    capacity = make_capacity()
    result = score_with_synthetic_policy(ao, company, [], capacity)
    assert result.score_global == 80.2
    assert result.decision == "GO SOUS RESERVE"


def test_policy_none_is_refused_and_no_default_is_computed():
    ao = make_ao(budget_estime=250_000, technologies_demandees=["Python", "Django"])
    company = CompanyProfile(secteur="Public", solidite_financiere="Bonne")
    with pytest.raises(PrivateConfigurationRequired):
        ScoringEngine().score(ao, company, [], make_capacity(), policy=None)


def test_policy_mastered_technologies_replaces_global_list_entirely():
    """A technology that WAS in the former global mastered set
    ('python') must be treated as UNMASTERED once a policy is injected that
    doesn't declare it — proving replacement, not a union/fallback merge."""
    ao = make_ao(technologies_demandees=["Python"])
    company = CompanyProfile()
    capacity = make_capacity()

    policy_without_python = ScoringPolicySnapshot.from_legacy(
        weights=dict(ALL_EQUAL_WEIGHTS), threshold_go=88, threshold_sous_reserve=60,
        mastered_technologies=frozenset({"cobol"}),  # deliberately NOT python
    )
    result = ScoringEngine().score(ao, company, [], capacity, policy=policy_without_python)
    assert criterion(result, "Adéquation expertise").score == 0.0, (
        "python is globally mastered but must score as unmastered under a policy that doesn't declare it"
    )

    policy_with_python = ScoringPolicySnapshot.from_legacy(
        weights=dict(ALL_EQUAL_WEIGHTS), threshold_go=88, threshold_sous_reserve=60,
        mastered_technologies=frozenset({"python"}),
    )
    result2 = ScoringEngine().score(ao, company, [], capacity, policy=policy_with_python)
    assert criterion(result2, "Adéquation expertise").score == 100.0


def test_policy_certifications_held_replaces_global_certs_ok():
    """'iso 27001' WAS in the former global certs_ok set — a policy that
    doesn't declare it must still block, proving no silent fallback."""
    ao = make_ao(certifications_obligatoires=["ISO 27001"])
    capacity = make_capacity()

    policy_without_cert = ScoringPolicySnapshot.from_legacy(
        weights=dict(ALL_EQUAL_WEIGHTS), threshold_go=88, threshold_sous_reserve=60,
        certifications_held=frozenset(),
    )
    result = ScoringEngine().score(ao, CompanyProfile(), [], capacity, policy=policy_without_cert)
    assert result.decision == "NO-GO"
    assert any("ISO 27001" in b for b in result.criteres_bloquants)

    policy_with_cert = ScoringPolicySnapshot.from_legacy(
        weights=dict(ALL_EQUAL_WEIGHTS), threshold_go=88, threshold_sous_reserve=60,
        certifications_held=frozenset({"iso 27001"}),
    )
    result2 = ScoringEngine().score(ao, CompanyProfile(), [], capacity, policy=policy_with_cert)
    assert result2.criteres_bloquants == []


def test_policy_weights_replace_global_weights_and_change_global_score():
    """Same scenario as the hand-computed 80.2 case in test_scoring_engine.py
    but with 'Adequation expertise' weighted 0 instead of 20 — the global
    score must move, proving the policy's weights are actually used."""
    ao = make_ao(budget_estime=250_000, technologies_demandees=["Python", "Django"])
    company = CompanyProfile(secteur="Public", solidite_financiere="Bonne")
    capacity = make_capacity()

    zeroed_weights = dict(ALL_EQUAL_WEIGHTS)
    zeroed_weights["Adequation expertise"] = 0
    zeroed_weights["Valeur strategique"] = 22  # keep the sum at 100
    policy = ScoringPolicySnapshot.from_legacy(weights=zeroed_weights, threshold_go=88, threshold_sous_reserve=60)
    result = ScoringEngine().score(ao, company, [], capacity, policy=policy)

    default_result = score_with_synthetic_policy(ao, company, [], capacity)
    assert result.score_global != default_result.score_global


def test_policy_thresholds_replace_global_config_thresholds():
    ao = make_ao(budget_estime=250_000, technologies_demandees=["Python", "Django"])
    company = CompanyProfile(secteur="Public", solidite_financiere="Bonne")
    capacity = make_capacity()

    # B06-T4: this test is about thresholds, not about completeness — the
    # four business rules are set to values that never gate this AO/
    # capacity (budget well above, charge well under, no unmastered techs,
    # no missing certification), so they only rule out an "INCOMPLET"
    # verdict without changing what this test actually exercises.
    business_rule_kwargs = dict(
        budget_minimum_eur=50_000, max_charge_pct=95,
        max_unmastered_technologies=4, certification_penalty_score=20,
    )
    lenient_policy = ScoringPolicySnapshot.from_legacy(
        weights=dict(ALL_EQUAL_WEIGHTS), threshold_go=1.0, threshold_sous_reserve=0.5, **business_rule_kwargs,
    )
    result = ScoringEngine().score(ao, company, [], capacity, policy=lenient_policy)
    assert result.decision == "GO", "score 80.2 must clear a policy threshold_go of 1.0"

    strict_policy = ScoringPolicySnapshot.from_legacy(
        weights=dict(ALL_EQUAL_WEIGHTS), threshold_go=99.9, threshold_sous_reserve=99.0, **business_rule_kwargs,
    )
    result2 = ScoringEngine().score(ao, company, [], capacity, policy=strict_policy)
    assert result2.decision == "NO-GO", "score 80.2 must not clear a policy threshold_sous_reserve of 99.0"


def test_policy_snapshot_defaults_are_empty_not_borrowed_from_globals():
    """A ScoringPolicySnapshot constructed without explicit mastered/certs
    is empty (frozenset()), never silently defaulting to any global set — this is what makes 'no repli vers les
    globaux' a structural guarantee of the dataclass itself, not just a
    convention callers must remember."""
    snapshot = ScoringPolicySnapshot.from_legacy(weights=dict(ALL_EQUAL_WEIGHTS), threshold_go=88, threshold_sous_reserve=60)
    assert snapshot.mastered_technologies == frozenset()
    assert snapshot.certifications_held == frozenset()
