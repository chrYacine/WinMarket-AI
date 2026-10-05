"""Lot 51 bis — passage windowing on top of the existing paragraph/table-level
KnowledgeChunk ("section" in the ticket's vocabulary — src/web/knowledge/
extraction.py already splits a document into these; unchanged by this
lot). A passage is a WINDOW of one section's text, sized in REAL tokens of
the embedding model actually loaded, with a configurable token overlap.

Lot 51's original version windowed by CHARACTER COUNT
(EMBEDDING_CHUNK_WINDOW_CHARS). Verified this lot (see src/rag/embeddings.py
module docstring for the full reproduction): the model's real effective
limit is 128 tokens INCLUDING 2 special tokens, i.e. 126 content tokens —
for French prose this is roughly 500-700 characters depending on
vocabulary, well under the old 1200-character default. Any window that
character-based scheme built past that point was silently truncated by
fastembed's own tokenizer before ever reaching the model — a large tail of
every long window contributed NOTHING to its vector, with no error, no
warning, and no trace. This module is now purely a function of REAL token
offsets (src/rag/embeddings.py::EmbeddingAdapter.content_token_offsets),
never a character count — "no blind limit increase, no hidden truncation"
per the ticket.

Pure and deterministic GIVEN a token-offsets list: the same offsets and
window/overlap sizes always produce the same (start_char, end_char) spans.
Token offsets themselves come from the real, loaded tokenizer (an I/O-ish
dependency deliberately kept OUT of this module — see
src/rag/hybrid_index.py for the caller that provides them).

`content == section_text[start:end]` holds by construction for every
window this returns — every window's bounds are exactly a token's own
start/end offset in the source text, never an arbitrary character cut.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PassageSpan:
    start_char: int
    end_char: int

    def content_of(self, section_text: str) -> str:
        return section_text[self.start_char:self.end_char]


def window_passages_by_tokens(
    token_offsets: list[tuple[int, int]], *, max_tokens: int, overlap_tokens: int
) -> list[PassageSpan]:
    """`token_offsets`: every CONTENT token's (start_char, end_char) span in
    the section text, in order, as produced by a tokenizer with truncation
    disabled (so this function is never handed an already-clipped list).
    `max_tokens`: the model's real per-window content-token budget (already
    excludes special tokens — see
    EmbeddingAdapter.max_content_tokens_per_window). A section with fewer
    tokens than `max_tokens` becomes a single window covering all of them
    (or none, if the section is empty)."""
    if max_tokens <= 0:
        raise ValueError("max_tokens must be positive")
    if overlap_tokens < 0 or overlap_tokens >= max_tokens:
        raise ValueError("overlap_tokens must be >= 0 and < max_tokens")

    n = len(token_offsets)
    if n == 0:
        return []
    if n <= max_tokens:
        return [PassageSpan(token_offsets[0][0], token_offsets[-1][1])]

    step = max_tokens - overlap_tokens
    spans: list[PassageSpan] = []
    start_tok = 0
    while start_tok < n:
        end_tok = min(start_tok + max_tokens, n)
        spans.append(PassageSpan(token_offsets[start_tok][0], token_offsets[end_tok - 1][1]))
        if end_tok == n:
            break
        start_tok += step
    return spans
