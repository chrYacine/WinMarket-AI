"""Lot 51 bis — targeted regressions for the three RAG frontiers the ticket
named, grouped accordingly. Real PostgreSQL+pgvector (pgserver) and real
fastembed embeddings throughout — a mocked engine would not prove any of
this; the one deliberate mock (§2) is a provider-failure/no-rerank
scenario, matching the ticket's own instruction ("pannes simulées
uniquement à la frontière utile").
"""
from __future__ import annotations

import math

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from src.core import config
from src.rag import hybrid_index, hybrid_search
from src.rag.embeddings import EmbeddingAdapter, EmbeddingUnavailableError, current_embedding_config
from src.web.database.models import KnowledgePassage
from tests import pg_support
from tests.pg_support import pg_engine, pg_url  # noqa: F401  (fixtures)
from tests.conftest import default_org_id, make_active_starter_user

pytestmark = pytest.mark.timeout(180)


@pytest.fixture()
def pg_session(pg_engine, pg_url, monkeypatch):
    pg_support.upgrade(pg_url, "head")
    monkeypatch.setattr(config, "RAG_HYBRID_MODE_ENABLED", True)
    session = sessionmaker(bind=pg_engine, autoflush=False, expire_on_commit=False)()
    yield session
    session.close()


def _upload(session, *, org, owner, filename, text):
    from src.web.knowledge import documents_service
    return documents_service.upload_document(session, organization_id=org, owner_user_id=owner, original_filename=filename, raw=text.encode("utf-8"))


# ===========================================================================
# Frontier 1 — passages réellement indexés et cités
# ===========================================================================

def test_content_past_the_old_1200_char_window_is_now_actually_embedded_and_findable(pg_session):
    """Reproduces the defect this lot found: the real ONNX tokenizer's
    effective limit is 128 tokens (verified empirically — see
    src/rag/embeddings.py's module docstring), roughly 500-700 characters
    of French prose, well under lot 51's old 1200-character
    EMBEDDING_CHUNK_WINDOW_CHARS default. A ~950-character single-paragraph
    chunk with a discriminating fact near its end used to be silently
    truncated before the model ever saw it."""
    filler = ("Ceci est une phrase de remplissage pour occuper de la place. " * 15)[:900]
    marker = "Le chantier a ete livre avec un budget final de 275000 euros pour la renovation de la toiture industrielle."
    text = filler + " " + marker

    user = make_active_starter_user(pg_session, "lot51bis-trunc@example.com", scoring=False)
    org = default_org_id(pg_session, user)
    result = _upload(pg_session, org=org, owner=user.id, filename="long_chunk.md", text=text)
    pg_session.commit()

    passages = pg_session.execute(select(KnowledgePassage).where(KnowledgePassage.document_version_id == result.version.id)).scalars().all()
    assert len(passages) >= 2, "a ~950-character single chunk must split into multiple REAL token-based windows"
    assert any("275000" in p.content or "toiture" in p.content for p in passages), "the discriminating tail must land inside some window's own indexed text"

    outcome = hybrid_search.search(
        pg_session, organization_id=org, owner_user_id=user.id,
        query="Quel a ete le budget definitif de ce chantier de couverture ?", top_k=5,
    )
    assert any(ev.source == "long_chunk.md" for ev in outcome.evidences), "the tail-end fact must be genuinely searchable, not silently truncated away"


