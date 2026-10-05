"""B06-T4 / B19-T1 — business rules come from the account's own validated
ScoringPolicy, and the scoring system prompt from its real ProviderProfile.

Two distinct contracts are covered here:

1. The four values that used to be hardcoded in ScoringEngine.score()
   (minimum budget, maximum team charge, maximum unmastered technologies,
   certification penalty) now come from ScoringPolicySnapshot. A rule the
   account never configured is NOT silently replaced by the old constant:
   its check is skipped, the rule is named in `scoring_missing`, and the
   decision becomes "INCOMPLET" rather than a fabricated GO/NO-GO.
   Lot 43: `policy=None` (the frozen Streamlit path that kept the old
   literals) no longer exists — it is refused; the same literals now only
   apply when a snapshot carries them explicitly (section 4 below).
2. enrich_with_llm's system prompt no longer asserts a fictional company
   biography ("200-500 collaborateurs", "300 marchés").

Pure engine-level tests: no DB, no network, no API key. Builders are
copied from tests/test_scoring_engine.py rather than imported, so this
file stands alone.
"""
from __future__ import annotations

from src.agents.scoring_engine import ScoringEngine, ScoringPolicySnapshot
from src.core.models import AOContext, CapacityResult, CompanyProfile, RAGEvidence
from tests.synthetic_scoring import score_with_synthetic_policy


# ---------------------------------------------------------------------------
# Builders (same shape as tests/test_scoring_engine.py)
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
    defaults = dict(raison_sociale="Client Synthétique", secteur="Retail", solidite_financiere="Bonne")
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


def criterion(result, label_display: str):
    match = next((c for c in result.criteres if c.nom == label_display), None)
    assert match is not None, f"critère {label_display!r} introuvable parmi {[c.nom for c in result.criteres]}"
    return match


WEIGHTS = {
    "Adequation expertise": 20, "References similaires": 15, "Disponibilite equipe": 10,
    "Rentabilite estimee": 10, "Faisabilite delai": 10, "Certifications requises": 10,
    "Complexite technique": 5, "Connaissance secteur": 5, "Potentiel commercial": 5,
    "Risque contractuel": 5, "Solidite client": 3, "Valeur strategique": 2,
}


def make_policy(**overrides) -> ScoringPolicySnapshot:
    defaults = dict(
        weights=dict(WEIGHTS), threshold_go=88, threshold_sous_reserve=60,
        mastered_technologies=frozenset({"python", "django"}),
        certifications_held=frozenset({"iso 27001"}),
        version=1,
    )
    defaults.update(overrides)
    return ScoringPolicySnapshot.from_legacy(**defaults)


FULLY_CONFIGURED = dict(
    budget_minimum_eur=120_000, max_charge_pct=70, max_unmastered_technologies=2,
    certification_penalty_score=35,
)


# ---------------------------------------------------------------------------
# 1 — a fully configured policy: the CONFIGURED number gates, not the old one
# ---------------------------------------------------------------------------

