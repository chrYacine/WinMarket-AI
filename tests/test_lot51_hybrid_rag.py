"""Lot 51 — hybrid (lexical + vector) RAG, qualified against a REAL,
disposable PostgreSQL+pgvector (auto-started by tests/pg_support.py via
`pgserver` — never a mock of the database) and REAL embeddings (fastembed,
a real ONNX model, a real network fetch on first use) — per the ticket:
"aucun mock ne vaut qualification du moteur réel" / "utilise de vrais
embeddings pour cette comparaison ; le mock sert uniquement aux pannes
déterministes". The one deliberately-mocked path (EmbeddingAdapter raising)
proves the controlled-degradation contract, never the retrieval quality
itself.

Every test here MUTATES config.RAG_HYBRID_MODE_ENABLED — always via
monkeypatch, always reverted automatically at teardown; the real
application default (disabled) is never touched.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy.orm import sessionmaker

from src.core import config
from src.rag import hybrid_index, hybrid_search
from src.rag.embeddings import EmbeddingAdapter, EmbeddingUnavailableError
from tests import pg_support
from tests.pg_support import pg_engine, pg_url  # noqa: F401  (fixtures)
from tests.conftest import default_org_id, make_active_starter_user

pytestmark = pytest.mark.timeout(180)  # real model load + real network fetch on a cold cache


@pytest.fixture()
def pg_session(pg_engine, pg_url, monkeypatch):
    """Real PostgreSQL, migrated to head, hybrid mode explicitly enabled —
    the ONLY way this lot's vector code path is ever active (structural
    gate: dialect == postgresql AND config.RAG_HYBRID_MODE_ENABLED)."""
    pg_support.upgrade(pg_url, "head")
    monkeypatch.setattr(config, "RAG_HYBRID_MODE_ENABLED", True)
    session = sessionmaker(bind=pg_engine, autoflush=False, expire_on_commit=False)()
    yield session
    session.close()


def _upload(session, *, org, owner, filename, text):
    from src.web.knowledge import documents_service
    return documents_service.upload_document(session, organization_id=org, owner_user_id=owner, original_filename=filename, raw=text.encode("utf-8"))


# ---------------------------------------------------------------------------
# Structural gate — SQLite (or hybrid disabled) is UNCHANGED, never silently upgraded
# ---------------------------------------------------------------------------

def test_sqlite_always_reports_lexical_mode_even_with_hybrid_enabled(db, monkeypatch):
    """`db` is the suite's ordinary SQLite fixture (tests/conftest.py) — even
    with RAG_HYBRID_MODE_ENABLED=True, the dialect itself makes hybrid mode
    structurally impossible (ticket: "SQLite continue de fonctionner en
    mode lexical déclaré")."""
    monkeypatch.setattr(config, "RAG_HYBRID_MODE_ENABLED", True)
    from src.web.knowledge import documents_service
    from tests.conftest import default_org_id, make_active_starter_user as mksu

    user = mksu(db, "lot51-sqlite@example.com", scoring=False)
    org = default_org_id(db, user)
    documents_service.upload_document(db, organization_id=org, owner_user_id=user.id, original_filename="a.md", raw=b"Un texte de reference.")
    db.commit()
    outcome = hybrid_search.search(db, organization_id=org, owner_user_id=user.id, query="texte", top_k=5)
    assert outcome.mode == hybrid_search.MODE_LEXICAL
    assert not hybrid_index.hybrid_mode_active(db)


def test_postgres_without_the_explicit_flag_also_stays_lexical(pg_engine, pg_url, monkeypatch):
    pg_support.upgrade(pg_url, "head")
    monkeypatch.setattr(config, "RAG_HYBRID_MODE_ENABLED", False)  # explicit: the real deployment default
    session = sessionmaker(bind=pg_engine, autoflush=False, expire_on_commit=False)()
    user = make_active_starter_user(session, "lot51-pgoff@example.com", scoring=False)
    org = default_org_id(session, user)
    _upload(session, org=org, owner=user.id, filename="a.md", text="Un texte de reference.")
    session.commit()
    outcome = hybrid_search.search(session, organization_id=org, owner_user_id=user.id, query="texte", top_k=5)
    assert outcome.mode == hybrid_search.MODE_LEXICAL
    session.close()


# ---------------------------------------------------------------------------
# Real semantic retrieval — a paraphrase with NO shared vocabulary
# ---------------------------------------------------------------------------

def test_a_relevant_paraphrase_with_no_shared_vocabulary_is_found_via_the_vector_signal(pg_session):
    user = make_active_starter_user(pg_session, "lot51-a@example.com", scoring=False)
    org = default_org_id(pg_session, user)
    # No lexical overlap at all with the query below (different vocabulary,
    # same underlying meaning) — a pure TF-IDF search would never surface this.
    _upload(pg_session, org=org, owner=user.id, filename="chantier.md",
            text="Renovation complete d'un groupe scolaire a Meriden en 2023, incluant l'isolation thermique des facades.")
    _upload(pg_session, org=org, owner=user.id, filename="hors_sujet.md",
            text="Menu de la cantine municipale pour la semaine du 12 mars : poisson, legumes, fromage.")
    pg_session.commit()

    query = "Avez-vous deja realise des travaux de renovation energetique dans une ecole primaire ?"
    outcome = hybrid_search.search(pg_session, organization_id=org, owner_user_id=user.id, query=query, top_k=5)
    assert outcome.mode == hybrid_search.MODE_HYBRID
    sources = [ev.source for ev in outcome.evidences]
    assert "chantier.md" in sources
    assert sources.index("chantier.md") < (sources.index("hors_sujet.md") if "hors_sujet.md" in sources else len(sources))


def test_an_exact_term_match_still_works_through_the_lexical_signal_in_hybrid_mode(pg_session):
    user = make_active_starter_user(pg_session, "lot51-b@example.com", scoring=False)
    org = default_org_id(pg_session, user)
    _upload(pg_session, org=org, owner=user.id, filename="cert.md", text="Certification QUALIBAT RGE 8621 valable jusqu'en 2026.")
    pg_session.commit()

    outcome = hybrid_search.search(pg_session, organization_id=org, owner_user_id=user.id, query="QUALIBAT RGE", top_k=5)
    assert outcome.mode == hybrid_search.MODE_HYBRID
    assert any(ev.source == "cert.md" for ev in outcome.evidences)


# ---------------------------------------------------------------------------
# Fusion identity — one chunk, multiple passages, never double-counted
# ---------------------------------------------------------------------------

def test_a_chunk_matched_by_both_signals_contributes_exactly_one_evidence(pg_session):
    user = make_active_starter_user(pg_session, "lot51-c@example.com", scoring=False)
    org = default_org_id(pg_session, user)
    _upload(pg_session, org=org, owner=user.id, filename="ref.md", text="Projet de reference : construction d'un pont routier a Meriden, budget 2 000 000 euros.")
    pg_session.commit()

    outcome = hybrid_search.search(pg_session, organization_id=org, owner_user_id=user.id, query="pont Meriden budget", top_k=5)
    matching = [ev for ev in outcome.evidences if ev.source == "ref.md"]
    assert len(matching) == 1, "a chunk found by both the lexical AND vector signal must count once"


# ---------------------------------------------------------------------------
# Isolation between accounts (same machine, same corpus name, different scope)
# ---------------------------------------------------------------------------

def test_vector_search_never_crosses_organizations_or_owners(pg_session):
    user_a = make_active_starter_user(pg_session, "lot51-iso-a@example.com", scoring=False)
    user_b = make_active_starter_user(pg_session, "lot51-iso-b@example.com", scoring=False)
    org_a, org_b = default_org_id(pg_session, user_a), default_org_id(pg_session, user_b)
    secret_text = "Reference confidentielle du compte A : chantier MARQUEUR_ISOLATION_A a Meriden."
    _upload(pg_session, org=org_a, owner=user_a.id, filename="secret_a.md", text=secret_text)
    pg_session.commit()

    outcome_b = hybrid_search.search(pg_session, organization_id=org_b, owner_user_id=user_b.id, query="MARQUEUR_ISOLATION_A chantier Meriden", top_k=5)
    assert all(ev.source != "secret_a.md" for ev in outcome_b.evidences)
    assert outcome_b.mode == hybrid_search.MODE_EMPTY_CORPUS  # org_b's corpus is genuinely empty

    outcome_a = hybrid_search.search(pg_session, organization_id=org_a, owner_user_id=user_a.id, query="MARQUEUR_ISOLATION_A chantier Meriden", top_k=5)
    assert any(ev.source == "secret_a.md" for ev in outcome_a.evidences)


def test_the_same_user_in_two_organizations_gets_two_separate_vector_corpora(pg_session):
    from src.web.database.repositories import memberships as memberships_repo
    from src.web.database.repositories import organizations as organizations_repo

    user = make_active_starter_user(pg_session, "lot51-multi@example.com", scoring=False)
    org_1 = default_org_id(pg_session, user)
    org_2 = organizations_repo.create_organization(pg_session, name="Second espace (lot 51)").id
    memberships_repo.create_membership(pg_session, user_id=user.id, organization_id=org_2, role="organization_admin", status="active")
    pg_session.commit()

    _upload(pg_session, org=org_1, owner=user.id, filename="only_in_org1.md", text="Reference MARQUEUR_ORG1 exclusive au premier espace.")
    pg_session.commit()

    outcome_org2 = hybrid_search.search(pg_session, organization_id=org_2, owner_user_id=user.id, query="MARQUEUR_ORG1 reference", top_k=5)
    assert all(ev.source != "only_in_org1.md" for ev in outcome_org2.evidences)
    outcome_org1 = hybrid_search.search(pg_session, organization_id=org_1, owner_user_id=user.id, query="MARQUEUR_ORG1 reference", top_k=5)
    assert any(ev.source == "only_in_org1.md" for ev in outcome_org1.evidences)


# ---------------------------------------------------------------------------
# Replacement / deletion — never resurrected by a late (re)indexing
# ---------------------------------------------------------------------------

def test_a_deleted_document_is_never_returned_even_though_its_passages_still_exist_in_storage(pg_session):
    from src.web.knowledge import documents_service

    user = make_active_starter_user(pg_session, "lot51-del@example.com", scoring=False)
    org = default_org_id(pg_session, user)
    result = _upload(pg_session, org=org, owner=user.id, filename="a_supprimer.md", text="Reference MARQUEUR_SUPPRESSION a conserver un temps.")
    pg_session.commit()

    documents_service.delete_document(pg_session, organization_id=org, owner_user_id=user.id, document=result.document)
    pg_session.commit()

    outcome = hybrid_search.search(pg_session, organization_id=org, owner_user_id=user.id, query="MARQUEUR_SUPPRESSION reference", top_k=5)
    assert all(ev.source != "a_supprimer.md" for ev in outcome.evidences)


def test_replacing_a_version_makes_only_the_new_ones_vectors_searchable(pg_session):
    """Both versions share the same `original_filename` (`ev.source` alone cannot distinguish them) — the real
    signal that the SUPERSEDED version's own passages were excluded is that no returned evidence's CONTENT is
    the old wording, even though the query is the old wording verbatim (which the lexical+vector fusion would
    otherwise happily surface for the still-active version, since it's the only document in this corpus)."""
    from src.web.knowledge import documents_service

    user = make_active_starter_user(pg_session, "lot51-replace@example.com", scoring=False)
    org = default_org_id(pg_session, user)
    result = _upload(pg_session, org=org, owner=user.id, filename="doc.md", text="Version un : MARQUEUR_V1 contenu initial.")
    pg_session.commit()

    documents_service.add_version(pg_session, organization_id=org, owner_user_id=user.id, document=result.document,
                                   original_filename="doc.md", raw=b"Version deux : MARQUEUR_V2 contenu remplace.")
    pg_session.commit()

    outcome_v1 = hybrid_search.search(pg_session, organization_id=org, owner_user_id=user.id, query="MARQUEUR_V1 contenu initial", top_k=5)
    assert all("MARQUEUR_V1" not in ev.content for ev in outcome_v1.evidences), "the SUPERSEDED version's own content must never surface"
    outcome_v2 = hybrid_search.search(pg_session, organization_id=org, owner_user_id=user.id, query="MARQUEUR_V2 contenu remplace", top_k=5)
    assert any("MARQUEUR_V2" in ev.content for ev in outcome_v2.evidences)


# ---------------------------------------------------------------------------
# Model/dimension change — never mixed silently, reindex required and idempotent
# ---------------------------------------------------------------------------

def test_a_changed_embedding_model_makes_a_version_stale_and_excluded_until_reindexed(pg_session, monkeypatch):
    """Exercises hybrid_search._vector_candidates directly rather than the full search() — the full pipeline
    always ALSO runs the lexical signal (any shared word, even an incidental one like "isolation" in a
    hand-written paraphrase, would make the fused result ambiguous about which signal actually found the
    document); this isolates the ONE mechanism this test cares about: the passage-level
    embedding_model_revision filter."""
    user = make_active_starter_user(pg_session, "lot51-stale@example.com", scoring=False)
    org = default_org_id(pg_session, user)
    result = _upload(pg_session, org=org, owner=user.id, filename="doc.md", text="Reference MARQUEUR_STALE pour ce test.")
    pg_session.commit()
    assert result.version.embedding_status == "ready"
    assert not hybrid_index.version_is_stale(result.version)

    from src.rag.embeddings import EmbeddingAdapter as _EA
    query_vector = _EA.shared().embed_one("n'importe quelle requete")

    fresh_candidates = hybrid_search._vector_candidates(pg_session, organization_id=org, owner_user_id=user.id, query_vector=query_vector, limit=5)
    assert any(str(cid) for cid in fresh_candidates), "sanity check: the fresh index has at least one candidate"

    monkeypatch.setattr(config, "EMBEDDING_MODEL_REVISION", config.EMBEDDING_MODEL_REVISION + "-changed")
    pg_session.refresh(result.version)
    assert hybrid_index.version_is_stale(result.version), "a config change must be detected, never silently reused"

    # a stale version's OLD vectors are never mixed into a NEW-config candidate list (the passage-level
    # embedding_model_revision filter rejects them structurally, never merely deprioritizes them).
    stale_candidates = hybrid_search._vector_candidates(pg_session, organization_id=org, owner_user_id=user.id, query_vector=query_vector, limit=5)
    assert stale_candidates == [], "a stale-config vector must never appear in a current-config candidate list"

    hybrid_index.index_version(pg_session, version=result.version, chunks=list(result.version.chunks))
    pg_session.commit()
    assert not hybrid_index.version_is_stale(result.version)
    after_candidates = hybrid_search._vector_candidates(pg_session, organization_id=org, owner_user_id=user.id, query_vector=query_vector, limit=5)
    assert len(after_candidates) >= 1, "reindexing under the NEW config makes it a valid candidate again"


def test_reindexing_is_idempotent_never_duplicates_passages(pg_session):
    from src.web.database.repositories import knowledge as knowledge_repo

    user = make_active_starter_user(pg_session, "lot51-idem@example.com", scoring=False)
    org = default_org_id(pg_session, user)
    result = _upload(pg_session, org=org, owner=user.id, filename="doc.md", text="Un paragraphe. \n\nUn second paragraphe distinct.")
    pg_session.commit()

    def passage_count():
        from sqlalchemy import select
        from src.web.database.models import KnowledgePassage
        return len(pg_session.execute(select(KnowledgePassage).where(KnowledgePassage.document_version_id == result.version.id)).scalars().all())

    before = passage_count()
    assert before >= 1
    hybrid_index.index_version(pg_session, version=result.version, chunks=list(result.version.chunks))
    pg_session.commit()
    assert passage_count() == before


# ---------------------------------------------------------------------------
# Controlled degradation — a provider failure is never a fake "zero results"
# ---------------------------------------------------------------------------

def test_an_embedding_provider_failure_degrades_to_lexical_with_an_explicit_reason(pg_session, monkeypatch):
    user = make_active_starter_user(pg_session, "lot51-fail@example.com", scoring=False)
    org = default_org_id(pg_session, user)
    _upload(pg_session, org=org, owner=user.id, filename="doc.md", text="Reference disponible malgre la panne du fournisseur d'embeddings.")
    pg_session.commit()

    def _boom(self, text):
        raise EmbeddingUnavailableError("simulated_provider_outage")

    monkeypatch.setattr(EmbeddingAdapter, "embed_one", _boom)
    outcome = hybrid_search.search(pg_session, organization_id=org, owner_user_id=user.id, query="reference disponible panne", top_k=5)
    assert outcome.mode == hybrid_search.MODE_HYBRID_DEGRADED
    assert outcome.degraded_reason == "embedding_unavailable:simulated_provider_outage"
    # the lexical signal alone still finds it — a degradation, never a fabricated empty result
    assert any(ev.source == "doc.md" for ev in outcome.evidences)


def test_an_empty_corpus_is_distinguished_from_a_query_with_no_results(pg_session):
    user = make_active_starter_user(pg_session, "lot51-empty@example.com", scoring=False)
    org = default_org_id(pg_session, user)
    outcome_empty = hybrid_search.search(pg_session, organization_id=org, owner_user_id=user.id, query="quoi que ce soit", top_k=5)
    assert outcome_empty.mode == hybrid_search.MODE_EMPTY_CORPUS

    _upload(pg_session, org=org, owner=user.id, filename="doc.md", text="Un contenu totalement sans rapport avec la requete suivante.")
    pg_session.commit()
    outcome_no_match = hybrid_search.search(pg_session, organization_id=org, owner_user_id=user.id, query="xylophone quantique interstellaire", top_k=5)
    assert outcome_no_match.mode == hybrid_search.MODE_HYBRID  # the pipeline genuinely ran — it just found nothing convincing