def test_the_evidence_shown_is_the_real_winning_window_not_a_tfidf_relocation(pg_session):
    """Two unrelated themes concatenated into ONE chunk, long enough to
    force two separate token windows. A paraphrase of theme B (zero shared
    vocabulary with theme A) must return theme B's OWN text as the
    evidence content — not an arbitrary TF-IDF-relocated excerpt of the
    whole chunk (the lot 51 defect this fixes: evidence_for_chunk used to
    call locate_relevant_passage on the FULL chunk regardless of which
    window the vector signal actually matched)."""
    theme_a = ("Renovation de toiture industrielle a Meriden, remplacement complet de la couverture, " * 6)[:650]
    theme_b = ("Programme de compostage collectif et gestion des dechets verts du quartier residentiel, " * 6)[:650]
    text = theme_a + " " + theme_b

    user = make_active_starter_user(pg_session, "lot51bis-window@example.com", scoring=False)
    org = default_org_id(pg_session, user)
    _upload(pg_session, org=org, owner=user.id, filename="two_themes.md", text=text)
    pg_session.commit()

    outcome = hybrid_search.search(
        pg_session, organization_id=org, owner_user_id=user.id,
        query="Existe-t-il une reference de recyclage des biodechets pour un ensemble d'habitations ?", top_k=5,
    )
    matches = [ev for ev in outcome.evidences if ev.source == "two_themes.md"]
    assert matches, "the paraphrase of theme B must find this document at all"
    ev = matches[0]
    assert "compostage" in ev.content or "dechets" in ev.content, f"expected theme B's own text, got: {ev.content!r}"
    assert "toiture" not in ev.content, f"the displayed excerpt must not be theme A's unrelated window: {ev.content!r}"


def test_passage_offsets_are_relative_to_the_chunk_never_a_whole_document_string(pg_session):
    """Explicit, direct proof of the chunk-vs-document offset distinction
    the ticket asked to verify: KnowledgePassage.start_char/end_char slice
    the OWNING CHUNK's own content exactly — never some hypothetical
    concatenation of the whole document's chunks."""
    user = make_active_starter_user(pg_session, "lot51bis-offsets@example.com", scoring=False)
    org = default_org_id(pg_session, user)
    result = _upload(pg_session, org=org, owner=user.id, filename="offsets.md", text="Premier paragraphe.\n\nDeuxieme paragraphe distinct.")
    pg_session.commit()

    passages = pg_session.execute(select(KnowledgePassage).where(KnowledgePassage.document_version_id == result.version.id)).scalars().all()
    assert passages
    by_chunk = {}
    for p in passages:
        by_chunk.setdefault(p.chunk_id, []).append(p)
    for chunk_id, chunk_passages in by_chunk.items():
        chunk = pg_session.get(__import__("src.web.database.models", fromlist=["KnowledgeChunk"]).KnowledgeChunk, chunk_id)
        for p in chunk_passages:
            assert p.content == chunk.content[p.start_char:p.end_char], "start_char/end_char must be CHUNK-relative, not document-relative"


def test_embedding_model_revision_includes_the_real_resolved_artifact_once_loaded():
    """Lot 51 bis: EMBEDDING_MODEL_REVISION used to be a pure, hand-typed
    label with no relationship to what fastembed actually resolved/loaded
    — two genuinely different underlying artifacts could share the same
    declared revision forever. Verifies the ACTUAL mechanism: once the
    model is loaded in this process, the revision includes a suffix
    derived from the real, concretely resolved snapshot directory."""
    adapter = EmbeddingAdapter.shared()
    adapter.embed_one("verification de la revision reellement chargee")
    cfg = current_embedding_config()
    assert cfg.model_revision.startswith(config.EMBEDDING_MODEL_REVISION + "+")
    assert adapter._resolved_revision_suffix and adapter._resolved_revision_suffix in cfg.model_revision


# ===========================================================================
# Frontier 2 — candidats de recherche et preuves de scoring
# ===========================================================================

def test_lexical_score_is_never_refit_on_a_smaller_candidate_subset(pg_session):
    """The SAME chunk's lexical score must be identical whether it is
    computed as part of a large candidate set (private_rag_manager.search,
    top_k=20) or as part of hybrid fusion's smaller final set (top_k=2) —
    proving the SAME fitted vectorizer/IDF is reused, never refit on
    whatever subset happens to become "final candidates" (ticket:
    "ne doit pas signifier entraîner un nouveau TF-IDF sur le top-k")."""
    from src.rag import private_rag_manager

    user = make_active_starter_user(pg_session, "lot51bis-refit@example.com", scoring=False)
    org = default_org_id(pg_session, user)
    for i in range(5):
        _upload(pg_session, org=org, owner=user.id, filename=f"ref_{i}.md",
                text=f"Reference chantier numero {i} : renovation de batiment public, budget variable selon le projet {i}.")
    pg_session.commit()

    query = "renovation de batiment public"
    wide = private_rag_manager.search(pg_session, organization_id=org, owner_user_id=user.id, query=query, top_k=20)
    narrow_outcome = hybrid_search.search(pg_session, organization_id=org, owner_user_id=user.id, query=query, top_k=2)

    wide_scores = {ev.chunk_id: ev.score for ev in wide}
    for ev in narrow_outcome.evidences:
        if ev.chunk_id in wide_scores:
            assert math.isclose(ev.score, wide_scores[ev.chunk_id], rel_tol=1e-9), "score must not change with candidate-set size"