class TestConfiguredBusinessRulesReplaceHardcodedDefaults:
    def test_configured_budget_minimum_gates_instead_of_the_old_50k_literal(self):
        """budget 80 000 € is ABOVE the old hardcoded 50 000 (so the
        pre-B06-T4 engine would NOT have blocked) but BELOW this account's
        configured 120 000 — proving the configured value is what actually
        gates."""
        ao = make_ao(budget_estime=80_000, technologies_demandees=["Python"])
        result = ScoringEngine().score(
            ao, make_company(), [], make_capacity(charge_actuelle_pct=40), policy=make_policy(**FULLY_CONFIGURED),
        )
        assert any("120 000" in b for b in result.criteres_bloquants), result.criteres_bloquants
        assert not any("50 000" in b for b in result.criteres_bloquants)
        assert result.decision == "NO-GO"
        assert result.scoring_completeness == "complete"
        assert result.scoring_missing == []

    def test_configured_max_charge_gates_instead_of_the_old_95_literal(self):
        """charge 80% is under the old hardcoded 95 but over the configured
        70."""
        ao = make_ao(budget_estime=250_000, technologies_demandees=["Python"])
        result = ScoringEngine().score(
            ao, make_company(), [], make_capacity(charge_actuelle_pct=80), policy=make_policy(**FULLY_CONFIGURED),
        )
        assert any("70%" in b for b in result.criteres_bloquants), result.criteres_bloquants
        assert not any("95%" in b for b in result.criteres_bloquants)
        assert result.decision == "NO-GO"
        assert result.scoring_completeness == "complete"

    def test_configured_max_unmastered_technologies_gates_instead_of_the_old_4_literal(self):
        """2 unmastered technologies is under the old hardcoded 4 but meets
        the configured maximum of 2."""
        ao = make_ao(budget_estime=250_000, technologies_demandees=["Python", "Cobol", "Fortran"])
        result = ScoringEngine().score(
            ao, make_company(), [], make_capacity(charge_actuelle_pct=40), policy=make_policy(**FULLY_CONFIGURED),
        )
        assert any("non maitrisees" in b.lower() for b in result.criteres_bloquants), result.criteres_bloquants
        assert result.decision == "NO-GO"
        assert result.scoring_completeness == "complete"
        assert result.scoring_missing == []

    def test_configured_certification_penalty_replaces_the_hardcoded_20(self):
        ao = make_ao(budget_estime=250_000, technologies_demandees=["Python"], certifications_obligatoires=["SecNumCloud"])
        result = ScoringEngine().score(
            ao, make_company(), [], make_capacity(charge_actuelle_pct=40), policy=make_policy(**FULLY_CONFIGURED),
        )
        assert criterion(result, "Certifications requises").score == 35
        assert result.scoring_completeness == "complete"
        assert result.scoring_missing == []

    def test_fully_configured_favorable_ao_is_complete_and_not_blocked(self):
        """Counter-case: a configured policy whose rules are all satisfied
        produces an ordinary, complete decision — the new machinery must not
        block or mark incomplete on its own."""
        ao = make_ao(budget_estime=250_000, technologies_demandees=["Python", "Django"])
        result = ScoringEngine().score(
            ao, make_company(secteur="Public"), [], make_capacity(charge_actuelle_pct=40),
            policy=make_policy(**FULLY_CONFIGURED),
        )
        assert result.criteres_bloquants == []
        assert result.scoring_completeness == "complete"
        assert result.scoring_missing == []
        assert result.decision in ("GO", "GO SOUS RESERVE")


# ---------------------------------------------------------------------------
# 2 — a policy activated before this ticket: nothing configured at all
# ---------------------------------------------------------------------------

