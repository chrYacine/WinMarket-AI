"""B26-T1 — backup and restore, database + files, kept coherent together.

A backup that only dumps the database (or only copies files) is NOT a
restorable backup — `analysis_documents.storage_path` references depend on
the two staying in sync. This script always handles both together.

Subcommands:
    backup   <output_dir>
    restore  <backup_dir> --target-db-url URL --target-data-dir DIR
             [--target-local-storage-path DIR] [--target-output-dir DIR]
    purge    <backups_root> --older-than-days N [--execute]

Database support: SQLite (via SQLite's own Online Backup API,
`sqlite3.Connection.backup` — see `_sqlite_online_backup`, lot 55) and
PostgreSQL (via the `pg_dump`/`psql` CLI tools, if present on PATH). This
environment has neither `pg_dump` nor `psql` installed — the PostgreSQL
code path below is written and ready but genuinely UNVERIFIED here; running
it prints an explicit, unambiguous notice rather than a fabricated success
if those tools are missing. See docs/qa/ for the exact remaining command to
run once a real PostgreSQL + client tools are available.

File backup (lot 55 fix): the roots copied are derived from the actually
referenced configuration values (`DATA_DIR`, `LOCAL_STORAGE_PATH`,
`OUTPUT_DIR` — see `_collect_file_roots`), never a hardcoded two-name list.
The previous `for name in ("outputs", "historique")` loop silently missed
`data/knowledge/` (private documents, B03) and any other subtree — a real,
reproduced gap (see docs/qa/lot_55_20260926/RAPPORT.md). Every copied file
is hashed (sha256) and its permission bits recorded in
`files_manifest.json`, so a restore can prove — not assume — that files
came back identical.

Restore ALWAYS targets an explicit, isolated location — it refuses outright
if --target-db-url/--target-data-dir/--target-local-storage-path/
--target-output-dir match this process's own live configuration, so a
restore can never silently overwrite the live database or file store it
was run next to.

No real deletion ever happens without --execute (purge defaults to
dry-run) and no default retention duration is invented — --older-than-days
is a required argument, not a hardcoded business rule.
"""
from __future__ import annotations

# Lot 56: retained for regression/legacy reference, not an authorized data-import path.
import os as _guard_os
if __name__ == '__main__' and _guard_os.getenv('WM_DB_TEST_MODE') != '1':
    raise SystemExit('Legacy CLI disabled in this isolated delivery. Use local_env.py, operator_access.py and runtime_backup.py. Historical import requires a separate operation.')

import argparse
import hashlib
import json
import shutil
import sqlite3
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.config import DATA_DIR, DATABASE_URL, LOCAL_STORAGE_PATH, OUTPUT_DIR

MANIFEST_NAME = "manifest.json"
FILES_MANIFEST_NAME = "files_manifest.json"


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite:///")


def _sqlite_path(url: str) -> Path:
    return Path(url[len("sqlite:///"):])


def _db_dialect(url: str) -> str:
    if _is_sqlite(url):
        return "sqlite"
    if url.startswith("postgresql"):
        return "postgresql"
    return "unknown"


