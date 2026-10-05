"""Lot 50 bis §2 — content classification for private knowledge base documents (référence, certification,
présentation, autre, indéterminé). Real routes/engine, LLM disabled in this test environment (no `.env`,
heuristic-only path exercised) — the LLM wiring itself is unit-tested with a stub in
`tests/test_lot50bis_agents_llm.py` and qualified for real separately (see the lot report).
"""
from __future__ import annotations

import io

from tests.conftest import make_active_starter_user
from tests.test_b03_private_knowledge import _login, _upload


def test_a_certification_document_is_proposed_that_category_never_verified(client, db):
    make_active_starter_user(db, "l50bis-k1@example.com", scoring=False)
    csrf = _login(client, "l50bis-k1@example.com")
    content = b"Certification ISO 9001 obtenue par notre entreprise en 2023, valable jusqu'en 2026. Qualification reconnue."
    r = _upload(client, "certif.txt", content, csrf=csrf)
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    assert version["content_category_proposed"] == "certification", version
    assert version["content_category_final"] == "certification"
    assert version["content_category_source"] in ("heuristic", "heuristic_llm_unavailable")
    assert version["content_category_reason"]


def test_an_ambiguous_document_is_indetermine_never_a_guess(client, db):
    make_active_starter_user(db, "l50bis-k2@example.com", scoring=False)
    csrf = _login(client, "l50bis-k2@example.com")
    r = _upload(client, "notes.txt", b"Quelques notes diverses sans rapport avec un type reconnu de document.", csrf=csrf)
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    assert version["content_category_proposed"] is None
    assert version["content_category_final"] is None


def test_classification_never_blocks_a_successful_upload_even_if_the_classifier_raises(client, db, monkeypatch):
    from src.agents.knowledge_content_classifier import KnowledgeContentClassifierAgent

    def boom(self, *a, **k):
        raise RuntimeError("simulated classifier bug")

    monkeypatch.setattr(KnowledgeContentClassifierAgent, "classify", boom)
    make_active_starter_user(db, "l50bis-k3@example.com", scoring=False)
    csrf = _login(client, "l50bis-k3@example.com")
    r = _upload(client, "ref.txt", "Référence client : mission de nettoyage pour la Mairie de Lyon en 2024.".encode("utf-8"), csrf=csrf)
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    assert version["status"] == "ready", "a classifier bug must never take down an otherwise-successful upload"
    assert version["content_category_proposed"] is None  # column default, classification simply never ran


def test_a_user_correction_is_traceable_and_never_reruns_classification(client, db):
    make_active_starter_user(db, "l50bis-k4@example.com", scoring=False)
    csrf = _login(client, "l50bis-k4@example.com")
    r = _upload(client, "plaquette.txt", "Notes diverses sans vocabulaire caractéristique de type connu.".encode("utf-8"), csrf=csrf)
    doc_id, version_id = r.json()["document"]["id"], r.json()["version"]["id"]
    assert r.json()["version"]["content_category_final"] is None

    r2 = client.post(
        f"/api/knowledge/documents/{doc_id}/versions/{version_id}/category",
        json={"category": "presentation"}, headers={"X-CSRF-Token": csrf},
    )
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["content_category_final"] == "presentation"
    assert body["content_category_source"] == "user"
    assert body["content_category_reason"]

    # re-reading the document shows the same corrected, traceable state — not re-run, not re-guessed
    detail = client.get(f"/api/knowledge/documents/{doc_id}").json()
    v = next(v for v in detail["versions"] if v["id"] == version_id)
    assert v["content_category_final"] == "presentation" and v["content_category_source"] == "user"


def test_correcting_with_an_unknown_category_is_refused(client, db):
    make_active_starter_user(db, "l50bis-k5@example.com", scoring=False)
    csrf = _login(client, "l50bis-k5@example.com")
    r = _upload(client, "doc.txt", b"Contenu quelconque pour ce test.", csrf=csrf)
    doc_id, version_id = r.json()["document"]["id"], r.json()["version"]["id"]
    r2 = client.post(
        f"/api/knowledge/documents/{doc_id}/versions/{version_id}/category",
        json={"category": "not_a_real_category"}, headers={"X-CSRF-Token": csrf},
    )
    assert r2.status_code == 422


def test_correction_requires_csrf(client, db):
    make_active_starter_user(db, "l50bis-k6@example.com", scoring=False)
    csrf = _login(client, "l50bis-k6@example.com")
    r = _upload(client, "doc.txt", b"Contenu quelconque.", csrf=csrf)
    doc_id, version_id = r.json()["document"]["id"], r.json()["version"]["id"]
    r2 = client.post(f"/api/knowledge/documents/{doc_id}/versions/{version_id}/category", json={"category": "reference"})
    assert r2.status_code in (401, 403)


def test_another_account_cannot_correct_a_foreign_documents_category(client, db):
    make_active_starter_user(db, "l50bis-k7@example.com", scoring=False)
    csrf = _login(client, "l50bis-k7@example.com")
    r = _upload(client, "doc.txt", b"Contenu quelconque appartenant au premier compte.", csrf=csrf)
    doc_id, version_id = r.json()["document"]["id"], r.json()["version"]["id"]

    make_active_starter_user(db, "l50bis-k8@example.com", scoring=False)
    csrf2 = _login(client, "l50bis-k8@example.com")
    r2 = client.post(
        f"/api/knowledge/documents/{doc_id}/versions/{version_id}/category",
        json={"category": "reference"}, headers={"X-CSRF-Token": csrf2},
    )
    assert r2.status_code == 404