class TestUnconfiguredRulesProduceAnExplicitlyIncompleteResult:
    def test_all_rules_unconfigured_yields_incomplet_without_fabricating_a_decision(self):
        """An AO/capacity that WOULD need these rules to know whether a
        blocker exists: budget 80 000 and charge 80% could each be blocking
        or not depending on numbers this account never configured. The
        engine must say so rather than guess either way."""
        ao = make_ao(budget_estime=80_000, technologies_demandees=["Python", "Cobol"])
        result = ScoringEngine().score(
            ao, make_company(), [], make_capacity(charge_actuelle_pct=80), policy=make_policy(),
        )
        assert result.scoring_completeness == "incomplete"
        assert result.scoring_missing == [
            "budget_minimum_eur", "max_charge_pct", "max_unmastered_technologies",
        ]
        assert result.decision == "INCOMPLET"
        # Neither guessed outcome was fabricated: no blocker was invented...
        assert result.criteres_bloquants == []
        # ...and the skipped checks are named, not silently passed.
        assert "certification_penalty_score" not in result.scoring_missing

    def test_incomplete_result_keeps_a_real_finite_weighted_score(self):
        """score_global is never zeroed, never renormalized to hide the gap
        — it is the same weighted sum the complete path computes over the
        criteria that WERE calculated. Verified against an independent
        hand-computation, not against the engine's own criteria list."""
        ao = make_ao(budget_estime=250_000, technologies_demandees=["Python", "Django"])
        company = make_company(secteur="Public", solidite_financiere="Bonne")
        capacity = make_capacity(charge_actuelle_pct=50)
        result = ScoringEngine().score(ao, company, [], capacity, policy=make_policy())

        assert result.decision == "INCOMPLET"
        assert result.scoring_completeness == "incomplete"
        # Same scenario as tests/test_scoring_engine.py's hand-computed
        # 80.2 case (identical weights, identical inputs, both technologies
        # mastered by this policy) — the arithmetic is untouched by
        # incompleteness.
        assert result.score_global == 80.2
        expected = round(sum(c.score * c.poids for c in result.criteres) / 100, 1)
        assert result.score_global == expected
        assert result.score_global == result.score_global  # not NaN
        assert result.score_global > 0

    def test_missing_rule_names_are_sorted_and_use_the_business_rules_keys(self):
        result = ScoringEngine().score(
            make_ao(budget_estime=80_000), make_company(), [], make_capacity(), policy=make_policy(),
        )
        assert result.scoring_missing == sorted(result.scoring_missing)
        assert set(result.scoring_missing) <= {
            "budget_minimum_eur", "max_charge_pct", "max_unmastered_technologies",
            "certification_penalty_score",
        }

    def test_partially_configured_policy_reports_only_what_is_missing(self):
        ao = make_ao(budget_estime=250_000, technologies_demandees=["Python"])
        policy = make_policy(budget_minimum_eur=100_000, max_charge_pct=90)
        result = ScoringEngine().score(ao, make_company(), [], make_capacity(charge_actuelle_pct=40), policy=policy)
        assert result.scoring_missing == ["max_unmastered_technologies"]
        assert result.scoring_completeness == "incomplete"
        assert result.decision == "INCOMPLET"

    def test_incomplete_recommendations_do_not_prescribe_confident_go_actions(self):
        """The GO branch's "désigner le chef de projet / lancer le mémoire
        technique" actions must not be emitted for an analysis that could
        not evaluate its own blockers."""
        result = ScoringEngine().score(
            make_ao(budget_estime=250_000, technologies_demandees=["Python"]),
            make_company(), [], make_capacity(), policy=make_policy(),
        )
        assert result.decision == "INCOMPLET"
        joined = " ".join(result.recommandations).lower()
        assert "chef de projet" not in joined
        # Lot 45: the technical rule key stays in the contract (`scoring_missing`);
        # the recommendation names the CRITERION (its label), not the raw key.
        assert "max_charge_pct" in result.scoring_missing
        assert "max_charge_pct" not in joined
        assert "disponibilité équipe" in joined


# ---------------------------------------------------------------------------
# 3 — the certification-penalty exception (a numeric score is unavoidable)
# ---------------------------------------------------------------------------

class TestCertificationPenaltyException:
    def test_missing_penalty_falls_back_to_20_and_is_reported_as_missing(self):
        """cert_missing is non-empty and certification_penalty_score is
        unconfigured: CriterionScore.score is a required float, so the
        engine uses its generic 20 placeholder — but says so through
        scoring_missing instead of passing it off as a business decision.

        Here the certification blocker DOES fire (a fully determined rule:
        the policy's certifications_held simply doesn't contain it), so per
        the precedence rule the decision stays a confirmed "NO-GO" — the
        result is still flagged incomplete."""
        ao = make_ao(
            budget_estime=250_000, technologies_demandees=["Python"],
            certifications_obligatoires=["SecNumCloud"],
        )
        result = ScoringEngine().score(ao, make_company(), [], make_capacity(), policy=make_policy())
        assert criterion(result, "Certifications requises").score == 20
        assert "certification_penalty_score" in result.scoring_missing
        assert result.scoring_completeness == "incomplete"
        assert any("SecNumCloud" in b for b in result.criteres_bloquants)
        assert result.decision == "NO-GO", (
            "a blocker that DID fire through a fully-determined rule is a confirmed NO-GO, "
            "never downgraded to INCOMPLET"
        )

    def test_penalty_as_the_only_missing_rule_still_reports_a_confirmed_no_go(self):
        """Structural consequence of the contract, asserted explicitly: the
        penalty is only ever recorded as missing when cert_missing is
        non-empty, and a non-empty cert_missing ALWAYS raises the
        certification blocker. So "certification_penalty_score" can never
        be the reason a decision becomes INCOMPLET — the NO-GO it
        accompanies is fully determined by certifications_held alone."""
        ao = make_ao(
            budget_estime=250_000, technologies_demandees=["Python"],
            certifications_obligatoires=["SecNumCloud"],
        )
        policy = make_policy(budget_minimum_eur=100_000, max_charge_pct=90, max_unmastered_technologies=3)
        result = ScoringEngine().score(ao, make_company(), [], make_capacity(charge_actuelle_pct=40), policy=policy)
        assert result.scoring_missing == ["certification_penalty_score"]
        assert result.scoring_completeness == "incomplete"
        assert result.decision == "NO-GO"
        assert criterion(result, "Certifications requises").score == 20

    def test_no_missing_certification_never_reports_the_penalty_as_a_gap(self):
        """cert_missing EMPTY with certification_penalty_score None: the
        penalty was never needed, so its absence is not a configuration
        gap and must not appear in scoring_missing."""
        ao = make_ao(budget_estime=250_000, technologies_demandees=["Python"], certifications_obligatoires=[])
        policy = make_policy(budget_minimum_eur=100_000, max_charge_pct=90, max_unmastered_technologies=3)
        result = ScoringEngine().score(ao, make_company(), [], make_capacity(charge_actuelle_pct=40), policy=policy)
        assert criterion(result, "Certifications requises").score == 100
        assert "certification_penalty_score" not in result.scoring_missing
        assert result.scoring_completeness == "complete"


