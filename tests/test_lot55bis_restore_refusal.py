"""A restore with unresolved references or failed verification must never succeed."""
import argparse
import sqlite3

import pytest
from sqlalchemy import text

from scripts import backup_restore as br
from tests.test_lot55_backup_restore_fixes import _make_sqlite_env


def _backup(tmp_path, monkeypatch, missing_reference=False):
    _, engine, _, _ = _make_sqlite_env(tmp_path, 'source', monkeypatch)
    if missing_reference:
        # Deliberately broken historical reference, no document bytes to copy.
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO analysis_documents (id,analysis_id,user_id,organization_id,filename,original_filename,storage_path,mime_type,file_size,created_at) VALUES ('a','b','c','d','old.pdf','old.pdf','local://outputs/old.pdf','application/pdf',4,CURRENT_TIMESTAMP)"))
    engine.dispose()
    backup = tmp_path / 'backup'
    assert br.cmd_backup(argparse.Namespace(output_dir=str(backup))) == 0
    return argparse.Namespace(backup_dir=str(backup), target_db_url=f'sqlite:///{tmp_path / "restored.db"}', target_data_dir=str(tmp_path / 'restored_data'))


def test_restore_rejects_real_missing_file_reference(tmp_path, monkeypatch):
    args = _backup(tmp_path, monkeypatch, missing_reference=True)
    assert br.cmd_restore(args) == 1


@pytest.mark.parametrize('table', ['analysis_jobs', 'analysis_documents'])
def test_restore_rejects_an_unverifiable_database(tmp_path, monkeypatch, table):
    args = _backup(tmp_path, monkeypatch)
    with sqlite3.connect(tmp_path / 'backup/db_backup.sqlite3') as db:
        db.execute('DROP TABLE ' + table)
    assert br.cmd_restore(args) == 1
