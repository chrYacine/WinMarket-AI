"""Test suite for ScoringEngine — real assertions against the real engine.

B04 rewrite (see docs/qa/b04_20260914/MATRICE_34_INTENTIONS.md for the full
correspondence with the 34 historical intentions this replaces — every one
of them, none dropped, is traced there). Every test here calls the real
`ScoringEngine.score(...)` (never mocked) with synthetic, explicit
AOContext/CompanyProfile/RAGEvidence/CapacityResult builders. Tests marked
`@pytest.mark.known_regression` assert the CORRECT/desired behavior and are
expected to FAIL against the current engine — that failure is intentional
and documented (see docs/qa/b04_20260914/BACKLOG_CORRECTIONS_SCORING.md);
never skip/xfail them to hide the defect, never weaken their assertion.

Run the stable contract suite only:      pytest tests/test_scoring_engine.py -m "not known_regression"
Run the known-regression suite alone:    pytest tests/test_scoring_engine.py -m known_regression
"""
from __future__ import annotations

import pytest

from src.agents.scoring_engine import ScoringEngine
from src.core.models import AOContext, CapacityResult, CompanyProfile, RAGEvidence, ScoringResult
from tests.synthetic_scoring import SYNTHETIC_WEIGHTS, score_with_synthetic_policy


# ---------------------------------------------------------------------------
# Builders — explicit synthetic inputs, never a real corpus/company/demo
# profile as ground truth (ticket B04 section 6).
# ---------------------------------------------------------------------------

def make_ao(**overrides) -> AOContext:
    defaults = dict(
        titre="AO synthétique", client="Client Synthétique", secteur="Retail",
        budget_estime=None, deadline_reponse="", duree_projet_mois=None,
        technologies_demandees=[], competences_requises=[], questions_client=[],
        livrables=[], contraintes=[], certifications_obligatoires=[], texte_source="",
    )
    defaults.update(overrides)
    return AOContext(**defaults)


def make_company(**overrides) -> CompanyProfile:
    defaults = dict(
        raison_sociale="Client Synthétique", secteur="Retail", solidite_financiere="Bonne",
    )
    defaults.update(overrides)
    return CompanyProfile(**defaults)


def make_capacity(**overrides) -> CapacityResult:
    defaults = dict(charge_actuelle_pct=50, capacite_restante_pct=50, equipe_disponible=True, commentaire="Équipe disponible.")
    defaults.update(overrides)
    return CapacityResult(**defaults)


def make_evidence(**overrides) -> RAGEvidence:
    defaults = dict(query="q", source="ref.md", score=0.5, content="Contenu de référence.")
    defaults.update(overrides)
    return RAGEvidence(**defaults)


def criterion(result: ScoringResult, label_display: str):
    match = next((c for c in result.criteres if c.nom == label_display), None)
    assert match is not None, f"critère {label_display!r} introuvable parmi {[c.nom for c in result.criteres]}"
    return match


# ---------------------------------------------------------------------------
# S06 — Adéquation expertise (old: TestScoringCriteria expertise_*)
# ---------------------------------------------------------------------------

class TestExpertise:
    def test_expertise_full_match_scores_100(self):
        ao = make_ao(technologies_demandees=["Python", "Django"])
        result = score_with_synthetic_policy(ao, make_company(), [], make_capacity())
        c = criterion(result, "Adéquation expertise")
        assert c.score == 100.0
        # Lot 44: the justification states the ratio and its source (the
        # account's declared profile) — it no longer presumes the provider is an ESN.
        assert c.justification == "2/2 technologies ou compétences demandées figurent dans le profil déclaré."

    def test_expertise_partial_match_scores_by_ratio(self):
        # 4 technologies, 1 unmastered ("cobol") -> ratio 3/4 = 75.0, not 80 —
        # the historical "80% match" name assumed a round ratio; the engine
        # computes whatever ratio the inputs actually produce.
        ao = make_ao(technologies_demandees=["Python", "React", "Docker", "Cobol"])
        result = score_with_synthetic_policy(ao, make_company(), [], make_capacity())
        c = criterion(result, "Adéquation expertise")
        assert c.score == 75.0
        assert c.justification == "3/4 technologies ou compétences demandées figurent dans le profil déclaré."

    def test_expertise_zero_match_scores_0(self):
        ao = make_ao(technologies_demandees=["Cobol", "Fortran"])
        result = score_with_synthetic_policy(ao, make_company(), [], make_capacity())
        c = criterion(result, "Adéquation expertise")
        assert c.score == 0.0

    def test_expertise_no_technologies_uses_default_80(self):
        """Distinct from the zero-match case above: no technology listed at
        all is scored as a neutral 80, not a penalized 0 — a different
        branch in the engine (scoring_engine.py:69-70)."""
        ao = make_ao(technologies_demandees=[], competences_requises=[])
        result = score_with_synthetic_policy(ao, make_company(), [], make_capacity())
        c = criterion(result, "Adéquation expertise")
        assert c.score == 80.0


