"""B18-T4 (DEFECT F09/F12/E-5) — test group B: a REAL temporary corpus
(actual DB-backed chunks, real TfidfVectorizer.fit_transform, real top_k
selection via src/rag/private_rag_manager.py — no cosine_similarity
mocking here, unlike tests/test_rag_producer_validation.py). Uploads use
single-paragraph content (no blank line) so each document becomes exactly
one chunk, keeping chunk-count reasoning in these tests exact.
"""
from __future__ import annotations

import io
import re

from tests.conftest import default_org_id, make_active_starter_user

SIMILARITY_TOLERANCE = 1e-6


def _csrf_from(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


def _login(client, email, password="Sup3rSecret!"):
    r = client.get("/login")
    csrf = _csrf_from(r.text)
    client.post("/login", data={"email": email, "password": password, "next": "/app", "csrf_token": csrf})
    return csrf


def _upload(client, csrf, filename: str, content: str):
    r = client.post(
        "/api/knowledge/documents",
        files={"file": (filename, io.BytesIO(content.encode("utf-8")), "text/plain")},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 201, r.text


REFERENCE_TEXT = "Reference projet portail client stack Python Django secteur retail livre en 2024."
OTHER_TEXT = "Reference projet migration cloud AWS pour secteur banque livree en 2023."


def test_renamed_exact_copies_do_not_evict_or_duplicate_in_real_search(client, db):
    from src.rag import private_rag_manager

    user = make_active_starter_user(db, "realcorpus@example.com", scoring=False)
    org_id = default_org_id(db, user)
    csrf = _login(client, "realcorpus@example.com")

    _upload(client, csrf, "original.md", REFERENCE_TEXT)
    _upload(client, csrf, "different.md", OTHER_TEXT)

    query = "reference projet secteur portail client python retail migration cloud aws banque"
    first_search = private_rag_manager.search(db, organization_id=org_id, owner_user_id=user.id, query=query, top_k=6)
    assert {ev.source for ev in first_search} == {"original.md", "different.md"}
    scores_before = {ev.source: ev.score for ev in first_search}

    # Add several RENAMED exact copies of the same reference.
    for name in ("copy_1.md", "copy_2.md", "copy_3.md"):
        _upload(client, csrf, name, REFERENCE_TEXT)

    # Force a rebuild: the corpus generation bumps on every upload, so the
    # next search re-triggers _build_snapshot (existing cache invalidation,
    # reused unchanged — see src/rag/private_rag_manager.py).
    second_search = private_rag_manager.search(db, organization_id=org_id, owner_user_id=user.id, query=query, top_k=6)

    sources = [ev.source for ev in second_search]
    # Exactly one representative for the 4 exact copies (original + 3
    # renamed) — never 4 separate "relevant" hits crowding out the
    # genuinely different reference, never more than 2 unique references.
    assert len(second_search) == 2, f"expected 2 unique references, got {sources}"
    representative_sources = {ev.source for ev in second_search}
    assert "different.md" in representative_sources
    # The representative for the duplicated content is ONE of the 4 exact
    # copies (whichever the deterministic "first occurrence" rule picked)
    # — never more than one of them appearing, and its similarity is
    # stable within an explicit numeric tolerance across the rebuild.
    duplicate_group_sources = {"original.md", "copy_1.md", "copy_2.md", "copy_3.md"}
    representative = next(ev for ev in second_search if ev.source in duplicate_group_sources)
    assert representative.source in duplicate_group_sources
    assert set(representative.duplicate_sources) == duplicate_group_sources - {representative.source}
    assert abs(representative.score - scores_before["original.md"]) < SIMILARITY_TOLERANCE

    other = next(ev for ev in second_search if ev.source == "different.md")
    assert abs(other.score - scores_before["different.md"]) < SIMILARITY_TOLERANCE


def test_same_prefix_different_ending_documents_are_not_merged_in_real_corpus(client, db):
    from src.rag import private_rag_manager

    user = make_active_starter_user(db, "realcorpusprefix@example.com", scoring=False)
    org_id = default_org_id(db, user)
    csrf = _login(client, "realcorpusprefix@example.com")

    shared_prefix = "Reference projet portail client stack Python Django secteur retail livre en "
    _upload(client, csrf, "a.md", shared_prefix + "2024 pour Acme Corp.")
    _upload(client, csrf, "b.md", shared_prefix + "2025 pour une autre societe totalement differente avec un budget distinct.")

    results = private_rag_manager.search(db, organization_id=org_id, owner_user_id=user.id, query="portail client python retail", top_k=6)
    assert {ev.source for ev in results} == {"a.md", "b.md"}, "same-prefix, different-ending documents must remain distinct"
    for ev in results:
        assert ev.duplicate_sources == []
