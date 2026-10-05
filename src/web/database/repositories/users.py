"""Pure data-access functions for the `users` table.

No password hashing, no business rules here — see src/web/auth/service.py
for that. This module only talks to the database.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.web.database.models import User

VALID_STATUSES = {"pending", "active", "rejected", "disabled"}


def get_by_id(db: Session, user_id: uuid.UUID | str) -> User | None:
    return db.get(User, user_id)


def get_by_email(db: Session, email: str) -> User | None:
    stmt = select(User).where(User.email == email.strip().lower())
    return db.execute(stmt).scalar_one_or_none()


def create_user(
    db: Session,
    *,
    email: str,
    password_hash: str,
    first_name: str,
    last_name: str,
    company: str | None = None,
    job_title: str | None = None,
    phone: str | None = None,
    status: str = "pending",
) -> User:
    user = User(
        email=email.strip().lower(),
        password_hash=password_hash,
        first_name=first_name.strip(),
        last_name=last_name.strip(),
        company=(company or "").strip() or None,
        job_title=(job_title or "").strip() or None,
        phone=(phone or "").strip() or None,
        status=status,
    )
    db.add(user)
    db.flush()
    return user


def set_status(db: Session, user: User, status: str) -> User:
    if status not in VALID_STATUSES:
        raise ValueError(f"Statut utilisateur invalide : {status}")
    user.status = status
    db.flush()
    return user


def record_login(db: Session, user: User) -> User:
    user.last_login_at = datetime.now(timezone.utc)
    db.flush()
    return user


def list_pending(db: Session) -> list[User]:
    stmt = select(User).where(User.status == "pending").order_by(User.created_at)
    return list(db.execute(stmt).scalars().all())


def list_all(db: Session) -> list[User]:
    stmt = select(User).order_by(User.created_at)
    return list(db.execute(stmt).scalars().all())