# ---------------------------------------------------------------------------
# S07 — Disponibilité équipe (old: TestScoringCriteria availability_*)
# Caractérisation : le critère est booléen (equipe_disponible), pas un seuil
# de pourcentage — voir MATRICE_34_INTENTIONS.md #4/#5.
# ---------------------------------------------------------------------------

class TestAvailability:
    def test_availability_equipe_disponible_scores_90(self):
        result = score_with_synthetic_policy(make_ao(), make_company(), [], make_capacity(equipe_disponible=True))
        assert criterion(result, "Disponibilité équipe").score == 90.0

    def test_availability_equipe_indisponible_scores_45(self):
        result = score_with_synthetic_policy(make_ao(), make_company(), [], make_capacity(equipe_disponible=False))
        assert criterion(result, "Disponibilité équipe").score == 45.0

    def test_availability_justification_is_capacity_comment_verbatim(self):
        """The justification for this ONE criterion is entirely delegated
        to CapacityResult.commentaire — ScoringEngine does not compose its
        own text for it (scoring_engine.py:132)."""
        capacity = make_capacity(commentaire="PREUVE_COMMENTAIRE_CAPACITE_EXACT")
        result = score_with_synthetic_policy(make_ao(), make_company(), [], capacity)
        assert criterion(result, "Disponibilité équipe").justification == "PREUVE_COMMENTAIRE_CAPACITE_EXACT"

    def test_charge_over_95_percent_blocks_independently_of_equipe_disponible(self):
        """The >95% charge blocker (a separate rule from the boolean
        criterion above) fires off CapacityResult.charge_actuelle_pct, not
        equipe_disponible — the two can disagree, and the blocker wins."""
        capacity = make_capacity(equipe_disponible=True, charge_actuelle_pct=99)
        result = score_with_synthetic_policy(make_ao(), make_company(), [], capacity)
        assert any("95%" in b for b in result.criteres_bloquants)
        assert result.decision == "NO-GO"


# ---------------------------------------------------------------------------
# S08 — Rentabilité / budget (old: profitability_* — redefined, see D#6/#7)
# ---------------------------------------------------------------------------

class TestBudgetTiers:
    """The historical tests assumed a profit-margin percentage that does not
    exist anywhere in AOContext or the engine — 'rentabilité' here is
    entirely a function of absolute budget tiers. See
    MATRICE_34_INTENTIONS.md #6/#7 (category D: margin-based profitability
    is a future feature, not yet decided — backlog B08)."""

    @pytest.mark.parametrize("budget,expected_score", [
        (None, 65), (0, 65), (30_000, 40), (60_000, 55), (100_000, 80), (200_000, 90), (500_000, 90),
    ])
    def test_rentabilite_budget_tiers_are_ordered(self, budget, expected_score):
        ao = make_ao(budget_estime=budget)
        result = score_with_synthetic_policy(ao, make_company(), [], make_capacity())
        assert criterion(result, "Rentabilité estimée").score == expected_score

    def test_budget_below_50k_blocks_decision(self):
        ao = make_ao(budget_estime=40_000)
        result = score_with_synthetic_policy(ao, make_company(), [], make_capacity())
        assert any("50 000" in b for b in result.criteres_bloquants)
        assert result.decision == "NO-GO"


# ---------------------------------------------------------------------------
# S05 — Certifications (old: certification_blocking_rule)
# ---------------------------------------------------------------------------

