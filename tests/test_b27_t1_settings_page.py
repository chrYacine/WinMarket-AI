"""B27-T1 — /app/parametres: the settings screen for ProviderProfile +
ScoringPolicy that previously did not exist at all (only the JSON API
did). Without it, a brand-new real account could never get past
/api/analyze's own existing 409 SCORING_NOT_CONFIGURED gate through the
web UI. These are page-rendering smoke tests (Jinja renders without
error, the right state is shown) — the actual save/validate/activate
flow is already covered end-to-end by tests/test_b06_scoring_config.py
against the JSON API this page's JS calls verbatim.
"""
from __future__ import annotations

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


def test_settings_page_renders_a_configurer_state_for_a_fresh_account(client, db):
    make_active_starter_user(db, "freshsettings@example.com", scoring=False)
    _login(client, "freshsettings@example.com")
    r = client.get("/app/parametres")
    assert r.status_code == 200
    assert "À configurer" in r.text
    assert "Paramètres de scoring" in r.text
    # The nav link now exists on every /app/* page, not only this one.
    assert '/app/parametres' in r.text


def test_settings_page_renders_activated_state_when_policy_is_active(client, db):
    make_active_starter_user(db, "activesettings@example.com", scoring=True)
    _login(client, "activesettings@example.com")
    r = client.get("/app/parametres")
    assert r.status_code == 200
    assert "Politique activée" in r.text
    assert "Politique actuellement activée" in r.text


def test_settings_link_present_in_sidebar_on_other_app_pages(client, db):
    make_active_starter_user(db, "navsettings@example.com", scoring=False)
    _login(client, "navsettings@example.com")
    r = client.get("/app/analyser")
    assert r.status_code == 200
    assert "/app/parametres" in r.text