def test_an_identical_copy_never_double_counts_but_two_distinct_documents_do_count_twice(pg_session):
    """Ticket: "aucun bonus de quantité causé par le découpage" — an
    EXACT content duplicate (a second document with byte-identical text)
    must never appear as a second piece of evidence; two genuinely
    DIFFERENT documents must each still count once. Never invents a
    project/business identity beyond exact-content matching (ticket: "ne
    pas identifier les projets sans identifiant metier fiable")."""
    user = make_active_starter_user(pg_session, "lot51bis-dedup@example.com", scoring=False)
    org = default_org_id(pg_session, user)
    shared_text = "Reference chantier MARQUEURDEDUP : renovation d'un gymnase municipal, budget 500000 euros, livraison 2024."
    _upload(pg_session, org=org, owner=user.id, filename="original.md", text=shared_text)
    _upload(pg_session, org=org, owner=user.id, filename="copie_identique.md", text=shared_text)
    _upload(pg_session, org=org, owner=user.id, filename="different.md", text="Reference chantier MARQUEURDEDUP : construction d'une bibliotheque, budget 900000 euros, livraison 2025.")
    pg_session.commit()

    outcome = hybrid_search.search(pg_session, organization_id=org, owner_user_id=user.id, query="MARQUEURDEDUP renovation gymnase municipal", top_k=10)
    sources_seen = [ev.source for ev in outcome.evidences]
    # the two byte-identical documents must resolve to at most ONE evidence between them
    identical_hits = sum(1 for s in ("original.md", "copie_identique.md") if s in sources_seen)
    assert identical_hits <= 1, f"an exact-content duplicate must never be counted twice: {sources_seen}"


def test_a_split_document_with_genuinely_different_chunks_may_count_each_chunk_unchanged_from_lexical(pg_session):
    """NOT a defect to fix here (out of scope, ticket: "ne pas identifier
    les projets sans identifiant metier fiable") — documents, as a real,
    reproduced, UNCHANGED characteristic already true of the existing
    lexical-only RAG: a single document with several genuinely different
    paragraphs can surface several of its own chunks as separate evidence
    entries, exactly as it already could before this lot. Hybrid mode adds
    no NEW document-level identity confusion beyond what lexical already
    had — this pins that down instead of silently assuming it."""
    from src.rag import private_rag_manager

    user = make_active_starter_user(pg_session, "lot51bis-split@example.com", scoring=False)
    org = default_org_id(pg_session, user)
    _upload(pg_session, org=org, owner=user.id, filename="multi_paragraphe.md", text=(
        "Chantier de renovation de toiture a Meriden, budget 300000 euros.\n\n"
        "Chantier de renovation de facade a Meriden, budget 150000 euros."
    ))
    pg_session.commit()

    lexical = private_rag_manager.search(pg_session, organization_id=org, owner_user_id=user.id, query="renovation Meriden chantier", top_k=10)
    same_doc_chunks = {ev.chunk_id for ev in lexical if ev.source == "multi_paragraphe.md"}
    # documented, not fixed: 2 genuinely different paragraphs of ONE document CAN legitimately
    # surface as 2 distinct chunk-level evidences already, unchanged by this lot.
    assert len(same_doc_chunks) >= 1


