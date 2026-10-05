"""Per-(organization, owner) RAG search — the only corpus search of the
SaaS (the former global LocalRAGManager singleton and its demo corpus were
removed in lot 43; see docs/architecture/B03_PRIVATE_KNOWLEDGE.md).

The database is the source of truth for "what changed": each snapshot is
cached under (organization_id, owner_user_id) together with the
KnowledgeCorpus.generation it was built from. A cache hit is only used if
its generation still matches the corpus's *current* generation in the DB —
read fresh on every call, so a second process (or a write in this same
process from a moment ago) is detected without any cross-process
invalidation mechanism. A failed rebuild leaves the previous, still-valid
cache entry in place (an old index is never worse than no index, as long as
it doesn't contain a document that has since become forbidden/deleted —
which is exactly what the generation check catches on the NEXT read).

The cache is bounded (config.KNOWLEDGE_INDEX_CACHE_MAX_ENTRIES) with
oldest-first eviction — a private per-user index is a fraction of the size
of the global corpus this replaces, but a large customer base must not
turn this into an unbounded memory leak.
"""
from __future__ import annotations

import threading
import uuid
from collections import OrderedDict
from dataclasses import dataclass

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from sqlalchemy.orm import Session

from src.core import config
from src.core.models import RAGEvidence
from src.core.rag_evidence_validation import sanitize_producer_similarity
from src.core.reference_identity import compute_content_fingerprint
from src.rag.passage_location import locate_relevant_passage
from src.rag.semantic_rerank import FRENCH_STOP_WORDS
from src.web.database.repositories import knowledge as knowledge_repo

# The one floor a lexical candidate must clear to become a search RESULT at
# all (unchanged since before lot 51). Lot 51 bis reuses this SAME constant
# — never a new, invented number — as the sole remaining confirmation
# signal for a vector-only candidate when no reranker actually validated it
# (src/rag/hybrid_search.py::confirm_evidence_after_rerank): a real semantic
# match legitimately scores at/under this on the lexical axis, so this is
# never used to reject a search RESULT — only to decide whether an
# unconfirmed candidate may become scoring EVIDENCE.
LEXICAL_RELEVANCE_FLOOR = 0.01


@dataclass
class _Snapshot:
    generation: int
    chunk_ids: list[uuid.UUID]
    texts: list[str]
    sources: list[str]
    vectorizer: TfidfVectorizer | None
    matrix: object | None
    # B18-T4: duplicate_sources[i] lists any OTHER chunk's source filename
    # whose content was an exact match of texts[i]/sources[i] — that
    # duplicate chunk was excluded from `texts` entirely, before
    # fit_transform, so it can neither skew IDF weights nor occupy a top_k
    # slot a genuinely different reference could have used.
    duplicate_sources: list[list[str]]
    # B18-T5: document_version_ids[i] is the KnowledgeDocumentVersion this
    # representative chunk belongs to — real provenance, straight from the
    # already-loaded ORM row, never fabricated.
    document_version_ids: list[uuid.UUID]


_cache: "OrderedDict[tuple[uuid.UUID, uuid.UUID], _Snapshot]" = OrderedDict()
_lock = threading.Lock()


def _build_snapshot(db: Session, *, organization_id: uuid.UUID, owner_user_id: uuid.UUID, generation: int) -> _Snapshot:
    rows = knowledge_repo.active_chunks_for_corpus(db, organization_id=organization_id, owner_user_id=owner_user_id)

    # B18-T4 (DEFECT F09/F12/E-5): eliminate exact-content-duplicate chunks
    # BEFORE fit/fit_transform — a repeated or renamed-but-identical
    # reference must not skew TF-IDF weights or take a candidate slot away
    # from a genuinely different one. Identity is a fingerprint of the
    # FULL chunk content (before any later truncation for display/prompt
    # purposes), never the filename/title — two chunks with the same
    # opening text but a different ending fingerprint differently and are
    # kept as separate references. A stable representative (first
    # occurrence, in this query's own deterministic order) is chosen per
    # unique fingerprint; every other chunk sharing that fingerprint is
    # recorded in `duplicate_sources` for traceability, never deleted and
    # never having its ownership changed.
    chunk_ids: list[uuid.UUID] = []
    texts: list[str] = []
    sources: list[str] = []
    duplicate_sources: list[list[str]] = []
    document_version_ids: list[uuid.UUID] = []
    seen_at: dict[str, int] = {}
    for chunk, filename in rows:
        fingerprint = compute_content_fingerprint(chunk.content)
        if fingerprint in seen_at:
            idx = seen_at[fingerprint]
            if filename != sources[idx] and filename not in duplicate_sources[idx]:
                duplicate_sources[idx].append(filename)
            continue
        seen_at[fingerprint] = len(texts)
        chunk_ids.append(chunk.id)
        texts.append(chunk.content)
        sources.append(filename)
        duplicate_sources.append([])
        document_version_ids.append(chunk.document_version_id)

    if not texts:
        return _Snapshot(
            generation=generation, chunk_ids=[], texts=[], sources=[], vectorizer=None, matrix=None,
            duplicate_sources=[], document_version_ids=[],
        )
    vectorizer = TfidfVectorizer(stop_words=FRENCH_STOP_WORDS, ngram_range=(1, 2), max_features=8000, sublinear_tf=True)
    matrix = vectorizer.fit_transform(texts)
    return _Snapshot(
        generation=generation, chunk_ids=chunk_ids, texts=texts, sources=sources,
        vectorizer=vectorizer, matrix=matrix, duplicate_sources=duplicate_sources,
        document_version_ids=document_version_ids,
    )