# ---------------------------------------------------------------------------
# 4 — the former hardcoded literals (Lot 43: no longer a policy=None path —
#     the same values, carried explicitly by a fully configured snapshot,
#     produce the same results byte for byte)
# ---------------------------------------------------------------------------

class TestFormerLiteralsBehaveIdenticallyWhenConfiguredExplicitly:
    def test_budget_below_the_old_50k_literal_still_blocks_with_the_explicit_value(self):
        ao = make_ao(budget_estime=40_000)
        result = score_with_synthetic_policy(ao, make_company(), [], make_capacity())
        # Lot 44: same blocker, same threshold — the wording no longer presumes
        # an ESN or a profitability rule ("seuil minimal de rentabilité ESN").
        assert result.criteres_bloquants == ["Budget inférieur au minimum fixé par la politique (50 000)"]
        assert result.decision == "NO-GO"
        assert result.scoring_completeness == "complete"
        assert result.scoring_missing == []

    def test_charge_above_the_old_95_literal_still_blocks_with_the_explicit_value(self):
        result = score_with_synthetic_policy(
            make_ao(budget_estime=250_000), make_company(), [], make_capacity(charge_actuelle_pct=99),
        )
        assert "Charge equipe superieure a 95% — impossible de demarrer" in result.criteres_bloquants
        assert result.decision == "NO-GO"
        assert result.scoring_completeness == "complete"

    def test_four_unmastered_technologies_still_block_with_the_explicit_value(self):
        ao = make_ao(budget_estime=250_000, technologies_demandees=["Cobol", "Fortran", "Delphi", "RPG"])
        result = score_with_synthetic_policy(ao, make_company(), [], make_capacity())
        assert any("non maitrisees" in b.lower() for b in result.criteres_bloquants)
        assert result.decision == "NO-GO"
        assert result.scoring_missing == []

    def test_missing_certification_still_scores_20_with_the_explicit_value(self):
        ao = make_ao(budget_estime=250_000, certifications_obligatoires=["SecNumCloud"])
        result = score_with_synthetic_policy(ao, make_company(), [], make_capacity())
        assert criterion(result, "Certifications requises").score == 20
        assert result.scoring_completeness == "complete"
        assert result.scoring_missing == []

    def test_a_fully_configured_snapshot_never_produces_the_incomplet_decision(self):
        """Every rule is configured, so there is nothing missing to report
        (INCOMPLET is reserved for an unconfigured rule/criterion)."""
        for ao in (
            make_ao(), make_ao(budget_estime=40_000), make_ao(budget_estime=500_000, technologies_demandees=["Python"]),
        ):
            result = score_with_synthetic_policy(ao, make_company(), [], make_capacity(charge_actuelle_pct=99))
            assert result.decision != "INCOMPLET"
            assert result.scoring_completeness == "complete"

    def test_no_snapshot_at_all_is_refused_instead_of_applying_the_old_literals(self):
        from src.core.private_configuration import PrivateConfigurationRequired
        import pytest
        with pytest.raises(PrivateConfigurationRequired):
            ScoringEngine().score(make_ao(budget_estime=40_000), make_company(), [], make_capacity(), policy=None)