def _same_database_target(url_a: str, url_b: str) -> bool:
    """Whether two DATABASE_URL values point at the SAME physical database
    — used ONLY to refuse a restore that would overwrite the live target.

    DEFECT confirmed by review: a bare `url_a == url_b` string comparison
    (the original version of this check) is bypassed by any cosmetically
    different but equivalent URL — a different drive-letter case, or a
    `./` in the path (both resolve to the identical sqlite file) — and was
    reproduced LIVE overwriting a real sqlite database through exactly
    this gap. The sibling check for `--target-data-dir` two lines below
    was already correctly `.resolve()`-normalized; this brings the
    database-URL check to the same standard for sqlite, where "the same
    database" has an unambiguous, fully computable answer (the same file
    on disk).

    For non-sqlite dialects (PostgreSQL): a full answer would require
    actually connecting and comparing server identity (host/port/dbname
    can be spelled many equivalent ways — an IP vs. a hostname that
    resolves to it, a default port stated explicitly or not, parameter
    order in the URL, ...) — genuinely NOT fully resolvable from the
    string alone, and this project has no real PostgreSQL instance
    available in this environment to validate a connect-and-compare
    approach against. A best-effort case/whitespace-insensitive string
    comparison is applied instead, and this residual risk is stated
    plainly rather than papered over: a sufficiently differently-spelled
    but equivalent PostgreSQL URL could still slip past this specific
    guard. Treat any PostgreSQL restore's target as unverified by this
    function alone — confirm it by hand before running one for real.
    """
    if _is_sqlite(url_a) and _is_sqlite(url_b):
        return _sqlite_path(url_a).resolve() == _sqlite_path(url_b).resolve()
    if _is_sqlite(url_a) != _is_sqlite(url_b):
        return False  # one sqlite, one not — never the same target
    return url_a.strip().lower() == url_b.strip().lower()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _collect_file_roots() -> list[tuple[str, Path]]:
    """Actually-used storage roots, read from configuration — never a
    hardcoded directory-name list.

    `LOCAL_STORAGE_PATH` (knowledge/, ao_dossiers/, ...) defaults to
    `DATA_DIR` itself; `OUTPUT_DIR` is always `DATA_DIR / "outputs"`. In
    that (default, current) configuration all three resolve to nested or
    identical paths, so this collapses to a single root that already
    covers everything under `data/` — historique, knowledge, ao_examples,
    any future subtree — with no per-subtree name to keep in sync by hand.
    If `LOCAL_STORAGE_PATH` is ever pointed elsewhere (a separate disk),
    it is kept as its own, distinct root instead of being silently missed.
    """
    candidates = [
        ("data_dir", Path(DATA_DIR).resolve()),
        ("local_storage_path", Path(LOCAL_STORAGE_PATH).resolve()),
        ("output_dir", Path(OUTPUT_DIR).resolve()),
    ]
    candidates.sort(key=lambda item: len(item[1].parts))
    kept: list[tuple[str, Path]] = []
    for label, path in candidates:
        if any(path == kept_path or _is_within(path, kept_path) for _, kept_path in kept):
            continue
        kept.append((label, path))
    return kept


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _scan_tree(root: Path) -> dict[str, dict]:
    """sha256 + size + permission bits for every file under `root`, keyed
    by its POSIX-style relative path — the proof a restore is compared
    against (see `_verify_restored_tree`)."""
    entries: dict[str, dict] = {}
    if not root.exists():
        return entries
    for path in sorted(root.rglob("*")):
        if path.is_file():
            rel = path.relative_to(root).as_posix()
            st = path.stat()
            entries[rel] = {
                "sha256": _hash_file(path),
                "size": st.st_size,
                "mode": oct(stat.S_IMODE(st.st_mode)),
            }
    return entries


def _verify_restored_tree(root: Path, expected: dict[str, dict]) -> dict:
    """Recomputes hashes/permissions on a just-restored tree and compares
    them against the backup's recorded manifest — a restore that merely
    "completed without error" is not proof the files came back identical."""
    actual = _scan_tree(root)
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    mismatched = []
    for rel, exp in expected.items():
        act = actual.get(rel)
        if act is None:
            continue
        if act["sha256"] != exp["sha256"]:
            mismatched.append({"path": rel, "reason": "hash mismatch"})
        elif act["mode"] != exp["mode"]:
            mismatched.append({"path": rel, "reason": f"mode {exp['mode']} -> {act['mode']}"})
    ok = len(expected) - len(missing) - len(mismatched)
    return {"ok": ok, "missing": missing, "extra": extra, "mismatched": mismatched}


def _sqlite_online_backup(src_path: Path, dest_path: Path) -> None:
    """Uses SQLite's own Online Backup API (`sqlite3.Connection.backup`)
    instead of a raw file copy.

    A raw `shutil.copy2` of the main db file can capture a torn/
    inconsistent snapshot if anything is mid-write — including under
    rollback-journal mode (confirmed the real database's actual mode this
    lot, via a connection-free header-byte read: not WAL, but a raw copy
    is still not safe against a concurrently open connection). The backup
    API is transactionally consistent regardless of journal mode and is
    the documented, correct way to copy a live SQLite database.
    """
    if dest_path.exists():
        dest_path.unlink()
    src_conn = sqlite3.connect(f"file:{src_path.as_posix()}?mode=ro", uri=True)
    dest_conn = sqlite3.connect(dest_path)
    try:
        src_conn.backup(dest_conn)
    finally:
        dest_conn.close()
        src_conn.close()


