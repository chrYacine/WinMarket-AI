"""Lot 49 — durable record of what a user DECLARED to complete an analysis into a new revision.

Write-once, read-scoped: a complement is recorded exactly once, at the moment the revision that used it is
created, and is never edited afterwards (a later correction is a NEW complement on a NEW revision, keeping
its own trail). Distinct from `ao_dossier_pieces`/observations (extracted) and from `ProviderProfile`
(the account's own permanent profile, written separately when a complement's subject is 'prestataire' and
the request explicitly confirmed a permanent write — see src/web/completion_service.py)."""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.web.database.models import AnalysisComplement


def record_many(
    db: Session, *, job_id: str, organization_id: uuid.UUID, user_id: uuid.UUID, created_by_user_id: uuid.UUID,
    items: list[dict[str, Any]],
) -> list[AnalysisComplement]:
    """`items`: [{"need_id", "subject", "field_key", "field_label", "value", "unit", "origin"?,
    "source_json"?}, ...] — already validated by the caller (src/web/completion_service.py's whitelist);
    this function only persists. Lot 52: `origin`/`source_json` default to 'declared_user'/None, exactly
    the lot 49/49 bis behaviour, for any item that does not set them explicitly."""
    rows = [
        AnalysisComplement(
            job_id=job_id, organization_id=organization_id, user_id=user_id, created_by_user_id=created_by_user_id,
            need_id=item["need_id"], subject=item["subject"], field_key=item["field_key"],
            field_label=item["field_label"], value_json=item["value"], unit=item.get("unit"),
            origin=item.get("origin", "declared_user"), source_json=item.get("source_json"),
        )
        for item in items
    ]
    db.add_all(rows)
    db.flush()
    return rows


def list_for_job(db: Session, *, job_id: str, organization_id: uuid.UUID, user_id: uuid.UUID) -> list[AnalysisComplement]:
    stmt = (
        select(AnalysisComplement)
        .where(AnalysisComplement.job_id == job_id, AnalysisComplement.organization_id == organization_id, AnalysisComplement.user_id == user_id)
        .order_by(AnalysisComplement.created_at)
    )
    return list(db.execute(stmt).scalars().all())