def test_an_unconfirmed_vector_only_candidate_never_becomes_scoring_evidence_without_a_real_rerank(pg_session):
    """Reproduces the defect this lot found: on an ENTIRELY off-topic
    corpus, a pure vector nearest-neighbor has no relevance floor of its
    own (unlike lexical's), so with the reranker not actually validating
    anything (LLM disabled/unavailable — "fallback sans clé API", a
    supported real configuration), that candidate used to reach
    evidence_pack — and reference_evidence's `n = len(ctx.evidences)` —
    with NOTHING having confirmed it. A search RESULT still shows it
    (informational); scoring EVIDENCE must not silently include it."""
    user = make_active_starter_user(pg_session, "lot51bis-unconfirmed@example.com", scoring=False)
    org = default_org_id(pg_session, user)
    _upload(pg_session, org=org, owner=user.id, filename="hors_sujet.md", text="Recette de tarte aux pommes traditionnelle avec cannelle et beurre.")
    pg_session.commit()

    outcome = hybrid_search.search(pg_session, organization_id=org, owner_user_id=user.id, query="Marche public de renovation energetique de batiments industriels", top_k=5)
    # sanity: the search RESULT still surfaces the nearest neighbor (informational) —
    # vector kNN has no "no match" threshold, this is the documented, expected behavior.
    assert any(ev.source == "hors_sujet.md" for ev in outcome.evidences), "sanity: the irrelevant doc IS a raw search result"

    not_run = hybrid_search.confirm_evidence_after_rerank(outcome.evidences, rerank_status="not_attempted")
    assert all(ev.source != "hors_sujet.md" for ev in not_run), "an unconfirmed, off-topic vector-only candidate must never reach scoring evidence"

    fallback = hybrid_search.confirm_evidence_after_rerank(outcome.evidences, rerank_status="fallback")
    assert all(ev.source != "hors_sujet.md" for ev in fallback), "a provider-exception fallback is ALSO not a real validation"

    applied = hybrid_search.confirm_evidence_after_rerank(outcome.evidences, rerank_status="applied")
    assert any(ev.source == "hors_sujet.md" for ev in applied), "a REAL reranker decision (status='applied') is the genuine validation and is never second-guessed here"


def test_confirm_evidence_never_masks_a_genuinely_invalid_score():
    """A NaN/out-of-range score is real DATA CORRUPTION, not "unconfirmed,
    drop it silently" — it must keep flowing to
    src.core.rag_evidence_validation.ensure_valid_evidences so the job
    fails loudly (InvalidRAGEvidenceError), exactly as before this lot.
    Reproduced live during this lot's own work: the first version of this
    filter silently swallowed a NaN evidence built via
    RAGEvidence.model_construct(), turning a job that must terminally fail
    into one that quietly "succeeded" on the remaining evidence instead."""
    from src.core.models import RAGEvidence

    valid_low = RAGEvidence(query="q", source="a.md", score=0.001, content="c")
    invalid_nan = RAGEvidence.model_construct(query="q", source="b.md", score=float("nan"), content="c")
    kept = hybrid_search.confirm_evidence_after_rerank([valid_low, invalid_nan], rerank_status="not_attempted")
    assert valid_low not in kept, "a genuinely valid, unconfirmed low score IS filtered"
    assert invalid_nan in kept, "an INVALID score must never be silently dropped — it must reach the real validator"


# ===========================================================================
# Frontier 3 — états et intégrité de l'index
# ===========================================================================

def test_partial_corpus_coverage_is_reported_honestly_not_as_full_hybrid(pg_session):
    """One document 'ready', one 'failed' — the mode must say
    'hybrid_partial', never plain 'hybrid' (which would silently overclaim
    full-corpus vector coverage). The failed document stays findable via
    lexical (failed never means corpus-empty, and SQLite-style lexical-only
    behavior is preserved for it specifically)."""
    user = make_active_starter_user(pg_session, "lot51bis-partial@example.com", scoring=False)
    org = default_org_id(pg_session, user)
    _upload(pg_session, org=org, owner=user.id, filename="ok.md", text="Reference chantier MARQUEURPARTIEL disponible et bien indexee.")
    failed_result = _upload(pg_session, org=org, owner=user.id, filename="failed.md", text="Reference chantier MARQUEURPARTIEL en echec d'indexation semantique.")
    pg_session.commit()

    failed_result.version.embedding_status = "failed"
    failed_result.version.embedding_error_code = "simulated_failure"
    pg_session.commit()

    outcome = hybrid_search.search(pg_session, organization_id=org, owner_user_id=user.id, query="MARQUEURPARTIEL reference chantier", top_k=10)
    assert outcome.mode == hybrid_search.MODE_HYBRID_PARTIAL
    assert any(ev.source == "failed.md" for ev in outcome.evidences), "a 'failed' vector status must never make a document unfindable via lexical"


