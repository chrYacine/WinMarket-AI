"""Lot 58 §D — purge ALL accounts' private RAG documents and AO dossier pieces on an explicitly
identified target. Reuses the EXISTING per-item deletion services exactly as the normal user-facing
routes do — never a bulk DROP/TRUNCATE, never new deletion logic:
  - src/web/knowledge/documents_service.py::delete_document (soft-delete + generation bump + physical
    file removal) for every active KnowledgeDocument, across every organization/owner.
  - src/web/database/repositories/ao_dossiers.py::delete_dossier (ORM cascade to pieces) plus
    src/web/ao_dossier/storage.py::remove_dossier_dir (path-checked physical cleanup) for every
    AoDossier, across every organization/owner.

Never touched (the authorization covers DOCUMENTS, never analysis history):
  - Analyses/AnalysisJob rows and their generated deliverables (analysis_documents) — a purged
    dossier's already-produced analysis result/PDF/DOCX is a separate table with no cascade here.
  - Accounts, memberships, organizations, subscriptions, profiles, capacities, scoring policies.
  - Staging preview cleanup is the EXISTING expiry sweeper's job (src/web/ao_dossier/expiry_sweeper.py),
    already running at application startup — never re-implemented here.

Traceable and resumable: each document/dossier is processed independently in its OWN transaction (one
failure never aborts the run or leaves a half-committed item), an item already deleted/absent on a
re-run is simply not in the inventory any more (safe to run twice), and a non-zero exit code means at
least one item could not be fully cleaned — reported by id only, never by content or secret.

Refuses (by default) if any job is queued/running, since a running job can recreate exactly the files
this script is removing — pass --force-with-active-jobs only after confirming coordination (stopping
the application, or accepting the residual race) explicitly.

Usage:
    python scripts/purge_documents.py --env-file <runtime>\\.env --credential-file <runtime>\\operator.token
    python scripts/purge_documents.py --env-file ... --credential-file ... --execute
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def run_purge(db, *, execute: bool, force_with_active_jobs: bool = False) -> dict:
    """The actual purge logic, importable and directly testable against a real Session — kept separate
    from CLI/env-file plumbing (see main()) so a test can exercise it against a real disposable
    PostgreSQL target without needing a real deployment-shaped DATABASE_URL. Returns a report dict;
    never raises for a single bad item (see module docstring)."""
    from sqlalchemy import select

    from src.web.ao_dossier import storage as dossier_storage
    from src.web.database.models import AnalysisJob, AoDossier, KnowledgeDocument
    from src.web.database.repositories import ao_dossiers as dossiers_repo
    from src.web.knowledge import documents_service

    active_jobs = db.execute(
        select(AnalysisJob.id).where(AnalysisJob.status.in_(("queued", "running")))
    ).scalars().all()
    if active_jobs and not force_with_active_jobs:
        return {"refused": True, "active_jobs": len(active_jobs), "documents": 0, "dossiers": 0, "errors": 0}

    documents = db.execute(select(KnowledgeDocument).where(KnowledgeDocument.status == "active")).scalars().all()
    dossiers = db.execute(select(AoDossier)).scalars().all()
    report = {"refused": False, "documents": len(documents), "dossiers": len(dossiers), "errors": 0, "executed": execute}
    if not execute:
        return report

    errors = 0
    for document in documents:
        document_id = document.id
        try:
            cleaned = documents_service.delete_document(
                db,
                organization_id=document.organization_id,
                owner_user_id=document.owner_user_id,
                document=document,
            )
            db.commit()
            if not cleaned:
                errors += 1
                print(f"  [document {document_id}] metadata deleted, physical file NOT fully removed — retry needed")
        except Exception as exc:  # noqa: BLE001 — one bad item must never abort the whole purge
            db.rollback()
            errors += 1
            print(f"  [document {document_id}] FAILED: {type(exc).__name__}")

    for dossier in dossiers:
        org_id, user_id, dossier_id = dossier.organization_id, dossier.user_id, dossier.id
        try:
            dossiers_repo.delete_dossier(db, dossier)
            db.commit()
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            errors += 1
            print(f"  [dossier {dossier_id}] FAILED (database): {type(exc).__name__}")
            continue
        try:
            dossier_storage.remove_dossier_dir(org_id, user_id, dossier_id)
        except Exception as exc:  # noqa: BLE001
            errors += 1
            print(f"  [dossier {dossier_id}] database row removed, physical files FAILED: {type(exc).__name__}")

    report["errors"] = errors
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--credential-file", type=Path, required=True)
    parser.add_argument("--execute", action="store_true", help="Without this flag: inventory/counts only, nothing deleted.")
    parser.add_argument("--force-with-active-jobs", action="store_true")
    args = parser.parse_args()

    from src.core.environment_guard import validate_environment, validate_path

    envfile = validate_path(args.env_file)
    credential_path = validate_path(args.credential_file)
    if (envfile.parent / "maintenance.lock").exists():
        print("REFUSED: runtime is in backup/restore maintenance.")
        return 1
    from dotenv import dotenv_values

    values = dotenv_values(envfile, interpolate=False)
    validate_environment(values, ROOT)
    if not values.get("DATABASE_URL"):
        parser.error("Explicit isolated database URL required")
    # This script's OWN storage root must never be the protected old installation's — belt-and-suspenders
    # on top of validate_environment (which already checked LOCAL_STORAGE_PATH/DATA_DIR/OUTPUT_DIR) since
    # a purge is destructive by nature.
    for name in ("DATA_DIR", "OUTPUT_DIR", "LOCAL_STORAGE_PATH"):
        if values.get(name):
            validate_path(values[name])
    os.environ["WM_ENV_FILE"] = str(envfile)

    from src.web.auth import manual_access as service
    from src.web.database.session import session_scope

    with session_scope() as db:
        credential = credential_path.read_text().strip()
        service.authenticate_operator(db, credential)

        report = run_purge(db, execute=args.execute, force_with_active_jobs=args.force_with_active_jobs)
        if report["refused"]:
            print(
                f"REFUSED: {report['active_jobs']} job(s) queued/running — a running job can recreate the "
                "files this script removes. Stop the application, or pass --force-with-active-jobs "
                "after explicitly confirming coordination."
            )
            return 1

        print(
            f"Inventory (no secrets, counts only): {report['documents']} active knowledge document(s), "
            f"{report['dossiers']} AO dossier(s), across every organization/owner in this target."
        )
        if not args.execute:
            print("Dry run (default) — nothing deleted. Re-run with --execute to actually purge.")
            return 0

        print(
            f"\nPurge complete: {report['documents']} document(s), {report['dossiers']} dossier(s) "
            f"processed, {report['errors']} error(s)."
        )
        return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
