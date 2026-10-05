"""Data-access for ScoringPolicy — versioned, private, org+owner scoped.

Unlike private_capacity.py/provider_profile.py's single-mutable-row idiom,
this keeps every version as its own row: a 'draft' is mutated in place
(save_draft), but 'active'/'archived' rows are never mutated by anything
here once written — a past analysis that recorded a policy version must be
able to keep meaning the exact numbers that were active when it ran (see
ticket B06-T1 section 5). Activation is optimistic-concurrency-checked at
the application level (expected_active_version) AND backstopped by the
DB's own partial unique index (uq_scoring_policies_one_active, migration
0005) — a real race between two transactions still fails one of them with
an IntegrityError even if both passed the application-level check, though
no test in this codebase exercises true multi-process concurrency (no
PostgreSQL environment available here — see docs/qa reserves)."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.agents import criteria_catalogue
from src.web.database.models import ScoringPolicy


class DraftFormatConflict(Exception):
    """A legacy-shaped save (weights / business rules / custom criteria) was
    attempted on a draft authored in the explicit-criteria format: it would
    silently discard that draft's criteria. The caller refuses (409)."""


class ActivationConflict(Exception):
    """Raised when the caller's expected_active_version doesn't match what
    is actually active right now — someone else already activated a
    different version since the caller last read the state."""

    def __init__(self, current_active_version: int | None):
        self.current_active_version = current_active_version
        super().__init__(
            f"expected active version {current_active_version!r} to differ; "
            f"a concurrent activation changed it"
        )


def get_active(db: Session, *, organization_id: uuid.UUID, owner_user_id: uuid.UUID) -> ScoringPolicy | None:
    stmt = select(ScoringPolicy).where(
        ScoringPolicy.organization_id == organization_id,
        ScoringPolicy.owner_user_id == owner_user_id,
        ScoringPolicy.status == "active",
    )
    return db.execute(stmt).scalar_one_or_none()


def get_draft(db: Session, *, organization_id: uuid.UUID, owner_user_id: uuid.UUID) -> ScoringPolicy | None:
    stmt = select(ScoringPolicy).where(
        ScoringPolicy.organization_id == organization_id,
        ScoringPolicy.owner_user_id == owner_user_id,
        ScoringPolicy.status == "draft",
    )
    return db.execute(stmt).scalar_one_or_none()


def list_versions(db: Session, *, organization_id: uuid.UUID, owner_user_id: uuid.UUID) -> list[ScoringPolicy]:
    stmt = select(ScoringPolicy).where(
        ScoringPolicy.organization_id == organization_id,
        ScoringPolicy.owner_user_id == owner_user_id,
    ).order_by(ScoringPolicy.version.desc())
    return list(db.execute(stmt).scalars())


def get_by_version(db: Session, *, organization_id: uuid.UUID, owner_user_id: uuid.UUID, version: int) -> ScoringPolicy | None:
    """Lot 49: the EXACT policy that scored a past analysis (`ScoringResult`/`Job.scoring_policy_version`
    pins this integer at launch time — see the module docstring's "past analysis must be able to keep
    meaning the exact numbers"). Whatever this returns is safe to re-score with unchanged: a version that
    was ever 'active' is immutable from that point on ('active' then 'archived', never mutated by anything
    in this module) — only a 'draft' row (never used to score a real analysis) can still change, and this
    lookup does not exclude by status, so a caller that got a version number from a completed job will
    always find the same frozen criteria/thresholds it used the first time. Returns None if the version was
    never created for this owner at all (cannot happen through the normal flow, but a caller must still
    treat it as "this analysis cannot be recomputed" rather than guessing a fallback)."""
    stmt = select(ScoringPolicy).where(
        ScoringPolicy.organization_id == organization_id,
        ScoringPolicy.owner_user_id == owner_user_id,
        ScoringPolicy.version == version,
    )
    return db.execute(stmt).scalar_one_or_none()


def get_by_id_for_owner(
    db: Session, *, policy_id: uuid.UUID, organization_id: uuid.UUID, owner_user_id: uuid.UUID
) -> ScoringPolicy | None:
    stmt = select(ScoringPolicy).where(
        ScoringPolicy.id == policy_id,
        ScoringPolicy.organization_id == organization_id,
        ScoringPolicy.owner_user_id == owner_user_id,
    )
    return db.execute(stmt).scalar_one_or_none()


def _next_version(db: Session, *, organization_id: uuid.UUID, owner_user_id: uuid.UUID) -> int:
    stmt = select(func.max(ScoringPolicy.version)).where(
        ScoringPolicy.organization_id == organization_id,
        ScoringPolicy.owner_user_id == owner_user_id,
    )
    current_max = db.execute(stmt).scalar()
    return (current_max or 0) + 1


