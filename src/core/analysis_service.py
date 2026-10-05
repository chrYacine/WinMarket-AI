"""B09-T1 — shared AO-analysis orchestration.

The extract -> enrich company -> search+rerank -> analyze capacity -> score
sequence used by `src/web/jobs.py::_run_analysis` (the SaaS job path), kept
as named steps so each stage stays individually testable.

Lot 43: the second caller this module was extracted for (the Streamlit demo,
`src/core/pipeline.py`) no longer exists. What remains is unchanged in
structure but its contract is now stricter: this module is a pure
orchestration layer that never opens a database session, never decides which
corpus/capacity/policy scope applies to a request, never caches anything
privately scoped, and — new — never supplies a default for any of them.
Every resource is INJECTED by the caller as an already-resolved value or an
explicit adapter callable:
- the caller resolves its access context (private, per organization_id/
  owner_user_id) BEFORE calling into this module, and passes a
  `search_evidences` callable bound to that private scope;
- the caller passes the account's own `CapacityPlan` and
  `ScoringPolicySnapshot`; a missing one is refused
  (`PrivateConfigurationRequired`), never replaced by a demo value.

Job state, HTTP transport, progress reporting, SQL/JSON persistence and
document rendering all stay OUTSIDE this module — see
`src/web/jobs.py::_run_analysis` for job-specific error codes/status
transitions and `src/livrables/document_generator.py` for rendering.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List, Optional

from src.agents.ao_extractor import AOExtractor
from src.agents.capacity_analyzer import CapacityAnalyzer, CapacityResult
from src.agents.company_enrichment import CompanyEnrichmentAgent, CompanyProfile
from src.agents.scoring_engine import ScoringEngine, ScoringPolicySnapshot
from src.core.capacity_plan import CapacityPlan
from src.core.models import AOContext, ScoringResult
from src.core.private_configuration import PrivateConfigurationRequired


def build_rag_query(ao: AOContext) -> str:
    """The one RAG query string built from an extracted AO (titre +
    technologies_demandees + certifications_obligatoires)."""
    return " ".join([ao.titre] + ao.technologies_demandees + ao.certifications_obligatoires)


def extract_ao(text: str, extractor: AOExtractor, *, requested_facts: Optional[dict] = None) -> AOContext:
    """Trivial wrapper — kept as a named step (not inlined at each call
    site) so the caller reads as a sequence of named stages.

    B05-T3: `requested_facts` (default None) is additive — built by the web
    path (src/web/jobs.py) from the active ScoringPolicy's own
    custom_criteria (src.agents.business_facts.requested_facts_from_criteria)."""
    return extractor.extract(text, requested_facts=requested_facts)


def enrich_company(
    ao: AOContext, company_agent: CompanyEnrichmentAgent, *, external_enrichment_enabled: bool,
) -> CompanyProfile:
    return company_agent.enrich(ao.client, external_enrichment_enabled=external_enrichment_enabled)


@dataclass
class RerankOutcome:
    """The full result of searching + reranking — not just the evidence
    list — so a caller (jobs.py) can attach `rag_selection_status`/
    `rag_selection_reason` onto its ScoringResult exactly as it already
    does, without reaching back into the reranker's thread-local state
    itself."""
    evidences: List
    rag_synthesis: str
    selection_status: Optional[str]
    selection_reason: Optional[str]


def search_and_rerank_evidences(
    ao: AOContext,
    *,
    search_evidences: Callable[[str, int], List],
    reranker,
    llm,
    top_k: int = 8,
) -> RerankOutcome:
    """`search_evidences(query, top_k)` is the injected corpus adapter — a
    private, DB-backed corpus search already bound to the caller's own
    (organization_id, owner_user_id) (`src.rag.private_rag_manager.search`).
    This function never searches a corpus itself and never decides which
    one is reachable — that access-context resolution stays entirely with
    the caller.

    `reranker` (src.rag.semantic_rerank.SemanticReranker) is used ONLY for
    its stateless `semantic_rerank` LLM call and its thread-local
    `last_selection_status`/`last_selection_reason` (read back immediately,
    on this same thread) — never for searching a corpus, and it holds no
    private data (ticket: "pas de cache global de données privées")."""
    query = build_rag_query(ao)
    evidences = search_evidences(query, top_k)
    evidences, rag_synthesis = reranker.semantic_rerank(ao.texte_source, evidences, llm)
    return RerankOutcome(
        evidences=evidences,
        rag_synthesis=rag_synthesis,
        selection_status=reranker.last_selection_status,
        selection_reason=reranker.last_selection_reason,
    )


def analyze_capacity(
    ao: AOContext, capacity_analyzer: CapacityAnalyzer, plan: CapacityPlan,
) -> CapacityResult:
    """`plan` is the account's own private capacity plan. Lot 43: a missing
    plan is refused — there is no file-based/demo capacity to fall back on."""
    if plan is None:
        raise PrivateConfigurationRequired("capacity_plan")
    return capacity_analyzer.analyze(ao, plan=plan)


def score_and_enrich(
    ao: AOContext,
    company: CompanyProfile,
    capacity: CapacityResult,
    rerank: RerankOutcome,
    *,
    scoring_engine: ScoringEngine,
    llm,
    policy: ScoringPolicySnapshot,
    provider_profile=None,
) -> ScoringResult:
    """Scoring + RAG-outcome attachment + LLM enrichment, in the one order
    used everywhere — never reordered.

    Lot 43: `policy` is required. The caller must resolve and pass a real
    `ScoringPolicySnapshot` built from the account's own ACTIVE policy and
    profile (this module has no database access to resolve one itself); a
    missing one raises PrivateConfigurationRequired from the engine — the
    process-wide weights/thresholds/mastered/certs_ok globals it used to
    fall back on no longer exist.
    `provider_profile=None` keeps the enrichment prompt generic, never
    inventing a company identity — see
    `ScoringEngine._build_scoring_system_prompt`."""
    result = scoring_engine.score(ao, company, rerank.evidences, capacity, policy=policy)
    result.rag_synthesis = rerank.rag_synthesis
    result.rag_selection_status = rerank.selection_status
    result.rag_selection_reason = rerank.selection_reason
    return scoring_engine.enrich_with_llm(ao, result, llm, provider_profile=provider_profile)
