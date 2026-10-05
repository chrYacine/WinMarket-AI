"""B06-T5 — ScoringEngine.score() with additive, sector-neutral custom
criteria on top of the fixed 12 IT-specific criteria.

Two synthetic, explicitly-non-IT accounts (ticket: "deux comptes de métiers
différents configurent leurs propres critères... et ces critères
influencent réellement le calcul sans dépendre d'une liste de technologies
IT") — a cleaning company and a construction company — plus one IT account
proving the pre-existing formula is byte-for-byte unchanged when no custom
criterion is configured.
"""
from __future__ import annotations

from src.agents.scoring_engine import ScoringEngine, ScoringPolicySnapshot
from tests.synthetic_scoring import score_with_synthetic_policy
from src.core.models import AOContext, CapacityResult, CompanyProfile, ExtractedFact, RAGEvidence, ScoringResult


def make_ao(**overrides) -> AOContext:
    defaults = dict(
        titre="AO synthétique", client="Client Synthétique", secteur="Services",
        budget_estime=80_000, deadline_reponse="", duree_projet_mois=None,
        technologies_demandees=[], competences_requises=[], questions_client=[],
        livrables=[], contraintes=[], certifications_obligatoires=[],
        texte_source="Marché de prestation de services.",
    )
    defaults.update(overrides)
    return AOContext(**defaults)


def make_capacity(**overrides) -> CapacityResult:
    defaults = dict(charge_actuelle_pct=50, capacite_restante_pct=50, equipe_disponible=True, commentaire="Équipe disponible.")
    defaults.update(overrides)
    return CapacityResult(**defaults)


def criterion(result: ScoringResult, label: str):
    match = next((c for c in result.criteres if c.nom == label), None)
    assert match is not None, f"critère {label!r} introuvable parmi {[c.nom for c in result.criteres]}"
    return match


# Fixed-12 weights all zeroed out except a couple, leaving 20 points of
# budget for the two custom criteria below — a real non-IT account has no
# reason to care about "Complexité technique" (technology count) or
# "Valeur stratégique" (IA/Cloud keywords).
NON_IT_BASE_WEIGHTS = {
    "Adequation expertise": 0, "References similaires": 10, "Disponibilite equipe": 15,
    "Rentabilite estimee": 15, "Faisabilite delai": 10, "Certifications requises": 10,
    "Complexite technique": 0, "Connaissance secteur": 10, "Potentiel commercial": 5,
    "Risque contractuel": 5, "Solidite client": 5, "Valeur strategique": 0,
}  # sums to 85 — 15 left for custom criteria below (matches the fixtures' own weights)

ZONE_CRITERION = {
    "id": "zone_couverte", "label": "Zone d'intervention couverte", "fact_key": "zone_intervention",
    "operator": "list_coverage", "weight": 10, "blocking": True, "pass_score": 100, "fail_score": 0,
}
FREQUENCE_CRITERION = {
    "id": "frequence_ok", "label": "Fréquence de nettoyage compatible", "fact_key": "frequence_nettoyage",
    "operator": "numeric_threshold", "comparison": "provider_gte_ao",
    "weight": 5, "blocking": False, "pass_score": 100, "fail_score": 20,
}

MATERIEL_CRITERION = {
    "id": "materiel_ok", "label": "Matériel requis disponible", "fact_key": "materiel_disponible",
    "operator": "list_coverage", "weight": 10, "blocking": True, "pass_score": 100, "fail_score": 0,
}
CAPACITE_CRITERION = {
    "id": "capacite_ok", "label": "Capacité de chantier suffisante", "fact_key": "capacite_chantier",
    "operator": "numeric_threshold", "comparison": "provider_gte_ao",
    "weight": 5, "blocking": False, "pass_score": 100, "fail_score": 20,
}


# Permissive, fully-configured values for the four PRE-EXISTING (B06-T4)
# business rules — unrelated to this ticket's custom criteria, but any
# UNCONFIGURED one of these already makes ScoringEngine.score() report
# "incomplete" on its own (see _resolve_rule) regardless of custom
# criteria. Every fixture below must set all four, exactly like every
# other real-job test in this codebase (e.g. tests/test_b06_scoring_
# config.py's own _save_draft helper), so that ONLY the custom-criteria
# behavior under test can affect completeness.
_PERMISSIVE_BUSINESS_RULES = dict(
    budget_minimum_eur=0, max_charge_pct=100, max_unmastered_technologies=999, certification_penalty_score=20,
)


