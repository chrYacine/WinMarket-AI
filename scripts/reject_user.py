"""Reject a pending account: users.status = rejected.

Usage:
    python scripts/reject_user.py email@example.com
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.web.database.repositories import users as users_repo
from src.web.database.session import session_scope


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage : python scripts/reject_user.py email@example.com")
        raise SystemExit(1)
    email = sys.argv[1].strip().lower()

    with session_scope() as db:
        user = users_repo.get_by_email(db, email)
        if user is None:
            print(f"Aucun utilisateur avec l'email {email}")
            raise SystemExit(1)
        users_repo.set_status(db, user, "rejected")

    print(f"Compte refusé : {email}")


if __name__ == "__main__":
    main()
