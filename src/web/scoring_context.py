"""Lot 44 — the ONE place that resolves the private scoring configuration.

Before this module the analysis job (`jobs.py`) and the simulation route
(`routes_scoring_policy.simulate_policy`) each built their own
`ScoringPolicySnapshot` from their own reads. Now both call
`resolve_scoring_context`:

- ONE database session, ONE consistent read of policy + profile + capacity
  for the same (organization_id, owner_user_id) pair — no second, possibly
  contradictory read while the calculation runs;
- every value is COPIED into plain, immutable-by-convention data (deep copies
  of the JSON columns, tuples for lists): nothing the calculation uses is a
  live ORM object;
- `source="active"` for a real analysis (the account's active policy),
  `source="draft"` for the simulation (the current draft, never activated);
- a missing configuration raises `ScoringConfigurationMissing` — there is no
  global, demo or "other account" fallback, and no policy is ever created.
"""
from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass
from typing import Optional

from sqlalchemy.orm import Session

from src.agents import criteria_catalogue
from src.agents.scoring_engine import ScoringPolicySnapshot
from src.core.capacity_plan import CapacityPlan
from src.web.database.repositories import private_capacity as private_capacity_repo
from src.web.database.repositories import provider_profile as provider_profile_repo
from src.web.database.repositories import scoring_policy as scoring_policy_repo


class ScoringConfigurationMissing(Exception):
    """`kind`: "scoring_policy" (no active policy), "draft" (no draft to
    simulate), "capacity_plan" (no configured private capacity),
    "criteria_not_materialized" (a policy row that migration 0010 has not
    processed), or "policy_version_unavailable" (lot 49 — the EXACT policy
    version that scored a past analysis could not be found for this owner;
    the correct response is "a new analysis is required", never a
    recompute against a different, possibly-inconsistent version)."""

    def __init__(self, kind: str):
        self.kind = kind
        super().__init__(f"Configuration privée manquante : {kind}")


@dataclass(frozen=True)
class ProviderIdentity:
    """The account's declared identity/profile, detached from the ORM. Same
    attribute names as ProviderProfile, so prompt builders that read
    `raison_sociale` / `competences` / `certifications` work unchanged."""
    raison_sociale: str | None
    competences: tuple
    certifications: tuple
    external_enrichment_enabled: bool
    business_facts: dict


@dataclass(frozen=True)
class ScoringContext:
    source: str
    organization_id: uuid.UUID
    owner_user_id: uuid.UUID
    policy_id: uuid.UUID
    policy_version: int
    criteria_version: int
    origin: str
    snapshot: ScoringPolicySnapshot
    requested_facts: dict
    provider: ProviderIdentity
    capacity_plan: CapacityPlan


def provider_snapshot_from_identity(provider: ProviderIdentity) -> dict:
    """Lot 49 bis: the plain, JSON-storable shape `ScoringResult.provider_snapshot` freezes — the exact
    inputs `resolve_scoring_context` would otherwise re-read live. Deep-copied so the stored snapshot can
    never alias (and later be mutated through) the live `ProviderIdentity` this was built from."""
    return {
        "raison_sociale": provider.raison_sociale,
        "competences": list(provider.competences),
        "certifications": [copy.deepcopy(c) for c in provider.certifications],
        "external_enrichment_enabled": provider.external_enrichment_enabled,
        "business_facts": copy.deepcopy(provider.business_facts),
    }


def provider_identity_from_snapshot(snapshot: dict) -> ProviderIdentity:
    """Inverse of `provider_snapshot_from_identity` — rebuilds a `ProviderIdentity` from a frozen snapshot
    dict (e.g. `ScoringResult.provider_snapshot`, possibly amended with explicitly-confirmed complements by
    src/web/completion_service.py) instead of a live `ProviderProfile` read."""
    return ProviderIdentity(
        raison_sociale=snapshot.get("raison_sociale"),
        competences=tuple(str(c) for c in (snapshot.get("competences") or [])),
        certifications=tuple(copy.deepcopy(c) for c in (snapshot.get("certifications") or [])),
        external_enrichment_enabled=bool(snapshot.get("external_enrichment_enabled", False)),
        business_facts=copy.deepcopy(snapshot.get("business_facts") or {}),
    )