class TestCertifications:
    def test_missing_mandatory_certification_blocks_decision(self):
        # "SecNumCloud" is not in the synthetic held-certifications set (which only covers
        # iso 27001 / rgpd / qualiopi) — genuinely missing, unlike ISO 27001
        # which would already be considered covered.
        ao = make_ao(certifications_obligatoires=["SecNumCloud"], technologies_demandees=["Python"])
        result = score_with_synthetic_policy(ao, make_company(), [], make_capacity())
        c = criterion(result, "Certifications requises")
        assert c.score == 20
        assert "SecNumCloud" in c.justification
        assert any("SecNumCloud" in b for b in result.criteres_bloquants)
        assert result.decision == "NO-GO"

    def test_certification_justification_lists_missing_certs(self):
        ao = make_ao(certifications_obligatoires=["RGPD", "HDS"])
        result = score_with_synthetic_policy(ao, make_company(), [], make_capacity())
        c = criterion(result, "Certifications requises")
        assert "HDS" in c.justification  # RGPD is in certs_ok, HDS is not
        assert c.score == 20

    def test_known_certification_does_not_block(self):
        ao = make_ao(certifications_obligatoires=["RGPD", "ISO 27001", "Qualiopi"])
        result = score_with_synthetic_policy(ao, make_company(), [], make_capacity())
        c = criterion(result, "Certifications requises")
        assert c.score == 100
        assert result.criteres_bloquants == [] or not any("Certification" in b for b in result.criteres_bloquants)


# ---------------------------------------------------------------------------
# S03 — Seuils de décision (old: TestGlobalScore + TestBlockingRules partly)
# ---------------------------------------------------------------------------

