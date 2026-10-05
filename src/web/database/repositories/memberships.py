"""Pure data-access functions for the `memberships` table.

A Membership is never hard-deleted by a revoke — status flips to 'revoked'
and revoked_at is stamped, so history is preserved (ticket B02 section 3).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.web.database.models import Membership

VALID_ROLES = {"viewer", "analyst", "organization_admin"}
VALID_STATUSES = {"active", "revoked"}


def create_membership(
    db: Session, *, user_id: uuid.UUID, organization_id: uuid.UUID, role: str, status: str = "active"
) -> Membership:
    if role not in VALID_ROLES:
        raise ValueError(f"Rôle invalide : {role}")
    if status not in VALID_STATUSES:
        raise ValueError(f"Statut d'appartenance invalide : {status}")
    membership = Membership(user_id=user_id, organization_id=organization_id, role=role, status=status)
    db.add(membership)
    db.flush()
    return membership


def get_active(db: Session, *, user_id: uuid.UUID, organization_id: uuid.UUID) -> Membership | None:
    """The one query every access check ultimately relies on: is this user
    currently (not historically) an active member of this organization?"""
    stmt = select(Membership).where(
        Membership.user_id == user_id,
        Membership.organization_id == organization_id,
        Membership.status == "active",
    )
    return db.execute(stmt).scalar_one_or_none()


def list_active_for_user(db: Session, user_id: uuid.UUID) -> list[Membership]:
    stmt = select(Membership).where(Membership.user_id == user_id, Membership.status == "active")
    return list(db.execute(stmt).scalars().all())


def list_for_organization(db: Session, organization_id: uuid.UUID) -> list[Membership]:
    stmt = select(Membership).where(Membership.organization_id == organization_id).order_by(Membership.created_at)
    return list(db.execute(stmt).scalars().all())


def revoke(db: Session, membership: Membership) -> Membership:
    membership.status = "revoked"
    membership.revoked_at = datetime.now(timezone.utc)
    db.flush()
    return membership


def update_role(db: Session, membership: Membership, role: str) -> Membership:
    if role not in VALID_ROLES:
        raise ValueError(f"Rôle invalide : {role}")
    membership.role = role
    db.flush()
    return membership
