"""Lot 41 — B27-T2 raccord: server-side guarantees the /app/parametres
screen relies on (the browser recette itself is a separate, manual proof).
"""
from __future__ import annotations

import re
from pathlib import Path

from src.web.database.repositories import memberships as memberships_repo
from src.web.database.repositories import organizations as organizations_repo
from tests.conftest import make_active_starter_user

ROOT = Path(__file__).resolve().parents[1]


def _login(client, email):
    page = client.get("/login")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    client.post("/login", data={"email": email, "password": "Sup3rSecret!", "next": "/app", "csrf_token": csrf})
    return csrf


def test_catalogue_guides_the_form_without_becoming_an_authority(client, db):
    make_active_starter_user(db, "cat41@example.com", scoring=False)
    _login(client, "cat41@example.com")
    catalogue = client.get("/api/scoring-config").json()["business_facts_catalogue"]
    assert catalogue["operator_fact_types"]["list_coverage"] == ["list"]
    assert catalogue["operator_fact_types"]["numeric_threshold"] == ["number"]
    assert catalogue["unit_fact_types"] == ["number"]
    page = client.get("/app/parametres")
    assert page.status_code == 200 and "WM_CATALOGUE" in page.text and "WM_ORGANIZATION_ID" in page.text


def test_blank_account_gets_no_prefilled_business_configuration(client, db):
    make_active_starter_user(db, "blank41@example.com", scoring=False)
    _login(client, "blank41@example.com")
    body = client.get("/api/scoring-config").json()
    assert body["profile"]["business_facts"] == {}
    assert body["policy"] == {"active": None, "draft": None}
    page = client.get("/app/parametres").text
    assert "window.WM_DRAFT_POLICY = null;" in page and "window.WM_ACTIVE_POLICY = null;" in page


def test_multi_organization_account_without_selection_gets_a_chooser_not_a_dead_end(client, db):
    user = make_active_starter_user(db, "multi41@example.com", scoring=False)
    second = organizations_repo.create_organization(db, name="Seconde organisation 41")
    memberships_repo.create_membership(db, user_id=user.id, organization_id=second.id, role="organization_admin")
    db.commit()
    _login(client, "multi41@example.com")

    chooser = client.get("/app/parametres")
    assert chooser.status_code == 409
    assert chooser.text.count("data-org-choice") == 2
    assert "Seconde organisation 41" in chooser.text

    # a selection is only ever a choice among the caller's own memberships
    ok = client.get("/app/parametres", cookies={"wm_org_id": str(second.id)})
    assert ok.status_code == 200
    foreign = organizations_repo.create_organization(db, name="Organisation étrangère 41")
    db.commit()
    assert client.get(f"/app/parametres?organization_id={foreign.id}").status_code == 403


def test_the_settings_script_never_writes_server_text_as_html():
    """Server messages embed user-supplied identifiers/labels; the screen
    must only ever use textContent/DOM nodes (a stored XSS otherwise)."""
    script = (ROOT / "static" / "js" / "parametres.js").read_text(encoding="utf-8")
    code = "\n".join(line for line in script.splitlines() if not line.strip().startswith("//"))
    for sink in (".innerHTML", ".outerHTML", "insertAdjacentHTML(", "document.write(", "eval(", "new Function("):
        assert sink not in code, f"unsafe sink {sink!r} in parametres.js"
