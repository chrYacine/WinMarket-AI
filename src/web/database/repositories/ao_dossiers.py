"""Data access for the tender dossier (lot 47 bis). Every read is scoped by (organization_id, user_id):
there is no "list everything" function, and a dossier that belongs to someone else is indistinguishable
from one that does not exist (the callers answer 404 for both)."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.web.database.models import AoDossier, AoDossierJobLink, AoDossierPiece


def create_dossier(
    db: Session, *, dossier_id: uuid.UUID, organization_id: uuid.UUID, user_id: uuid.UUID, total_bytes: int, pieces: list[dict[str, Any]],
    status: str = "validated", staging_expires_at: Optional[datetime] = None,
    categories_missing: Optional[list[str]] = None, scope_limited: bool = False, origin_job_id: Optional[str] = None,
) -> AoDossier:
    """Lot 50: `status="staging"` + `staging_expires_at` creates a dossier awaiting confirmation (§3) instead
    of an immediately-usable one — see `create_staging`/`confirm_staging` below, which build on this same
    function so the row shape is identical either way. `categories_missing`/`scope_limited` are frozen here
    for a dossier that is ALREADY final at creation (the direct-submit path, `dossier_service.commit`) —
    `confirm_staging` sets the same two fields for the preview/confirm path instead. Lot 50 bis §3:
    `origin_job_id` marks a dossier created by "Ajouter les pièces restantes" — `None` for every ordinary
    dossier."""
    dossier = AoDossier(
        id=dossier_id, organization_id=organization_id, user_id=user_id, status=status,
        total_bytes=total_bytes, piece_count=len(pieces), staging_expires_at=staging_expires_at,
        categories_missing=categories_missing, scope_limited=scope_limited, origin_job_id=origin_job_id,
    )
    db.add(dossier)
    db.flush()
    for position, piece in enumerate(pieces, start=1):
        db.add(AoDossierPiece(dossier_id=dossier.id, organization_id=organization_id, user_id=user_id, position=position, **piece))
    db.flush()
    return dossier


def create_staging(
    db: Session, *, dossier_id: uuid.UUID, organization_id: uuid.UUID, user_id: uuid.UUID, total_bytes: int,
    pieces: list[dict[str, Any]], expires_at: datetime, origin_job_id: Optional[str] = None,
) -> AoDossier:
    """Lot 50 §3 — a received, vetted dossier awaiting the user's confirmation of the final admitted set. Not
    resolvable through `get_by_job` (no job exists yet) and never returned by `GET /api/analyze/{job}/dossier`.
    Lot 50 bis §3: `origin_job_id` (optional) marks this as an "Ajouter les pièces restantes" staging dossier."""
    return create_dossier(
        db, dossier_id=dossier_id, organization_id=organization_id, user_id=user_id, total_bytes=total_bytes,
        pieces=pieces, status="staging", staging_expires_at=expires_at, origin_job_id=origin_job_id,
    )


def get_staging_for_owner(db: Session, *, dossier_id: uuid.UUID, organization_id: uuid.UUID, user_id: uuid.UUID) -> AoDossier | None:
    stmt = select(AoDossier).where(
        AoDossier.id == dossier_id, AoDossier.organization_id == organization_id, AoDossier.user_id == user_id,
        AoDossier.status == "staging",
    )
    return db.execute(stmt).scalar_one_or_none()


def confirm_staging(
    db: Session, dossier: AoDossier, *, confirmed_by_user_id: uuid.UUID, categories_missing: list[str], scope_limited: bool,
) -> None:
    """Flips a 'staging' dossier to 'validated' (ready for `link_job`, exactly like a direct submission) —
    the caller has ALREADY applied each piece's final `admitted`/`category_final`/`exclusion_reason`/
    `user_link_note` (see routes_api.py's confirm route) and verified at least one admitted, non-blocked
    piece remains. `piece_count`/`total_bytes` are NOT recomputed from the admitted subset: they stay the
    real received totals (the manifest's own `categories_missing`/`scope_limited` and each piece's `admitted`
    flag are the record of what was actually retained, not a rewrite of what was received)."""
    dossier.status = "validated"
    dossier.staging_expires_at = None
    dossier.confirmed_by_user_id = confirmed_by_user_id
    dossier.confirmed_at = datetime.now(timezone.utc)
    dossier.categories_missing = categories_missing
    dossier.scope_limited = scope_limited
    db.flush()


def discard_staging(db: Session, dossier: AoDossier) -> None:
    db.delete(dossier)
    db.flush()


def get_for_owner(db: Session, *, dossier_id: uuid.UUID, organization_id: uuid.UUID, user_id: uuid.UUID) -> AoDossier | None:
    stmt = select(AoDossier).where(
        AoDossier.id == dossier_id, AoDossier.organization_id == organization_id, AoDossier.user_id == user_id,
    )
    return db.execute(stmt).scalar_one_or_none()


def get_by_job(db: Session, *, job_id: str, organization_id: uuid.UUID, user_id: uuid.UUID) -> AoDossier | None:
    """Lot 49: resolved through `ao_dossier_job_links`, which keeps a row for EVERY job ever associated
    with the dossier — the original submission, every `resume`, every completion revision — not just the
    most recent one (`AoDossier.job_id` alone used to make an earlier job lose this lookup the moment a
    later one was linked; see docs/qa/lot_49_20260923/RAPPORT_LOT_49.md)."""
    stmt = (
        select(AoDossier)
        .join(AoDossierJobLink, AoDossierJobLink.dossier_id == AoDossier.id)
        .where(AoDossierJobLink.job_id == job_id, AoDossier.organization_id == organization_id, AoDossier.user_id == user_id)
    )
    return db.execute(stmt).scalars().first()


def piece_categories_for_jobs(
    db: Session, *, job_ids: list[str], organization_id: uuid.UUID, user_id: uuid.UUID,
) -> dict[str, list[str]]:
    """{job_id: piece categories in dossier order} for the dossier-based analyses among `job_ids` — ONE query for a page
    of the history, scoped to the caller's organization and user. Lot 49: joined through `ao_dossier_job_links` so an
    older job (superseded by a `resume` or a completion revision) still shows its dossier's categories."""
    if not job_ids:
        return {}
    stmt = (
        select(AoDossierJobLink.job_id, AoDossierPiece.category)
        .join(AoDossier, AoDossier.id == AoDossierJobLink.dossier_id)
        .join(AoDossierPiece, AoDossierPiece.dossier_id == AoDossier.id)
        .where(AoDossierJobLink.job_id.in_(job_ids), AoDossierJobLink.organization_id == organization_id, AoDossierJobLink.user_id == user_id)
        .order_by(AoDossierJobLink.job_id, AoDossierPiece.position)
    )
    out: dict[str, list[str]] = {}
    for job_id, category in db.execute(stmt).all():
        out.setdefault(job_id, []).append(category)
    return out


def link_job(db: Session, dossier: AoDossier, job_id: str) -> None:
    """Records `job_id` as (also) analysing this dossier. `AoDossier.job_id`/`status` still track only the
    MOST RECENT job (existing display/status contract, unchanged); `ao_dossier_job_links` additionally keeps
    a permanent row for THIS job specifically, so `get_by_job(job_id=...)` keeps resolving even after a
    later job becomes the new "most recent" one. Idempotent: calling this twice for the same job_id (it
    cannot legitimately happen — job ids are minted once — but a retry must never raise) leaves one row."""
    dossier.job_id = job_id
    dossier.status = "submitted"
    existing = db.execute(select(AoDossierJobLink).where(AoDossierJobLink.job_id == job_id)).scalar_one_or_none()
    if existing is None:
        db.add(AoDossierJobLink(dossier_id=dossier.id, organization_id=dossier.organization_id, user_id=dossier.user_id, job_id=job_id))
    db.flush()


def delete_dossier(db: Session, dossier: AoDossier) -> None:
    db.delete(dossier)
    db.flush()