# ---------------------------------------------------------------------------
# 5 — B19-T1: the system prompt no longer fabricates a company biography
# ---------------------------------------------------------------------------

class _PromptCapturingLLM:
    """Stands in for the `llm` argument of enrich_with_llm: captures the
    prompt/system it was handed and returns a minimal valid payload. No
    network, no key, no provider."""

    enabled = True

    def __init__(self):
        self.prompt = None
        self.system = None

    def json_complete(self, prompt, system=None, **kwargs):
        self.prompt = prompt
        self.system = system
        return {"justifications": {}}


class _FakeProviderProfile:
    """Duck-type of the two src.web.database.models.ProviderProfile fields
    the prompt builder reads — avoids importing the ORM (and its DB
    machinery) into a pure engine test."""

    def __init__(self, raison_sociale=None, competences=None):
        self.raison_sociale = raison_sociale
        self.competences = competences if competences is not None else []


FABRICATED_CLAIMS = ("200-500", "300 marchés", "20 ans")


def _scored_result():
    return score_with_synthetic_policy(
        make_ao(budget_estime=250_000, technologies_demandees=["Python"]), make_company(), [], make_capacity(),
    )


class TestSystemPromptBuiltFromRealProviderProfile:
    def test_real_profile_name_and_competences_reach_the_system_prompt(self):
        llm = _PromptCapturingLLM()
        profile = _FakeProviderProfile(raison_sociale="Nova Digital", competences=["python", "aws"])
        ScoringEngine().enrich_with_llm(make_ao(), _scored_result(), llm, provider_profile=profile)

        assert llm.system is not None
        assert "Nova Digital" in llm.system
        assert "python" in llm.system and "aws" in llm.system
        for claim in FABRICATED_CLAIMS:
            assert claim not in llm.system, f"fabricated claim {claim!r} still present: {llm.system!r}"

    def test_absent_profile_falls_back_to_a_generic_prompt_with_no_fabricated_facts(self):
        llm = _PromptCapturingLLM()
        ScoringEngine().enrich_with_llm(make_ao(), _scored_result(), llm)

        assert llm.system is not None
        # Lot 44: no assumed role ("directeur avant-vente"), sector or ESN.
        assert "ESN" not in llm.system and "avant-vente" not in llm.system
        assert "ne te sont pas communiqués" in llm.system
        for claim in FABRICATED_CLAIMS:
            assert claim not in llm.system, f"fabricated claim {claim!r} still present: {llm.system!r}"

    def test_profile_without_raison_sociale_uses_the_generic_fallback(self):
        llm = _PromptCapturingLLM()
        profile = _FakeProviderProfile(raison_sociale="   ", competences=["python"])
        ScoringEngine().enrich_with_llm(make_ao(), _scored_result(), llm, provider_profile=profile)

        for claim in FABRICATED_CLAIMS:
            assert claim not in llm.system
        assert "ESN" not in llm.system and "avant-vente" not in llm.system
        assert "ne te sont pas communiqués" in llm.system

    def test_profile_with_no_competences_omits_the_clause_rather_than_inventing_one(self):
        llm = _PromptCapturingLLM()
        profile = _FakeProviderProfile(raison_sociale="Nova Digital", competences=[])
        ScoringEngine().enrich_with_llm(make_ao(), _scored_result(), llm, provider_profile=profile)

        assert "Nova Digital" in llm.system
        assert "compétences déclarées" not in llm.system
        for claim in FABRICATED_CLAIMS:
            assert claim not in llm.system

    def test_fabricated_biography_is_absent_from_the_module_source_entirely(self):
        """Not just from the built prompt: the claims must not survive as a
        class constant anyone could reintroduce into a prompt."""
        import inspect

        import src.agents.scoring_engine as module

        source = inspect.getsource(module)
        assert "200-500 collaborateurs" not in source
        assert "300 marchés" not in source
