"""B18-T2 — closes two gaps found in the first B18-T1 delivery:

1. The producer-side floating-point clamp (`min(1.0, max(0.0, x))`) used
   to correct ANY out-of-range value indiscriminately (2.0 -> 1.0,
   -5.0 -> 0.0), and ran AFTER the `> 0.01` threshold filter — meaning a
   negative or NaN similarity would just fail that comparison and vanish
   from the candidate list silently, never even reaching an error.
2. A historical result with a corrupted evidence/score needed a visible
   API-level signal, not just a server log line.

This file exercises test group A: the real producer search path
(src/rag/private_rag_manager.py::search — the global LocalRAGManager.search
was the second one until lot 43), parametrized with simulated cosine_similarity
outputs — not just the shared validation helper in isolation. No real
TF-IDF corpus content matters here (`cosine_similarity` itself is
monkeypatched to return the exact test value), only that at least one
real chunk/document exists so the normal "empty corpus" early-return
doesn't mask the code path under test.
"""
from __future__ import annotations

import io
import math
import re

import numpy as np
import pytest

from src.core.rag_evidence_validation import InvalidRAGEvidenceError, SIMILARITY_ROUNDING_EPSILON
from tests.conftest import default_org_id, make_active_starter_user


def _csrf_from(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


def _login(client, email, password="Sup3rSecret!"):
    r = client.get("/login")
    csrf = _csrf_from(r.text)
    client.post("/login", data={"email": email, "password": password, "next": "/app", "csrf_token": csrf})
    return csrf


# Both producers additionally filter candidates by an existing, unrelated
# relevance threshold (`sims[i] > 0.01`) — 0.0 and anything clamped to it
# are legitimately excluded by THAT filter (not an error, just "not
# relevant enough"), so those two cases are asserted separately below
# rather than mixed into the "becomes exactly one evidence" cases.
VALID_CASES = [
    pytest.param(0.5, 0.5, id="intermediate"),
    pytest.param(1.0, 1.0, id="one"),
    pytest.param(1.0 + 1e-13, 1.0, id="tiny_overshoot_above_within_epsilon"),
]

VALID_BUT_BELOW_RELEVANCE_THRESHOLD_CASES = [
    pytest.param(0.0, id="zero"),
    pytest.param(0.0 - 1e-13, id="tiny_overshoot_below_within_epsilon"),
]

REJECTED_CASES = [
    pytest.param(-5.0, id="clearly_negative"),
    pytest.param(2.0, id="clearly_above_one"),
    pytest.param(1.0 + 10 * SIMILARITY_ROUNDING_EPSILON, id="overshoot_larger_than_epsilon"),
    pytest.param(math.nan, id="nan"),
    pytest.param(math.inf, id="positive_infinity"),
    pytest.param(-math.inf, id="negative_infinity"),
]


# ---------------------------------------------------------------------------
# A — src/rag/private_rag_manager.py::search (the real SaaS path)
# ---------------------------------------------------------------------------

def _upload_one_document(client, email: str) -> None:
    csrf = _login(client, email)
    r = client.post(
        "/api/knowledge/documents",
        files={"file": ("ref.md", io.BytesIO(b"# Reference\n\nContenu de reference pour la recherche."), "text/plain")},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 201


@pytest.mark.parametrize("raw_value,expected_score", VALID_CASES)
def test_private_rag_manager_search_accepts_valid_similarities(client, db, raw_value, expected_score, monkeypatch):
    import src.rag.private_rag_manager as private_rag_manager

    user = make_active_starter_user(db, "producervalid@example.com", scoring=False)
    org_id = default_org_id(db, user)
    _upload_one_document(client, "producervalid@example.com")

    monkeypatch.setattr(private_rag_manager, "cosine_similarity", lambda q, m: np.array([[raw_value]]))
    evidences = private_rag_manager.search(db, organization_id=org_id, owner_user_id=user.id, query="reference", top_k=6)
    assert len(evidences) == 1
    assert evidences[0].score == pytest.approx(expected_score, abs=1e-9)


@pytest.mark.parametrize("raw_value", VALID_BUT_BELOW_RELEVANCE_THRESHOLD_CASES)
def test_private_rag_manager_search_accepts_but_excludes_low_relevance_similarities(client, db, raw_value, monkeypatch):
    """A validly-in-range similarity of ~0.0 is accepted by the numeric
    validation but still legitimately excluded by the PRE-EXISTING, unrelated
    relevance threshold (`> 0.01`) — an empty result here is correct
    behavior, not a masked error (contrast with the REJECTED_CASES below,
    which must raise instead of quietly returning [])."""
    import src.rag.private_rag_manager as private_rag_manager

    user = make_active_starter_user(db, "producerlowrel@example.com", scoring=False)
    org_id = default_org_id(db, user)
    _upload_one_document(client, "producerlowrel@example.com")

    monkeypatch.setattr(private_rag_manager, "cosine_similarity", lambda q, m: np.array([[raw_value]]))
    evidences = private_rag_manager.search(db, organization_id=org_id, owner_user_id=user.id, query="reference", top_k=6)
    assert evidences == []


@pytest.mark.parametrize("raw_value", REJECTED_CASES)
def test_private_rag_manager_search_rejects_invalid_similarities_before_threshold_filtering(client, db, raw_value, monkeypatch):
    """The critical regression check: -5.0/NaN/etc. would previously fail
    the `> 0.01` threshold check and simply be excluded from the results —
    `search()` returning `[]` silently, no error at all. It must now raise
    BEFORE that filtering ever runs."""
    import src.rag.private_rag_manager as private_rag_manager

    user = make_active_starter_user(db, "producerinvalid@example.com", scoring=False)
    org_id = default_org_id(db, user)
    _upload_one_document(client, "producerinvalid@example.com")

    monkeypatch.setattr(private_rag_manager, "cosine_similarity", lambda q, m: np.array([[raw_value]]))
    with pytest.raises(InvalidRAGEvidenceError):
        private_rag_manager.search(db, organization_id=org_id, owner_user_id=user.id, query="reference", top_k=6)


def test_private_rag_manager_search_invalid_similarity_among_valid_ones_still_raises(client, db, monkeypatch):
    """Mirrors the mixed valid/invalid case from the engine level, but at
    the producer: two chunks, one with a valid similarity, one with NaN —
    the whole search() call must raise, not return the one valid result."""
    import src.rag.private_rag_manager as private_rag_manager

    user = make_active_starter_user(db, "producermixed@example.com", scoring=False)
    org_id = default_org_id(db, user)
    csrf = _login(client, "producermixed@example.com")
    for name, content in [("a.md", b"# A\n\nContenu A."), ("b.md", b"# B\n\nContenu B.")]:
        r = client.post(
            "/api/knowledge/documents",
            files={"file": (name, io.BytesIO(content), "text/plain")},
            headers={"X-CSRF-Token": csrf},
        )
        assert r.status_code == 201

    monkeypatch.setattr(private_rag_manager, "cosine_similarity", lambda q, m: np.array([[0.5, math.nan]]))
    with pytest.raises(InvalidRAGEvidenceError):
        private_rag_manager.search(db, organization_id=org_id, owner_user_id=user.id, query="contenu", top_k=6)


# Lot 43: the "A — LocalRAGManager.search (legacy/Streamlit path)" section
# (three tests) is gone with the removed global demo producer; the private
# producer above carries the same three groups (valid, below relevance
# threshold, invalid rejected before threshold filtering).