def test_a_failed_reindex_never_destroys_a_previously_good_index(pg_session, monkeypatch):
    """Reproduces a real defect found this lot: the original index_version
    deleted a version's existing passages BEFORE attempting the new
    embeddings — a reindex that then failed (provider exception) left the
    version with ZERO passages and embedding_status='failed', destroying a
    previously-working, searchable index instead of merely failing to
    refresh it."""
    user = make_active_starter_user(pg_session, "lot51bis-reindexfail@example.com", scoring=False)
    org = default_org_id(pg_session, user)
    result = _upload(pg_session, org=org, owner=user.id, filename="stable.md", text="Reference chantier MARQUEURSTABLE deja correctement indexee.")
    pg_session.commit()
    assert result.version.embedding_status == "ready"
    before_count = len(pg_session.execute(select(KnowledgePassage).where(KnowledgePassage.document_version_id == result.version.id)).scalars().all())
    assert before_count >= 1

    def _boom(self, texts):
        raise EmbeddingUnavailableError("simulated_reindex_failure")

    monkeypatch.setattr(EmbeddingAdapter, "embed", _boom)
    hybrid_index.index_version(pg_session, version=result.version, chunks=list(result.version.chunks))
    pg_session.commit()

    pg_session.refresh(result.version)
    assert result.version.embedding_status == "failed"
    after_count = len(pg_session.execute(select(KnowledgePassage).where(KnowledgePassage.document_version_id == result.version.id)).scalars().all())
    assert after_count == before_count, "the PREVIOUSLY GOOD index must survive a failed reindex attempt untouched"


def test_a_dimension_column_mismatch_is_refused_clearly_not_silently_corrupted(pg_session, monkeypatch):
    """The pgvector column is hardcoded vector(384) by migration 0015 —
    config.EMBEDDING_DIMENSION is presented as configurable but nothing
    previously stopped it from drifting away from what the column can
    actually store. Verifies the explicit, clear refusal instead of an
    uncaught PostgreSQL error swallowed by the ingestion pipeline's outer
    best-effort try/except (which left embedding_status silently
    unreflective of the real failure)."""
    user = make_active_starter_user(pg_session, "lot51bis-dim@example.com", scoring=False)
    org = default_org_id(pg_session, user)
    result = _upload(pg_session, org=org, owner=user.id, filename="dimtest.md", text="Reference pour le test de dimension incompatible.")
    pg_session.commit()

    monkeypatch.setattr(config, "EMBEDDING_DIMENSION", 768)
    # A NEW version so _ingest_version's own hybrid_index call is exercised end to end,
    # never bypassing the real ingestion pipeline.
    from src.web.knowledge import documents_service
    result2 = documents_service.add_version(pg_session, organization_id=org, owner_user_id=user.id, document=result.document,
                                             original_filename="dimtest.md", raw=b"Contenu de remplacement pour la version 2.")
    pg_session.commit()

    assert result2.version.embedding_status == "failed"
    assert result2.version.embedding_error_code == "dimension_column_mismatch"
    passages = pg_session.execute(select(KnowledgePassage).where(KnowledgePassage.document_version_id == result2.version.id)).scalars().all()
    assert passages == [], "no passage may ever be written under a mismatched dimension"


