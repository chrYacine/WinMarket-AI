"""Lot 55 — scripts/backup_restore.py fixes, all on disposable data.

Two real defects reproduced against the actual project this lot (see
docs/qa/lot_55_20260926/RAPPORT.md):

1. The old file-backup loop (`for name in ("outputs", "historique")`) never
   covered `data/knowledge/` (private documents, B03) nor any other
   subtree — a hardcoded two-name list is not proof of coverage. Fixed via
   `_collect_file_roots`, which reads the actually-referenced configuration
   values (DATA_DIR/LOCAL_STORAGE_PATH/OUTPUT_DIR) instead.
2. A raw `shutil.copy2` of a live SQLite file can capture a torn/
   inconsistent snapshot mid-write. Fixed via SQLite's own Online Backup
   API (`sqlite3.Connection.backup`), verified here against a writer that
   holds an OPEN, UNCOMMITTED transaction during the backup call.

Also covers the new restore-time hash+permission verification
(`_verify_restored_tree`) refusing a divergence instead of reporting a
silent, false "restore succeeded".
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import stat as stat_module

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import scripts.backup_restore as backup_restore
from src.web.database.models import Base


def _make_sqlite_env(tmp_path, name: str, monkeypatch):
    db_path = tmp_path / f"{name}.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    data_dir = tmp_path / f"{name}_data"
    data_dir.mkdir(parents=True)
    monkeypatch.setattr(backup_restore, "DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setattr(backup_restore, "DATA_DIR", data_dir)
    monkeypatch.setattr(backup_restore, "LOCAL_STORAGE_PATH", data_dir)
    monkeypatch.setattr(backup_restore, "OUTPUT_DIR", data_dir / "outputs")
    return db_path, engine, Session, data_dir


def test_backup_covers_knowledge_directory_never_named_by_the_old_hardcoded_list(tmp_path, monkeypatch):
    """Reproduces the exact real-world gap: data/knowledge/ is a sibling of
    data/outputs/ and data/historique/ that the OLD two-name loop silently
    skipped. Populates all three plus an arbitrary future subtree name, and
    proves every one of them is present in the backup with a matching hash
    AND permission bits after a full restore round-trip."""
    _db_path, engine, _Session, data_dir = _make_sqlite_env(tmp_path, "src", monkeypatch)
    engine.dispose()

    populated = {
        "outputs/rapport.pdf": b"%PDF-fake",
        "historique/analyses/old.json": b'{"a": 1}',
        "knowledge/org1/user1/doc.pdf": b"private-knowledge-bytes",
        "some_future_subtree/file.txt": b"not yet invented at lot-55 time",
    }
    for rel, content in populated.items():
        path = data_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        path.chmod(0o644)

    backup_dir = tmp_path / "backup"
    rc = backup_restore.cmd_backup(argparse.Namespace(output_dir=str(backup_dir)))
    assert rc == 0

    for rel in populated:
        assert (backup_dir / "files" / "data_dir" / rel).exists(), f"{rel} missing from backup"

    files_manifest = json.loads((backup_dir / "files_manifest.json").read_text())
    for rel in populated:
        assert rel in files_manifest["data_dir"], f"{rel} missing from files_manifest.json"

    target_data_dir = tmp_path / "restored_data"
    rc = backup_restore.cmd_restore(argparse.Namespace(
        backup_dir=str(backup_dir),
        target_db_url=f"sqlite:///{tmp_path / 'restored.db'}",
        target_data_dir=str(target_data_dir),
        target_local_storage_path=None,
        target_output_dir=None,
    ))
    assert rc == 0

    for rel, content in populated.items():
        restored_path = target_data_dir / rel
        assert restored_path.exists(), f"{rel} missing after restore"
        assert restored_path.read_bytes() == content, f"{rel} content changed by restore"
        assert stat_module.S_IMODE(restored_path.stat().st_mode) == stat_module.S_IMODE(
            (data_dir / rel).stat().st_mode
        ), f"{rel} permission bits changed by restore"


def test_restore_refuses_a_files_manifest_hash_divergence_instead_of_reporting_false_success(tmp_path, monkeypatch):
    """A restore whose copied bytes no longer match the backup's own
    recorded hash must be refused (non-zero exit), never silently accepted
    — this is the "compare canonical values, don't just assume the copy
    worked" proof the lot 55 ticket asks for, applied to files."""
    _db_path, engine, _Session, data_dir = _make_sqlite_env(tmp_path, "src2", monkeypatch)
    engine.dispose()

    (data_dir / "knowledge").mkdir()
    (data_dir / "knowledge" / "doc.txt").write_bytes(b"original-content")

    backup_dir = tmp_path / "backup2"
    rc = backup_restore.cmd_backup(argparse.Namespace(output_dir=str(backup_dir)))
    assert rc == 0

    # Corrupt the backed-up copy itself so the restore copies WRONG bytes —
    # simulating storage-level corruption between backup and restore.
    (backup_dir / "files" / "data_dir" / "knowledge" / "doc.txt").write_bytes(b"CORRUPTED-DIFFERENT-CONTENT")

    target_data_dir = tmp_path / "restored_data2"
    rc = backup_restore.cmd_restore(argparse.Namespace(
        backup_dir=str(backup_dir),
        target_db_url=f"sqlite:///{tmp_path / 'restored2.db'}",
        target_data_dir=str(target_data_dir),
        target_local_storage_path=None,
        target_output_dir=None,
    ))
    assert rc == 1, "a hash divergence between the manifest and the restored file must be refused, not ignored"


