"""B02 data migration — assign a private Organization to every existing
User, then attach their existing analyses/documents to it.

Must run AFTER `alembic upgrade 0002` (which adds the organizations/
memberships tables and nullable organization_id columns) and BEFORE
`alembic upgrade 0003` (which makes those columns NOT NULL and adds the
composite consistency FK — that revision refuses to run while any row is
still unassigned).

Design:
- One Organization + one `organization_admin` Membership per user that
  doesn't already have one (idempotent: re-running skips users who already
  have an active membership, and skips analyses/documents whose
  organization_id is already set — never creates a second organization for
  the same user, never reassigns an already-assigned row).
- Never groups two users into one organization because they share a
  `company` string or email domain — ticket B02 is explicit that this must
  never happen implicitly.
- Every analysis/document is attached to *its own user's* private
  organization — never guessed from title, client, or any other heuristic.
- Flags (but does not fix) rows that can't be attributed unambiguously:
  an analysis whose user_id doesn't resolve to any user row. That should be
  impossible under the existing NOT NULL FK on analyses.user_id, but the
  check exists anyway as a hard safety net — this script does not do
  "silent repair", it aborts the whole run at the transaction level.
- --dry-run prints counts and exits 0 without writing.
- Runs as one DB transaction: commits only if every step succeeds.

Usage:
    python scripts/backfill_b02_organizations.py --dry-run
    python scripts/backfill_b02_organizations.py
"""
from __future__ import annotations

# Lot 56: retained for regression/legacy reference, not an authorized data-import path.
import os as _guard_os
if __name__ == '__main__' and _guard_os.getenv('WM_DB_TEST_MODE') != '1':
    raise SystemExit('Legacy CLI disabled in this isolated delivery. Use local_env.py, operator_access.py and runtime_backup.py. Historical import requires a separate operation.')

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from src.web.database.models import Analysis, AnalysisDocument, Membership, Organization, User
from src.web.database.repositories import memberships as memberships_repo
from src.web.database.repositories import organizations as organizations_repo
from src.web.database.session import session_scope


def _org_name_for(user: User) -> str:
    return (user.company or "").strip() or f"Organisation de {user.full_name}".strip() or f"Organisation {user.email}"


class _Abort(Exception):
    """Raised to make session_scope() roll back instead of committing —
    never caught by anything except run(), and never used for a case that
    should instead be silently skipped (idempotency uses plain skips, not
    this)."""


def run(dry_run: bool) -> int:
    try:
        return _run(dry_run)
    except _Abort as exc:
        print(f"ABORT : {exc}")
        return 1


def _run(dry_run: bool) -> int:
    with session_scope() as db:
        users = list(db.execute(select(User)).scalars().all())

        users_needing_org: list[User] = []
        for user in users:
            existing = db.execute(
                select(Membership).where(Membership.user_id == user.id, Membership.status == "active")
            ).scalars().first()
            if existing is None:
                users_needing_org.append(user)

        orphan_analyses = list(db.execute(
            select(Analysis).where(Analysis.organization_id.is_(None))
        ).scalars().all())
        orphan_documents = list(db.execute(
            select(AnalysisDocument).where(AnalysisDocument.organization_id.is_(None))
        ).scalars().all())

        # Hard safety net — should be structurally impossible given the
        # existing NOT NULL FK on analyses.user_id, but this script never
        # silently attributes an orphaned row to the wrong owner.
        known_user_ids = {u.id for u in users}
        unattributable = [a for a in orphan_analyses if a.user_id not in known_user_ids]
        if unattributable:
            raise _Abort(
                f"{len(unattributable)} analyse(s) référencent un user_id introuvable "
                f"— rattachement impossible sans deviner le propriétaire. IDs : "
                f"{[str(a.id) for a in unattributable]}"
            )

        print(f"{len(users)} utilisateur(s) au total")
        print(f"{len(users_needing_org)} utilisateur(s) sans organisation active (à créer)")
        print(f"{len(orphan_analyses)} analyse(s) sans organization_id (à rattacher)")
        print(f"{len(orphan_documents)} document(s) sans organization_id (à rattacher)")

        if dry_run:
            print("\n--dry-run : aucune écriture effectuée.")
            return 0

        user_to_org: dict = {}
        created_orgs = 0
        for user in users_needing_org:
            org = organizations_repo.create_organization(db, name=_org_name_for(user))
            memberships_repo.create_membership(
                db, user_id=user.id, organization_id=org.id, role="organization_admin", status="active"
            )
            user_to_org[user.id] = org.id
            created_orgs += 1

        # For users who already had a membership (idempotent re-run), reuse it.
        for user in users:
            if user.id in user_to_org:
                continue
            active = memberships_repo.list_active_for_user(db, user.id)
            if active:
                user_to_org[user.id] = active[0].organization_id

        attached_analyses = 0
        for analysis in orphan_analyses:
            org_id = user_to_org.get(analysis.user_id)
            if org_id is None:
                raise _Abort(f"aucune organisation résolue pour l'analyse {analysis.id} (user_id={analysis.user_id})")
            analysis.organization_id = org_id
            attached_analyses += 1

        db.flush()  # so documents can see their parent analysis's now-set organization_id

        attached_documents = 0
        for document in orphan_documents:
            parent = db.get(Analysis, document.analysis_id)
            if parent is None or parent.organization_id is None:
                raise _Abort(f"document {document.id} n'a pas d'analyse parente rattachée à une organisation")
            document.organization_id = parent.organization_id
            attached_documents += 1

        print(f"\n{created_orgs} organisation(s) créée(s)")
        print(f"{attached_analyses} analyse(s) rattachée(s)")
        print(f"{attached_documents} document(s) rattaché(s)")
        print("\nTransaction validée.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Print counts only, write nothing.")
    args = parser.parse_args()
    raise SystemExit(run(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