def _cleaning_policy(**overrides):
    base = dict(
        weights=NON_IT_BASE_WEIGHTS, threshold_go=80, threshold_sous_reserve=55,
        custom_criteria=[ZONE_CRITERION, FREQUENCE_CRITERION],
        declared_facts={
            "zone_intervention": {"value": ["Lyon", "Villeurbanne"], "unit": None},
            "frequence_nettoyage": {"value": 3, "unit": "par_semaine"},
        },
        **_PERMISSIVE_BUSINESS_RULES,
    )
    base.update(overrides)
    return ScoringPolicySnapshot.from_legacy(**base)


def _btp_policy(**overrides):
    base = dict(
        weights=NON_IT_BASE_WEIGHTS, threshold_go=80, threshold_sous_reserve=55,
        custom_criteria=[MATERIEL_CRITERION, CAPACITE_CRITERION],
        declared_facts={
            "materiel_disponible": {"value": ["grue", "nacelle", "betonniere"], "unit": None},
            "capacite_chantier": {"value": 4, "unit": "equipes"},
        },
        **_PERMISSIVE_BUSINESS_RULES,
    )
    base.update(overrides)
    return ScoringPolicySnapshot.from_legacy(**base)


def test_cleaning_account_zone_covered_and_frequency_ok_contributes_full_score():
    ao = make_ao(extracted_facts={
        "zone_intervention": ExtractedFact(value=["Lyon"], status="found", provenance="llm"),
        "frequence_nettoyage": ExtractedFact(value=2, unit="par_semaine", status="found", provenance="llm"),
    })
    result = ScoringEngine().score(ao, CompanyProfile(), [], make_capacity(), policy=_cleaning_policy())
    zone = criterion(result, "Zone d'intervention couverte")
    freq = criterion(result, "Fréquence de nettoyage compatible")
    assert zone.score == 100.0
    assert freq.score == 100.0
    assert result.scoring_completeness == "complete"
    assert not result.criteres_bloquants


def test_cleaning_account_zone_not_covered_is_a_confirmed_no_go():
    """Zone d'intervention is BLOCKING — a real, determined failure must
    produce NO-GO, never INCOMPLET (the check WAS run, it just failed)."""
    ao = make_ao(extracted_facts={
        "zone_intervention": ExtractedFact(value=["Marseille"], status="found", provenance="llm"),
        "frequence_nettoyage": ExtractedFact(value=2, unit="par_semaine", status="found", provenance="llm"),
    })
    result = ScoringEngine().score(ao, CompanyProfile(), [], make_capacity(), policy=_cleaning_policy())
    zone = criterion(result, "Zone d'intervention couverte")
    assert zone.score == 0.0
    assert result.decision == "NO-GO"
    assert any("Zone d'intervention couverte" in b for b in result.criteres_bloquants)


def test_cleaning_account_frequency_insufficient_is_not_blocking_but_lowers_score():
    """Fréquence is NOT blocking — an unmet threshold degrades the score
    (fail_score) without producing a hard NO-GO by itself."""
    ao_ok_zone_bad_freq = make_ao(extracted_facts={
        "zone_intervention": ExtractedFact(value=["Lyon"], status="found", provenance="llm"),
        "frequence_nettoyage": ExtractedFact(value=10, unit="par_semaine", status="found", provenance="llm"),
    })
    result = ScoringEngine().score(ao_ok_zone_bad_freq, CompanyProfile(), [], make_capacity(), policy=_cleaning_policy())
    freq = criterion(result, "Fréquence de nettoyage compatible")
    assert freq.score == 20.0
    assert not result.criteres_bloquants, "a non-blocking criterion failing must never add a blocker"


