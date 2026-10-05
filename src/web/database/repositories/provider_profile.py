"""Pure data-access for ProviderProfile — one row per (organization,
owner_user_id), the ESN's own declared business identity/competencies/
certifications. Never the AO buyer's CompanyProfile (src/core/models.py),
never inferred from an uploaded knowledge document."""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.web.database.models import ProviderProfile


def get_for_owner(db: Session, *, organization_id: uuid.UUID, owner_user_id: uuid.UUID) -> ProviderProfile | None:
    stmt = select(ProviderProfile).where(
        ProviderProfile.organization_id == organization_id,
        ProviderProfile.owner_user_id == owner_user_id,
    )
    return db.execute(stmt).scalar_one_or_none()


def save_for_owner(
    db: Session, *, organization_id: uuid.UUID, owner_user_id: uuid.UUID,
    raison_sociale: str | None, effectif: str | None,
    competences: list[str], certifications: list[dict],
    external_enrichment_enabled: bool = False,
    business_facts: dict | None = None,
) -> ProviderProfile:
    """Upsert-style save, same idiom as private_capacity.save_for_owner —
    always this owner's own row, never a colleague's (the caller must
    always pass ctx.user.id as owner_user_id, never a client-supplied id).
    `status` becomes 'complete' only once raison_sociale is non-empty — the
    single required field for this ticket (see
    docs/api/B06_SCORING_CONFIG_CONTRACT.md: every other field is optional
    and must never block activation on its own).

    B07-T1: `external_enrichment_enabled` defaults to False here too (not
    just on the column) — every save re-asserts the caller's explicit
    choice, since the route itself always sends it (see
    routes_scoring_policy.py::save_profile).

    B06-T5: `business_facts` defaults to None, meaning "leave the current
    value untouched" (same idiom as scoring_policy.save_draft's
    `business_rules` parameter) — a caller that only sends raison_sociale/
    competences/certifications never resets already-declared business
    facts back to empty on an unrelated save. Each entry is validated by
    the caller (routes_scoring_policy.py) via src.agents.business_facts
    BEFORE reaching here — this function only persists."""
    profile = get_for_owner(db, organization_id=organization_id, owner_user_id=owner_user_id)
    if profile is None:
        profile = ProviderProfile(organization_id=organization_id, owner_user_id=owner_user_id, business_facts={})
        db.add(profile)
    profile.raison_sociale = raison_sociale
    profile.effectif = effectif
    profile.competences = [str(c).strip().lower() for c in competences if str(c).strip()]
    profile.certifications = certifications
    profile.external_enrichment_enabled = external_enrichment_enabled
    if business_facts is not None:
        profile.business_facts = dict(business_facts)
    profile.status = "complete" if (raison_sociale or "").strip() else "incomplete"
    profile.version = (profile.version or 0) + 1
    db.flush()
    return profile
