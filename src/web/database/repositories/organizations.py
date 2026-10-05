"""Pure data-access functions for the `organizations` table.

No permission logic here — see src/web/auth/access_context.py for that. This
module only talks to the database.
"""
from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from src.web.database.models import Organization

VALID_STATUSES = {"active", "suspended"}


def create_organization(db: Session, *, name: str, status: str = "active", corpus_access: bool = False) -> Organization:
    if status not in VALID_STATUSES:
        raise ValueError(f"Statut d'organisation invalide : {status}")
    org = Organization(name=(name or "").strip()[:255] or "Organisation", status=status, corpus_access=corpus_access)
    db.add(org)
    db.flush()
    return org


def get_by_id(db: Session, organization_id: uuid.UUID | str) -> Organization | None:
    return db.get(Organization, organization_id)


def set_status(db: Session, org: Organization, status: str) -> Organization:
    if status not in VALID_STATUSES:
        raise ValueError(f"Statut d'organisation invalide : {status}")
    org.status = status
    db.flush()
    return org
