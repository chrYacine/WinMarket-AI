"""B02: organization-scoped access control, layered on top of B01's
user/subscription checks (src/web/auth/dependencies.py), which are
unchanged and still the billing gate.

AccessContext answers one question for a request that is about to create or
select a resource: "acting as which organization, with which role?" It is
NOT used to re-authorize reads of an already-existing, already-organization-
tagged resource (an analysis, a document) — those stay scoped by ownership
(user_id) via the repositories, per the product rule that an analysis is
private to its author even among organization-mates (see
src/web/database/repositories/analyses.py). For that case, use
`require_active_membership` below to confirm the acting user's membership in
*that specific resource's* organization hasn't been revoked since it was
created — that is what makes a revoked membership cut off access to a job
already in flight or already downloaded once.

Everything here re-reads role/status from PostgreSQL on every request —
never trusts a role, organization_id or user_id supplied by the client.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from src.core import config
from src.web.auth.dependencies import require_active_starter_user
from src.web.database.models import Membership, User
from src.web.database.repositories import memberships as memberships_repo
from src.web.database.repositories import organizations as organizations_repo
from src.web.database.session import get_db

# Permission matrix (ticket B02 section 3, extended by B06-T1 section 3).
# Deliberately flat and small: no per-resource-type overrides yet, no
# inheritance. organization_admin does NOT imply reading other members'
# private analyses, and does NOT imply any scoring/business override — see
# module docstring and src/web/database/repositories/analyses.py.
#
# B06-T1 additions — "capacity:configure" and "scoring:configure" — are
# deliberately granted to `analyst` as well as `organization_admin`, unlike
# every other write permission so far. This is safe ONLY because both
# permissions gate writes to resources that are *always* scoped to the
# CALLER's own (organization_id, owner_user_id) by the repository layer
# itself (src/web/database/repositories/private_capacity.py,
# scoring_policy.py, provider_profile.py — every save/activate function
# takes owner_user_id as an explicit parameter the route always fills with
# ctx.user.id, never a client-supplied value): an analyst configuring
# "their own" capacity/scoring can structurally never reach a colleague's
# row, so widening who may call these routes does not widen WHAT any
# individual call can touch. `capacity:configure` replaces `org:configure`
# on POST /api/capacity (previously organization_admin-only since B02-C1) —
# a narrowing of WHO must be admin to configure capacity was requested by
# ticket B06-T1 section 3 specifically because analysts otherwise couldn't
# complete their own private configuration; org:configure itself is
# unchanged and still gates genuinely organization-wide settings
# (org:manage_members's sibling).
ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "viewer": frozenset({"resource:read"}),
    "analyst": frozenset({
        "resource:read", "analysis:create", "knowledge:write", "capacity:configure", "scoring:configure",
    }),
    "organization_admin": frozenset({
        "resource:read", "analysis:create", "knowledge:write", "org:manage_members", "org:configure",
        "capacity:configure", "scoring:configure",
    }),
}


def has_permission(role: str, permission: str) -> bool:
    return permission in ROLE_PERMISSIONS.get(role, frozenset())


@dataclass(frozen=True)
class AccessContext:
    user: User
    organization_id: uuid.UUID
    membership_id: uuid.UUID
    role: str
    permissions: frozenset[str]

    def require(self, permission: str) -> None:
        if permission not in self.permissions:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Action non autorisée pour votre rôle.")


_ORG_SELECTION_COOKIE = "wm_org_id"


def get_access_context(
    request: Request,
    user: User = Depends(require_active_starter_user),
    db: Session = Depends(get_db),
    organization_id: uuid.UUID | None = Query(
        default=None,
        description="Only meaningful when the user belongs to more than one "
        "organization — selects which one to act as. Never a grant of "
        "access by itself: it is checked against the caller's own active "
        "memberships below, never trusted as-is.",
    ),
) -> AccessContext:
    """Resolve the organization + role a request acts as, straight from the
    database. With one active membership, it is used automatically. With
    several, the caller must pass `organization_id` explicitly (or have it
    remembered via the `wm_org_id` cookie — B27-T1's organization switcher
    sets it so the selection survives normal navigation without every link
    on every page needing to carry the query param) and it must match one
    of them — never silently picks the first one. With zero, the request is
    refused outright (no implicit global/default organization). A cookie
    naming an organization the caller no longer belongs to is silently
    ignored (falls through to the query param / single-membership / ambiguous
    rules below) — never trusted any more than the query param is.
    """
    memberships = memberships_repo.list_active_for_user(db, user.id)
    if not memberships:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Aucune organisation active pour ce compte.")

    # An explicit query-param selection is a deliberate request — a mismatch
    # is a hard refusal below. A cookie is only a REMEMBERED convenience
    # (set by B27-T1's organization switcher so the choice survives normal
    # navigation) — a stale/foreign/revoked value there must never block
    # access; it is silently dropped and resolution falls through to the
    # single-membership/ambiguous rules exactly as if no cookie existed.
    explicit_selection = organization_id is not None
    if not explicit_selection:
        cookie_value = request.cookies.get(_ORG_SELECTION_COOKIE)
        if cookie_value:
            try:
                candidate = uuid.UUID(cookie_value)
            except ValueError:
                candidate = None
            if candidate is not None and any(m.organization_id == candidate for m in memberships):
                organization_id = candidate

    selected: Membership
    if organization_id is not None:
        match = next((m for m in memberships if m.organization_id == organization_id), None)
        if match is None:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Organisation non autorisée pour ce compte.")
        selected = match
    elif len(memberships) == 1:
        selected = memberships[0]
    else:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Plusieurs organisations sont disponibles pour ce compte : précisez organization_id.",
        )

    org = organizations_repo.get_by_id(db, selected.organization_id)
    if org is None or org.status != "active":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Organisation indisponible.")

    from src.web.auth.manual_access import check_scope
    check_scope(db, user_id=user.id, organization_id=org.id)
    return AccessContext(
        user=user,
        organization_id=org.id,
        membership_id=selected.id,
        role=selected.role,
        permissions=ROLE_PERMISSIONS[selected.role],
    )


def require_permission(permission: str):
    """Dependency factory: `Depends(require_permission("analysis:create"))`."""

    def _dependency(ctx: AccessContext = Depends(get_access_context)) -> AccessContext:
        ctx.require(permission)
        return ctx

    return _dependency


def require_active_membership(
    db: Session, *, user_id: uuid.UUID, organization_id: uuid.UUID | None, not_found_detail: str = "Document introuvable."
) -> None:
    """Confirm the user's membership in a specific resource's organization
    is still active, right now. Used by routes that read a resource created
    earlier (a job, an analysis) — not the broad AccessContext resolution
    above, since reading your own past resource must not require selecting
    among current organizations, and must not succeed via a resource whose
    recorded organization_id is missing or no longer one you belong to.

    Raises HTTPException(404) — the same generic response as "not found" —
    on any of: no organization recorded (legacy/ambiguous), no membership,
    or a revoked/inactive membership. This is what makes a revoked
    membership cut off access to a job already in flight or a previously
    downloaded document's re-download, and what stops a legacy record with
    no verified organization from silently working via a fallback path.
    """
    if organization_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, not_found_detail)
    membership = memberships_repo.get_active(db, user_id=user_id, organization_id=organization_id)
    if membership is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, not_found_detail)
    org = organizations_repo.get_by_id(db, organization_id)
    if org is None or org.status != "active":
        raise HTTPException(status.HTTP_404_NOT_FOUND, not_found_detail)
    from src.web.auth.manual_access import check_scope
    check_scope(db, user_id=user_id, organization_id=organization_id)


def require_shared_corpus_access(
    ctx: AccessContext = Depends(get_access_context), db: Session = Depends(get_db)
) -> AccessContext:
    """B02/B03 boundary guard (ticket B02 section 6). The RAG corpus and
    capacity plan behind /api/analyze, /api/capacity and /api/knowledge* are
    still process-wide singletons — real per-organization isolation is B03,
    not implemented here. Apply this dependency to every route that touches
    them.

    While config.MULTI_CLIENT_MODE is False (the default — a single-tenant
    deployment), this is a no-op: today's behavior is preserved exactly, no
    regression. Once a deployment sets MULTI_CLIENT_MODE=true because it
    serves more than one independent client organization, only
    organizations an operator has explicitly flagged
    `Organization.corpus_access=True` may use these routes; everyone else
    gets a clear 503 rather than silently reading another client's
    references or capacity data through the shared corpus.
    """
    if not config.MULTI_CLIENT_MODE:
        return ctx
    org = organizations_repo.get_by_id(db, ctx.organization_id)
    if org is None or not org.corpus_access:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Fonctionnalité indisponible pour votre organisation : l'isolation "
            "multi-organisation du corpus RAG et du plan de capacité (ticket "
            "B03) n'est pas encore livrée.",
        )
    return ctx
