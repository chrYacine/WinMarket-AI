"""Lot 51 bis — pure, deterministic tests for
src/rag/chunking.py::window_passages_by_tokens. No DB, no embeddings, no
network — token offsets are supplied directly (as a real, loaded tokenizer
would produce them), keeping this module's own tests fast and independent
of fastembed. The real-tokenizer integration itself is covered by
tests/test_lot51_hybrid_rag.py (real fastembed, real long content).
"""
from __future__ import annotations

import pytest

from src.rag.chunking import window_passages_by_tokens


def _offsets_for(text: str, tokens_per_char: int = 1) -> list[tuple[int, int]]:
    """A tiny deterministic stand-in tokenizer: one token per character —
    keeps these tests simple and independent of any real vocabulary while
    still exercising exact offset arithmetic."""
    return [(i, i + 1) for i in range(len(text))]


def test_a_section_shorter_than_the_budget_becomes_one_single_passage():
    text = "Une courte section de reference."
    offsets = _offsets_for(text)
    spans = window_passages_by_tokens(offsets, max_tokens=126, overlap_tokens=32)
    assert len(spans) == 1
    assert spans[0].start_char == 0 and spans[0].end_char == len(text)
    assert spans[0].content_of(text) == text


def test_content_of_always_equals_the_exact_slice_between_real_token_offsets():
    text = "A" * 50 + "B" * 50 + "C" * 50 + "D" * 50
    offsets = _offsets_for(text)
    for span in window_passages_by_tokens(offsets, max_tokens=60, overlap_tokens=10):
        assert span.content_of(text) == text[span.start_char:span.end_char]


def test_windows_cover_every_token_with_no_gap_and_the_configured_overlap():
    text = "x" * 500
    offsets = _offsets_for(text)
    max_tokens, overlap = 120, 30
    spans = window_passages_by_tokens(offsets, max_tokens=max_tokens, overlap_tokens=overlap)
    assert spans[0].start_char == 0
    step = max_tokens - overlap
    for a, b in zip(spans, spans[1:]):
        assert b.start_char == a.start_char + step
        assert b.start_char < a.end_char  # genuine overlap, never a gap
    assert spans[-1].end_char == len(text)  # full coverage — the tail is never dropped


def test_no_tokens_yields_no_passages():
    assert window_passages_by_tokens([], max_tokens=126, overlap_tokens=32) == []


def test_invalid_max_tokens_or_overlap_is_rejected():
    with pytest.raises(ValueError):
        window_passages_by_tokens([(0, 1)], max_tokens=0, overlap_tokens=0)
    with pytest.raises(ValueError):
        window_passages_by_tokens([(0, 1)], max_tokens=10, overlap_tokens=10)  # overlap >= max
    with pytest.raises(ValueError):
        window_passages_by_tokens([(0, 1)], max_tokens=10, overlap_tokens=-1)


def test_a_section_exactly_at_the_budget_boundary_still_needs_only_one_window():
    text = "y" * 126
    offsets = _offsets_for(text)
    spans = window_passages_by_tokens(offsets, max_tokens=126, overlap_tokens=32)
    assert len(spans) == 1
    assert spans[0].end_char == 126


def test_one_token_over_the_budget_forces_a_second_window():
    text = "z" * 127
    offsets = _offsets_for(text)
    spans = window_passages_by_tokens(offsets, max_tokens=126, overlap_tokens=32)
    assert len(spans) == 2
    assert spans[-1].end_char == 127  # the extra token is never silently dropped