def save_draft(
    db: Session, *, organization_id: uuid.UUID, owner_user_id: uuid.UUID, created_by_user_id: uuid.UUID,
    weights: dict[str, float], threshold_go: float | None, threshold_sous_reserve: float | None,
    business_rules: dict | None = None, custom_criteria: list | None = None,
) -> ScoringPolicy:
    """Create-or-resume-editing the single 'draft' row for this owner. Never
    touches an 'active' or 'archived' row — if none exists yet, allocates
    the next version number (continuing from the highest version ever used
    by this owner, active/archived/draft alike, so version numbers are
    never reused).

    B06-T4: `business_rules` defaults to None, meaning "leave the draft's
    current value untouched" (mirrors `disponibilite_minimum_pct` in
    private_capacity.py::save_for_owner) — a caller that only sends
    weights/thresholds never resets already-configured business rules back
    to empty on an unrelated save.

    B06-T5: `custom_criteria` follows the exact same "None = untouched"
    idiom. A draft may hold an incomplete/invalid list (e.g. referencing a
    fact not yet declared) without refusing the save itself — only
    /validate and /activate (src.agents.scoring_policy_validation.
    validate_for_activation) enforce the rules, same as every other draft
    field."""
    draft = get_draft(db, organization_id=organization_id, owner_user_id=owner_user_id)
    if draft is None:
        draft = ScoringPolicy(
            organization_id=organization_id, owner_user_id=owner_user_id,
            version=_next_version(db, organization_id=organization_id, owner_user_id=owner_user_id),
            status="draft", created_by_user_id=created_by_user_id, business_rules={}, custom_criteria=[],
            origin="legacy", criteria_version=criteria_catalogue.SCHEMA_VERSION, criteria=[], settings={},
        )
        db.add(draft)
    elif draft.origin != "legacy":
        raise DraftFormatConflict("the current draft is in the explicit-criteria format")
    draft.weights = dict(weights)
    draft.threshold_go = threshold_go
    draft.threshold_sous_reserve = threshold_sous_reserve
    if business_rules is not None:
        draft.business_rules = dict(business_rules)
    if custom_criteria is not None:
        # Reviewer-caught (confirmed): `list(custom_criteria)` silently
        # mangled a malformed non-list payload instead of surfacing it —
        # e.g. a client-sent dict {"id": "x"} became `['id']` (its own
        # keys), a corrupted draft with no error anywhere. Stored exactly
        # as given instead: a genuine list round-trips unchanged, and a
        # malformed shape is caught cleanly by business_facts.
        # validate_custom_criteria's own `isinstance(criteria, list)`
        # check at /validate and /activate — never silently reinterpreted
        # here.
        draft.custom_criteria = custom_criteria
    # Lot 44: the legacy columns stay authoritative for a legacy-origin draft;
    # the criteria the engine reads are re-materialized from them on every
    # save (same function the 0010 back-fill uses), so the two can never
    # drift apart.
    draft.criteria, draft.settings = criteria_catalogue.materialize_legacy(
        weights=draft.weights, business_rules=draft.business_rules, custom_criteria=draft.custom_criteria,
    )
    draft.criteria_version = criteria_catalogue.SCHEMA_VERSION
    db.flush()
    return draft


def save_draft_criteria(
    db: Session, *, organization_id: uuid.UUID, owner_user_id: uuid.UUID, created_by_user_id: uuid.UUID,
    criteria, settings, threshold_go: float | None, threshold_sous_reserve: float | None,
) -> ScoringPolicy:
    """Create-or-resume the single draft in the EXPLICIT-CRITERIA format
    (origin='user'). A new policy starts empty: nothing is imposed here. The
    three legacy columns are emptied — the criteria are the only content. A
    draft that was in the legacy format is REPLACED by this content (the caller
    explicitly sent the new format). Never touches an active/archived row.
    Stored as given (an incomplete or invalid draft is saveable; /validate and
    /activate enforce the rules)."""
    draft = get_draft(db, organization_id=organization_id, owner_user_id=owner_user_id)
    if draft is None:
        draft = ScoringPolicy(
            organization_id=organization_id, owner_user_id=owner_user_id,
            version=_next_version(db, organization_id=organization_id, owner_user_id=owner_user_id),
            status="draft", created_by_user_id=created_by_user_id,
        )
        db.add(draft)
    draft.origin = "user"
    draft.criteria_version = criteria_catalogue.SCHEMA_VERSION
    draft.criteria = criteria
    draft.settings = settings
    draft.weights, draft.business_rules, draft.custom_criteria = {}, {}, []
    draft.threshold_go = threshold_go
    draft.threshold_sous_reserve = threshold_sous_reserve
    db.flush()
    return draft


def activate_draft(
    db: Session, *, organization_id: uuid.UUID, owner_user_id: uuid.UUID, expected_active_version: int | None,
) -> ScoringPolicy:
    """Atomically archive the currently-active row (if any) and promote the
    draft to 'active'. Raises ActivationConflict without writing anything
    if `expected_active_version` doesn't match what is actually active
    right now (the caller read a stale state — e.g. someone else already
    activated a different version since). Raises ValueError if there is no
    draft to activate. The caller is responsible for validating the draft
    (src/agents/scoring_policy_validation.py) BEFORE calling this — this
    function does not re-validate weights/thresholds itself."""
    draft = get_draft(db, organization_id=organization_id, owner_user_id=owner_user_id)
    if draft is None:
        raise ValueError("no draft to activate for this owner")

    current_active = get_active(db, organization_id=organization_id, owner_user_id=owner_user_id)
    current_version = current_active.version if current_active is not None else None
    if current_version != expected_active_version:
        raise ActivationConflict(current_version)

    # Archived and activated in two separate flushes, deliberately: SQLite
    # (and PostgreSQL) check a partial unique index after each individual
    # UPDATE, not deferred to transaction end — flushing both status
    # changes in the same flush() risks a transient window where the new
    # row is already 'active' while the old one hasn't been archived yet
    # (or vice versa, depending on statement ordering SQLAlchemy chooses),
    # spuriously tripping uq_scoring_policies_one_active even though the
    # caller's expected_active_version check already passed correctly.
    if current_active is not None:
        current_active.status = "archived"
        db.flush()
    draft.status = "active"
    draft.activated_at = datetime.now(timezone.utc)
    try:
        db.flush()
    except IntegrityError:
        # Backstop for a genuine cross-transaction race the application-level
        # check above could not see (never exercised by this codebase's test
        # suite — no multi-process/PostgreSQL environment available here).
        db.rollback()
        raise ActivationConflict(current_version) from None
    return draft
