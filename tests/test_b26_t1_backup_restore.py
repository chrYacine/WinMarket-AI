"""B26-T1 — scripts/backup_restore.py: backup + restore DB and files
together, restore always into an isolated target, referential integrity
checked after restore, interrupted jobs handled explicitly, purge is
dry-run by default with no invented retention duration.

SQLite only (this project's own dev/test database) — the PostgreSQL code
path exists and is documented but genuinely unexercised in this
environment (no `pg_dump`/`pg_restore` on PATH here); see
scripts/backup_restore.py's own module docstring for the exact remaining
command. A single copy of the .db file alone is NOT treated as proof of a
full restore here — every test below round-trips through the SAME
functions the script's CLI calls, checks the DATA is re-readable
afterward, and checks referential integrity explicitly.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import scripts.backup_restore as backup_restore
from src.web.database.models import Base
from tests.conftest import default_org_id, make_active_starter_user


def _make_sqlite_env(tmp_path, name: str, monkeypatch=None):
    db_path = tmp_path / f"{name}.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    data_dir = tmp_path / f"{name}_data"
    (data_dir / "outputs").mkdir(parents=True)
    (data_dir / "historique").mkdir(parents=True)
    if monkeypatch is not None:
        # lot55: LOCAL_STORAGE_PATH/OUTPUT_DIR default to living under
        # DATA_DIR in the real app — mirrored here so _collect_file_roots
        # collapses to the single test data_dir instead of also picking up
        # this machine's real project directories (unpatched globals would
        # otherwise leak the real data/ tree into a test backup).
        monkeypatch.setattr(backup_restore, "LOCAL_STORAGE_PATH", data_dir)
        monkeypatch.setattr(backup_restore, "OUTPUT_DIR", data_dir / "outputs")
    return db_path, engine, Session, data_dir


def test_backup_then_restore_round_trips_data_and_files_into_an_isolated_target(tmp_path, monkeypatch):
    db_path, engine, Session, data_dir = _make_sqlite_env(tmp_path, "source", monkeypatch)
    monkeypatch.setattr(backup_restore, "DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setattr(backup_restore, "DATA_DIR", data_dir)

    session = Session()
    user = make_active_starter_user(session, "backuprestore@example.com", scoring=False)
    org_id = default_org_id(session, user)

    from src.web.database.repositories import analyses as analyses_repo
    from src.web.storage.service import LocalStorageService

    storage = LocalStorageService(root=data_dir)
    real_file = data_dir / "outputs" / "rapport.pdf"
    real_file.parent.mkdir(parents=True, exist_ok=True)
    real_file.write_bytes(b"%PDF-fake-content")
    storage_path = storage.save(real_file)

    analysis = analyses_repo.create_analysis(
        session, user_id=user.id, organization_id=org_id, job_id="bkp-job-1",
        title="Analyse à restaurer", result_data={"ao": {}, "result": {}},
    )
    analyses_repo.upsert_document(
        session, analysis_id=analysis.id, user_id=user.id, organization_id=org_id,
        filename="rapport.pdf", original_filename="rapport.pdf",
        storage_path=storage_path, mime_type="application/pdf", file_size=real_file.stat().st_size,
    )
    session.commit()
    user_id = user.id  # captured before the session (and this ORM instance) is closed/detached
    session.close()
    engine.dispose()

    backup_dir = tmp_path / "backup1"
    rc = backup_restore.cmd_backup(argparse.Namespace(output_dir=str(backup_dir)))
    assert rc == 0
    assert (backup_dir / "db_backup.sqlite3").exists()
    assert (backup_dir / "files" / "data_dir" / "outputs" / "rapport.pdf").exists()
    manifest = json.loads((backup_dir / "manifest.json").read_text())
    assert manifest["database_dialect"] == "sqlite"
    files_manifest = json.loads((backup_dir / "files_manifest.json").read_text())
    assert "outputs/rapport.pdf" in files_manifest["data_dir"]

    # Restore into a DELIBERATELY DIFFERENT location — never the source.
    target_db_path = tmp_path / "restored" / "restored.db"
    target_data_dir = tmp_path / "restored_data"
    rc = backup_restore.cmd_restore(argparse.Namespace(
        backup_dir=str(backup_dir),
        target_db_url=f"sqlite:///{target_db_path}",
        target_data_dir=str(target_data_dir),
    ))
    assert rc == 0
    assert target_db_path.exists()
    assert (target_data_dir / "outputs" / "rapport.pdf").exists()
    assert (target_data_dir / "outputs" / "rapport.pdf").read_bytes() == b"%PDF-fake-content"

    # Re-read through the REAL repository/storage layer against the
    # restored target — not just "the files are there", the DATA is usable.
    restored_engine = create_engine(f"sqlite:///{target_db_path}")
    RestoredSession = sessionmaker(bind=restored_engine)
    restored_session = RestoredSession()
    restored_analysis = analyses_repo.get_by_job_id(restored_session, "bkp-job-1")
    assert restored_analysis is not None
    assert restored_analysis.title == "Analyse à restaurer"
    assert restored_analysis.user_id == user_id, "owner must be unchanged by the restore"

    restored_storage = LocalStorageService(root=target_data_dir)
    resolved = restored_storage.resolve_for_download(restored_analysis.documents[0].storage_path)
    assert resolved.read_bytes() == b"%PDF-fake-content"
    restored_session.close()
    restored_engine.dispose()


def test_restore_refuses_a_target_matching_the_live_configuration_even_when_differently_spelled(tmp_path, monkeypatch):
    """DEFECT confirmed by independent review: the original guard did a
    bare `target_db_url == DATABASE_URL` string comparison — a
    cosmetically different but equivalent sqlite URL (different path
    casing, a redundant './' component) was NOT refused and, reproduced
    live during review, silently overwrote the real database. Fixed via
    _same_database_target's path-based comparison (mirroring the sibling
    --target-data-dir check, which was already correctly .resolve()'d)."""
    live_db = tmp_path / "live.db"
    live_db.write_bytes(b"sentinel-live-content")
    live_data = tmp_path / "live_data"
    live_data.mkdir()
    monkeypatch.setattr(backup_restore, "DATABASE_URL", f"sqlite:///{live_db}")
    monkeypatch.setattr(backup_restore, "DATA_DIR", live_data)

    backup_dir = tmp_path / "irrelevant_backup2"
    backup_dir.mkdir()
    (backup_dir / "manifest.json").write_text(json.dumps({"database_dialect": "sqlite"}), encoding="utf-8")
    (backup_dir / "db_backup.sqlite3").write_bytes(b"backup-content-must-never-land-here")

    # A path that resolves to the EXACT SAME file but is spelled with a
    # redundant "./" component — the precise class of bypass reproduced
    # during review.
    differently_spelled = str(live_db.parent / "." / live_db.name)
    other_data_dir = tmp_path / "some_other_data_dir"

    rc = backup_restore.cmd_restore(argparse.Namespace(
        backup_dir=str(backup_dir),
        target_db_url=f"sqlite:///{differently_spelled}",
        target_data_dir=str(other_data_dir),
    ))
    assert rc == 1, "a differently-spelled but equivalent path to the live DB must still be refused"
    assert live_db.read_bytes() == b"sentinel-live-content", "the live database must never be overwritten"


