"""Coordinator integration test: main.py's actual wiring of
src/web/body_limit_middleware.py::BodySizeLimitMiddleware, exercised over a
real HTTP request through the full `main.app` (not a hand-built ASGI scope
like tests/test_b13_t2_reception_bounds.py's unit-level tests use).

DEFECT confirmed and fixed during independent review: the first integration
attempt registered this middleware AFTER (outermost relative to)
`inject_current_user_state`'s `@app.middleware("http")`, reasoning that
"outermost sees raw ASGI bytes first" — true, but not the property that
matters here. Starlette's BaseHTTPMiddleware relays the request body
through its own internal `anyio.create_task_group()`-based receive proxy;
an exception raised while being awaited FROM WITHIN that task-group context
gets wrapped into an ExceptionGroup by anyio on the way out, which no
longer matches FastAPI's `except HTTPException: raise`
(fastapi/routing.py) and silently degrades into a generic
`400 {"detail": "There was an error parsing the body"}` instead of the
documented 413/error_code. Fixed by (a) making RequestBodyTooLargeError an
HTTPException subclass itself, carrying its own status_code/detail, and
(b) registering the middleware INNER to (before, in main.py's source
order) inject_current_user_state's BaseHTTPMiddleware, so the exception is
raised outside any task-group context.

This file exists specifically so a future edit to main.py's middleware
registration order (an easy, innocent-looking reshuffle) is caught here
rather than silently reintroducing the defect — the unit-level tests in
test_b13_t2_reception_bounds.py cannot catch this class of regression since
they never go through main.app's real middleware stack.
"""
from __future__ import annotations

import re

from src.core import config
from tests.conftest import make_active_starter_user


def _login(client, email, password="Sup3rSecret!"):
    r = client.get("/login")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', r.text).group(1)
    client.post("/login", data={"email": email, "password": password, "next": "/app", "csrf_token": csrf})
    # B14-T1: /api/analyze and /api/capacity now require the same
    # double-submit CSRF token via the X-CSRF-Token header (see
    # src/web/security/csrf.py / docs/api/B14_T1_CSRF_RATE_LIMIT_CONTRACT.md).
    # The token minted for the login page's hidden field is still valid
    # afterward (get_or_create_csrf_token reuses the cookie's token) — return
    # it so callers below can attach it to their own mutating requests.
    return csrf


def test_oversized_upload_through_the_real_app_returns_413_with_error_code(client, db):
    make_active_starter_user(db, "wiring413@example.com", scoring=True)
    _login(client, "wiring413@example.com")

    # Lot 47 bis: /api/analyze is the AO-dossier route, so its ceiling is DOSSIER_MAX_TOTAL_BYTES + a bounded multipart
    # margin, no longer MAX_REQUEST_BODY_MB. The bytes are counted as they arrive, whatever the body looks like.
    ceiling = config.DOSSIER_MAX_TOTAL_BYTES + config.DOSSIER_MULTIPART_MARGIN_BYTES
    resp = client.post(
        "/api/analyze", content=b"A" * (ceiling + 1024),
        headers={"Content-Type": "multipart/form-data; boundary=wm"},
    )

    assert resp.status_code == 413, (
        f"expected 413 REQUEST_BODY_TOO_LARGE, got {resp.status_code}: {resp.text!r} — "
        "if this is 400 'There was an error parsing the body', the middleware ordering "
        "regression described in this file's module docstring has reappeared."
    )
    assert resp.json()["detail"]["error_code"] == "REQUEST_BODY_TOO_LARGE"


def test_oversized_body_via_scoring_config_simulate_also_returns_413(client, db):
    """Second guarded prefix, same real-app path — not just /api/analyze."""
    make_active_starter_user(db, "wiring413sim@example.com", scoring=True)
    _login(client, "wiring413sim@example.com")

    payload = b"A" * (config.MAX_REQUEST_BODY_MB * 1024 * 1024 + 1024)
    resp = client.post(
        "/api/scoring-config/simulate",
        data={"mode": "upload"},
        files={"file": ("ao.txt", payload, "text/plain")},
    )
    assert resp.status_code == 413
    assert resp.json()["detail"]["error_code"] == "REQUEST_BODY_TOO_LARGE"


def test_normal_sized_request_is_unaffected_by_the_middleware_ordering(client, db, monkeypatch, tmp_path):
    """Guards against a fix that stops the 400 by breaking the happy path
    instead (e.g. accidentally short-circuiting inject_current_user_state)."""
    from src.rag import private_rag_manager
    from src.web import jobs

    monkeypatch.setattr(jobs, "ANALYSES_DIR", tmp_path / "analyses")
    monkeypatch.setattr(jobs, "ANALYSIS_FILES_DIR", tmp_path / "outputs")
    monkeypatch.setattr(private_rag_manager, "search", lambda db, **kw: [])

    make_active_starter_user(db, "wiringnormal@example.com", scoring=True)
    csrf = _login(client, "wiringnormal@example.com")
    client.post("/api/capacity", json={
        "charge_globale_pct": 40, "nombre_projets_en_cours": 1,
        "projets_en_cours": ["Projet test"], "capacites_par_pole": {"Software Engineering": 40},
    }, headers={"X-CSRF-Token": csrf})

    resp = client.post("/api/analyze", data={
        "mode": "paste",
        "text": "Appel d'offres test. Acheteur: X. Budget: 200000 euros. Date limite: 30/11/2026. Python Django.",
    }, headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 200
    assert "job_id" in resp.json()
