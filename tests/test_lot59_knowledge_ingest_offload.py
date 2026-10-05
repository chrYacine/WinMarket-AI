"""Lot 59 — knowledge ingestion no longer blocks the event loop.

Observed on the Render demo: `POST /api/knowledge/documents` ran extraction + local embeddings inline in an
`async def` route, so Uvicorn's single event loop answered nothing else for ~15 s per file, /healthz timed out
and the instance was restarted mid-import. Every test below synchronises explicitly (threading.Event, the
limiter's own statistics) — no sleep-based timing.
"""
from __future__ import annotations

import io
import re
import threading
import time
from pathlib import Path

import pytest

from src.web import routes_knowledge_documents as routes
from src.web.knowledge import documents_service
from tests.conftest import make_active_starter_user

ROOT = Path(__file__).resolve().parents[1]
WAIT = 20  # generous upper bound for an explicit event; a passing run never waits this long


def _login(client, email, password="Sup3rSecret!"):
    page = client.get("/login").text
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
    client.post("/login", data={"email": email, "password": password, "next": "/app", "csrf_token": csrf})
    return csrf


def _upload(client, csrf, name, content):
    return client.post("/api/knowledge/documents", headers={"X-CSRF-Token": csrf},
                       files={"file": (name, io.BytesIO(content), "text/plain")})


def _in_thread(fn):
    box = {}
    thread = threading.Thread(target=lambda: box.update(response=fn()), daemon=True)
    thread.start()
    return thread, box


def _wait_until(predicate, what):
    deadline = time.monotonic() + WAIT
    while not predicate():
        if time.monotonic() > deadline:
            pytest.fail(f"timed out waiting for {what}")
        time.sleep(0.01)


def _blocking_spy(monkeypatch, name):
    """Replaces documents_service.<name> by a version that stops inside the ingestion until released."""
    original = getattr(documents_service, name)
    state = {"calls": 0, "active": 0, "max_active": 0, "slots_seen": []}
    entered, release = threading.Event(), threading.Event()
    lock = threading.Lock()

    def spy(*args, **kwargs):
        with lock:
            state["calls"] += 1
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
            state["slots_seen"].append(routes.INGEST_LIMITER.borrowed_tokens)
        entered.set()
        try:
            assert release.wait(WAIT)
            return original(*args, **kwargs)
        finally:
            with lock:
                state["active"] -= 1

    monkeypatch.setattr(documents_service, name, spy)
    return state, entered, release


def test_healthz_answers_before_a_blocked_upload_finishes_and_success_follows_the_commit(client, db, monkeypatch):
    make_active_starter_user(db, "lot59-healthz@example.com")
    csrf = _login(client, "lot59-healthz@example.com")
    state, entered, release = _blocking_spy(monkeypatch, "upload_document")

    upload, upload_box = _in_thread(lambda: _upload(client, csrf, "ref.md", b"# Ref\n\nLOT59_HEALTHZ reference."))
    health = None
    try:
        assert entered.wait(WAIT), "the upload never reached the ingestion"
        health, health_box = _in_thread(lambda: client.get("/healthz"))
        health.join(5)
        assert not health.is_alive(), "/healthz did not answer while an upload was being ingested"
        assert health_box["response"].status_code == 200
        assert upload.is_alive(), "the upload must still be blocked: /healthz answered BEFORE its end"
        assert "response" not in upload_box
    finally:
        release.set()
        upload.join(WAIT)
        if health is not None:
            health.join(WAIT)

    response = upload_box["response"]
    assert response.status_code == 201
    assert state["slots_seen"] == [1]  # ran inside the ingestion limiter, i.e. in its worker thread
    # A 201 is only sent after the commit: the document is visible from a fresh request.
    listed = client.get("/api/knowledge/documents").json()["documents"]
    assert [d["id"] for d in listed] == [response.json()["document"]["id"]]


def test_concurrent_uploads_are_limited_and_a_waiting_one_does_not_block_the_loop(client, db, monkeypatch):
    assert routes.INGEST_LIMITER.total_tokens == 1
    make_active_starter_user(db, "lot59-limit@example.com")
    csrf = _login(client, "lot59-limit@example.com")
    state, entered, release = _blocking_spy(monkeypatch, "upload_document")

    first, first_box = _in_thread(lambda: _upload(client, csrf, "a.md", b"LOT59_A premier document."))
    second = None
    try:
        assert entered.wait(WAIT)
        second, second_box = _in_thread(lambda: _upload(client, csrf, "b.md", b"LOT59_B second document."))
        _wait_until(lambda: routes.INGEST_LIMITER.statistics().tasks_waiting == 1, "the second upload to queue")
        assert state["calls"] == 1, "a second embedding computation started while the first was running"
        assert client.get("/healthz").status_code == 200  # the queued request waits without blocking the loop
    finally:
        release.set()
        first.join(WAIT)
        if second is not None:
            second.join(WAIT)

    assert first_box["response"].status_code == 201
    assert second_box["response"].status_code == 201
    assert state["calls"] == 2 and state["max_active"] == 1
    assert routes.INGEST_LIMITER.borrowed_tokens == 0


