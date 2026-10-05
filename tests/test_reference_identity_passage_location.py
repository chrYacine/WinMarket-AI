"""B18-T5 (DEFECT F11/F12/E-6) — test group B: identity and scoring must
stay stable as passage localization is introduced. Specifically (ticket
section 3): document identity is NEVER recalculated on the located
excerpt alone — two documents that happen to share an identical passage
are NOT merged, and multiple passages of one parent never multiply into
several references. Also verifies (ticket section 1) that changing which
passage gets localized never changes a document's own similarity score
or the resulting business calculation (decision/score_global) — that
depends only on `.score`/selection, never on `.content`.

This is also the direct regression test for the bug found and fixed
mid-ticket in src/core/reference_identity.py: `deduplicate_evidences`
used to hash `.content`, which is now a passage rather than the full
canonical text — grouping by the passage instead of the document's own
`content_fingerprint` would wrongly merge two different documents that
happen to share the same located text.
"""
from __future__ import annotations

import io
import re

from src.core.models import RAGEvidence
from src.core.reference_identity import compute_content_fingerprint, deduplicate_evidences
from tests.conftest import default_org_id, make_active_starter_user
from tests.synthetic_scoring import score_with_synthetic_policy

SHARED_PASSAGE = "Ce passage est identique mot pour mot dans les deux documents suivants."


# ---------------------------------------------------------------------------
# Unit level: deduplicate_evidences must key on content_fingerprint, not
# on the (now passage-level) .content field.
# ---------------------------------------------------------------------------

def test_shared_passage_but_different_documents_are_not_merged():
    doc_a_fingerprint = compute_content_fingerprint("Document A, texte complet totalement different. " + SHARED_PASSAGE)
    doc_b_fingerprint = compute_content_fingerprint("Document B, texte complet lui aussi different. " + SHARED_PASSAGE)
    assert doc_a_fingerprint != doc_b_fingerprint

    ev_a = RAGEvidence(
        query="q", source="a.md", score=0.7, content=SHARED_PASSAGE, content_fingerprint=doc_a_fingerprint,
    )
    ev_b = RAGEvidence(
        query="q", source="b.md", score=0.6, content=SHARED_PASSAGE, content_fingerprint=doc_b_fingerprint,
    )

    deduped = deduplicate_evidences([ev_a, ev_b])

    assert len(deduped) == 2, "identical PASSAGES from two different documents must not collapse into one reference"
    assert {ev.source for ev in deduped} == {"a.md", "b.md"}
    for ev in deduped:
        assert ev.duplicate_sources == []


def test_renamed_full_document_copies_with_content_fingerprint_still_dedup():
    """The T4 guarantee, now exercised with the T5 fields present: even
    though `.content` here is a passage (not the full text), two evidences
    sharing the same `content_fingerprint` (the FULL canonical text's
    identity) must still be recognized as exact copies."""
    full_text_fingerprint = compute_content_fingerprint("Un document complet original, jamais tronque pour le hash. " + SHARED_PASSAGE)
    ev_original = RAGEvidence(
        query="q", source="original.md", score=0.6, content=SHARED_PASSAGE, content_fingerprint=full_text_fingerprint,
    )
    ev_renamed_copy = RAGEvidence(
        query="q", source="renamed_copy.md", score=0.9, content=SHARED_PASSAGE, content_fingerprint=full_text_fingerprint,
    )

    deduped = deduplicate_evidences([ev_original, ev_renamed_copy])

    assert len(deduped) == 1
    representative = deduped[0]
    assert representative.source == "renamed_copy.md"  # best score kept
    assert representative.duplicate_sources == ["original.md"]


def test_fallback_to_hashing_content_when_no_fingerprint_set():
    """Backward compatibility: evidence built without a content_fingerprint
    at all (e.g. a test fixture predating this ticket, or any future
    caller that omits it) must still dedup exactly as B18-T4 did, by
    hashing `.content` directly."""
    ev1 = RAGEvidence(query="q", source="x.md", score=0.5, content="texte identique sans fingerprint")
    ev2 = RAGEvidence(query="q", source="y.md", score=0.8, content="texte identique sans fingerprint")
    deduped = deduplicate_evidences([ev1, ev2])
    assert len(deduped) == 1
    assert deduped[0].source == "y.md"


# ---------------------------------------------------------------------------
# Localization must not perturb document-level similarity or the business
# calculation — only `.content`/positions change, never `.score`.
# ---------------------------------------------------------------------------

