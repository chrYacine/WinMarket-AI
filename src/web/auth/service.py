"""Authentication business logic: password hashing, registration, login.

Password hashing uses Argon2 (argon2-cffi) — passwords are never stored or
logged in clear text. This module knows about users/subscriptions
repositories but nothing about HTTP, cookies or sessions (see
src/web/auth/dependencies.py and src/web/auth/session.py for that).
"""
from __future__ import annotations

import re

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy.orm import Session

from src.web.database.models import User
from src.web.database.repositories import memberships as memberships_repo
from src.web.database.repositories import organizations as organizations_repo
from src.web.database.repositories import subscriptions as subscriptions_repo
from src.web.database.repositories import users as users_repo

_hasher = PasswordHasher()

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class RegistrationError(ValueError):
    """User-facing validation error raised during registration."""


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def is_valid_email(email: str) -> bool:
    return bool(_EMAIL_RE.match((email or "").strip()))


def password_strength_error(password: str) -> str | None:
    if not password or len(password) < 10:
        return "Le mot de passe doit contenir au moins 10 caractères."
    if not re.search(r"[a-z]", password) or not re.search(r"[A-Z]", password) or not re.search(r"\d", password):
        return "Le mot de passe doit contenir au moins une majuscule, une minuscule et un chiffre."
    return None


def register_starter_user(
    db: Session,
    *,
    email: str,
    password: str,
    password_confirm: str,
    first_name: str,
    last_name: str,
    company: str | None = None,
    job_title: str | None = None,
    phone: str | None = None,
) -> User:
    """Create a pending User + a pending starter Subscription.

    Raises RegistrationError with a message safe to show the user.
    """
    email = (email or "").strip().lower()
    first_name = (first_name or "").strip()
    last_name = (last_name or "").strip()

    if not is_valid_email(email):
        raise RegistrationError("Adresse email invalide.")
    if not first_name:
        raise RegistrationError("Le prénom est requis.")
    if not last_name:
        raise RegistrationError("Le nom est requis.")
    if password != password_confirm:
        raise RegistrationError("Les mots de passe ne correspondent pas.")
    strength_error = password_strength_error(password)
    if strength_error:
        raise RegistrationError(strength_error)
    if users_repo.get_by_email(db, email) is not None:
        # Deliberately generic — do not confirm which email is registered.
        raise RegistrationError("Impossible de créer ce compte. Vérifiez vos informations.")

    user = users_repo.create_user(
        db,
        email=email,
        password_hash=hash_password(password),
        first_name=first_name,
        last_name=last_name,
        company=company,
        job_title=job_title,
        phone=phone,
        status="pending",
    )
    subscriptions_repo.create_subscription(db, user_id=user.id, plan="starter", status="pending")
    subscriptions_repo.get_latest_for_user(db, user.id).access_origin = "manual_without_payment"
    create_private_organization_for_user(db, user)
    return user


def create_private_organization_for_user(db: Session, user: User):
    """Every user gets their own private Organization + an
    `organization_admin` Membership, atomically with the rest of
    registration (same transaction — the caller commits once).

    Deliberately never groups two users by a shared `company` value or email
    domain: two signups from the same company get two separate,
    unconnected organizations unless a human explicitly adds a Membership
    later (ticket B02 section 2 — no implicit collective sharing).
    """
    org_name = (user.company or "").strip() or f"Organisation de {user.full_name}".strip() or f"Organisation {user.email}"
    org = organizations_repo.create_organization(db, name=org_name)
    memberships_repo.create_membership(
        db, user_id=user.id, organization_id=org.id, role="organization_admin", status="active"
    )
    return org


def authenticate(db: Session, *, email: str, password: str) -> User | None:
    """Return the User on success, None otherwise. Never raises on bad input."""
    user = users_repo.get_by_email(db, (email or "").strip().lower())
    if user is None:
        # Hash a dummy value so failure timing doesn't reveal whether the
        # email exists (Argon2 hashing dominates the request time either way).
        _hasher.hash(password or "dummy-password-for-constant-time")
        return None
    if not verify_password(password or "", user.password_hash):
        return None
    return user