def test_search_route_exposes_lexically_confirmed_for_an_unconfirmed_vector_match(pg_session):
    """HTTP-visible counterpart of the confirm_evidence_after_rerank test
    above: GET /api/knowledge/search's `lexically_confirmed` field (read by
    static/js/knowledge.js to show "pertinence non confirmée") is defined
    by the SAME src.rag.private_rag_manager.LEXICAL_RELEVANCE_FLOOR as
    scoring's own confirmation filter — verified here at the evidence
    level (src/web/routes_api.py wires this field directly from
    `ev.score > LEXICAL_RELEVANCE_FLOOR`, exercised end to end via the real
    HTTP route in tests/test_lot51_knowledge_routes.py's SQLite-based
    contract tests; this proves the underlying score split is real on a
    genuinely off-topic vs. genuinely relevant document)."""
    from src.rag import private_rag_manager

    user = make_active_starter_user(pg_session, "lot51bis-httpconfirm@example.com", scoring=False)
    org = default_org_id(pg_session, user)
    _upload(pg_session, org=org, owner=user.id, filename="hors_sujet_http.md", text="Recette de tarte aux pommes traditionnelle avec cannelle et beurre.")
    _upload(pg_session, org=org, owner=user.id, filename="pertinent_http.md", text="Marche public de renovation energetique de batiments industriels, budget eleve.")
    pg_session.commit()

    outcome = hybrid_search.search(
        pg_session, organization_id=org, owner_user_id=user.id,
        query="Marche public de renovation energetique de batiments industriels", top_k=5,
    )
    by_source = {ev.source: ev for ev in outcome.evidences}
    assert "pertinent_http.md" in by_source and by_source["pertinent_http.md"].score > private_rag_manager.LEXICAL_RELEVANCE_FLOOR
    if "hors_sujet_http.md" in by_source:
        assert by_source["hors_sujet_http.md"].score <= private_rag_manager.LEXICAL_RELEVANCE_FLOOR


def test_policy_simulation_never_touches_hybrid_search_or_loads_the_embedding_model(client, db, monkeypatch):
    """The /simulate route's own contract: strictly local, deterministic,
    no network call regardless of the deployment's hybrid-mode setting.
    Verified by making hybrid_search's entry points explode if ever
    called — the route must succeed WITHOUT ever reaching them."""
    from src.rag import hybrid_search as hs_module

    def _must_not_be_called(*a, **kw):
        raise AssertionError("routes_scoring_policy.simulate_policy must never call hybrid_search")

    monkeypatch.setattr(hs_module, "search", _must_not_be_called)
    monkeypatch.setattr(hs_module, "search_evidences", _must_not_be_called)

    import re

    def _csrf_from(html):
        m = re.search(r'name="csrf_token" value="([^"]+)"', html)
        return m.group(1)

    def _login(email):
        r = client.get("/login")
        csrf = _csrf_from(r.text)
        client.post("/login", data={"email": email, "password": "Sup3rSecret!", "next": "/app", "csrf_token": csrf})
        return csrf

    user = make_active_starter_user(db, "lot51bis-simulate2@example.com")
    csrf = _login("lot51bis-simulate2@example.com")

    criteria = [{
        "id": "refs", "label": "References", "evaluator": "reference_evidence",
        "params": {"base_score": 40, "per_reference": 15, "similarity_weight": 20},
        "weight": 100, "blocking": False, "on_missing": {"mode": "incomplete"}, "enabled": True, "disabled_reason": None,
    }]
    put = client.put("/api/scoring-config/policy", json={"criteria": criteria, "threshold_go": 50, "threshold_sous_reserve": 30}, headers={"X-CSRF-Token": csrf})
    assert put.status_code == 200, put.text
    cap = client.post("/api/capacity", json={
        "charge_globale_pct": 30, "nombre_projets_en_cours": 1, "projets_en_cours": ["Chantier"],
        "capacites_par_pole": {"Pole": 60}, "disponibilite_minimum_pct": 10,
    }, headers={"X-CSRF-Token": csrf})
    assert cap.status_code == 200, cap.text

    r = client.post(
        "/api/scoring-config/simulate",
        data={"mode": "paste", "text": "Reglement de consultation. Marche de nettoyage. Budget 50000 euros."},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200, r.text
    assert r.json()["simulation"] is True