def test_cleaning_account_missing_ao_fact_is_incomplete_never_a_fabricated_decision():
    """The AO simply never states a required zone (extraction genuinely
    found nothing) — this must be INCOMPLET, never a silently favorable or
    unfavorable guess."""
    ao = make_ao(extracted_facts={
        "zone_intervention": ExtractedFact(value=None, status="absent", provenance="absent"),
        "frequence_nettoyage": ExtractedFact(value=2, unit="par_semaine", status="found", provenance="llm"),
    })
    result = ScoringEngine().score(ao, CompanyProfile(), [], make_capacity(), policy=_cleaning_policy())
    assert result.decision == "INCOMPLET"
    assert result.scoring_completeness == "incomplete"
    assert "custom:zone_couverte" in result.scoring_missing
    zone = criterion(result, "Zone d'intervention couverte")
    assert zone.score == 0.0, "an unresolvable criterion contributes 0, never a fabricated favorable score"


def test_btp_account_is_independent_of_the_cleaning_accounts_configuration():
    """Ticket: 'sans effet sur l'autre compte' — a completely different
    account, different facts/criteria/weights, computed independently in
    the same test run."""
    ao = make_ao(extracted_facts={
        "materiel_disponible": ExtractedFact(value=["grue"], status="found", provenance="llm"),
        "capacite_chantier": ExtractedFact(value=3, unit="equipes", status="found", provenance="llm"),
    })
    result = ScoringEngine().score(ao, CompanyProfile(), [], make_capacity(), policy=_btp_policy())
    materiel = criterion(result, "Matériel requis disponible")
    capacite = criterion(result, "Capacité de chantier suffisante")
    assert materiel.score == 100.0
    assert capacite.score == 100.0
    # None of the cleaning-specific criteria names ever leak into this
    # account's result.
    assert not any("Zone d'intervention" in c.nom or "nettoyage" in c.nom for c in result.criteres)


def test_btp_account_insufficient_equipment_is_a_confirmed_no_go():
    ao = make_ao(extracted_facts={
        "materiel_disponible": ExtractedFact(value=["pelleteuse"], status="found", provenance="llm"),
        "capacite_chantier": ExtractedFact(value=3, unit="equipes", status="found", provenance="llm"),
    })
    result = ScoringEngine().score(ao, CompanyProfile(), [], make_capacity(), policy=_btp_policy())
    assert result.decision == "NO-GO"
    materiel = criterion(result, "Matériel requis disponible")
    assert materiel.score == 0.0


def test_unit_mismatch_between_ao_and_provider_never_converted_and_is_incomplete():
    """Ticket: 'ne pas convertir une heure-personne en disponibilité
    globale sans données permettant le calcul' — a genuine unit mismatch
    must be an honest incomplete, never a silent conversion."""
    ao = make_ao(extracted_facts={
        "zone_intervention": ExtractedFact(value=["Lyon"], status="found", provenance="llm"),
        "frequence_nettoyage": ExtractedFact(value=2, unit="par_mois", status="found", provenance="llm"),
    })
    result = ScoringEngine().score(ao, CompanyProfile(), [], make_capacity(), policy=_cleaning_policy())
    assert "custom:frequence_ok" in result.scoring_missing
    assert result.decision == "INCOMPLET"


def test_it_account_with_no_custom_criteria_gets_exactly_the_fixed_twelve_criteria():
    """Non-regression: an account that configures NO custom criterion must
    see the exact pre-existing 12-criteria-only formula — confirming
    custom_criteria=[]/declared_facts={} (the dataclass defaults) change
    nothing. Lot 43: the `policy=None` engine this used to be compared with
    is gone; the byte-for-byte equivalence with what that engine returned
    before the cleanup is pinned by tests/test_lot43_cleanup.py against
    results recorded BEFORE the removal."""
    ao = make_ao(technologies_demandees=["Python", "React"], budget_estime=250_000)
    capacity = make_capacity()
    default_result = score_with_synthetic_policy(ao, CompanyProfile(), [], capacity)
    explicit_result = score_with_synthetic_policy(ao, CompanyProfile(), [], capacity, custom_criteria=[], declared_facts={})

    assert default_result.score_global == explicit_result.score_global
    assert default_result.decision == explicit_result.decision
    assert [c.nom for c in default_result.criteres] == [c.nom for c in explicit_result.criteres]
    assert len(explicit_result.criteres) == 12, "no custom criterion configured means exactly the fixed 12, nothing more"
