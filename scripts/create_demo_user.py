"""Create the first WinMarket AI account: an already-active Starter user.

Interactive on purpose — the password is typed at a hidden prompt
(getpass), never passed as a command-line argument, never written to a
file, never committed to Git.

Usage:
    python scripts/create_demo_user.py
"""
# Lot 56: retained for regression/legacy reference, not an authorized data-import path.
import os as _guard_os
if __name__ == '__main__' and _guard_os.getenv('WM_DB_TEST_MODE') != '1':
    raise SystemExit('Legacy CLI disabled in this isolated delivery. Use local_env.py, operator_access.py and runtime_backup.py. Historical import requires a separate operation.')

import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.orm import Session

from src.web.auth import service as auth_service
from src.web.database.models import User
from src.web.database.repositories import subscriptions as subscriptions_repo
from src.web.database.repositories import users as users_repo


class DemoUserError(ValueError):
    """Raised for input validation failures — safe to print as-is."""


def create_active_starter_account(
    db: Session, *, email: str, password: str, first_name: str, last_name: str
) -> User:
    """Pure logic, no I/O — this is what scripts/migrate_history_to_postgresql.py
    and tests call directly, without going through the interactive prompts."""
    email = (email or "").strip().lower()
    if not auth_service.is_valid_email(email):
        raise DemoUserError("Adresse email invalide.")
    strength_error = auth_service.password_strength_error(password)
    if strength_error:
        raise DemoUserError(strength_error)
    if users_repo.get_by_email(db, email) is not None:
        raise DemoUserError(f"Un compte existe déjà avec l'email {email}.")

    user = users_repo.create_user(
        db,
        email=email,
        password_hash=auth_service.hash_password(password),
        first_name=first_name,
        last_name=last_name,
        status="active",
    )
    subscription = subscriptions_repo.create_subscription(db, user_id=user.id, plan="starter", status="pending")
    subscriptions_repo.activate(db, subscription)
    auth_service.create_private_organization_for_user(db, user)
    return user


def main() -> None:
    from src.web.database.session import session_scope

    print("Création du compte de démonstration WinMarket AI\n")

    first_name = input("Prénom : ").strip()
    last_name = input("Nom : ").strip()
    email = input("Email : ").strip().lower()

    while True:
        password = getpass.getpass("Mot de passe : ")
        strength_error = auth_service.password_strength_error(password)
        if strength_error:
            print(strength_error)
            continue
        password_confirm = getpass.getpass("Confirmer le mot de passe : ")
        if password != password_confirm:
            print("Les mots de passe ne correspondent pas. Réessayez.")
            continue
        break

    try:
        with session_scope() as db:
            user = create_active_starter_account(
                db, email=email, password=password, first_name=first_name, last_name=last_name
            )
            user_id = user.id
    except DemoUserError as exc:
        print(str(exc))
        raise SystemExit(1)

    print(f"\nCompte créé et activé : {email} (user_id={user_id})")
    print("Ce compte peut maintenant se connecter sur /login et accéder à /app.")


if __name__ == "__main__":
    main()
