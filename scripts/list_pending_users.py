"""List all Starter accounts awaiting manual validation.

Usage:
    python scripts/list_pending_users.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.web.database.repositories import users as users_repo
from src.web.database.session import session_scope


def main() -> None:
    with session_scope() as db:
        pending = users_repo.list_pending(db)
        if not pending:
            print("Aucun compte en attente de validation.")
            return
        print(f"{len(pending)} compte(s) en attente :\n")
        for user in pending:
            print(f"  {user.email:<40} {user.full_name:<30} créé le {user.created_at:%d/%m/%Y %H:%M}")


if __name__ == "__main__":
    main()
