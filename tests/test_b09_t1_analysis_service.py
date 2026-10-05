"""B09-T1 — shared AO-analysis orchestration (src/core/analysis_service.py).

Proof required by the ticket: the same inputs and the same injected doubles
must produce the same business result through BOTH adapter shapes (a
web-style search callable bound to a private scope, and a demo-style global
search callable) — and a private refusal/failure raised by the injected
search callable must propagate unchanged, never swallowed by the shared
module. Builders are copied locally (this codebase's convention: test files
do not import from one another), same shape as tests/test_scoring_engine.py.
"""
from __future__ import annotations

import pytest

from src.core import analysis_service
from src.core.capacity_plan import CapacityPlan
from src.core.models import AOContext, CapacityResult, CompanyProfile, RAGEvidence
from src.core.private_configuration import PrivateConfigurationRequired
from tests.synthetic_scoring import synthetic_policy


def make_ao(**overrides) -> AOContext:
    defaults = dict(
        titre="Refonte portail client", client="Client Synthétique", secteur="Retail",
        budget_estime=250_000.0, deadline_reponse="2026-11-30", duree_projet_mois=6,
        technologies_demandees=["Python", "React"], competences_requises=["Django"],
        questions_client=[], livrables=[], contraintes=[],
        certifications_obligatoires=["ISO 27001"],
        texte_source="Appel d'offres synthétique pour un portail client.",
    )
    defaults.update(overrides)
    return AOContext(**defaults)


def make_company(**overrides) -> CompanyProfile:
    defaults = dict(raison_sociale="Client Synthétique", secteur="Retail", solidite_financiere="Bonne")
    defaults.update(overrides)
    return CompanyProfile(**defaults)


def make_capacity(**overrides) -> CapacityResult:
    defaults = dict(charge_actuelle_pct=40, capacite_restante_pct=60, equipe_disponible=True, commentaire="OK")
    defaults.update(overrides)
    return CapacityResult(**defaults)


def make_evidence(**overrides) -> RAGEvidence:
    defaults = dict(query="q", source="ref.md", score=0.6, content="Référence synthétique pertinente.")
    defaults.update(overrides)
    return RAGEvidence(**defaults)


class FakeLLM:
    """enabled=False takes semantic_rerank's/enrich_with_llm's fast,
    zero-network path — deterministic, no real provider ever constructed."""
    enabled = False

    def json_complete(self, *args, **kwargs):
        raise AssertionError("no LLM call is expected in this test")


class FakeReranker:
    """Stands in for SemanticReranker: exposes only what
    search_and_rerank_evidences actually uses (semantic_rerank +
    thread-local selection status/reason) — never a real corpus."""

    def __init__(self):
        self.last_selection_status = None
        self.last_selection_reason = None

    def semantic_rerank(self, ao_text, evidences, llm):
        assert not llm.enabled
        self.last_selection_status = "not_attempted"
        self.last_selection_reason = None
        return evidences, ""


class RealScoringEngine:
    """Import lazily so a module-level import failure elsewhere can't hide
    behind this test file's own name."""


def _real_scoring_engine():
    from src.agents.scoring_engine import ScoringEngine
    return ScoringEngine()


def _real_capacity_analyzer():
    from src.agents.capacity_analyzer import CapacityAnalyzer
    return CapacityAnalyzer()


def test_web_style_and_demo_style_adapters_produce_the_same_result_from_the_same_doubles():
    """Two different search_evidences callables (one shaped like the web
    adapter — closes over a fake 'db'/org/owner exactly like jobs.py's
    lambda; one shaped like the demo adapter — a bare global search
    function) that both return the SAME evidence list must yield an
    IDENTICAL ScoringResult through search_and_rerank_evidences +
    score_and_enrich — proving the shared module treats both adapter
    shapes identically once the data is the same."""
    ao = make_ao()
    company = make_company()
    capacity = make_capacity()
    fixed_evidences = [make_evidence(source="ref-A.md"), make_evidence(source="ref-B.md", score=0.4)]
    llm = FakeLLM()

    calls_web = []

    def web_style_search(query, top_k, _fake_db=object(), _org="org-1", _owner="user-1"):
        # Mirrors jobs.py's lambda shape: closes over a "session"/scope and
        # ignores the query/top_k for this fixed-fixture test.
        calls_web.append((query, top_k, _fake_db, _org, _owner))
        return list(fixed_evidences)

    def demo_style_search(query, top_k):
        return list(fixed_evidences)

    rerank_web = analysis_service.search_and_rerank_evidences(
        ao, search_evidences=web_style_search, reranker=FakeReranker(), llm=llm, top_k=8,
    )
    rerank_demo = analysis_service.search_and_rerank_evidences(
        ao, search_evidences=demo_style_search, reranker=FakeReranker(), llm=llm, top_k=8,
    )

    assert [e.source for e in rerank_web.evidences] == [e.source for e in rerank_demo.evidences]
    assert rerank_web.rag_synthesis == rerank_demo.rag_synthesis == ""
    assert calls_web[0][0] == analysis_service.build_rag_query(ao)
    assert calls_web[0][1] == 8

    scoring_engine = _real_scoring_engine()
    capacity_analyzer = _real_capacity_analyzer()
    capacity_computed = analysis_service.analyze_capacity(
        ao, capacity_analyzer, plan=CapacityPlan(charge_globale_pct=40, disponibilite_minimum_pct=10),
    )

    result_web = analysis_service.score_and_enrich(
        ao, company, capacity_computed, rerank_web, scoring_engine=scoring_engine, llm=llm, policy=synthetic_policy(),
    )
    result_demo = analysis_service.score_and_enrich(
        ao, company, capacity_computed, rerank_demo, scoring_engine=scoring_engine, llm=llm, policy=synthetic_policy(),
    )

    assert result_web.score_global == result_demo.score_global
    assert result_web.decision == result_demo.decision
    assert [c.nom for c in result_web.criteres] == [c.nom for c in result_demo.criteres]
    assert [c.score for c in result_web.criteres] == [c.score for c in result_demo.criteres]
    assert result_web.rag_selection_status == result_demo.rag_selection_status == "not_attempted"