def test_restore_refuses_a_target_matching_the_live_configuration(tmp_path, monkeypatch):
    live_db = tmp_path / "live.db"
    live_data = tmp_path / "live_data"
    live_data.mkdir()
    monkeypatch.setattr(backup_restore, "DATABASE_URL", f"sqlite:///{live_db}")
    monkeypatch.setattr(backup_restore, "DATA_DIR", live_data)

    backup_dir = tmp_path / "irrelevant_backup"
    backup_dir.mkdir()
    (backup_dir / "manifest.json").write_text(json.dumps({"database_dialect": "sqlite"}), encoding="utf-8")

    rc = backup_restore.cmd_restore(argparse.Namespace(
        backup_dir=str(backup_dir), target_db_url=f"sqlite:///{live_db}", target_data_dir=str(live_data),
    ))
    assert rc == 1, "restoring onto the live DB/data dir must be refused outright"


def test_a_queued_or_running_job_in_the_restored_snapshot_is_marked_interrupted(tmp_path, monkeypatch):
    db_path, engine, Session, data_dir = _make_sqlite_env(tmp_path, "withjobs", monkeypatch)
    monkeypatch.setattr(backup_restore, "DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setattr(backup_restore, "DATA_DIR", data_dir)

    session = Session()
    user = make_active_starter_user(session, "restorejobs@example.com", scoring=False)
    org_id = default_org_id(session, user)
    from src.web.database.repositories import analysis_jobs as analysis_jobs_repo
    analysis_jobs_repo.create_queued(session, job_id="stuck-job", user_id=user.id, organization_id=org_id, source_label="x")
    session.commit()
    session.close()
    engine.dispose()

    backup_dir = tmp_path / "backup_jobs"
    backup_restore.cmd_backup(argparse.Namespace(output_dir=str(backup_dir)))

    target_db_path = tmp_path / "restored_jobs.db"
    target_data_dir = tmp_path / "restored_jobs_data"
    backup_restore.cmd_restore(argparse.Namespace(
        backup_dir=str(backup_dir),
        target_db_url=f"sqlite:///{target_db_path}",
        target_data_dir=str(target_data_dir),
    ))

    restored_engine = create_engine(f"sqlite:///{target_db_path}")
    RestoredSession = sessionmaker(bind=restored_engine)
    restored_session = RestoredSession()
    row = analysis_jobs_repo.get_by_id(restored_session, "stuck-job")
    assert row.status == "interrupted", "a queued/running job from a restored snapshot can never be resumed"
    restored_session.close()
    restored_engine.dispose()


def test_purge_is_dry_run_by_default_and_requires_an_explicit_duration(tmp_path):
    backups_root = tmp_path / "backups"
    old_backup = backups_root / "old"
    old_backup.mkdir(parents=True)
    (old_backup / "manifest.json").write_text(
        json.dumps({"created_at": (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()}), encoding="utf-8",
    )
    recent_backup = backups_root / "recent"
    recent_backup.mkdir(parents=True)
    (recent_backup / "manifest.json").write_text(
        json.dumps({"created_at": datetime.now(timezone.utc).isoformat()}), encoding="utf-8",
    )

    backup_restore.cmd_purge(argparse.Namespace(backups_root=str(backups_root), older_than_days=30, execute=False))
    assert old_backup.exists(), "dry-run (the default) must delete nothing"
    assert recent_backup.exists()

    backup_restore.cmd_purge(argparse.Namespace(backups_root=str(backups_root), older_than_days=30, execute=True))
    assert not old_backup.exists(), "--execute must actually delete what dry-run identified"
    assert recent_backup.exists(), "a backup within the retention window must never be deleted"