class TestDecisionThresholds:
    def test_weighted_global_score_matches_hand_computed_value(self):
        """S02: one fully hand-computed scenario. Expected value derived
        independently below (poids from the synthetic policy weights, which sum to
        100) — NOT by re-deriving it from the engine's own criteria list.

        Adequation expertise    100 * 20 = 2000
        References similaires    30 * 15 =  450   (0 evidences -> ref_score = min(100, 30+0+0) = 30)
        Disponibilite equipe     90 * 10 =  900
        Rentabilite estimee      90 * 10 =  900   (budget 250_000 >= 200_000)
        Faisabilite delai        75 * 10 =  750   (no tight-deadline phrase in texte_source)
        Certifications requises 100 * 10 = 1000   (no certifications required)
        Complexite technique     80 *  5 =  400   (2 technologies <= 4)
        Connaissance secteur     80 *  5 =  400   (secteur="Public", not the two "unknown" sentinels)
        Potentiel commercial     85 *  5 =  425   (budget >= 150_000)
        Risque contractuel       80 *  5 =  400   (no "pénalité/garantie/sla" in texte_source)
        Solidite client          85 *  3 =  255   (solidite_financiere="Bonne")
        Valeur strategique       70 *  2 =  140   (no ia/rag/llm/cloud/azure/aws/data in technos)
                                          -------
                                            8020  / 100 = 80.2
        """
        ao = make_ao(
            budget_estime=250_000, technologies_demandees=["Python", "Django"],
            certifications_obligatoires=[], texte_source="",
        )
        company = make_company(secteur="Public", solidite_financiere="Bonne")
        capacity = make_capacity(equipe_disponible=True, charge_actuelle_pct=50)
        result = score_with_synthetic_policy(ao, company, [], capacity)

        assert result.score_global == 80.2
        assert result.criteres_bloquants == []
        assert result.decision == "GO SOUS RESERVE"  # 60 <= 80.2 < 88 under the synthetic (former default) thresholds

    def _base_favorable_no_blockers(self):
        """Same scenario as the hand-computed test above (score 80.2, no
        blockers) — reused so the threshold-boundary tests below vary ONLY
        the module's threshold constants, never the score itself."""
        ao = make_ao(budget_estime=250_000, technologies_demandees=["Python", "Django"])
        company = make_company(secteur="Public", solidite_financiere="Bonne")
        capacity = make_capacity(equipe_disponible=True, charge_actuelle_pct=50)
        return ao, company, capacity

    def test_score_at_or_above_go_threshold_decides_go(self):
        # Lot 43: the thresholds are the ACCOUNT's (policy snapshot), no
        # longer module globals to monkeypatch — same values, same assertions.
        ao, company, capacity = self._base_favorable_no_blockers()
        result = score_with_synthetic_policy(ao, company, [], capacity, threshold_go=80.2, threshold_sous_reserve=60)
        assert result.score_global == 80.2
        assert result.decision == "GO"

    def test_score_between_thresholds_decides_sous_reserve(self):
        ao, company, capacity = self._base_favorable_no_blockers()
        result = score_with_synthetic_policy(ao, company, [], capacity, threshold_go=80.3, threshold_sous_reserve=80.2)
        assert result.score_global == 80.2
        assert result.decision == "GO SOUS RESERVE"

    def test_score_below_sous_reserve_threshold_decides_no_go(self):
        ao, company, capacity = self._base_favorable_no_blockers()
        result = score_with_synthetic_policy(ao, company, [], capacity, threshold_go=80.3, threshold_sous_reserve=80.3)
        assert result.score_global == 80.2
        assert result.decision == "NO-GO"

    def test_real_default_thresholds_go_case(self):
        """Complete case under the synthetic (former default) thresholds (88/60) — a
        maximally favorable, blocker-free scenario should land at or above 88."""
        ao = make_ao(
            budget_estime=500_000, technologies_demandees=["Python", "React", "AWS", "Docker"],
            texte_source="",
        )
        company = make_company(secteur="Finance", solidite_financiere="Bonne")
        capacity = make_capacity(equipe_disponible=True, charge_actuelle_pct=20)
        # B18-T4: 5 genuinely DISTINCT references (different content/source)
        # — not 5 identical copies, which the engine now correctly
        # deduplicates to a single reference (DEFECT F09/F12/E-5).
        evidences = [make_evidence(score=0.9, source=f"ref{i}.md", content=f"Contenu de référence numéro {i}.") for i in range(5)]
        result = score_with_synthetic_policy(ao, company, evidences, capacity)
        assert result.score_global >= 88
        assert result.decision == "GO"

    def test_real_default_thresholds_no_go_by_score_case(self):
        """A genuinely low-scoring but blocker-free scenario should fall
        below 60 through the real weighted formula, not a blocker."""
        ao = make_ao(
            budget_estime=None, technologies_demandees=["Cobol", "Fortran", "Delphi"],
            texte_source="Démarrage impératif sous 4 semaines, pénalités de retard applicables.",
        )
        company = make_company(secteur="", solidite_financiere="Moyenne")
        capacity = make_capacity(equipe_disponible=False, charge_actuelle_pct=80)
        result = score_with_synthetic_policy(ao, company, [], capacity)
        assert len(result.criteres_bloquants) == 0, "this case must be low-scoring, not blocked, to test the score path"
        assert result.score_global < 60
        assert result.decision == "NO-GO"


# ---------------------------------------------------------------------------
# S04 — Bloqueurs prioritaires (old: TestBlockingRules)
# ---------------------------------------------------------------------------