def test_unified_rag_query_no_longer_diverges_between_the_two_previously_duplicated_builders():
    """Before this ticket, src/web/jobs.py and src/core/pipeline.py built
    two DIFFERENT RAG query strings from the same AOContext (pipeline.py
    additionally folded in competences_requises) — a real, silent
    divergence this ticket closes by having both call the same
    build_rag_query(). Pinned here so a future edit to either caller can't
    quietly reintroduce two builders."""
    ao = make_ao(competences_requises=["Kubernetes"])
    query = analysis_service.build_rag_query(ao)
    assert "Kubernetes" not in query, (
        "build_rag_query is jobs.py's shipped construction (titre + "
        "technologies_demandees + certifications_obligatoires only) — "
        "competences_requises was pipeline.py's now-removed divergence"
    )
    assert ao.titre in query
    assert "ISO 27001" in query


def test_a_private_search_failure_propagates_unchanged_never_swallowed():
    """The ticket's other proof requirement: 'un refus privé/échec conserve
    son contrat' — search_and_rerank_evidences must never catch or
    translate an exception raised by the injected search_evidences
    callable (e.g. src.web.jobs's InvalidRAGEvidenceError path), so the
    caller's own try/except (jobs.py) keeps deciding what it means."""
    ao = make_ao()

    class SentinelError(Exception):
        pass

    def failing_search(query, top_k):
        raise SentinelError("simulated private-corpus refusal")

    with pytest.raises(SentinelError):
        analysis_service.search_and_rerank_evidences(
            ao, search_evidences=failing_search, reranker=FakeReranker(), llm=FakeLLM(), top_k=8,
        )


def test_a_missing_policy_or_capacity_plan_is_refused_never_replaced_by_a_default():
    """Lot 43 (replaces the test that pinned the removed `policy=None` demo
    fallback): the shared steps never supply a default. A missing policy or
    capacity plan raises PrivateConfigurationRequired."""
    ao = make_ao()
    rerank = analysis_service.RerankOutcome(
        evidences=[make_evidence()], rag_synthesis="", selection_status="not_attempted", selection_reason=None,
    )
    with pytest.raises(PrivateConfigurationRequired) as no_policy:
        analysis_service.score_and_enrich(
            ao, make_company(), make_capacity(), rerank, scoring_engine=_real_scoring_engine(), llm=FakeLLM(), policy=None,
        )
    assert no_policy.value.missing == "scoring_policy"
    with pytest.raises(PrivateConfigurationRequired) as no_plan:
        analysis_service.analyze_capacity(ao, _real_capacity_analyzer(), plan=None)
    assert no_plan.value.missing == "capacity_plan"


def test_an_explicit_policy_without_a_provider_profile_keeps_the_generic_enrichment_path():
    """The other half of the old test: with an explicit policy and no
    provider profile, the result is computed and enrichment stays generic —
    no provider identity is ever invented."""
    ao = make_ao()
    company = make_company()
    capacity = make_capacity()
    llm = FakeLLM()
    rerank = analysis_service.RerankOutcome(
        evidences=[make_evidence()], rag_synthesis="", selection_status="not_attempted", selection_reason=None,
    )
    scoring_engine = _real_scoring_engine()

    result = analysis_service.score_and_enrich(
        ao, company, capacity, rerank, scoring_engine=scoring_engine, llm=llm, policy=synthetic_policy(),
    )
    assert result.score_global is not None
    assert result.decision in ("GO", "GO SOUS RESERVE", "NO-GO")
    assert result.enrichment_status == "not_attempted"
    assert result.enrichment_reason == "llm_disabled"