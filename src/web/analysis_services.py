"""The stateless services one analysis job needs — the replacement for the
former process-wide `AOPipeline` singleton (lot 43).

That singleton (`src/web/pipeline_singleton.py` + `src/core/pipeline.py`)
built, at first use, a TF-IDF index over the demo corpus in `data/reg_docs`
and a file-backed capacity analyzer, none of which the SaaS path reads (it
searches the account's own private corpus and uses the account's own
capacity plan). Building the services below loads no corpus and reads no file:
they are cheap, stateless objects created per job, so nothing is shared
between accounts or between jobs.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.agents.scoring_engine import ScoringEngine
from src.core.content_preparation import ContentPreparer
from src.core.content_security import ContentSecurityGate
from src.livrables.document_generator import DocumentGenerator
from src.rag.semantic_rerank import SemanticReranker


@dataclass(frozen=True)
class AnalysisServices:
    security: ContentSecurityGate
    preparer: ContentPreparer
    reranker: SemanticReranker
    scoring: ScoringEngine
    generator: DocumentGenerator


def build_analysis_services() -> AnalysisServices:
    return AnalysisServices(
        security=ContentSecurityGate(),
        preparer=ContentPreparer(),
        reranker=SemanticReranker(),
        scoring=ScoringEngine(),
        generator=DocumentGenerator(),
    )