class TestBlockingRulesPriority:
    def test_no_blockers_when_all_conditions_favorable(self):
        """Essential counter-case: a favorable scenario must NOT be
        blocked — an engine that always returned NO-GO would still pass
        every other blocker test without this one."""
        ao = make_ao(budget_estime=250_000, technologies_demandees=["Python"], certifications_obligatoires=[])
        result = score_with_synthetic_policy(ao, make_company(), [], make_capacity(charge_actuelle_pct=50))
        assert result.criteres_bloquants == []

    def test_single_blocker_forces_no_go_on_otherwise_favorable_ao(self):
        ao = make_ao(
            budget_estime=500_000, technologies_demandees=["Python", "React", "AWS"],
            certifications_obligatoires=["SecNumCloud"],  # the only unfavorable fact
        )
        capacity = make_capacity(equipe_disponible=True, charge_actuelle_pct=20)
        result = score_with_synthetic_policy(ao, make_company(), [make_evidence(score=0.9)] * 5, capacity)
        assert len(result.criteres_bloquants) == 1
        assert result.decision == "NO-GO"

    def test_multiple_blockers_are_all_listed(self):
        ao = make_ao(
            budget_estime=30_000, certifications_obligatoires=["SecNumCloud"],
            technologies_demandees=["Cobol", "Fortran", "Delphi", "RPG"],
        )
        capacity = make_capacity(charge_actuelle_pct=99)
        result = score_with_synthetic_policy(ao, make_company(), [], capacity)
        assert len(result.criteres_bloquants) == 4  # cert, budget, charge, unknown-tech
        assert result.decision == "NO-GO"

    def test_blocker_forces_no_go_without_capping_numeric_score(self):
        """MATRICE #15: there is no score cap tied to a blocker — score_global
        is computed purely from the weighted criteria, independently of
        whether any blocker fired. A blocked AO can still show a high
        numeric score alongside a NO-GO decision."""
        ao = make_ao(
            budget_estime=500_000, technologies_demandees=["Python", "React", "AWS", "Docker"],
            certifications_obligatoires=["SecNumCloud"],  # the only blocker
        )
        capacity = make_capacity(equipe_disponible=True, charge_actuelle_pct=20)
        result = score_with_synthetic_policy(ao, make_company(secteur="Finance"), [make_evidence(score=0.9)] * 5, capacity)
        assert result.decision == "NO-GO"
        assert result.score_global >= 60, (
            "this scenario is favorable on every axis except the one certification blocker — "
            "the numeric score must reflect that, not be capped down to some fixed ceiling"
        )

    def test_contract_risk_and_solidite_are_not_blocking_rules(self):
        """MATRICE #16/#17: 'Risque contractuel' and 'Solidité client' are
        ordinary weighted criteria (weight 5 and 3) — neither ever appears
        in criteres_bloquants, however low they score. NON IMPLÉMENTÉ: a
        blocking rule tied to either of these does not exist in the engine."""
        ao = make_ao(texte_source="Pénalités de retard, garantie de résultat, clause SLA stricte.")
        company = make_company(solidite_financiere="Faible")  # not in the "good" set -> low score
        result = score_with_synthetic_policy(ao, company, [], make_capacity())
        assert criterion(result, "Risque contractuel").score < 70
        assert criterion(result, "Solidité client").score < 70
        assert not any("contractuel" in b.lower() or "solidit" in b.lower() for b in result.criteres_bloquants)


# ---------------------------------------------------------------------------
# S12 — Données incomplètes (old: TestEdgeCases, partly)
# ---------------------------------------------------------------------------

class TestIncompleteData:
    def test_score_does_not_crash_on_minimal_ao_context(self):
        """AOContext()/CompanyProfile() with every field left at its
        Pydantic default must not raise — a robustness invariant, not a
        business rule."""
        result = score_with_synthetic_policy(AOContext(), CompanyProfile(), [], make_capacity())
        assert isinstance(result, ScoringResult)
        assert len(result.criteres) == 12


# ---------------------------------------------------------------------------
# S13 — Scénarios réalistes (old: TestRealisticScenarios)
# ---------------------------------------------------------------------------

