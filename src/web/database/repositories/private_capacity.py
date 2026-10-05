"""Pure data-access for PrivateCapacityPlan — one row per (organization,
owner_user_id). Replaces the global capacite_charge_planification.md file
as the source of truth (the file-backed CapacityRepository that read it was
removed in lot 43; the file itself stays in data/ untouched)."""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.web.database.models import PrivateCapacityPlan


def get_for_owner(db: Session, *, organization_id: uuid.UUID, owner_user_id: uuid.UUID) -> PrivateCapacityPlan | None:
    stmt = select(PrivateCapacityPlan).where(
        PrivateCapacityPlan.organization_id == organization_id,
        PrivateCapacityPlan.owner_user_id == owner_user_id,
    )
    return db.execute(stmt).scalar_one_or_none()


def save_for_owner(
    db: Session, *, organization_id: uuid.UUID, owner_user_id: uuid.UUID,
    charge_globale_pct: int, nombre_projets_en_cours: int, projets_en_cours: list[str], capacites_par_pole: dict[str, int],
    disponibilite_minimum_pct: int | None = None,
) -> PrivateCapacityPlan:
    """B08-T1: `disponibilite_minimum_pct` defaults to None (keep the
    existing/model-default value) so a caller that doesn't send it (an
    older frontend build) never resets an already-chosen threshold back to
    10 on every unrelated save."""
    plan = get_for_owner(db, organization_id=organization_id, owner_user_id=owner_user_id)
    if plan is None:
        plan = PrivateCapacityPlan(organization_id=organization_id, owner_user_id=owner_user_id)
        db.add(plan)
    plan.status = "configured"
    plan.charge_globale_pct = charge_globale_pct
    plan.nombre_projets_en_cours = nombre_projets_en_cours
    plan.projets_en_cours = projets_en_cours
    plan.capacites_par_pole = capacites_par_pole
    if disponibilite_minimum_pct is not None:
        plan.disponibilite_minimum_pct = disponibilite_minimum_pct
    plan.version = (plan.version or 0) + 1
    db.flush()
    return plan