def cmd_backup(args: argparse.Namespace) -> int:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    db_url = DATABASE_URL
    dialect = _db_dialect(db_url)

    db_backup_note = None
    db_backup_method = None
    if not db_url:
        db_backup_note = "no DATABASE_URL configured — nothing to back up"
    elif dialect == "sqlite":
        src = _sqlite_path(db_url)
        if not src.exists():
            db_backup_note = f"sqlite file not found: {src}"
        else:
            _sqlite_online_backup(src, output_dir / "db_backup.sqlite3")
            db_backup_note = "sqlite online backup (sqlite3.Connection.backup, journal-mode safe)"
            db_backup_method = "sqlite3.Connection.backup"
    elif dialect == "postgresql":
        if shutil.which("pg_dump") is None:
            db_backup_note = (
                "pg_dump not found on PATH — PostgreSQL backup NOT performed. "
                "Install the PostgreSQL client tools and re-run: "
                f"pg_dump --format=custom --file={output_dir / 'db_backup.pgdump'} \"{db_url}\""
            )
        else:
            dump_path = output_dir / "db_backup.pgdump"
            result = subprocess.run(
                ["pg_dump", "--format=custom", f"--file={dump_path}", db_url],
                capture_output=True, text=True,
            )
            db_backup_note = "pg_dump succeeded" if result.returncode == 0 else f"pg_dump FAILED: {result.stderr[:300]}"
    else:
        db_backup_note = f"unrecognized DATABASE_URL dialect ({dialect!r}) — not backed up"

    files_dir = output_dir / "files"
    files_dir.mkdir(exist_ok=True)
    roots = _collect_file_roots()
    copied_roots = []
    files_manifest: dict[str, dict] = {}
    for label, src_root in roots:
        if not src_root.exists():
            continue
        dest_root = files_dir / label
        shutil.copytree(src_root, dest_root, dirs_exist_ok=True)
        files_manifest[label] = _scan_tree(dest_root)
        copied_roots.append({"label": label, "source": str(src_root), "files": len(files_manifest[label])})

    (output_dir / FILES_MANIFEST_NAME).write_text(json.dumps(files_manifest, indent=2), encoding="utf-8")

    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "database_dialect": dialect,
        "database_backup_note": db_backup_note,
        "database_backup_method": db_backup_method,
        "file_roots": copied_roots,
    }
    (output_dir / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    print(f"Backup written to {output_dir}")
    print(f"  database: {db_backup_note}")
    for entry in copied_roots:
        print(f"  files [{entry['label']}]: {entry['files']} file(s) from {entry['source']}")
    if not copied_roots:
        print("  files: (none found)")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    backup_dir = Path(args.backup_dir)
    manifest_path = backup_dir / MANIFEST_NAME
    if not manifest_path.exists():
        print(f"Not a valid backup directory (no {MANIFEST_NAME}): {backup_dir}")
        return 1
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    target_db_url = args.target_db_url
    target_data_dir = Path(args.target_data_dir)
    _raw_lsp = getattr(args, "target_local_storage_path", None)
    _raw_out = getattr(args, "target_output_dir", None)
    target_local_storage_path = Path(_raw_lsp) if _raw_lsp else None
    target_output_dir = Path(_raw_out) if _raw_out else None

    # B23-T1/B26-T1/lot55: restore ALWAYS targets an isolated location —
    # never this process's own live configuration, so a restore can never
    # silently overwrite the real database or file store it happens to be
    # run next to. Each optional root target gets the same guard as
    # --target-data-dir; a missing guard here would be exactly the same
    # class of defect the URL-comparison guard above was written to close.
    if DATABASE_URL and _same_database_target(target_db_url, DATABASE_URL):
        print("Refusing to restore: --target-db-url matches this process's own live DATABASE_URL.")
        return 1
    if target_data_dir.resolve() == Path(DATA_DIR).resolve():
        print("Refusing to restore: --target-data-dir matches this process's own live DATA_DIR.")
        return 1
    if target_local_storage_path and target_local_storage_path.resolve() == Path(LOCAL_STORAGE_PATH).resolve():
        print("Refusing to restore: --target-local-storage-path matches this process's own live LOCAL_STORAGE_PATH.")
        return 1
    if target_output_dir and target_output_dir.resolve() == Path(OUTPUT_DIR).resolve():
        print("Refusing to restore: --target-output-dir matches this process's own live OUTPUT_DIR.")
        return 1

    target_dialect = _db_dialect(target_db_url)
    source_dialect = manifest.get("database_dialect")
    db_restore_note = None

    if target_dialect == "sqlite":
        db_backup_file = backup_dir / "db_backup.sqlite3"
        if source_dialect != "sqlite" or not db_backup_file.exists():
            db_restore_note = "no sqlite backup file in this backup — database not restored"
        else:
            target_path = _sqlite_path(target_db_url)
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(db_backup_file, target_path)
            db_restore_note = f"sqlite file restored to {target_path}"
    elif target_dialect == "postgresql":
        dump_file = backup_dir / "db_backup.pgdump"
        if not dump_file.exists():
            db_restore_note = "no pg_dump file in this backup — database not restored"
        elif shutil.which("pg_restore") is None:
            db_restore_note = (
                "pg_restore not found on PATH — PostgreSQL restore NOT performed. "
                f"Install the PostgreSQL client tools and re-run: "
                f"pg_restore --clean --if-exists --dbname=\"{target_db_url}\" {dump_file}"
            )
        else:
            result = subprocess.run(
                ["pg_restore", "--clean", "--if-exists", f"--dbname={target_db_url}", str(dump_file)],
                capture_output=True, text=True,
            )
            db_restore_note = "pg_restore succeeded" if result.returncode == 0 else f"pg_restore FAILED: {result.stderr[:300]}"
    else:
        db_restore_note = f"unrecognized target dialect ({target_dialect!r}) — database not restored"

    target_data_dir.mkdir(parents=True, exist_ok=True)
    files_src = backup_dir / "files"
    files_manifest_path = backup_dir / FILES_MANIFEST_NAME
    files_manifest = (
        json.loads(files_manifest_path.read_text(encoding="utf-8")) if files_manifest_path.exists() else {}
    )

    # lot55: each backed-up root (see _collect_file_roots) needs an
    # explicit target. "data_dir" — the common case, since LOCAL_STORAGE_PATH
    # and OUTPUT_DIR default to living underneath it — restores directly
    # into --target-data-dir so the restored layout mirrors the real one
    # (target_data_dir/knowledge, /historique, /outputs, ...). A label with
    # no corresponding --target-* argument is refused rather than silently
    # skipped, so a distinct root can never be dropped on the floor.
    label_targets = {
        "data_dir": target_data_dir,
        "local_storage_path": target_local_storage_path,
        "output_dir": target_output_dir,
    }

    restored_roots = []
    verify_reports: dict[str, dict] = {}
    if files_src.exists():
        for label_dir in sorted(files_src.iterdir()):
            if not label_dir.is_dir():
                continue
            label = label_dir.name
            target_root = label_targets.get(label)
            if target_root is None:
                print(f"Refusing to restore: backup contains root '{label}' but no matching --target-* was given.")
                return 1
            target_root.mkdir(parents=True, exist_ok=True)
            shutil.copytree(label_dir, target_root, dirs_exist_ok=True)
            restored_roots.append(label)
            expected = files_manifest.get(label)
            if expected is not None:
                verify_reports[label] = _verify_restored_tree(target_root, expected)

    print(f"Restore from {backup_dir} to target:")
    print(f"  database: {db_restore_note}")
    print(f"  files: {', '.join(restored_roots) or '(none)'}")

    restore_integrity_failed = False
    for label, report in verify_reports.items():
        problems = len(report["missing"]) + len(report["mismatched"])
        print(
            f"  hash/permission check [{label}]: {report['ok']} ok, "
            f"{len(report['missing'])} missing, {len(report['mismatched'])} mismatched"
        )
        if problems:
            restore_integrity_failed = True
            for m in report["missing"][:10]:
                print(f"    MISSING: {m}")
            for m in report["mismatched"][:10]:
                print(f"    MISMATCH: {m['path']} ({m['reason']})")
    if restore_integrity_failed:
        print("Restore integrity check FAILED — restored files do not match the backup manifest.")
        return 1

    # B26-T1: a restored snapshot's in-flight jobs (queued/running at
    # backup time) can never be resumed — the process that was executing
    # them is gone. Reused verbatim from B12-T1's own startup-reconciliation
    # SQL (never a copy/duplicate) so "interrupted" means the same thing
    # here as it does after a normal process restart.
    interrupted_count = None
    if target_dialect == "sqlite" and db_restore_note and "restored" in db_restore_note:
        try:
            interrupted_count = _reconcile_interrupted_jobs(target_db_url)
        except Exception as exc:
            print(f"Restore FAILED: interrupted-job reconciliation could not complete ({type(exc).__name__}).")
            return 1
        print(f"  interrupted jobs marked (queued/running snapshots cannot be resumed): {interrupted_count}")

    try:
        referential_report = _check_referential_integrity(target_db_url, target_data_dir) if target_dialect == "sqlite" else None
    except Exception as exc:
        print(f"Restore FAILED: file-reference verification could not complete ({type(exc).__name__}).")
        return 1
    if referential_report is not None:
        print(f"  referential check: {referential_report['ok']} ok, {referential_report['missing']} missing file(s)")
        if referential_report['missing']:
            print("Restore FAILED: referenced files are missing.")
            return 1

    return 0


def _reconcile_interrupted_jobs(target_db_url: str) -> int:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from src.web.database.repositories import analysis_jobs as analysis_jobs_repo

    engine = create_engine(target_db_url)
    try:
        Session = sessionmaker(bind=engine)
        session = Session()
        try:
            count = analysis_jobs_repo.reconcile_stale_jobs(session, staleness_seconds=0)
            session.commit()
            return count
        finally:
            session.close()
    finally:
        engine.dispose()


def _check_referential_integrity(target_db_url: str, target_data_dir: Path) -> dict:
    """A restored backup whose DB references files that were never actually
    copied back is NOT a complete restore, even though the copy step
    reported no error — this checks that every AnalysisDocument row's
    storage_path resolves to a real file under the restored data dir."""
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker

    from src.web.database.models import AnalysisDocument
    from src.web.storage.service import LocalStorageService

    ok, missing = 0, 0
    engine = create_engine(target_db_url)
    try:
        Session = sessionmaker(bind=engine)
        session = Session()
        try:
            storage = LocalStorageService(root=target_data_dir)
            for (storage_path,) in session.execute(select(AnalysisDocument.storage_path)).all():
                if storage_path and storage.exists(storage_path):
                    ok += 1
                else:
                    missing += 1
        finally:
            session.close()
    finally:
        engine.dispose()
    return {"ok": ok, "missing": missing}


def cmd_purge(args: argparse.Namespace) -> int:
    root = Path(args.backups_root)
    if not root.exists():
        print(f"No such directory: {root}")
        return 1
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.older_than_days)

    candidates = []
    for child in sorted(root.iterdir()):
        manifest_path = child / MANIFEST_NAME
        if not child.is_dir() or not manifest_path.exists():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            created_at = datetime.fromisoformat(manifest["created_at"])
        except Exception:
            continue
        if created_at < cutoff:
            candidates.append(child)

    verb = "Deleting" if args.execute else "[dry-run] Would delete"
    for backup_dir in candidates:
        print(f"{verb}: {backup_dir}")
        if args.execute:
            shutil.rmtree(backup_dir)

    if not args.execute and candidates:
        print(f"\n{len(candidates)} backup(s) older than {args.older_than_days} day(s) — re-run with --execute to actually delete.")
    elif not candidates:
        print(f"No backup older than {args.older_than_days} day(s) found under {root}.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_backup = sub.add_parser("backup")
    p_backup.add_argument("output_dir")
    p_backup.set_defaults(func=cmd_backup)

    p_restore = sub.add_parser("restore")
    p_restore.add_argument("backup_dir")
    p_restore.add_argument("--target-db-url", required=True)
    p_restore.add_argument("--target-data-dir", required=True)
    p_restore.add_argument(
        "--target-local-storage-path",
        help="Required only if the backup contains a distinct 'local_storage_path' root "
        "(LOCAL_STORAGE_PATH configured outside DATA_DIR at backup time).",
    )
    p_restore.add_argument(
        "--target-output-dir",
        help="Required only if the backup contains a distinct 'output_dir' root "
        "(OUTPUT_DIR configured outside DATA_DIR at backup time).",
    )
    p_restore.set_defaults(func=cmd_restore)

    p_purge = sub.add_parser("purge")
    p_purge.add_argument("backups_root")
    p_purge.add_argument("--older-than-days", type=int, required=True)
    p_purge.add_argument("--execute", action="store_true")
    p_purge.set_defaults(func=cmd_purge)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
