"""Migrate the existing JSON history (data/historique/) into PostgreSQL.

Reads:
  - data/historique/historique_ao.json        (summary rows)
  - data/historique/analyses/{job_id}.json     (full AOContext + ScoringResult,
                                                 when the row has a job_id)

Writes one `analyses` row per entry, owned by the given user (your first
active Starter account — see scripts/create_demo_user.py).

Idempotent: matched by `analyses.job_id`. Rows that already have a job_id
use it directly; older rows with no job_id get a synthetic
"legacy:<date>:<title>" key instead, so re-running this script never
creates duplicates. No source file is ever modified or deleted — a
timestamped backup is taken first regardless.

B23-T1 (DEFECT confirmed): each row's `create_analysis` call is now wrapped
in its own SAVEPOINT (`db.begin_nested()`), not just a per-row Python
try/except. Without this, a single row whose INSERT violates a constraint
at `db.flush()` time leaves the surrounding transaction ABORTED on a real
PostgreSQL backend — every subsequent row's flush would then also fail
(cascading, misleading "N errors" instead of "1 error"), and the final
`session_scope()` commit could discard every row already counted as
"migrated" in the printed report, a silent partial-import failure the
script's own summary would not reveal. A SAVEPOINT confines a bad row's
rollback to that row alone; every other row's already-flushed INSERT
survives to the final commit.

--dry-run: reports exactly what WOULD be migrated/skipped/erred without
writing a single row (no `create_analysis` call, no commit) — for
reviewing a real historique before committing to the write.

Usage:
    python scripts/migrate_history_to_postgresql.py owner@example.com [--dry-run]
"""
# Lot 56: retained for regression/legacy reference, not an authorized data-import path.
import os as _guard_os
if __name__ == '__main__' and _guard_os.getenv('WM_DB_TEST_MODE') != '1':
    raise SystemExit('Legacy CLI disabled in this isolated delivery. Use local_env.py, operator_access.py and runtime_backup.py. Historical import requires a separate operation.')

import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.config import DATA_DIR
from src.web.database.repositories import analyses as analyses_repo
from src.web.database.repositories import memberships as memberships_repo
from src.web.database.repositories import users as users_repo
from src.web.database.session import session_scope

HIST_FILE = DATA_DIR / "historique" / "historique_ao.json"
ANALYSES_DIR = DATA_DIR / "historique" / "analyses"
BACKUP_ROOT = DATA_DIR / "historique" / "_migration_backups"


def _backup() -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = BACKUP_ROOT / timestamp
    backup_dir.mkdir(parents=True, exist_ok=True)
    if HIST_FILE.exists():
        shutil.copy2(HIST_FILE, backup_dir / HIST_FILE.name)
    if ANALYSES_DIR.exists():
        shutil.copytree(ANALYSES_DIR, backup_dir / "analyses", dirs_exist_ok=True)
    return backup_dir


def _dedup_key(record: dict) -> str:
    if record.get("job_id"):
        return record["job_id"]
    return f"legacy:{record.get('date', '')}:{(record.get('titre') or '')[:40]}"


def main() -> None:
    args = [a for a in sys.argv[1:] if a != "--dry-run"]
    dry_run = "--dry-run" in sys.argv[1:]
    if len(args) != 1:
        print("Usage : python scripts/migrate_history_to_postgresql.py owner@example.com [--dry-run]")
        raise SystemExit(1)
    owner_email = args[0].strip().lower()

    if not HIST_FILE.exists():
        print(f"Aucun historique trouvé ({HIST_FILE}) — rien à migrer.")
        return

    records = json.loads(HIST_FILE.read_text(encoding="utf-8"))
    detected = len(records)
    # B23-T1: --dry-run takes NO filesystem action either — a real backup
    # write would itself be a side effect a pure preview must not have.
    backup_dir = None if dry_run else _backup()

    migrated = 0
    skipped_existing = 0
    errors = 0

    with session_scope() as db:
        owner = users_repo.get_by_email(db, owner_email)
        if owner is None:
            print(
                f"Aucun utilisateur trouvé avec l'email {owner_email}. "
                f"Crée-le d'abord avec scripts/create_demo_user.py."
            )
            raise SystemExit(1)

        owner_memberships = memberships_repo.list_active_for_user(db, owner.id)
        if len(owner_memberships) != 1:
            print(
                f"{owner_email} a {len(owner_memberships)} organisation(s) active(s) "
                f"— rattachement ambigu, migration refusée. Ce script exige exactement "
                f"une organisation active pour le propriétaire ciblé."
            )
            raise SystemExit(1)
        owner_organization_id = owner_memberships[0].organization_id

        for record in records:
            title_for_log = record.get("titre", "?")
            try:
                job_id = _dedup_key(record)
                if analyses_repo.get_by_job_id(db, job_id) is not None:
                    skipped_existing += 1
                    continue

                result_data = record
                summary_data = None
                if record.get("job_id"):
                    detail_path = ANALYSES_DIR / f"{record['job_id']}.json"
                    if detail_path.exists():
                        result_data = json.loads(detail_path.read_text(encoding="utf-8"))
                        summary_data = record

                if dry_run:
                    # Read-only check above already ran (idempotence is
                    # still previewed accurately); no write, no savepoint
                    # needed — nothing here can fail at flush time.
                    migrated += 1
                    continue

                budget = record.get("budget")
                # B23-T1 (DEFECT confirmed): each row's write is now its own
                # SAVEPOINT — without this, a single row whose INSERT fails
                # at db.flush() (inside create_analysis) leaves the WHOLE
                # surrounding transaction aborted on a real PostgreSQL
                # backend; every later row's flush would then also fail
                # (cascading, misleading error counts), and the final
                # session_scope() commit could silently discard every row
                # already counted "migrated" above — a partial-import
                # failure this script's own report would not reveal. The
                # SAVEPOINT confines that rollback to THIS row alone.
                with db.begin_nested():
                    analyses_repo.create_analysis(
                        db,
                        user_id=owner.id,
                        organization_id=owner_organization_id,
                        job_id=job_id,
                        title=record.get("titre"),
                        client_name=record.get("client"),
                        sector=record.get("secteur"),
                        score=record.get("score"),
                        decision=record.get("decision"),
                        budget=str(budget) if budget is not None else None,
                        technologies=record.get("techs") or [],
                        result_data=result_data,
                        summary_data=summary_data,
                    )
                migrated += 1
            except Exception as exc:  # noqa: BLE001 — one bad row must not abort the run
                errors += 1
                print(f"  Erreur sur l'analyse {title_for_log!r} : {exc}")

    print("\n=== Rapport de migration ===" + (" (DRY-RUN — aucune écriture réelle)" if dry_run else ""))
    print(f"{detected} analyse(s) détectée(s) dans l'historique JSON")
    print(f"{migrated} analyse(s) {'à migrer' if dry_run else 'migrée(s)'}")
    print(f"{skipped_existing} analyse(s) déjà présente(s) (ignorée(s), script rejouable)")
    print(f"{errors} erreur(s)")
    if dry_run:
        print("Aucune écriture effectuée (--dry-run) — aucun backup créé, aucune ligne insérée.")
    else:
        print(f"Backup créé dans {backup_dir}")
    print(f"Utilisateur propriétaire : {owner_email} ({owner.id})")
    print("\nAucun fichier source n'a été modifié ni supprimé.")


if __name__ == "__main__":
    main()
