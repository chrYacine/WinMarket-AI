"""List every registered account, whatever its status.

Usage:
    python scripts/list_users.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.web.database.repositories import users as users_repo
from src.web.database.repositories import subscriptions as subscriptions_repo
from src.web.database.session import session_scope


def main() -> None:
    with session_scope() as db:
        all_users = users_repo.list_all(db)
        if not all_users:
            print("Aucun compte enregistré.")
            return
        print(f"{len(all_users)} compte(s) :\n")
        for user in all_users:
            subscription = subscriptions_repo.get_latest_for_user(db, user.id)
            plan = f"{subscription.plan}/{subscription.status}" if subscription else "aucun forfait"
            print(
                f"  {user.email:<40} {user.full_name:<25} statut={user.status:<10} "
                f"forfait={plan:<20} créé le {user.created_at:%d/%m/%Y %H:%M}"
            )


if __name__ == "__main__":
    main()
