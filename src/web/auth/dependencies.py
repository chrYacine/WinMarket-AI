"""Central access-control logic.

Two flavors are provided, matching the two kinds of routes in this app:

- For `/api/*` JSON routes: FastAPI dependencies (`require_authenticated_user`,
  `require_active_starter_user`) that raise a generic HTTPException.
- For `/app/*` HTML page routes: `resolve_app_access()`, called explicitly at
  the top of each route, which returns either the User or a ready-to-return
  RedirectResponse (to /login?next=..., or /account/pending) — pages don't
  want a raw JSON 401/403, they want a redirect.

Every check re-reads status from PostgreSQL on every request — the session
cookie only carries a user_id pointer, never the authority.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from src.web.auth.session_cookie import get_session_identity
from src.web.database.models import User
from src.web.database.repositories import subscriptions as subscriptions_repo
from src.web.database.repositories import users as users_repo
from src.web.database.session import get_db


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    """Best-effort lookup — returns None for anonymous, invalid, or revoked
    (stale session_version — B21-T1, e.g. after a password reset) sessions."""
    identity = get_session_identity(request)
    if identity is None:
        return None
    raw_user_id, session_version = identity
    try:
        user_id = uuid.UUID(raw_user_id)
    except ValueError:
        return None
    user = users_repo.get_by_id(db, user_id)
    if user is None or user.session_version != session_version:
        return None
    return user


def require_authenticated_user(user: User | None = Depends(get_current_user)) -> User:
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentification requise.")
    return user


@dataclass(frozen=True)
class SubscriptionAccessDecision:
    """The outcome of evaluate_subscription_access() below. `reason` is
    None iff `granted` is True; otherwise one of: "no_user",
    "user_not_active", "no_subscription", "wrong_plan",
    "not_active_status", "not_yet_started", "expired", "ambiguous_dates".
    See docs/api/B20_T1_SUBSCRIPTION_ACCESS_CONTRACT.md for the full
    contract."""

    granted: bool
    reason: str | None


def _as_utc(value: datetime | None) -> datetime | None:
    """Normalizes a datetime read back from the database to a UTC-aware
    value before it is ever compared against `datetime.now(timezone.utc)`.

    B20-T1: `Subscription.started_at`/`expires_at` are declared
    `DateTime(timezone=True)`, and every writer in this codebase
    (subscriptions_repo.activate(), and any future billing code) is
    expected to populate them with `datetime.now(timezone.utc)` — always
    UTC. But on SQLite (this project's local/test database), SQLAlchemy's
    DATETIME type does not actually preserve tzinfo across a write/read
    round-trip: a value written as UTC-aware comes back NAIVE. Comparing a
    naive value directly against an aware `datetime.now(timezone.utc)`
    raises `TypeError: can't compare offset-naive and offset-aware
    datetimes` — on PostgreSQL (production) the same column stays aware,
    so this would only surface unpredictably, and only against the real
    database, if left unhandled here. A naive value is therefore assumed
    to already be UTC (true given the writer convention above); an aware
    value is converted (not just reinterpreted) to UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def evaluate_subscription_access(db: Session, user: User | None) -> SubscriptionAccessDecision:
    """The ONE place that decides whether `user` currently has the access
    an active Starter subscription is supposed to grant, WHY when denied.
    `user_has_active_starter_subscription` below is the boolean-only
    wrapper every existing caller (require_active_starter_user,
    resolve_app_access, the template-context middleware) keeps using
    unchanged.

    Boundary convention (pinned by a test at exactly this instant in
    tests/test_b20_t1_subscription_access.py) — the access window is
    HALF-OPEN, [started_at, expires_at):
      - `started_at`: access is granted STARTING AT that instant
        (now >= started_at grants — a subscription "starting now" is
        already usable).
      - `expires_at`: access is granted only STRICTLY BEFORE that instant
        (now < expires_at grants; at the exact expiry instant the
        subscription is already treated as expired — the boundary instant
        belongs to the state that is STARTING, never to the state that is
        ENDING). This is symmetric with `started_at` above.
    Both fields are nullable: a null `started_at` is never itself a reason
    to deny access (e.g. a subscription activated before this field
    existed); a null `expires_at` means "no expiry recorded" — never
    expires through this check. No trial/grace period is invented for
    either null case — this is a literal "no constraint from this field",
    not a substitute date.

    Ambiguous/incoherent data ("si le contrat ne tranche pas... refuser...
    avec un code explicite, sans inventer un droit"): if BOTH dates are
    set and `expires_at <= started_at` (ends at-or-before it starts — not
    a coherent window, whether exactly equal or truly reversed), this
    refuses access with reason "ambiguous_dates" rather than falling
    through to what the individual date checks below would otherwise say.
    This is the only state here where the schema/data genuinely doesn't
    decide; every other combination (including one date present and the
    other absent) is decidable by the ordinary per-field checks below, and
    is decided there instead — deliberately not folded into this same
    reason code, so a real ambiguity is always distinguishable from an
    ordinary expiry/not-yet-started denial.
    """
    if user is None:
        return SubscriptionAccessDecision(False, "no_user")
    if user.status != "active":
        return SubscriptionAccessDecision(False, "user_not_active")

    subscription = subscriptions_repo.get_latest_for_user(db, user.id)
    if subscription is None:
        return SubscriptionAccessDecision(False, "no_subscription")
    if subscription.plan != "starter":
        return SubscriptionAccessDecision(False, "wrong_plan")
    if subscription.status != "active":
        return SubscriptionAccessDecision(False, "not_active_status")

    if subscription.access_origin == "manual_without_payment" and (subscription.expires_at is None or subscription.max_analyses is None or subscription.max_analyses <= 0 or subscription.organization_id is None):
        return SubscriptionAccessDecision(False, "manual_grant_incomplete")

    started_at = _as_utc(subscription.started_at)
    expires_at = _as_utc(subscription.expires_at)

    if started_at is not None and expires_at is not None and expires_at <= started_at:
        return SubscriptionAccessDecision(False, "ambiguous_dates")

    now = datetime.now(timezone.utc)
    if started_at is not None and now < started_at:
        return SubscriptionAccessDecision(False, "not_yet_started")
    if expires_at is not None and now >= expires_at:
        return SubscriptionAccessDecision(False, "expired")

    return SubscriptionAccessDecision(True, None)


def user_has_active_starter_subscription(db: Session, user: User | None) -> bool:
    """Shared predicate — used by the API gate, the page gate, and the
    landing/header template context (to decide which CTAs to show). See
    evaluate_subscription_access() above for the actual decision logic and
    reason codes; this is a boolean-compatible wrapper so every pre-B20-T1
    caller keeps working unmodified."""
    return evaluate_subscription_access(db, user).granted


def require_active_starter_user(
    user: User = Depends(require_authenticated_user),
    db: Session = Depends(get_db),
) -> User:
    """The gate every /api/* route that touches user data must depend on."""
    if user.status != "active":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Compte non actif.")
    if not user_has_active_starter_subscription(db, user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Acc?s actif requis ; contactez l?op?rateur.")
    return user


def resolve_app_access(request: Request, db: Session) -> tuple[User | None, RedirectResponse | None]:
    """For HTML page routes. Usage:

        user, redirect = resolve_app_access(request, db)
        if redirect:
            return redirect
    """
    user = get_current_user(request, db)
    if user is None:
        return None, RedirectResponse(f"/login?next={request.url.path}", status_code=303)
    if user.status == "pending":
        return None, RedirectResponse("/account/pending", status_code=303)
    if user.status in ("rejected", "disabled"):
        return None, RedirectResponse("/login?blocked=1", status_code=303)

    if not user_has_active_starter_subscription(db, user):
        return None, RedirectResponse("/account/pending", status_code=303)

    return user, None