def _get_snapshot(db: Session, *, organization_id: uuid.UUID, owner_user_id: uuid.UUID) -> _Snapshot:
    key = (organization_id, owner_user_id)
    corpus = knowledge_repo.get_or_create_corpus(db, organization_id=organization_id, owner_user_id=owner_user_id)
    current_generation = corpus.generation

    with _lock:
        cached = _cache.get(key)
        if cached is not None and cached.generation == current_generation:
            _cache.move_to_end(key)
            return cached

    # Build outside the lock (TF-IDF fit can be slow) — a rebuild racing
    # another rebuild just does redundant work, never corrupts state, since
    # each thread computes its own independent _Snapshot object.
    snapshot = _build_snapshot(db, organization_id=organization_id, owner_user_id=owner_user_id, generation=current_generation)

    with _lock:
        _cache[key] = snapshot
        _cache.move_to_end(key)
        while len(_cache) > config.KNOWLEDGE_INDEX_CACHE_MAX_ENTRIES:
            _cache.popitem(last=False)
    return snapshot


def invalidate(*, organization_id: uuid.UUID, owner_user_id: uuid.UUID) -> None:
    """Optional local fast-path (this process only) — correctness never
    depends on this being called: the generation check in _get_snapshot
    catches a stale cache regardless, including in a process that never
    called this."""
    with _lock:
        _cache.pop((organization_id, owner_user_id), None)


def corpus_is_empty(db: Session, *, organization_id: uuid.UUID, owner_user_id: uuid.UUID) -> bool:
    snapshot = _get_snapshot(db, organization_id=organization_id, owner_user_id=owner_user_id)
    return not snapshot.texts


def search(
    db: Session, *, organization_id: uuid.UUID, owner_user_id: uuid.UUID, query: str, top_k: int = 6
) -> list[RAGEvidence]:
    snapshot = _get_snapshot(db, organization_id=organization_id, owner_user_id=owner_user_id)
    if not snapshot.texts or snapshot.vectorizer is None:
        return []

    q = snapshot.vectorizer.transform([query])
    sims_raw = cosine_similarity(q, snapshot.matrix).flatten()
    # B18-T2 (DEFECT-B04-04, closing a B18-T1 gap): sanitize/validate EVERY
    # computed similarity BEFORE any threshold filtering or sorting — doing
    # this after filtering (as the first B18-T1 delivery did) let a
    # negative or NaN value simply fail the `> 0.01` comparison and vanish
    # from the candidate list unnoticed, never even reaching an error.
    # sanitize_producer_similarity accepts [0, 1] unchanged, clamps only a
    # documented ~1e-12 floating-point overshoot, and raises
    # InvalidRAGEvidenceError for anything else (2.0, -5.0, NaN, ...) —
    # never a blanket min/max that would also mask a real anomaly.
    sims = np.array([sanitize_producer_similarity(v) for v in sims_raw])
    order = sims.argsort()[::-1][:top_k]
    candidate_ids = [snapshot.chunk_ids[i] for i in order if sims[i] > LEXICAL_RELEVANCE_FLOOR]

    # Belt-and-suspenders re-check, right before results leave this
    # function: even if the cache is momentarily stale (a write landed
    # between _get_snapshot's generation read and now, in another thread),
    # a chunk that is no longer authorized is filtered out here — nothing
    # unauthorized becomes a candidate or a citation (ticket B03 section 7).
    still_authorized = knowledge_repo.active_chunk_ids_for_corpus(db, organization_id=organization_id, owner_user_id=owner_user_id)

    evidences = []
    for i in order:
        if sims[i] <= LEXICAL_RELEVANCE_FLOOR or snapshot.chunk_ids[i] not in still_authorized:
            continue
        # B18-T5 (DEFECT F11/F12/E-6): locate the relevant passage WITHIN
        # the full, untruncated canonical text (snapshot.texts[i]) instead
        # of blindly keeping its first 3500 characters — a term matched by
        # this very search could otherwise sit past that cutoff and never
        # reach the evidence at all. `.score` is untouched: it is (and
        # stays) the document-level similarity computed above, never
        # replaced by this passage-level relevance.
        passage = locate_relevant_passage(snapshot.texts[i], query, snapshot.vectorizer)
        evidences.append(RAGEvidence(
            query=query, source=snapshot.sources[i], score=float(sims[i]),
            content=passage.content, start_char=passage.start_char, end_char=passage.end_char,
            content_fingerprint=compute_content_fingerprint(snapshot.texts[i]),
            document_version_id=str(snapshot.document_version_ids[i]),
            duplicate_sources=list(snapshot.duplicate_sources[i]),
            chunk_id=str(snapshot.chunk_ids[i]),
        ))
    return evidences


