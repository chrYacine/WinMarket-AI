"""Pure data-access functions for the `subscriptions` table."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.web.database.models import Subscription

VALID_PLANS = {"starter", "business", "enterprise"}
VALID_STATUSES = {"pending", "active", "cancelled", "expired"}


def create_subscription(db: Session, *, user_id: uuid.UUID, plan: str, status: str = "pending") -> Subscription:
    if plan not in VALID_PLANS:
        raise ValueError(f"Offre invalide : {plan}")
    if status not in VALID_STATUSES:
        raise ValueError(f"Statut d'abonnement invalide : {status}")
    sub = Subscription(user_id=user_id, plan=plan, status=status)
    db.add(sub)
    db.flush()
    return sub


def get_latest_for_user(db: Session, user_id: uuid.UUID) -> Subscription | None:
    stmt = (
        select(Subscription)
        .where(Subscription.user_id == user_id)
        .order_by(Subscription.created_at.desc())
        .limit(1)
    )
    return db.execute(stmt).scalar_one_or_none()


def activate(db: Session, subscription: Subscription) -> Subscription:
    subscription.status = "active"
    subscription.started_at = datetime.now(timezone.utc)
    db.flush()
    return subscription