def resolve_policy_snapshot_from_frozen_provider(
    db: Session, *, organization_id: uuid.UUID, owner_user_id: uuid.UUID, policy_version: int, provider_snapshot: dict,
) -> tuple[ScoringPolicySnapshot, ProviderIdentity]:
    """Lot 49 bis: builds the exact `ScoringPolicySnapshot` a completion revision must score with — the
    policy's EXACT historical version (read from the database, like `resolve_scoring_context(source=
    "version")`), but the provider inputs (competences/certifications/declared facts) come ENTIRELY from
    `provider_snapshot` (already frozen + any explicitly-confirmed complement merged in by
    src/web/completion_service.py::apply_completion) — this function never reads `ProviderProfile` itself,
    so nothing about the account's CURRENT profile (an unrelated fact, competency or certification changed
    after the parent analysis ran) can silently reach the calculation. Raises `ScoringConfigurationMissing
    ("policy_version_unavailable")` if the version no longer exists for this owner — never a fallback to a
    different policy."""
    policy = scoring_policy_repo.get_by_version(db, organization_id=organization_id, owner_user_id=owner_user_id, version=policy_version)
    if policy is None or not policy.criteria_version:
        raise ScoringConfigurationMissing("policy_version_unavailable")

    competences = tuple(str(c) for c in (provider_snapshot.get("competences") or []))
    certifications = tuple(copy.deepcopy(c) for c in (provider_snapshot.get("certifications") or []))
    declared_facts = copy.deepcopy(provider_snapshot.get("business_facts") or {})
    criteria = copy.deepcopy(policy.criteria) if isinstance(policy.criteria, list) else policy.criteria
    settings = copy.deepcopy(policy.settings) if isinstance(policy.settings, dict) else {}
    snapshot = ScoringPolicySnapshot(
        criteria=criteria, threshold_go=policy.threshold_go, threshold_sous_reserve=policy.threshold_sous_reserve,
        mastered_technologies=frozenset(competences),
        certifications_held=frozenset(
            str(c.get("nom", "")).strip().lower() for c in certifications if isinstance(c, dict) and str(c.get("nom", "")).strip()
        ),
        declared_facts=declared_facts, settings=settings, version=policy.version,
        criteria_version=policy.criteria_version, origin=policy.origin,
    )
    provider = provider_identity_from_snapshot(provider_snapshot)
    return snapshot, provider


def resolve_scoring_context(
    db: Session, *, organization_id: uuid.UUID, owner_user_id: uuid.UUID, source: str = "active",
    policy_version: Optional[int] = None,
) -> ScoringContext:
    """Lot 49: `source="version"` (with `policy_version` required) resolves the EXACT historical policy that
    scored a past analysis — for recomputing a completion revision deterministically. Never "active" (which
    could have changed since) and never "draft" (never used to score a real analysis) — see
    scoring_policy_repo.get_by_version's own docstring for why any version it returns is safe to reuse
    unchanged."""
    if source not in ("active", "draft", "version"):
        raise ValueError("source must be 'active', 'draft' or 'version'")
    if source == "version":
        if policy_version is None:
            raise ValueError("policy_version is required when source='version'")
        policy = scoring_policy_repo.get_by_version(db, organization_id=organization_id, owner_user_id=owner_user_id, version=policy_version)
    else:
        getter = scoring_policy_repo.get_active if source == "active" else scoring_policy_repo.get_draft
        policy = getter(db, organization_id=organization_id, owner_user_id=owner_user_id)
    if policy is None:
        raise ScoringConfigurationMissing({"active": "scoring_policy", "draft": "draft", "version": "policy_version_unavailable"}[source])
    if not policy.criteria_version:
        raise ScoringConfigurationMissing("criteria_not_materialized")
    profile = provider_profile_repo.get_for_owner(db, organization_id=organization_id, owner_user_id=owner_user_id)
    plan_row = private_capacity_repo.get_for_owner(db, organization_id=organization_id, owner_user_id=owner_user_id)
    if plan_row is None or getattr(plan_row, "status", "configured") != "configured":
        raise ScoringConfigurationMissing("capacity_plan")

    business_facts = copy.deepcopy(dict(profile.business_facts or {})) if profile is not None and isinstance(profile.business_facts, dict) else {}
    competences = tuple(str(c) for c in (profile.competences or [])) if profile is not None else ()
    certifications = tuple(copy.deepcopy(c) for c in (profile.certifications or [])) if profile is not None else ()
    provider = ProviderIdentity(
        raison_sociale=profile.raison_sociale if profile is not None else None,
        competences=competences, certifications=certifications,
        external_enrichment_enabled=bool(profile.external_enrichment_enabled) if profile is not None else False,
        business_facts=business_facts,
    )
    criteria = copy.deepcopy(policy.criteria) if isinstance(policy.criteria, list) else policy.criteria
    settings = copy.deepcopy(policy.settings) if isinstance(policy.settings, dict) else {}
    snapshot = ScoringPolicySnapshot(
        criteria=criteria, threshold_go=policy.threshold_go, threshold_sous_reserve=policy.threshold_sous_reserve,
        mastered_technologies=frozenset(competences),
        certifications_held=frozenset(
            str(c.get("nom", "")).strip().lower() for c in certifications if isinstance(c, dict) and str(c.get("nom", "")).strip()
        ),
        declared_facts=copy.deepcopy(business_facts), settings=settings, version=policy.version,
        criteria_version=policy.criteria_version, origin=policy.origin,
    )
    plan = CapacityPlan(
        charge_globale_pct=plan_row.charge_globale_pct, nombre_projets_en_cours=plan_row.nombre_projets_en_cours,
        projets_en_cours=list(plan_row.projets_en_cours or []), capacites_par_pole=dict(plan_row.capacites_par_pole or {}),
        disponibilite_minimum_pct=plan_row.disponibilite_minimum_pct,
    )
    return ScoringContext(
        source=source, organization_id=organization_id, owner_user_id=owner_user_id, policy_id=policy.id,
        policy_version=policy.version, criteria_version=policy.criteria_version, origin=policy.origin, snapshot=snapshot,
        requested_facts=criteria_catalogue.requested_facts(criteria, known_facts=business_facts),
        provider=provider, capacity_plan=plan,
    )