def evidence_for_passage_hit(
    db: Session, *, organization_id: uuid.UUID, owner_user_id: uuid.UUID, chunk_id: uuid.UUID, query: str,
    passage_content: str, passage_start: int, passage_end: int,
) -> RAGEvidence | None:
    """Lot 51 bis (replaces the lot 51 `evidence_for_chunk`, which located
    its displayed excerpt via `locate_relevant_passage` — a TF-IDF-based
    picker — even for a chunk the VECTOR signal alone found. For a genuine
    paraphrase with near-zero lexical overlap, that picker has nothing
    reliable to rank sub-passages by and could show an excerpt UNRELATED
    to the window that actually produced the embedding match — a real
    defect: the "proof" shown to the reranker/result could misrepresent
    why the reference was retrieved at all.

    `passage_content`/`passage_start`/`passage_end` are the ACTUAL winning
    KnowledgePassage's own stored fields (src/rag/hybrid_search.py's
    `_vector_candidates`, straight from `knowledge_passages` — never
    re-derived here) — `end > start`, `passage_content ==
    chunk.content[passage_start:passage_end]` by construction, since that
    invariant was already enforced at indexing time
    (src/rag/hybrid_index.py). This function's ONLY remaining job is the
    lexical `.score` (same fitted vectorizer, no refit — same definition
    `search()` uses) plus the same authorization/dedup snapshot lookup
    `search()` already does, WITHOUT touching passage location: a real
    semantic match can legitimately have a near-zero lexical score (ticket:
    "documenter cette limite, notamment une preuve sémantique à faible
    score lexical"). Returns None if the chunk no longer exists/is no
    longer authorized (deleted/superseded between the vector query and
    this call, or excluded as an exact-content duplicate of another chunk
    already kept — see _build_snapshot's dedup) — never fabricates a
    stand-in.
    """
    snapshot = _get_snapshot(db, organization_id=organization_id, owner_user_id=owner_user_id)
    if not snapshot.texts or snapshot.vectorizer is None:
        return None
    try:
        i = snapshot.chunk_ids.index(chunk_id)
    except ValueError:
        return None

    still_authorized = knowledge_repo.active_chunk_ids_for_corpus(db, organization_id=organization_id, owner_user_id=owner_user_id)
    if chunk_id not in still_authorized:
        return None

    q = snapshot.vectorizer.transform([query])
    sim_raw = cosine_similarity(q, snapshot.matrix[i]).flatten()[0]
    sim = sanitize_producer_similarity(sim_raw)
    return RAGEvidence(
        query=query, source=snapshot.sources[i], score=float(sim),
        content=passage_content, start_char=passage_start, end_char=passage_end,
        content_fingerprint=compute_content_fingerprint(snapshot.texts[i]),
        document_version_id=str(snapshot.document_version_ids[i]),
        duplicate_sources=list(snapshot.duplicate_sources[i]),
        chunk_id=str(snapshot.chunk_ids[i]),
    )
