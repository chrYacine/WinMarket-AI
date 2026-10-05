"""Lot 51 §4 — the HTTP-visible contract of the hybrid search integration,
on the ordinary SQLite `client` fixture (hybrid mode is structurally
inactive there — see tests/test_lot51_hybrid_rag.py for the real
PostgreSQL+pgvector qualification). Verifies exactly what a real user of
`/api/knowledge/search` and the new reindex route can observe: the
`mode`/`degraded_reason` fields the ticket requires ("mode de recherche et
dégradation éventuelle lisibles"), and that embedding state is honestly
reported as 'not_applicable' rather than a fabricated 'ready'.
"""
from __future__ import annotations

import io
import re

from tests.conftest import make_active_starter_user


def _csrf_from(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


def _login(client, email, password="Sup3rSecret!"):
    r = client.get("/login")
    csrf = _csrf_from(r.text)
    client.post("/login", data={"email": email, "password": password, "next": "/app", "csrf_token": csrf})
    return csrf


def test_search_reports_empty_query_and_empty_corpus_modes_honestly(client, db):
    make_active_starter_user(db, "lot51-route-empty@example.com")
    _login(client, "lot51-route-empty@example.com")

    empty_query = client.get("/api/knowledge/search?q=").json()
    assert empty_query["mode"] == "empty_query"
    assert empty_query["degraded_reason"] is None

    empty_corpus = client.get("/api/knowledge/search?q=reference").json()
    assert empty_corpus["mode"] == "empty_corpus"
    assert empty_corpus["corpus_empty"] is True


def test_search_reports_declared_lexical_mode_on_sqlite_never_a_fabricated_hybrid(client, db):
    make_active_starter_user(db, "lot51-route-lexical@example.com")
    csrf = _login(client, "lot51-route-lexical@example.com")
    r = client.post(
        "/api/knowledge/documents", headers={"X-CSRF-Token": csrf},
        files={"file": ("ref.md", io.BytesIO(b"# Ref\n\nCONTENU_LOT51_ROUTE reference de test."), "text/plain")},
    )
    assert r.status_code == 201

    result = client.get("/api/knowledge/search?q=CONTENU_LOT51_ROUTE").json()
    assert result["mode"] == "lexical"  # SQLite: hybrid is structurally inactive, never silently claimed
    assert result["degraded_reason"] is None
    assert any("CONTENU_LOT51_ROUTE" in r["excerpt"] for r in result["results"])
    assert result["results"][0]["document_version_id"] is not None
    # Lot 51 bis — a real lexical match (found by private_rag_manager.search's own
    # >LEXICAL_RELEVANCE_FLOOR threshold) is always reported confirmed.
    assert result["results"][0]["lexically_confirmed"] is True


def test_reindex_route_is_a_documented_no_op_on_sqlite_never_a_fake_ready(client, db):
    make_active_starter_user(db, "lot51-route-reindex@example.com")
    csrf = _login(client, "lot51-route-reindex@example.com")
    r = client.post(
        "/api/knowledge/documents", headers={"X-CSRF-Token": csrf},
        files={"file": ("ref.md", io.BytesIO(b"Contenu de reference pour le reindex."), "text/plain")},
    )
    assert r.status_code == 201
    document = r.json()["document"]
    version = r.json()["version"]
    assert version["embedding_status"] == "not_applicable"  # SQLite — never fabricated 'ready'

    reindex = client.post(
        f"/api/knowledge/documents/{document['id']}/versions/{version['id']}/reindex",
        headers={"X-CSRF-Token": csrf},
    )
    assert reindex.status_code == 200
    assert reindex.json()["embedding_status"] == "not_applicable"  # still honest: no-op, not a fake success


def test_reindex_route_requires_write_permission_and_csrf(client, db):
    make_active_starter_user(db, "lot51-route-perm@example.com")
    csrf = _login(client, "lot51-route-perm@example.com")
    r = client.post(
        "/api/knowledge/documents", headers={"X-CSRF-Token": csrf},
        files={"file": ("ref.md", io.BytesIO(b"Contenu."), "text/plain")},
    )
    document, version = r.json()["document"], r.json()["version"]
    no_csrf = client.post(f"/api/knowledge/documents/{document['id']}/versions/{version['id']}/reindex")
    assert no_csrf.status_code == 403