class TestRealisticScenarios:
    def test_scenario_favorable_conditions_decides_go(self):
        ao = make_ao(
            budget_estime=400_000, technologies_demandees=["Python", "AWS", "Docker"],
            certifications_obligatoires=[],
        )
        capacity = make_capacity(equipe_disponible=True, charge_actuelle_pct=30)
        # B18-T4: 4 distinct references — 4 identical copies are now
        # correctly deduplicated to 1 (DEFECT F09/F12/E-5).
        evidences = [make_evidence(score=0.8, source=f"ref{i}.md", content=f"Contenu de référence numéro {i}.") for i in range(4)]
        result = score_with_synthetic_policy(ao, make_company(secteur="Finance"), evidences, capacity)
        assert result.decision == "GO"

    def test_scenario_mixed_conditions_decides_sous_reserve(self):
        ao = make_ao(budget_estime=250_000, technologies_demandees=["Python", "Django"])
        capacity = make_capacity(equipe_disponible=True, charge_actuelle_pct=50)
        result = score_with_synthetic_policy(ao, make_company(secteur="Public"), [], capacity)
        assert result.decision == "GO SOUS RESERVE"

    def test_scenario_with_blockers_decides_no_go(self):
        ao = make_ao(budget_estime=20_000, certifications_obligatoires=["SecNumCloud"])
        capacity = make_capacity(charge_actuelle_pct=98)
        result = score_with_synthetic_policy(ao, make_company(), [], capacity)
        assert result.decision == "NO-GO"
        assert len(result.criteres_bloquants) >= 2

    def test_unmastered_legacy_technologies_lower_expertise_and_can_block(self):
        ao = make_ao(technologies_demandees=["Cobol", "RPG", "Fortran", "Delphi"])
        result = score_with_synthetic_policy(ao, make_company(), [], make_capacity())
        assert criterion(result, "Adéquation expertise").score == 0.0
        assert any("non maitrisees" in b.lower() or "non maîtrisées" in b.lower() for b in result.criteres_bloquants)

    def test_cutting_edge_unrecognized_tech_is_not_rewarded(self):
        """Characterization, not a bug: 'quantum computing' and
        'blockchain' are in neither `mastered` nor the `strategic` bonus
        set — a technologically cutting-edge AO is scored WORSE, not
        better, by the current engine. Whether this should change is a
        product decision for B06, not a defect to silently fix here."""
        ao = make_ao(technologies_demandees=["Quantum Computing", "Blockchain"])
        result = score_with_synthetic_policy(ao, make_company(), [], make_capacity())
        assert criterion(result, "Adéquation expertise").score == 0.0
        assert criterion(result, "Valeur stratégique").score == 70.0  # the lower, "not strategic" branch

    def test_duree_projet_mois_has_no_effect_on_delai_criterion(self):
        """Characterization, not a bug (revised 2026-09-14 — see
        docs/qa/validation_b04_20260914/): AOContext.duree_projet_mois is
        never read anywhere in ScoringEngine.score() — 'Faisabilité délai'
        is driven purely by a regex over texte_source. This test only
        asserts the FACT that a structured 3-month and a structured
        36-month duration (both with empty texte_source) currently produce
        the IDENTICAL score and justification.

        An earlier version of this test (in TestKnownRegressions) asserted
        that the 3-month case SHOULD score worse than the 36-month one.
        That assumed an unmade product decision — a short duration is not
        automatically tighter (scope, team size, and available resources
        also matter, and none of those are modeled by duration alone).
        Whether/how duree_projet_mois should influence this criterion is a
        product question for B06, not a confirmed bug with a known correct
        direction — see BACKLOG_CORRECTIONS_SCORING.md DEFECT-B04-01."""
        short = make_ao(duree_projet_mois=3, texte_source="")
        long = make_ao(duree_projet_mois=36, texte_source="")
        capacity = make_capacity()
        result_short = score_with_synthetic_policy(short, make_company(), [], capacity)
        result_long = score_with_synthetic_policy(long, make_company(), [], capacity)
        crit_short = criterion(result_short, "Faisabilité délai")
        crit_long = criterion(result_long, "Faisabilité délai")
        assert crit_short.score == crit_long.score
        assert crit_short.justification == crit_long.justification


# ---------------------------------------------------------------------------
# S14 — Justifications (old: TestJustifications)
# ---------------------------------------------------------------------------

class TestJustifications:
    def test_expertise_justification_states_ratio(self):
        ao = make_ao(technologies_demandees=["Python", "Cobol"])
        result = score_with_synthetic_policy(ao, make_company(), [], make_capacity())
        assert criterion(result, "Adéquation expertise").justification == "1/2 technologies ou compétences demandées figurent dans le profil déclaré."

    def test_all_twelve_criteria_have_non_empty_justification(self):
        result = score_with_synthetic_policy(make_ao(), make_company(), [], make_capacity())
        assert len(result.criteres) == 12
        assert all(c.justification.strip() for c in result.criteres)
        assert len({c.nom for c in result.criteres}) == 12  # no duplicate criterion names


# ---------------------------------------------------------------------------
# S01 — Structure et échelle
# ---------------------------------------------------------------------------