def test_changing_localized_passage_does_not_change_score_or_decision():
    from src.agents.scoring_engine import ScoringEngine
    from src.core.models import AOContext, CapacityResult, CompanyProfile

    base_kwargs = dict(query="q", source="ref.md", score=0.75, content_fingerprint="fp-fixed")
    content_1 = "Premiere fenetre localisee, tout debut du document."
    content_2 = "Deuxieme fenetre localisee, plus loin dans le document."
    evidences_window_1 = [RAGEvidence(content=content_1, start_char=0, end_char=len(content_1), **base_kwargs)]
    evidences_window_2 = [RAGEvidence(content=content_2, start_char=4000, end_char=4000 + len(content_2), **base_kwargs)]

    ao = AOContext(
        titre="Portail client", client="Client Test", secteur="Retail",
        budget_estime=200000.0, technologies_demandees=["Python"], certifications_obligatoires=[],
    )
    capacity = CapacityResult(charge_actuelle_pct=40, capacite_restante_pct=60, equipe_disponible=True, commentaire="ok")
    company = CompanyProfile(raison_sociale="ESN", secteur="Retail")

    result_1 = score_with_synthetic_policy(ao, company, evidences_window_1, capacity)
    result_2 = score_with_synthetic_policy(ao, company, evidences_window_2, capacity)

    assert result_1.score_global == result_2.score_global
    assert result_1.decision == result_2.decision
    # Only the localization-dependent fields differ between the two runs —
    # confirming the test actually varied what it claims to vary.
    assert result_1.evidence_pack[0].content != result_2.evidence_pack[0].content
    assert result_1.evidence_pack[0].score == result_2.evidence_pack[0].score == 0.75


# ---------------------------------------------------------------------------
# Real corpus: multiple internal segments of ONE document still yield
# exactly one evidence/reference, never one per segment.
# ---------------------------------------------------------------------------

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


def test_one_document_with_many_internal_segments_yields_a_single_evidence(client, db):
    from src.rag import private_rag_manager

    user = make_active_starter_user(db, "onedocmanysegments@example.com", scoring=False)
    org_id = default_org_id(db, user)
    csrf = _login(client, "onedocmanysegments@example.com")

    # A single long paragraph (no blank lines) is still ONE KnowledgeChunk
    # (src/web/knowledge/extraction.py splits on "\n\n" only) but internally
    # windows into several passage_location segments once it exceeds
    # MAX_PASSAGE_CHARS — the multi-segment case this test targets.
    long_single_paragraph = ("reference python django secteur retail projet portail client. " * 100)
    assert len(long_single_paragraph) > 3500
    _upload(client, csrf, "long.md", long_single_paragraph)

    results = private_rag_manager.search(db, organization_id=org_id, owner_user_id=user.id, query="portail client python retail", top_k=6)

    assert len(results) == 1, "one document, however many internal segments, must yield exactly one evidence"
    assert results[0].source == "long.md"


def test_same_full_content_at_two_owners_never_merges_across_organizations(client, db):
    """B03 isolation reused: identical content_fingerprints could only ever
    collide WITHIN one owner's own snapshot — private_rag_manager builds a
    separate snapshot per (organization_id, owner_user_id), so this must
    never merge or leak across accounts even though the fingerprint value
    itself would be identical."""
    from src.rag import private_rag_manager

    text = "Reference identique chez deux comptes differents, meme contenu exact."
    user_a = make_active_starter_user(db, "fpisoa@example.com", scoring=False)
    org_a = default_org_id(db, user_a)
    csrf_a = _login(client, "fpisoa@example.com")
    _upload(client, csrf_a, "shared.md", text)

    user_b = make_active_starter_user(db, "fpisob@example.com", scoring=False)
    org_b = default_org_id(db, user_b)
    csrf_b = _login(client, "fpisob@example.com")
    _upload(client, csrf_b, "shared.md", text)

    results_a = private_rag_manager.search(db, organization_id=org_a, owner_user_id=user_a.id, query="reference identique deux comptes", top_k=6)
    results_b = private_rag_manager.search(db, organization_id=org_b, owner_user_id=user_b.id, query="reference identique deux comptes", top_k=6)

    assert len(results_a) == 1 and len(results_b) == 1
    assert results_a[0].content_fingerprint == results_b[0].content_fingerprint  # same content, expected
    assert results_a[0].duplicate_sources == []
    assert results_b[0].duplicate_sources == []
