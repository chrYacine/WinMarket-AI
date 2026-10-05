"""B14-T1 — CSRF verification + generalized rate limiting.

Covers:
  1. A valid, session-linked CSRF token succeeds on one route from each of
     the three route files this ticket touched (routes_api.py,
     routes_knowledge_documents.py, routes_scoring_policy.py).
  2. A missing/mismatched CSRF token is refused 403 on each of those same
     routes BEFORE any business write happens (proven by monkeypatching the
     underlying repository/service call to raise if invoked at all).
  3. "Two tabs": a token minted at one page load is still valid for a later
     request after a SECOND page load independently rendered (and reused)
     the same token.
  4. Rate limiting: exceeding the configured threshold for a real action
     (register) returns a real 429 with Retry-After, and advancing the
     injectable clock (never a real time.sleep()) past the window lets a
     subsequent attempt succeed again — all through the real app via
     TestClient, never by calling rate_limit.py's functions directly.
  5. The shared in-memory counter increments consistently across repeated
     SEQUENTIAL real HTTP calls to the same (action, key) — this proves
     sequential correctness only; see rate_limit.py's own docstring for why
     this is NOT evidence of multi-process/concurrent correctness, and this
     file makes no such claim.
  6. No GET route this ticket touched performs a mutation.

test_b13_t2_middleware_wiring.py (413 wiring) and test_saas_auth.py are run
separately as a regression check per this ticket's own instructions — not
duplicated here.
"""
from __future__ import annotations

import re

import pytest

from src.core import config
from tests.conftest import make_active_starter_user


