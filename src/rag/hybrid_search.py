"""Lot 51 — hybrid (lexical + vector) search orchestration.

Structural gate, checked FIRST and always: vector search is only ever
attempted on PostgreSQL with pgvector AND config.RAG_HYBRID_MODE_ENABLED
(src/rag/hybrid_index.hybrid_mode_active). Every other case (SQLite, or
PostgreSQL with hybrid mode not explicitly turned on) returns EXACTLY the
existing lexical result unchanged — "SQLite continue de fonctionner en
mode lexical déclaré", never silently degraded, never a fabricated hybrid
label.

Fusion identity is the KnowledgeChunk (not the finer-grained passage): a
chunk with several matching passages, or a chunk found by BOTH lexical and
vector search, contributes exactly ONE candidate — never inflating its own
evidence count (ticket: "plusieurs passages d'une même référence
n'augmentent pas son nombre de preuves"). `RAGEvidence.score` is ALWAYS the
existing lexical cosine-similarity definition, recomputed for the FINAL
candidate set regardless of which signal found them — a vector-only match
can legitimately end up with a near-zero lexical score (a documented
limitation of keeping the two signals separate, never silently averaged
or rescaled into one another).
"""
from __future__ import annotations

import math
import uuid
from dataclasses import dataclass

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from src.core import config
from src.core.models import RAGEvidence
from src.rag import private_rag_manager
from src.rag.embeddings import EmbeddingAdapter, EmbeddingUnavailableError, current_embedding_config
from src.rag.hybrid_index import hybrid_mode_active

# Modes reported in HybridSearchOutcome.mode — the "mode réellement exécuté"
# the lot 51 ticket requires every result/snapshot to carry:
MODE_EMPTY_CORPUS = "empty_corpus"          # zero searchable chunks at all
MODE_LEXICAL = "lexical"                    # hybrid structurally inactive (SQLite, or disabled) — declared, not a failure
MODE_HYBRID = "hybrid"                      # both signals genuinely queried and fused, over the WHOLE active corpus
MODE_HYBRID_PARTIAL = "hybrid_partial"      # both signals ran, but at least one active document's vector index is failed/pending/stale — never silently reported as full hybrid coverage
MODE_HYBRID_DEGRADED = "hybrid_degraded_vector_unavailable"  # postgres+enabled, but THIS call's embedding failed — controlled fallback to lexical, never a fake "zero results"


@dataclass
class HybridSearchOutcome:
    evidences: list[RAGEvidence]
    mode: str
    degraded_reason: str | None = None


@dataclass(frozen=True)
class VectorHit:
    """The ACTUAL winning passage for one chunk — never re-derived. Lot 51
    bis: the previous version of this module discarded start_char/end_char/
    content entirely and let a chunk found only by the vector signal be
    displayed via a TF-IDF-based passage relocation instead, which could
    show an excerpt with nothing to do with why the embedding matched."""
    chunk_id: uuid.UUID
    content: str
    start_char: int
    end_char: int


def _vector_candidates(
    db: Session, *, organization_id: uuid.UUID, owner_user_id: uuid.UUID, query_vector: list[float], limit: int
) -> list[VectorHit]:
    """Best-matching chunk ids, nearest first, deduplicated (the BEST-
    scoring passage's own content/offsets are kept for each chunk — never
    just its id). Filtered on the CURRENT embedding config at the PASSAGE
    level (never trusting only the parent version's status flag) so a
    reindex-in-progress can never mix vectors from two different model
    configs in one query. No ANN index — a plain ORDER BY ... LIMIT
    sequential scan, sufficient at this corpus size (ticket: "pas d'index
    ANN obligatoire pour un petit corpus")."""
    from pgvector.sqlalchemy import Vector

    current = current_embedding_config()
    stmt = text("""
        SELECT p.chunk_id, p.content, p.start_char, p.end_char, p.embedding <=> :qvec AS distance
        FROM knowledge_passages p
        JOIN knowledge_chunks c ON c.id = p.chunk_id
        JOIN knowledge_document_versions v ON v.id = p.document_version_id
        JOIN knowledge_documents d ON d.id = v.document_id
        WHERE p.organization_id = :org AND p.owner_user_id = :owner
          AND d.status = 'active' AND d.active_version_id = v.id
          AND v.embedding_status = 'ready'
          AND p.embedding_model_id = :model_id AND p.embedding_model_revision = :model_revision
          AND p.embedding_dimension = :dimension AND p.embedding IS NOT NULL
        ORDER BY p.embedding <=> :qvec ASC
        LIMIT :row_limit
    """).bindparams(bindparam("qvec", type_=Vector(current.dimension)))
    rows = db.execute(stmt, {
        "qvec": query_vector, "org": organization_id, "owner": owner_user_id,
        "model_id": current.model_id, "model_revision": current.model_revision,
        "dimension": current.dimension,
        # a passage-level limit generously above the chunk-level limit — several
        # passages of the same chunk collapse to one candidate below, in order.
        "row_limit": limit * 4,
    }).all()

    ordered_unique: list[VectorHit] = []
    seen: set[uuid.UUID] = set()
    for chunk_id, content, start_char, end_char, _distance in rows:
        cid = chunk_id if isinstance(chunk_id, uuid.UUID) else uuid.UUID(str(chunk_id))
        if cid in seen:
            continue
        seen.add(cid)
        ordered_unique.append(VectorHit(chunk_id=cid, content=content, start_char=start_char, end_char=end_char))
        if len(ordered_unique) >= limit:
            break
    return ordered_unique


