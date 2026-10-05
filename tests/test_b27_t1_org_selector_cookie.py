"""B27-T1 — organization switcher: the `wm_org_id` cookie set by the
sidebar's organization selector lets a multi-organization account's choice
survive normal navigation without every page/link carrying an explicit
`?organization_id=` query parameter.

B22-T2 made every /app/* page and /api/history resolve a single, real
AccessContext (src.web.auth.access_context.get_access_context) instead of
implicitly aggregating every organization a user belongs to — a direct
side effect is that a multi-organization account got a 409 (ambiguous
selection) on every one of those pages, since none of them previously
selected an organization at all. The cookie closes that gap: it is a
REMEMBERED convenience only, never a grant by itself — a stale, foreign or
revoked value is silently ignored, never trusted more than the query
param already is.
"""
from __future__ import annotations

import re

from src.web.database.repositories import memberships as memberships_repo
from src.web.database.repositories import organizations as organizations_repo
from tests.conftest import default_org_id, make_active_starter_user


def _csrf_from(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


def _login(client, email, password="Sup3rSecret!"):
    r = client.get("/login")
    csrf = _csrf_from(r.text)
    client.post("/login", data={"email": email, "password": password, "next": "/app", "csrf_token": csrf})
    return csrf


def _make_two_org_user(db, email):
    user = make_active_starter_user(db, email, scoring=False)
    org_a = default_org_id(db, user)
    org_b = organizations_repo.create_organization(db, name=f"Deuxième organisation de {email}")
    memberships_repo.create_membership(db, user_id=user.id, organization_id=org_b.id, role="organization_admin")
    db.commit()
    return user, org_a, org_b.id


def test_no_selection_at_all_is_still_a_safe_409_never_a_silent_aggregate(client, db):
    user, org_a, org_b = _make_two_org_user(db, "cookienone@example.com")
    _login(client, "cookienone@example.com")
    r = client.get("/api/history")
    assert r.status_code == 409


def test_cookie_selection_resolves_without_any_query_param(client, db):
    user, org_a, org_b = _make_two_org_user(db, "cookieselect@example.com")
    _login(client, "cookieselect@example.com")
    r = client.get("/api/history", cookies={"wm_org_id": str(org_b)})
    assert r.status_code == 200


def test_stale_or_foreign_cookie_is_ignored_never_trusted(client, db):
    """A cookie naming an organization this account does NOT belong to
    (e.g. left over from a previous account on the same browser) must never
    grant access, and must never itself cause a hard failure — it is
    treated exactly as if absent, falling through to the normal ambiguous
    (409) rule for a two-organization account."""
    user, org_a, org_b = _make_two_org_user(db, "cookiestale@example.com")
    foreign_org = organizations_repo.create_organization(db, name="Organisation étrangère")
    db.commit()
    _login(client, "cookiestale@example.com")
    r = client.get("/api/history", cookies={"wm_org_id": str(foreign_org.id)})
    assert r.status_code == 409, "a foreign cookie must never grant access nor silently pick an organization"


def test_explicit_query_param_still_overrides_and_still_hard_refuses_unauthorized(client, db):
    """An explicit ?organization_id= is a deliberate selection, unlike the
    cookie's passive remembering — an unauthorized one is still a hard 403,
    never silently downgraded to the ambiguous-selection 409."""
    user, org_a, org_b = _make_two_org_user(db, "cookieoverride@example.com")
    foreign_org = organizations_repo.create_organization(db, name="Organisation étrangère 2")
    db.commit()
    _login(client, "cookieoverride@example.com")

    # Cookie says B, but an explicit query param for A must win.
    r = client.get(f"/api/history?organization_id={org_a}", cookies={"wm_org_id": str(org_b)})
    assert r.status_code == 200

    # An explicit, unauthorized query param is a hard refusal even though a
    # valid cookie is also present.
    r_bad = client.get(f"/api/history?organization_id={foreign_org.id}", cookies={"wm_org_id": str(org_a)})
    assert r_bad.status_code == 403


def test_single_membership_account_is_unaffected_by_any_cookie(client, db):
    """The overwhelming majority of accounts belong to exactly one
    organization — a stray/garbage cookie must never break them."""
    user = make_active_starter_user(db, "cookiesingle@example.com", scoring=False)
    _login(client, "cookiesingle@example.com")
    r = client.get("/api/history", cookies={"wm_org_id": "not-a-uuid"})
    assert r.status_code == 200
