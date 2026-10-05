"""B27-T1 — /app/historique no longer embeds any history rows server-side
(the pre-B22-T1, 200-row-capped `window.WM_HISTORY` dump is gone) — the
page renders unconditionally and static/js/history.js fetches the real
paginated GET /api/history itself. This is a page-rendering smoke test;
the pagination endpoint's own correctness is already covered by
tests/test_b22_t1_history_export.py.
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


def test_history_page_renders_without_any_server_embedded_rows(client, db):
    make_active_starter_user(db, "historypage@example.com", scoring=False)
    _login(client, "historypage@example.com")
    r = client.get("/app/historique")
    assert r.status_code == 200
    assert "window.WM_HISTORY" not in r.text, "the old unbounded server-side dump must be gone"
    assert "history-pagination" in r.text
    assert "/api/history/export.csv" in r.text