def _rrf_fuse(lexical_rank: dict[str, int], vector_rank: dict[str, int], *, k: int) -> list[str]:
    scores: dict[str, float] = {}
    for chunk_id, rank in lexical_rank.items():
        scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank + 1)
    for chunk_id, rank in vector_rank.items():
        scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores, key=lambda c: scores[c], reverse=True)


def _finalize(evidences: list[RAGEvidence], mode: str, *, db: Session, organization_id: uuid.UUID, owner_user_id: uuid.UUID, degraded_reason: str | None = None) -> HybridSearchOutcome:
    """A REAL absence of results is only ever reported as 'empty_corpus' when
    the corpus itself is genuinely empty — checked LAST, on the final
    evidence list, never as an up-front gate (calling private_rag_manager.
    search() unconditionally first is what lets an existing test substitute
    a deterministic fake search via monkeypatch — see module docstring).

    Deliberately reads knowledge_repo.active_chunk_ids_for_corpus directly
    (a plain SELECT) rather than private_rag_manager.corpus_is_empty: that
    function calls _get_snapshot -> knowledge_repo.get_or_create_corpus,
    which CREATES a corpus row as a side effect if none exists yet — fine
    for a real search, but several existing tests monkeypatch ONLY
    private_rag_manager.search (not corpus_is_empty) specifically to keep
    an AO-dossier analysis job from ever touching the private knowledge
    schema at all; calling the side-effecting function here defeated that
    isolation and made a dossier-only job silently create an (empty)
    knowledge_corpora row. A plain read has no such effect."""
    if not evidences:
        from src.web.database.repositories import knowledge as knowledge_repo
        if not knowledge_repo.active_chunk_ids_for_corpus(db, organization_id=organization_id, owner_user_id=owner_user_id):
            return HybridSearchOutcome(evidences=[], mode=MODE_EMPTY_CORPUS)
    return HybridSearchOutcome(evidences=evidences, mode=mode, degraded_reason=degraded_reason)


def search(db: Session, *, organization_id: uuid.UUID, owner_user_id: uuid.UUID, query: str, top_k: int = 6) -> HybridSearchOutcome:
    lexical_evidences = private_rag_manager.search(
        db, organization_id=organization_id, owner_user_id=owner_user_id, query=query, top_k=config.RAG_HYBRID_TOP_K_LEXICAL,
    )

    if not hybrid_mode_active(db):
        return _finalize(lexical_evidences[:top_k], MODE_LEXICAL, db=db, organization_id=organization_id, owner_user_id=owner_user_id)

    try:
        query_vector = EmbeddingAdapter.shared().embed_one(query)
    except EmbeddingUnavailableError as exc:
        return _finalize(
            lexical_evidences[:top_k], MODE_HYBRID_DEGRADED, db=db, organization_id=organization_id, owner_user_id=owner_user_id,
            degraded_reason=f"embedding_unavailable:{exc.reason}",
        )

    vector_hits = _vector_candidates(
        db, organization_id=organization_id, owner_user_id=owner_user_id, query_vector=query_vector,
        limit=config.RAG_HYBRID_TOP_K_VECTOR,
    )

    lexical_rank = {ev.chunk_id: i for i, ev in enumerate(lexical_evidences) if ev.chunk_id}
    vector_rank = {str(hit.chunk_id): i for i, hit in enumerate(vector_hits)}
    fused_order = _rrf_fuse(lexical_rank, vector_rank, k=config.RAG_RRF_K)

    lexical_by_chunk = {ev.chunk_id: ev for ev in lexical_evidences if ev.chunk_id}
    vector_hit_by_chunk = {str(hit.chunk_id): hit for hit in vector_hits}
    evidences: list[RAGEvidence] = []
    for chunk_id_str in fused_order:
        if len(evidences) >= top_k:
            break
        if chunk_id_str in lexical_by_chunk:
            # Already independently found lexically — that passage location
            # is a real, valid excerpt of this chunk; showing it (rather
            # than the vector signal's own window) is a deliberate choice
            # when both signals agree, not the failure mode this lot fixed
            # (a chunk found ONLY by the vector signal, below).
            evidences.append(lexical_by_chunk[chunk_id_str])
            continue
        hit = vector_hit_by_chunk.get(chunk_id_str)
        if hit is None:
            continue
        evidence = private_rag_manager.evidence_for_passage_hit(
            db, organization_id=organization_id, owner_user_id=owner_user_id,
            chunk_id=hit.chunk_id, query=query,
            passage_content=hit.content, passage_start=hit.start_char, passage_end=hit.end_char,
        )
        if evidence is not None:
            evidences.append(evidence)

    from src.web.database.repositories import knowledge as knowledge_repo
    current = current_embedding_config()
    full_coverage = knowledge_repo.corpus_fully_vector_ready(
        db, organization_id=organization_id, owner_user_id=owner_user_id,
        model_id=current.model_id, model_revision=current.model_revision, dimension=current.dimension,
    )
    mode = MODE_HYBRID if full_coverage else MODE_HYBRID_PARTIAL
    return _finalize(evidences, mode, db=db, organization_id=organization_id, owner_user_id=owner_user_id)


