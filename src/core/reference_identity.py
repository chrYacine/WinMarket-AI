"""B18-T4 (DEFECT F09/F12/E-5) — exact-copy content identity for RAG
references, used to stop a repeated or renamed-but-identical reference
from inflating "Références similaires"/score_global/decision.

Scope, explicitly: this recognizes EXACT content duplicates only (a
server-computed fingerprint of the full text used for indexing). It does
NOT attempt to detect that two differently-worded documents describe the
same real-world project — no LLM matching, no fuzzy/approximate title
comparison, no cross-account linkage. "En l'absence d'identité projet
fiable, garantir la déduplication des copies exactes et documenter la
limite" (ticket section 2) — this module IS that documented limit: a
genuine project-level grouping is out of scope here, not silently claimed.
"""
from __future__ import annotations

import hashlib
import unicodedata


def compute_content_fingerprint(text: str) -> str:
    """Server-side fingerprint of the FULL text used for indexing, BEFORE
    any truncation applied later for display/prompt purposes (RAGEvidence.
    content is capped at 3500 chars, prompt excerpts at 800 — this
    function must be called on the untruncated source text wherever
    possible, see each caller's own note on this).

    Normalization is deliberately minimal and documented: Unicode NFC
    (so two byte-different-but-visually-identical encodings of the same
    text collide as intended) and line-ending normalization (\\r\\n/\\r ->
    \\n, since the same text saved on different OSes must still count as
    identical). Never case-folding, whitespace-collapsing, or stopword
    removal — those would risk merging two texts that differ in
    meaningful ways (a figure, a negation, a changed clause), which this
    ticket explicitly must not do."""
    normalized = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _identity_fingerprint(ev) -> str:
    """B18-T5 (DEFECT F11/F12/E-6): the grouping key for deduplication is
    the DOCUMENT-level fingerprint (`ev.content_fingerprint`, the T4
    identity of the FULL canonical text, set by the real producers before
    any passage was located) whenever it is available — never the passage
    text alone (ticket section 3: "ne jamais recalculer l'identité de
    déduplication sur le seul extrait"). Two different documents can
    legitimately share an identical located passage without being exact
    copies of each other; grouping by the passage instead of the document
    would wrongly merge them.

    Falls back to hashing `.content` directly only when no
    `content_fingerprint` was set at all — this keeps the function correct
    for evidence built without passage location (synthetic/test
    RAGEvidence objects predating B18-T5, or any future caller that omits
    it), matching this module's original B18-T4 behavior exactly in that
    case."""
    if ev.content_fingerprint is not None:
        return ev.content_fingerprint
    return compute_content_fingerprint(ev.content)


def deduplicate_evidences(evidences: list) -> list:
    """Shared scoring-boundary defense (ticket section 5) — groups
    RAGEvidence objects by their document-level identity (see
    _identity_fingerprint above), keeping ONE representative per group:
    the one with the HIGHEST `.score` (the best already-validated
    similarity — never summed or averaged across duplicates, never a
    repetition bonus). First-seen order is preserved for the
    representatives. Any `duplicate_sources` already set upstream (e.g.
    by a producer's own earlier deduplication) is unioned with newly
    detected duplicates at this layer, never dropped — this is how
    traceability to every original source survives even through two
    dedup passes.

    Call ONLY after every evidence has already passed
    src.core.rag_evidence_validation.ensure_valid_evidences — validating
    first and deduplicating second is what guarantees an invalid
    duplicate always still raises InvalidRAGEvidenceError instead of
    silently disappearing behind a valid twin (ticket section 5).

    Caveat, documented rather than hidden: for evidence with no
    `content_fingerprint` at all, this falls back to `.content`, which may
    already be a located passage or a truncated prefix — two DIFFERENT
    documents sharing an identical prefix/passage up to that length would
    then collide. In practice this backstop only ever sees genuine
    duplicates for real producer output, because the real producers
    (src/rag/private_rag_manager.py; the second, global producer src/rag/rag_manager.py was removed in lot 43) both set
    `content_fingerprint` from the FULL, untruncated text before any
    passage location happens — this function exists as defense-in-depth
    for a list that reaches scoring through any other path."""
    groups: dict[str, list] = {}
    order: list[str] = []
    for ev in evidences:
        fingerprint = _identity_fingerprint(ev)
        if fingerprint not in groups:
            groups[fingerprint] = []
            order.append(fingerprint)
        groups[fingerprint].append(ev)

    deduped = []
    for fingerprint in order:
        group = groups[fingerprint]
        representative = max(group, key=lambda e: e.score)
        extra_sources = sorted({
            source
            for e in group
            for source in ([e.source] + list(e.duplicate_sources))
            if source != representative.source
        })
        if extra_sources:
            representative = representative.model_copy(update={"duplicate_sources": extra_sources})
        deduped.append(representative)
    return deduped


def _reference_grouping_key(ev) -> str:
    """The identity a REFERENCE counts by, for aggregation purposes only
    (see group_evidences_by_reference below) — `document_version_id` when
    available (the real identity of "which document"), falling back to
    `.source` (the filename) only for evidence built without it (a
    synthetic/legacy RAGEvidence). Never `None`/a constant: that would
    collapse every reference into one group."""
    if ev.document_version_id is not None:
        return f"version:{ev.document_version_id}"
    return f"source:{ev.source}"


def group_evidences_by_reference(evidences: list) -> list[list]:
    """Recette corpus utilisateur (2026-09-24), §4 — groups evidences by
    REFERENCE identity (document/version), not by exact content like
    `deduplicate_evidences` above. Two different chunks of the SAME
    document/version (different content, different
    `content_fingerprint` — the ordinary result of a long section being
    split into several token-based windows, or simply two different
    paragraphs both judged relevant) are the SAME reference, not two.

    Returns the groups themselves (never collapses to one representative
    per group — every citation stays available for display/reranking);
    callers that need a per-reference SCORE (src/agents/criteria_
    evaluators.py::_reference_evidence) take the max score within each
    group, exactly like `deduplicate_evidences`'s own "best representative,
    never summed/averaged across duplicates" precedent — never a new
    aggregation rule invented, the same B18 principle applied one level
    higher (per reference, not per exact-content chunk).

    Order-preserving (first-seen group order), so any caller iterating the
    result sees references in the same relative order they were retrieved.
    """
    groups: dict[str, list] = {}
    order: list[str] = []
    for ev in evidences:
        key = _reference_grouping_key(ev)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(ev)
    return [groups[key] for key in order]