def test_restore_refuses_a_backed_up_root_with_no_matching_target_argument(tmp_path, monkeypatch):
    """A backup containing a distinct 'local_storage_path' root (LOCAL_
    STORAGE_PATH configured outside DATA_DIR) must never be silently
    dropped when --target-local-storage-path is omitted from the restore
    call — this is the "deferred root" case from the ticket: a target must
    be given explicitly, or the restore refuses outright."""
    db_path = tmp_path / "src3.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    engine.dispose()
    data_dir = tmp_path / "src3_data"
    data_dir.mkdir()
    external_storage = tmp_path / "external_storage"
    (external_storage / "knowledge").mkdir(parents=True)
    (external_storage / "knowledge" / "doc.txt").write_bytes(b"external-root-content")

    monkeypatch.setattr(backup_restore, "DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setattr(backup_restore, "DATA_DIR", data_dir)
    monkeypatch.setattr(backup_restore, "LOCAL_STORAGE_PATH", external_storage)
    monkeypatch.setattr(backup_restore, "OUTPUT_DIR", data_dir / "outputs")

    backup_dir = tmp_path / "backup3"
    rc = backup_restore.cmd_backup(argparse.Namespace(output_dir=str(backup_dir)))
    assert rc == 0
    assert (backup_dir / "files" / "local_storage_path" / "knowledge" / "doc.txt").exists()

    rc = backup_restore.cmd_restore(argparse.Namespace(
        backup_dir=str(backup_dir),
        target_db_url=f"sqlite:///{tmp_path / 'restored3.db'}",
        target_data_dir=str(tmp_path / "restored3_data"),
        target_local_storage_path=None,
        target_output_dir=None,
    ))
    assert rc == 1, "omitting --target-local-storage-path for a backup that has that root must be refused"


def test_sqlite_online_backup_is_transactionally_consistent_against_an_open_uncommitted_writer(tmp_path):
    """The defect this replaces: a raw file copy of a live SQLite database
    can be taken mid-write. Here a writer connection holds an OPEN,
    UNCOMMITTED transaction (a real row insert not yet committed) while the
    backup runs — the online backup API must produce a valid, uncorrupted
    snapshot that reflects only the committed state (the uncommitted row
    absent), never a torn/corrupted file."""
    src_path = tmp_path / "live.db"
    conn = sqlite3.connect(src_path)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.execute("INSERT INTO t (v) VALUES ('committed-row')")
    conn.commit()

    # Open a second, concurrent writer connection and leave a transaction
    # uncommitted across the backup call.
    writer = sqlite3.connect(src_path)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO t (v) VALUES ('uncommitted-row')")

    dest_path = tmp_path / "backup.db"
    backup_restore._sqlite_online_backup(src_path, dest_path)

    writer.rollback()
    writer.close()
    conn.close()

    # The backup must be a valid, non-corrupt database...
    check_conn = sqlite3.connect(dest_path)
    integrity = check_conn.execute("PRAGMA integrity_check").fetchone()[0]
    assert integrity == "ok", "the online backup must never produce a corrupted file"
    rows = [r[0] for r in check_conn.execute("SELECT v FROM t").fetchall()]
    check_conn.close()

    # ...reflecting only the committed state, never a partially-written row.
    assert "committed-row" in rows
    assert "uncommitted-row" not in rows