class TestStructure:
    def test_weights_sum_to_100_and_are_positive_finite(self):
        # Lot 43: the engine has no weights of its own any more; this file's own
        # synthetic policy (the former defaults) is the only weight set left to check —
        # lot 48 removed `_IT_WEIGHTS_TEMPLATE`/`AVAILABLE_CRITERIA` as confirmed dead
        # code (never read by any frontend script, never tested for content: see
        # docs/qa/lot_48_20260922/RAPPORT_LOT_48.md).
        assert sum(SYNTHETIC_WEIGHTS.values()) == 100
        assert all(w > 0 for w in SYNTHETIC_WEIGHTS.values())

    def test_criteria_set_matches_labels_keys(self):
        assert set(SYNTHETIC_WEIGHTS) == set(ScoringEngine.labels.keys()) == set(ScoringEngine.label_display.keys())

    def test_the_engine_carries_no_demo_registries_of_its_own(self):
        """Lot 43: weights / mastered technologies / held certifications are
        the account's (policy snapshot) — never class-level defaults."""
        for name in ("weights", "mastered", "certs_ok"):
            assert not hasattr(ScoringEngine, name), name

    def test_a_missing_policy_is_refused_never_replaced_by_defaults(self):
        from src.core.private_configuration import PrivateConfigurationRequired
        with pytest.raises(PrivateConfigurationRequired) as excinfo:
            ScoringEngine().score(make_ao(), make_company(), [], make_capacity(), policy=None)
        assert excinfo.value.missing == "scoring_policy"
        with pytest.raises(TypeError):
            ScoringEngine().score(make_ao(), make_company(), [], make_capacity())  # type: ignore[call-arg]

    def test_all_criterion_scores_within_0_100_bounds(self):
        # A deliberately extreme mix of inputs to probe every branch at once.
        # B18-T1: evidence scores are now validated to [0, 1] at construction
        # (DEFECT-B04-04) — 1.0 (the maximum a real similarity can be) is
        # what actually stresses the "References similaires" formula's own
        # `min(100, ...)` ceiling now, not an out-of-domain 5.0.
        ao = make_ao(
            budget_estime=1_000_000, technologies_demandees=["Python", "AWS", "Cobol", "Quantum"],
            certifications_obligatoires=["SecNumCloud"], texte_source="Pénalité, SLA, 4 semaines impératif.",
        )
        result = score_with_synthetic_policy(ao, make_company(secteur=""), [make_evidence(score=1.0)] * 20, make_capacity(charge_actuelle_pct=99))
        assert all(0.0 <= c.score <= 100.0 for c in result.criteres)
        assert 0.0 <= result.score_global <= 100.0


# ---------------------------------------------------------------------------
# S13 — Comparaison entre deux AO (old: TestComparison)
# ---------------------------------------------------------------------------

class TestComparison:
    def test_higher_expertise_match_yields_higher_score_all_else_equal(self):
        base_kwargs = dict(budget_estime=200_000, certifications_obligatoires=[])
        capacity = make_capacity(equipe_disponible=True, charge_actuelle_pct=50)
        weak = score_with_synthetic_policy(make_ao(technologies_demandees=["Cobol", "Fortran"], **base_kwargs), make_company(), [], capacity)
        strong = score_with_synthetic_policy(make_ao(technologies_demandees=["Python", "React"], **base_kwargs), make_company(), [], capacity)
        assert strong.score_global > weak.score_global

    def test_score_varies_across_a_deterministic_set_of_branch_cases(self):
        """Redefined from the historical '100 random AOs' into a fixed,
        reproducible set covering distinct branches (budget tiers x
        blocker presence x expertise match) — no randomness, no
        requirement that every score be unique, only that the set is not
        degenerate (an engine returning one constant would fail this)."""
        cases = [
            make_ao(budget_estime=b, technologies_demandees=t, certifications_obligatoires=c)
            for b in (None, 40_000, 90_000, 300_000)
            for t in (["Python"], ["Cobol"])
            for c in ([], ["SecNumCloud"])
        ]
        capacity = make_capacity(equipe_disponible=True, charge_actuelle_pct=40)
        scores = [score_with_synthetic_policy(ao, make_company(), [], capacity).score_global for ao in cases]
        assert len(set(scores)) > 1, "a constant-returning engine must fail this"
        decisions = {score_with_synthetic_policy(ao, make_company(), [], capacity).decision for ao in cases}
        assert decisions == {"GO", "GO SOUS RESERVE", "NO-GO"} or len(decisions) >= 2