def _is_valid_finite_score(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def confirm_evidence_after_rerank(evidences: list[RAGEvidence], *, rerank_status: str) -> list[RAGEvidence]:
    """Closes a real gap the vector signal opens: a vector nearest-neighbor
    has NO relevance threshold (unlike lexical's existing
    `private_rag_manager.LEXICAL_RELEVANCE_FLOOR`), so a corpus-wide
    IRRELEVANT vector candidate can reach `evidence_pack` — and from there
    `reference_evidence`'s score (`n = len(ctx.evidences)`) — with NOTHING
    having actually validated it, whenever the reranker doesn't run for
    real: `rerank_status` is `"not_attempted"` (LLM disabled — the
    documented, supported "fallback sans clé API" mode — or zero
    candidates) or `"fallback"` (a provider exception/invalid response).
    Reproduced live: an entirely off-topic corpus + hybrid mode + no LLM
    key configured let a pure nearest-neighbor become "evidence" with no
    lexical support at all.

    Reuses the EXISTING lexical floor as the ONLY remaining confirmation
    signal in that case — never a new, invented vector threshold, never a
    fabricated score (ticket: "ne colle pas un seuil vectoriel arbitraire
    ni une note fabriquée"). When the reranker DID actually run
    (`"applied"`), its own selection is the real decision and nothing is
    filtered here — a candidate the vector signal alone found, that the
    LLM itself judged relevant, is kept exactly as the "paraphrase sans
    recouvrement lexical" case requires. For a pure-lexical evidence list
    (no hybrid mode involved) this is a guaranteed no-op: `search()` never
    returns a lexical evidence at or under this same floor in the first
    place."""
    if rerank_status == "applied":
        return evidences
    kept = []
    for ev in evidences:
        # A NaN/out-of-range score is never "unconfirmed, drop it silently"
        # — that would MASK a real data-corruption bug instead of letting
        # src.core.rag_evidence_validation.ensure_valid_evidences (called
        # inside ScoringEngine.score(), right after this function runs)
        # catch it and raise InvalidRAGEvidenceError as designed. This
        # filter only ever drops a genuinely VALID score at/under the
        # floor — reproduced live: an invalid (NaN) evidence injected via
        # RAGEvidence.model_construct() was silently disappearing here
        # before this guard, turning a job that must terminally fail into
        # one that quietly succeeded on the remaining evidence instead.
        if _is_valid_finite_score(ev.score) and ev.score <= private_rag_manager.LEXICAL_RELEVANCE_FLOOR:
            continue
        kept.append(ev)
    return kept


def search_evidences(db: Session, *, organization_id: uuid.UUID, owner_user_id: uuid.UUID, query: str, top_k: int = 6) -> list[RAGEvidence]:
    """Thin wrapper for the existing call sites (the real analysis job,
    the scoring-policy simulation preview) that only ever consumed a plain
    `list[RAGEvidence]` from private_rag_manager.search() directly — drops
    HybridSearchOutcome's mode/degraded_reason, which those callers have no
    contract to surface. Any InvalidRAGEvidenceError from the underlying
    lexical search still propagates unchanged (this function adds no new
    try/except of its own)."""
    return search(db, organization_id=organization_id, owner_user_id=owner_user_id, query=query, top_k=top_k).evidences