def test_a_crashing_ingestion_releases_its_slot_and_commits_nothing(client, db, monkeypatch):
    make_active_starter_user(db, "lot59-crash@example.com")
    csrf = _login(client, "lot59-crash@example.com")
    original = documents_service.upload_document

    def crash(*args, **kwargs):
        original(*args, **kwargs)  # writes rows in the thread's Session, then fails before the commit
        raise RuntimeError("simulated ingestion crash")

    monkeypatch.setattr(documents_service, "upload_document", crash)
    with pytest.raises(RuntimeError, match="simulated ingestion crash"):
        _upload(client, csrf, "boom.md", b"LOT59_CRASH contenu.")
    assert routes.INGEST_LIMITER.borrowed_tokens == 0
    assert client.get("/api/knowledge/documents").json()["documents"] == []  # rolled back, never committed

    monkeypatch.setattr(documents_service, "upload_document", original)
    assert _upload(client, csrf, "ok.md", b"LOT59_AFTER_CRASH contenu.").status_code == 201


def test_version_replacement_runs_off_the_loop_and_a_failed_one_keeps_the_active_version(client, db, monkeypatch):
    make_active_starter_user(db, "lot59-version@example.com")
    csrf = _login(client, "lot59-version@example.com")
    created = _upload(client, csrf, "ref.md", b"LOT59_V1 premiere version.")
    assert created.status_code == 201
    doc_id = created.json()["document"]["id"]

    state, entered, release = _blocking_spy(monkeypatch, "add_version")
    replacement, replacement_box = _in_thread(lambda: client.post(
        f"/api/knowledge/documents/{doc_id}/versions", headers={"X-CSRF-Token": csrf},
        files={"file": ("ref.md", io.BytesIO(b"LOT59_V2 seconde version."), "text/plain")}))
    try:
        assert entered.wait(WAIT)
        assert client.get("/healthz").status_code == 200
        assert replacement.is_alive()
    finally:
        release.set()
        replacement.join(WAIT)
    assert replacement_box["response"].status_code == 200
    assert replacement_box["response"].json()["document"]["active_version_number"] == 2
    assert state["slots_seen"] == [1]

    # A replacement that fails extraction is recorded as failed, and version 2 stays the active one.
    failed = client.post(f"/api/knowledge/documents/{doc_id}/versions", headers={"X-CSRF-Token": csrf},
                         files={"file": ("ref.md", io.BytesIO(b"   \n\n   "), "text/plain")})
    assert failed.status_code == 422
    assert failed.json()["detail"]["version"]["status"] == "failed"
    assert failed.json()["detail"]["document"]["active_version_number"] == 2
    assert routes.INGEST_LIMITER.borrowed_tokens == 0
    detail = client.get(f"/api/knowledge/documents/{doc_id}").json()
    assert detail["active_version_number"] == 2
    assert [v["status"] for v in detail["versions"]] == ["ready", "ready", "failed"]
    found = client.get("/api/knowledge/search?q=LOT59_V2").json()["results"]
    assert any("LOT59_V2" in r["excerpt"] for r in found)

    # Owner scope is re-checked inside the ingestion: another account gets the same generic 404.
    make_active_starter_user(db, "lot59-version-other@example.com")
    client.cookies.clear()
    other_csrf = _login(client, "lot59-version-other@example.com")
    foreign = client.post(f"/api/knowledge/documents/{doc_id}/versions", headers={"X-CSRF-Token": other_csrf},
                          files={"file": ("ref.md", io.BytesIO(b"LOT59_FOREIGN."), "text/plain")})
    assert foreign.status_code == 404


def test_page_shows_partial_batches_distinctly_and_never_a_fake_zero(client, db):
    make_active_starter_user(db, "lot59-ui@example.com")
    _login(client, "lot59-ui@example.com")
    page = client.get("/app/base-connaissances").text
    assert re.search(r'id="knowledge-warning" class="callout callout-warning"', page)

    # No JavaScript runtime in this suite: the contract is checked on the script itself.
    script = (ROOT / "static" / "js" / "knowledge.js").read_text(encoding="utf-8")
    assert "if (anyOk) showFeedback" not in script  # one success no longer turns a mixed batch green
    assert 'const kind = counts.ok === total ? "success" : (counts.ok > 0 ? "partial" : "failure");' in script
    assert 'if (kind === "success") showFeedback(summary); else if (kind === "partial") showWarning(summary); else showError(summary);' in script
    for part in ('" réussi(s)"', '" échoué(s)"', '" à vérifier"', '" non envoyé(s)"'):
        assert part in script
    # A lost answer (502…) is "unknown", stops the batch and is never re-sent automatically.
    assert "if (r.networkError || r.status >= 500) {" in script
    assert "unknown: true, stop: true" in script
    assert script.count('api("POST", "/api/knowledge/documents"') == 1
    # An unavailable list or summary shows "Indisponible", never a stale or zero count.
    assert "if (!listAnswer.ok) { renderListFailure(listAnswer); markKpisUnavailable(); return; }" in script
    assert script.count('textContent = "Indisponible"') >= 5