# ---------------------------------------------------------------------------
# Category D — historically named, no corresponding feature exists at all.
# Kept as explicit, honest skips (never silently dropped) — distinct from
# the @known_regression tests below, which DO have real, wrong behavior to
# assert against. See MATRICE_34_INTENTIONS.md #16/#17.
# ---------------------------------------------------------------------------

class TestNonImplemented:
    @pytest.mark.skip(reason="NON IMPLÉMENTÉ : aucune règle bloquante liée à 'Risque contractuel' n'existe dans le moteur — c'est un critère pondéré ordinaire (poids 5), jamais dans criteres_bloquants. Voir MATRICE_34_INTENTIONS.md #16.")
    def test_blocking_rule_contract_risk_0(self):
        ...

    @pytest.mark.skip(reason="NON IMPLÉMENTÉ : aucune règle bloquante liée à 'Solidité client' n'existe dans le moteur — c'est un critère pondéré ordinaire (poids 3), jamais dans criteres_bloquants. Voir MATRICE_34_INTENTIONS.md #17.")
    def test_blocking_rule_client_solidite_0(self):
        ...


# ---------------------------------------------------------------------------
# Category C — closed fixes (B06-T1, 2026-09-14). Both were formerly here as
# intentionally-red @pytest.mark.known_regression tests against
# DEFECT-B04-02/03; B06-T1 fixed src/agents/scoring_engine.py (budget
# None-vs-0 justification text, secteur accent/case-independent comparison)
# and both now pass for real — markers removed per B04's own rule ("ne
# retire le marqueur que s'il passe réellement au vert"). See
# docs/qa/b04_20260914/BACKLOG_CORRECTIONS_SCORING.md for the original
# defect write-up and docs/api/B06_SCORING_CONFIG_CONTRACT.md for what
# changed.
# ---------------------------------------------------------------------------

class TestClosedDefects:
    def test_explicit_zero_budget_gets_its_own_justification_distinct_from_unknown(self):
        """budget_estime=0 (explicit) and budget_estime=None (absent) both
        keep the SAME score (65, deliberately unchanged — no product
        decision was made to alter it), but now get DIFFERENT justification
        text: 'Budget nul' for an explicit 0, 'non communiqué' only for a
        genuinely absent value."""
        result_zero = score_with_synthetic_policy(make_ao(budget_estime=0), make_company(), [], make_capacity())
        result_none = score_with_synthetic_policy(make_ao(budget_estime=None), make_company(), [], make_capacity())
        just_zero = criterion(result_zero, "Rentabilité estimée").justification
        just_none = criterion(result_none, "Rentabilité estimée").justification
        assert "non communiqu" not in just_zero.lower(), f"got: {just_zero!r}"
        assert "non communiqu" in just_none.lower(), f"got: {just_none!r}"
        assert criterion(result_zero, "Rentabilité estimée").score == 65 == criterion(result_none, "Rentabilité estimée").score

    def test_default_company_profile_unknown_sector_now_scores_as_unknown(self):
        """CompanyProfile's own Pydantic default for `secteur` is
        "Non renseigné" (with an accent) — the engine's comparison is now
        accent/case-independent (_fold_accents), so this correctly scores
        as unknown (65) instead of the old silently-"known" (80)."""
        result = score_with_synthetic_policy(make_ao(), CompanyProfile(), [], make_capacity())
        assert criterion(result, "Connaissance secteur").score == 65

    def test_unknown_sector_recognized_regardless_of_case_or_accent_variant(self):
        """Characterizes the FIX itself (not just the default value): any
        casing/accent variant of the two known "unknown" sentinels folds to
        the same unknown branch — this is what makes the fix robust to
        future variants, not just today's exact literal."""
        for variant in ("Non renseigné", "non renseigne", "NON RENSEIGNÉ", "", "  "):
            company = make_company(secteur=variant if variant.strip() else "")
            result = score_with_synthetic_policy(make_ao(), company, [], make_capacity())
            assert criterion(result, "Connaissance secteur").score == 65, f"variant={variant!r}"
