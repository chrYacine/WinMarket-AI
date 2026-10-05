"""Lot 53 — a small, typed, PURE presentation projection built from ONE analysis's own frozen data
(`Job.result`, its `AnalysisComplement` rows, and — for a revision only — its parent's own frozen result).

Never reads the current active policy, the current provider profile, or performs any new RAG/LLM call: an
old result must render identically whenever it is viewed, and a revision's "what changed" must always
compare the snapshots that were ACTUALLY used, never the account's current configuration (ticket, verbatim:
"jamais recalculer le parent avec les paramètres actuels").

Shared by both the result page (`src/web/routes_pages.py`) and the PDF/DOCX generator
(`src/livrables/document_generator.py`) so the two never drift — no scoring/business logic is duplicated in
the Jinja template or in reportlab/python-docx code; both simply render the fields built here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from src.core.models import ScoringResult
from src.core.reference_identity import group_evidences_by_reference

ORIGIN_LABEL = {
    "declared_user": "Déclaré par vous",
    "llm_sourced": "Proposition documentaire acceptée (citation retrouvée)",
}


@dataclass(frozen=True)
class PolicyLabel:
    """`result.criteria_version`/`policy_origin` are frozen on the result at scoring time (lot 44) — this
    just turns them into one honest, short display string. `None` for a result stored before lot 44 (the
    fields did not exist yet) — never a fabricated "v?" placeholder."""
    version: Optional[int]
    origin: Optional[str]

    @property
    def text(self) -> Optional[str]:
        if self.version is None:
            return None
        origin_text = {"legacy": "politique historique migrée", "user": "votre politique"}.get(self.origin)
        return f"v{self.version}" + (f" ({origin_text})" if origin_text else "")


@dataclass(frozen=True)
class ComplementView:
    """One `AnalysisComplement` row, plus what the citation-side data (if any) actually says — never more
    than `source_json` already recorded at submission time (src/web/completion_service.py), never a fresh
    lookup of the underlying document/piece (which could have changed or vanished since)."""
    subject: str
    field_label: str
    value_json: Any
    unit: Optional[str]
    created_at: Any
    origin: str
    origin_label: str
    citation: Optional[str]
    source_label: Optional[str]
    source_kind: Optional[str]


@dataclass(frozen=True)
class CriterionDelta:
    nom: str
    critere_id: Optional[str]
    score_before: Optional[float]
    score_after: Optional[float]
    etat_before: Optional[str]
    etat_after: Optional[str]


@dataclass(frozen=True)
class RevisionDiff:
    """Built ONLY from the two frozen `ScoringResult`s (this revision's own, and its parent's) — never a
    live recompute. `criteria_changed` lists a criterion only when its score OR its état actually differ;
    a criterion present in one result but not the other (a policy version that added/removed a criterion
    between the two) is listed with the missing side as `None`, never silently skipped."""
    decision_before: str
    decision_after: str
    score_before: float
    score_after: float
    criteria_changed: list[CriterionDelta] = field(default_factory=list)


@dataclass(frozen=True)
class ResultView:
    policy_label: Optional[PolicyLabel]
    reference_count: int
    reference_passage_count: int
    complements: list[ComplementView]
    revision_diff: Optional[RevisionDiff]


def _complement_view(c) -> ComplementView:
    origin = getattr(c, "origin", None) or "declared_user"
    source = c.source_json if isinstance(getattr(c, "source_json", None), dict) else None
    return ComplementView(
        subject=c.subject, field_label=c.field_label, value_json=c.value_json, unit=c.unit, created_at=c.created_at,
        origin=origin, origin_label=ORIGIN_LABEL.get(origin, origin),
        citation=(source.get("citation") if source else None),
        source_label=(source.get("source_label") if source else None),
        source_kind=(source.get("kind") if source else None),
    )


def _criterion_key(c) -> str:
    return c.critere_id if c.critere_id else c.nom


def build_revision_diff(parent_result: Optional[ScoringResult], current_result: ScoringResult) -> Optional[RevisionDiff]:
    if parent_result is None:
        return None
    before_by_key = {_criterion_key(c): c for c in (parent_result.criteres or [])}
    after_by_key = {_criterion_key(c): c for c in (current_result.criteres or [])}
    changed: list[CriterionDelta] = []
    for key in dict.fromkeys([*before_by_key.keys(), *after_by_key.keys()]):
        before, after = before_by_key.get(key), after_by_key.get(key)
        if before is not None and after is not None and before.score == after.score and before.etat == after.etat:
            continue
        changed.append(CriterionDelta(
            nom=(after or before).nom, critere_id=(after or before).critere_id,
            score_before=(before.score if before else None), score_after=(after.score if after else None),
            etat_before=(before.etat if before else None), etat_after=(after.etat if after else None),
        ))
    return RevisionDiff(
        decision_before=parent_result.decision, decision_after=current_result.decision,
        score_before=parent_result.score_global, score_after=current_result.score_global,
        criteria_changed=changed,
    )


def build_result_view(result: ScoringResult, complements: list, *, scoring_policy_version: Optional[int], parent_result: Optional[ScoringResult]) -> ResultView:
    """`complements`: the raw `AnalysisComplement` ORM rows for THIS job (already scoped/fetched by the
    caller — this function performs no query of its own). `parent_result`: the parent job's own frozen
    `ScoringResult` when this job is a completion revision, else None (an ordinary analysis has no diff)."""
    groups = group_evidences_by_reference(result.evidence_pack or [])
    policy_label = PolicyLabel(version=scoring_policy_version, origin=result.policy_origin) if scoring_policy_version is not None else None
    return ResultView(
        policy_label=policy_label,
        reference_count=len(groups),
        reference_passage_count=len(result.evidence_pack or []),
        complements=[_complement_view(c) for c in complements],
        revision_diff=build_revision_diff(parent_result, result),
    )