def _csrf_from(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "csrf token not found in rendered page"
    return match.group(1)


def _login(client, email, password="Sup3rSecret!"):
    """Logs in and returns the CSRF token embedded on the login page — the
    SAME raw token is still valid afterward for X-CSRF-Token headers on
    JSON/multipart routes (get_or_create_csrf_token reuses the cookie's
    token; see src/web/security/csrf.py's module docstring)."""
    r = client.get("/login")
    csrf = _csrf_from(r.text)
    resp = client.post("/login", data={"email": email, "password": password, "next": "/app", "csrf_token": csrf})
    assert resp.status_code in (200, 303), f"login failed: {resp.status_code} {resp.text[:300]!r}"
    return csrf


def _register_once(client, email, password="Sup3rSecret!"):
    r = client.get("/register")
    csrf = _csrf_from(r.text)
    return client.post("/register", data={
        "first_name": "Ada", "last_name": "Lovelace", "email": email,
        "company": "", "job_title": "", "phone": "",
        "password": password, "password_confirm": password,
        "accept_terms": "1", "csrf_token": csrf,
    })


# ─────────────────────────────────────────────────────────────────────────
# 1 & 2. CSRF: valid token succeeds, missing/mismatched token is refused
#        BEFORE any business write, on one route per touched file.
# ─────────────────────────────────────────────────────────────────────────

def test_capacity_route_valid_csrf_succeeds(client, db):
    """routes_api.py: POST /api/capacity."""
    make_active_starter_user(db, "csrf-capacity-ok@example.com", capacity=False)
    csrf = _login(client, "csrf-capacity-ok@example.com")

    resp = client.post(
        "/api/capacity",
        json={
            "charge_globale_pct": 40, "nombre_projets_en_cours": 1,
            "projets_en_cours": ["Projet test"], "capacites_par_pole": {"Software Engineering": 40},
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200
    assert resp.json()["charge_globale_pct"] == 40


def test_capacity_route_missing_csrf_refused_before_write(client, db, monkeypatch):
    from src.web.database.repositories import private_capacity as private_capacity_repo

    make_active_starter_user(db, "csrf-capacity-missing@example.com", capacity=False)
    _login(client, "csrf-capacity-missing@example.com")

    def _must_not_be_called(*_a, **_kw):
        raise AssertionError("private_capacity_repo.save_for_owner must not run without a valid CSRF token")

    monkeypatch.setattr(private_capacity_repo, "save_for_owner", _must_not_be_called)

    resp = client.post("/api/capacity", json={
        "charge_globale_pct": 40, "nombre_projets_en_cours": 1,
        "projets_en_cours": [], "capacites_par_pole": {},
    })  # no X-CSRF-Token header at all
    assert resp.status_code == 403


def test_capacity_route_mismatched_csrf_refused_before_write(client, db, monkeypatch):
    from src.web.database.repositories import private_capacity as private_capacity_repo

    make_active_starter_user(db, "csrf-capacity-mismatch@example.com", capacity=False)
    _login(client, "csrf-capacity-mismatch@example.com")

    def _must_not_be_called(*_a, **_kw):
        raise AssertionError("private_capacity_repo.save_for_owner must not run with a wrong CSRF token")

    monkeypatch.setattr(private_capacity_repo, "save_for_owner", _must_not_be_called)

    resp = client.post(
        "/api/capacity",
        json={"charge_globale_pct": 40, "nombre_projets_en_cours": 1, "projets_en_cours": [], "capacites_par_pole": {}},
        headers={"X-CSRF-Token": "not-the-real-token"},
    )
    assert resp.status_code == 403


def test_knowledge_upload_route_valid_csrf_succeeds(client, db):
    """routes_knowledge_documents.py: POST /api/knowledge/documents."""
    make_active_starter_user(db, "csrf-upload-ok@example.com")
    csrf = _login(client, "csrf-upload-ok@example.com")

    resp = client.post(
        "/api/knowledge/documents",
        files={"file": ("reference.md", b"# Reference\n\nSome reusable project reference content.", "text/markdown")},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["document"]["original_filename"] == "reference.md"


def test_knowledge_upload_route_missing_csrf_refused_before_write(client, db, monkeypatch):
    from src.web.knowledge import documents_service

    make_active_starter_user(db, "csrf-upload-missing@example.com")
    _login(client, "csrf-upload-missing@example.com")

    def _must_not_be_called(*_a, **_kw):
        raise AssertionError("documents_service.read_upload_with_limit must not run without a valid CSRF token")

    monkeypatch.setattr(documents_service, "read_upload_with_limit", _must_not_be_called)

    resp = client.post(
        "/api/knowledge/documents",
        files={"file": ("reference.md", b"# Reference\n\nContent.", "text/markdown")},
    )  # no header
    assert resp.status_code == 403


def test_knowledge_upload_route_mismatched_csrf_refused_before_write(client, db, monkeypatch):
    from src.web.knowledge import documents_service

    make_active_starter_user(db, "csrf-upload-mismatch@example.com")
    _login(client, "csrf-upload-mismatch@example.com")

    def _must_not_be_called(*_a, **_kw):
        raise AssertionError("documents_service.read_upload_with_limit must not run with a wrong CSRF token")

    monkeypatch.setattr(documents_service, "read_upload_with_limit", _must_not_be_called)

    resp = client.post(
        "/api/knowledge/documents",
        files={"file": ("reference.md", b"# Reference\n\nContent.", "text/markdown")},
        headers={"X-CSRF-Token": "garbage"},
    )
    assert resp.status_code == 403


def test_scoring_profile_route_valid_csrf_succeeds(client, db):
    """routes_scoring_policy.py: PUT /api/scoring-config/profile."""
    make_active_starter_user(db, "csrf-profile-ok@example.com", scoring=False)
    csrf = _login(client, "csrf-profile-ok@example.com")

    resp = client.put(
        "/api/scoring-config/profile",
        json={"raison_sociale": "ACME ESN", "effectif": "50", "competences": ["Python"], "certifications": []},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["raison_sociale"] == "ACME ESN"


def test_scoring_profile_route_missing_csrf_refused_before_write(client, db, monkeypatch):
    from src.web.database.repositories import provider_profile as provider_profile_repo

    make_active_starter_user(db, "csrf-profile-missing@example.com", scoring=False)
    _login(client, "csrf-profile-missing@example.com")

    def _must_not_be_called(*_a, **_kw):
        raise AssertionError("provider_profile_repo.save_for_owner must not run without a valid CSRF token")

    monkeypatch.setattr(provider_profile_repo, "save_for_owner", _must_not_be_called)

    resp = client.put("/api/scoring-config/profile", json={"raison_sociale": "ACME ESN"})  # no header
    assert resp.status_code == 403


def test_scoring_profile_route_mismatched_csrf_refused_before_write(client, db, monkeypatch):
    from src.web.database.repositories import provider_profile as provider_profile_repo

    make_active_starter_user(db, "csrf-profile-mismatch@example.com", scoring=False)
    _login(client, "csrf-profile-mismatch@example.com")

    def _must_not_be_called(*_a, **_kw):
        raise AssertionError("provider_profile_repo.save_for_owner must not run with a wrong CSRF token")

    monkeypatch.setattr(provider_profile_repo, "save_for_owner", _must_not_be_called)

    resp = client.put(
        "/api/scoring-config/profile",
        json={"raison_sociale": "ACME ESN"},
        headers={"X-CSRF-Token": "wrong-token"},
    )
    assert resp.status_code == 403


# ─────────────────────────────────────────────────────────────────────────
# 3. "Two tabs": token minted at one page load is still valid for a later
#    request made after a second, independent page load reused it.
# ─────────────────────────────────────────────────────────────────────────

def test_two_tabs_same_csrf_token_reused_and_still_valid(client, db):
    make_active_starter_user(db, "twotabs@example.com", capacity=False)
    _login(client, "twotabs@example.com")

    # "Tab 1"
    r1 = client.get("/app/analyser")
    assert r1.status_code == 200
    token_tab1 = _csrf_from(r1.text)

    # "Tab 2" — a second, later render of a form-bearing page in the same
    # session must reuse the SAME token, not silently mint/overwrite a new
    # one (see get_or_create_csrf_token's docstring on why that matters).
    r2 = client.get("/app/analyser")
    assert r2.status_code == 200
    token_tab2 = _csrf_from(r2.text)

    assert token_tab1 == token_tab2

    # A request made using "tab 1"'s token, after "tab 2" was also opened,
    # still succeeds — the second render never invalidated the first.
    resp = client.post(
        "/api/capacity",
        json={"charge_globale_pct": 10, "nombre_projets_en_cours": 0, "projets_en_cours": [], "capacites_par_pole": {}},
        headers={"X-CSRF-Token": token_tab1},
    )
    assert resp.status_code == 200


# ─────────────────────────────────────────────────────────────────────────
# 4. Rate limiting: real 429 + Retry-After, then recovery after the
#    injectable clock advances past the window — all through TestClient.
# ─────────────────────────────────────────────────────────────────────────

def test_register_rate_limit_429_then_recovers_after_clock_advances(client, monkeypatch):
    from src.web.security import rate_limit

    fake_now = [1_700_000_000.0]
    monkeypatch.setattr(rate_limit, "_clock", lambda: fake_now[0])

    # Exhaust the configured threshold with distinct emails (a duplicate
    # email would fail registration for an unrelated reason and muddy the
    # count) — this is the real HTTP path, not a direct check_and_record()
    # call.
    for i in range(config.RATE_LIMIT_REGISTER_MAX_ATTEMPTS):
        resp = _register_once(client, f"ratelimit-register-{i}@example.com")
        assert resp.status_code == 200, f"attempt {i} unexpectedly blocked: {resp.status_code} {resp.text[:200]!r}"

    # One more, same window: real 429 + Retry-After.
    over_resp = _register_once(client, "ratelimit-register-overflow@example.com")
    assert over_resp.status_code == 429, over_resp.text
    retry_after = int(over_resp.headers["Retry-After"])
    assert retry_after > 0

    # Advance the injectable clock past the window — no real time.sleep().
    fake_now[0] += config.RATE_LIMIT_REGISTER_WINDOW_SECONDS + 1

    after_resp = _register_once(client, "ratelimit-register-after-window@example.com")
    assert after_resp.status_code == 200, after_resp.text


# ─────────────────────────────────────────────────────────────────────────
# 5. Shared counter: sequential real HTTP calls to the same (action, key)
#    observe a consistent, correctly-incremented count. SEQUENTIAL ONLY —
#    this does not and cannot prove concurrent/multi-process correctness
#    (see rate_limit.py's own docstring on that honest scope limitation).
# ─────────────────────────────────────────────────────────────────────────

def test_login_rate_limit_counter_increments_consistently_across_sequential_calls(client, db):
    make_active_starter_user(db, "counter@example.com")

    def _bad_login_attempt():
        r = client.get("/login")
        csrf = _csrf_from(r.text)
        return client.post("/login", data={
            "email": "counter@example.com", "password": "wrong-password", "next": "/app", "csrf_token": csrf,
        })

    # Exactly RATE_LIMIT_LOGIN_MAX_ATTEMPTS failed attempts must each still
    # be treated as "not yet limited" (same 200 + generic wrong-password
    # copy) — proving the counter is being read/incremented correctly on
    # every single sequential call, not just at the boundary.
    for i in range(config.RATE_LIMIT_LOGIN_MAX_ATTEMPTS):
        resp = _bad_login_attempt()
        assert resp.status_code == 200
        assert "incorrect" in resp.text, f"attempt {i} unexpectedly already rate-limited"

    # The next one crosses the threshold.
    limited_resp = _bad_login_attempt()
    assert "Trop de tentatives" in limited_resp.text


# ─────────────────────────────────────────────────────────────────────────
# 6. No GET route this ticket touched performs a mutation.
# ─────────────────────────────────────────────────────────────────────────

def test_get_capacity_never_writes(client, db, monkeypatch):
    from src.web.database.repositories import private_capacity as private_capacity_repo

    make_active_starter_user(db, "get-no-mutation@example.com", capacity=False)
    _login(client, "get-no-mutation@example.com")

    def _must_not_be_called(*_a, **_kw):
        raise AssertionError("GET /api/capacity must never write")

    monkeypatch.setattr(private_capacity_repo, "save_for_owner", _must_not_be_called)

    resp = client.get("/api/capacity")
    assert resp.status_code == 200
    assert resp.json()["status"] == "unconfigured"


def test_get_knowledge_documents_list_never_writes(client, db, monkeypatch):
    from src.web.knowledge import documents_service

    make_active_starter_user(db, "get-no-mutation-2@example.com")
    _login(client, "get-no-mutation-2@example.com")

    def _must_not_be_called(*_a, **_kw):
        raise AssertionError("GET /api/knowledge/documents must never write")

    monkeypatch.setattr(documents_service, "upload_document", _must_not_be_called)
    monkeypatch.setattr(documents_service, "add_version", _must_not_be_called)
    monkeypatch.setattr(documents_service, "delete_document", _must_not_be_called)

    resp = client.get("/api/knowledge/documents")
    assert resp.status_code == 200
    assert resp.json()["documents"] == []
