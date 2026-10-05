"""B18-T5 (DEFECT F11/F12/E-6) — deterministic, local passage location
within a RAG reference's canonical (full, untruncated) text.

The audited bug: a document was searched as a whole, then its first 3500
characters were returned as the "evidence" — a relevant passage located
further in was silently dropped. The RAG prompt then re-truncated that
already-truncated excerpt to 800 characters, compounding the loss.

This module only DECIDES WHICH WINDOW of the canonical text to keep — it
never changes RAGEvidence.score (the document-level TF-IDF similarity,
computed and validated/deduplicated entirely separately, T1-T4). No new
embeddings, provider, or LLM call: relevance-per-segment reuses the SAME
already-fitted TfidfVectorizer the caller already has for document-level
search, via `.transform()` (no refit).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

MAX_PASSAGE_CHARS = 3500  # matches the existing RAGEvidence.content convention

_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")
_WORD = re.compile(r"\w+", re.UNICODE)


@dataclass(frozen=True)
class PassageMatch:
    content: str
    start_char: int
    end_char: int


def _split_into_segments(text: str) -> list[tuple[int, int]]:
    """Deterministic segmentation into (start, end) character offsets —
    Unicode codepoint positions, matching Python's own string indexing.
    Paragraph boundaries first (blank line, "\\n\\n" — the same separator
    src/web/knowledge/extraction.py already splits on at ingestion), with
    a bounded-window fallback for any single paragraph that still exceeds
    MAX_PASSAGE_CHARS (e.g. a legacy whole-file "paragraph" with no blank
    lines at all, or one unusually long paragraph).

    Positions are derived purely by accumulating `len(part)` while
    iterating `text.split("\\n\\n")` — never by searching for the
    substring again (which could false-match an earlier, identical
    paragraph) — because `"\\n\\n".join(text.split("\\n\\n")) == text`
    always holds, this reconstructs exact offsets with no ambiguity."""
    if not text:
        return [(0, 0)]
    segments: list[tuple[int, int]] = []
    pos = 0
    for part in text.split("\n\n"):
        start, end = pos, pos + len(part)
        if part.strip():
            if end - start > MAX_PASSAGE_CHARS:
                window_start = start
                while window_start < end:
                    window_end = min(window_start + MAX_PASSAGE_CHARS, end)
                    segments.append((window_start, window_end))
                    window_start = window_end
            else:
                segments.append((start, end))
        pos = end + 2  # length of the "\n\n" separator just consumed
    return segments or [(0, len(text))]


def locate_relevant_passage(canonical_text: str, query: str, vectorizer) -> PassageMatch:
    """Returns the single most relevant passage (ticket section 3: "un
    passage principal par document suffit") as an exact, positioned slice
    of `canonical_text` — always `content == canonical_text[start_char:
    end_char]` by construction (a plain Python slice, not a copy that
    could drift). Falls back to the first MAX_PASSAGE_CHARS characters
    (byte-for-byte today's prior behavior) only when there is a single
    segment to choose from — no scoring needed or possible in that case.
    """
    segments = _split_into_segments(canonical_text)
    if len(segments) <= 1:
        start, end = segments[0] if segments else (0, len(canonical_text))
        end = min(end, start + MAX_PASSAGE_CHARS)
        return PassageMatch(content=canonical_text[start:end], start_char=start, end_char=end)

    from sklearn.metrics.pairwise import cosine_similarity

    segment_texts = [canonical_text[s:e] for s, e in segments]
    segment_matrix = vectorizer.transform(segment_texts)
    query_vector = vectorizer.transform([query])
    sims = cosine_similarity(query_vector, segment_matrix).flatten()
    best_index = int(sims.argmax()) if sims.size else 0
    start, end = segments[best_index]
    end = min(end, start + MAX_PASSAGE_CHARS)
    return PassageMatch(content=canonical_text[start:end], start_char=start, end_char=end)


# ---------------------------------------------------------------------------
# B18-T6 (complement to B18-T5, related to B15) — fitting an ALREADY
# LOCATED passage into a SMALLER downstream budget (the reference-
# selection prompt's per-candidate character budget,
# src/rag/context_budget.py), without reintroducing the audited defect
# (a blind `content[:800]` head-cut that could sever the discriminating
# term, or the negation/qualifier right next to it, from the excerpt).
#
# This is a SEPARATE, purely lexical pass from locate_relevant_passage
# above — it does not require a fitted TfidfVectorizer (the prompt-
# assembly call site, src/rag/semantic_rerank.py::semantic_rerank, is shared
# across BOTH RAG producers and only ever sees each evidence's own
# already-located `.content`, never the corpus-specific vectorizer that
# produced it — private_rag_manager.py's per-account vectorizer is a
# different object with a different vocabulary than the (removed) global rag_manager.py's,
# so reusing either one here would silently misscore the other producer's
# text). Token-overlap-against-the-query is a plain, local, deterministic
# heuristic — no new embeddings/provider/LLM call.
# ---------------------------------------------------------------------------

def _split_into_clauses(text: str) -> list[tuple[int, int]]:
    """Sentence-ish segmentation via trailing `.`/`!`/`?` plus whitespace,
    tracked by cumulative offsets exactly like _split_into_segments (never
    by re-searching a substring, which could false-match a repeated
    clause). Good enough to keep a short clause — and any negation or
    qualifier inside it — intact rather than cut through its middle;
    real sentence parsing is out of scope for a local, dependency-free
    heuristic."""
    if not text:
        return [(0, 0)]
    spans: list[tuple[int, int]] = []
    start = 0
    for match in _SENTENCE_BOUNDARY.finditer(text):
        end = match.start()
        if end > start:
            spans.append((start, end))
        start = match.end()
    if start < len(text):
        spans.append((start, len(text)))
    return spans or [(0, len(text))]


def _tokenize(text: str) -> set[str]:
    return {w.lower() for w in _WORD.findall(text)}


def _lexical_overlap_score(text: str, query_tokens: set[str]) -> int:
    if not query_tokens:
        return 0
    return len(_tokenize(text) & query_tokens)


def _best_match_offset(text: str, query_tokens: set[str]) -> int:
    """Character offset of the earliest query token found in `text`
    (case-insensitive substring search), or its midpoint if none match at
    all — used only as the centering point for a last-resort mechanical
    cut, never as a scoring mechanism on its own."""
    lowered = text.lower()
    positions = [pos for tok in query_tokens if tok and (pos := lowered.find(tok)) != -1]
    return min(positions) if positions else len(text) // 2


def fit_window_to_budget(text: str, query: str, max_chars: int) -> tuple[int, int]:
    """Given `text` (typically an evidence's own `.content`, itself
    already the T5-located passage), returns a (start, end) interval
    RELATIVE TO `text` of at most `max_chars` — the caller translates this
    into canonical-text-relative positions when `text` is itself a
    sub-passage (src/rag/context_budget.py::bound_candidate_excerpt).

    Prefers keeping the best-scoring CLAUSE fully intact (ticket section
    2: "éviter les coupures mécaniques de clause lorsque celle-ci tient
    dans le budget"), then greedily grows to neighboring clauses while
    budget remains, so nearby context — a negation or qualifier attached
    to a short clause — survives whenever it fits. Falls back to a
    mechanical cut, centered on the actual matched term rather than the
    clause's midpoint, only when even the single best clause exceeds the
    budget on its own."""
    if max_chars <= 0 or not text:
        return 0, 0
    if len(text) <= max_chars:
        return 0, len(text)

    query_tokens = _tokenize(query)
    clauses = _split_into_clauses(text)
    scored = [(s, e, _lexical_overlap_score(text[s:e], query_tokens)) for s, e in clauses]
    best_start, best_end, _ = max(scored, key=lambda c: c[2])

    if best_end - best_start > max_chars:
        match_offset = best_start + _best_match_offset(text[best_start:best_end], query_tokens)
        start = max(0, match_offset - max_chars // 2)
        end = min(len(text), start + max_chars)
        start = max(0, end - max_chars)
        return start, end

    start, end = best_start, best_end
    before = [c for c in clauses if c[1] <= start]
    after = [c for c in clauses if c[0] >= end]
    i, j = len(before) - 1, 0
    while True:
        grew = False
        if i >= 0 and end - before[i][0] <= max_chars:
            start = before[i][0]
            i -= 1
            grew = True
        if j < len(after) and after[j][1] - start <= max_chars:
            end = after[j][1]
            j += 1
            grew = True
        if not grew:
            break
    return start, end
