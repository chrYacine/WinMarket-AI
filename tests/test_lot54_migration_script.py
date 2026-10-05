"""Lot 54 §2 — scripts/migrate_sqlite_to_postgresql.py's core logic, exercised with two disposable SQLite
databases (dialect differences from PostgreSQL are covered separately by the real pgserver rehearsal,
docs/qa/lot_54_20260926/RAPPORT.md §3 — this file only proves the transfer LOGIC itself: FK order, dry-run,
empty-target refusal, resume/skip-by-PK, embedding_status reset, same-source-refusal).
"""
from __future__ import annotations

import subprocess
import sys
import uuid
from pathlib import Path

from sqlalchemy import create_engine, select

from scripts.migrate_sqlite_to_postgresql import copy_table, plan
from src.web.database.models import Base, KnowledgeDocumentVersion, Organization, User

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "migrate_sqlite_to_postgresql.py"


def _make_populated_engine(path: Path):
    engine = create_engine(f"sqlite:///{path}", future=True)
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        org_id = uuid.uuid4()
        conn.execute(Organization.__table__.insert().values(id=org_id, name="Org test"))
        conn.execute(User.__table__.insert().values(
            id=uuid.uuid4(), email="a@example.com", password_hash="x", first_name="A", last_name="B", status="active",
        ))
    return engine


def test_plan_reports_row_counts_in_fk_safe_order(tmp_path):
    src = _make_populated_engine(tmp_path / "src.db")
    table_plan = plan(src)
    names = [name for name, _ in table_plan]
    assert names.index("organizations") < names.index("memberships"), "FK-referenced table must come first"
    counts = dict(table_plan)
    assert counts["organizations"] == 1 and counts["users"] == 1


def test_copy_table_copies_rows_into_an_empty_target(tmp_path):
    src = _make_populated_engine(tmp_path / "src2.db")
    dst = create_engine(f"sqlite:///{tmp_path / 'dst2.db'}", future=True)
    Base.metadata.create_all(dst)
    report = copy_table(src, dst, Organization.__table__, mode="empty-target", batch_size=100)
    assert report == {"copied": 1, "skipped_existing": 0, "errors": 0}
    with dst.connect() as conn:
        assert conn.execute(select(Organization.__table__)).all()


def test_copy_table_resume_mode_skips_rows_already_present_by_primary_key(tmp_path):
    src = _make_populated_engine(tmp_path / "src3.db")
    dst = create_engine(f"sqlite:///{tmp_path / 'dst3.db'}", future=True)
    Base.metadata.create_all(dst)
    copy_table(src, dst, Organization.__table__, mode="empty-target", batch_size=100)
    # a second run in "resume" mode must skip the already-copied row, never duplicate or error
    report = copy_table(src, dst, Organization.__table__, mode="resume", batch_size=100)
    assert report == {"copied": 0, "skipped_existing": 1, "errors": 0}


def test_embedding_status_is_reset_never_copied_as_ready_with_no_vector_behind_it(tmp_path):
    src = _make_populated_engine(tmp_path / "src4.db")
    dst = create_engine(f"sqlite:///{tmp_path / 'dst4.db'}", future=True)
    Base.metadata.create_all(dst)
    # build the FK chain a KnowledgeDocumentVersion needs
    with src.begin() as conn:
        from src.web.database.models import KnowledgeCorpus, KnowledgeDocument
        org_id = conn.execute(select(Organization.__table__.c.id)).scalar_one()
        user_id = conn.execute(select(User.__table__.c.id)).scalar_one()
        corpus_id = uuid.uuid4()
        conn.execute(KnowledgeCorpus.__table__.insert().values(id=corpus_id, organization_id=org_id, owner_user_id=user_id, generation=1))
        doc_id = uuid.uuid4()
        conn.execute(KnowledgeDocument.__table__.insert().values(
            id=doc_id, corpus_id=corpus_id, organization_id=org_id, owner_user_id=user_id,
            original_filename="a.md", status="active",
        ))
        version_id = uuid.uuid4()
        conn.execute(KnowledgeDocumentVersion.__table__.insert().values(
            id=version_id, document_id=doc_id, organization_id=org_id, owner_user_id=user_id, version_number=1,
            storage_key="k", file_size=10, content_hash="h" * 64, extraction_status="ready", extractor_version="v1",
            embedding_status="ready", embedding_model_id="m", embedding_model_revision="r", embedding_dimension=384,
        ))
    for table in (Organization.__table__, User.__table__, KnowledgeCorpus.__table__, KnowledgeDocument.__table__, KnowledgeDocumentVersion.__table__):
        copy_table(src, dst, table, mode="empty-target", batch_size=100)
    with dst.connect() as conn:
        row = conn.execute(select(KnowledgeDocumentVersion.__table__.c.embedding_status)).scalar_one()
    assert row == "pending", "a version claiming 'ready' on the source must never arrive 'ready' on a target with no vector copied"


def test_cli_dry_run_never_writes_and_reports_counts(tmp_path):
    src_path = tmp_path / "cli_src.db"
    _make_populated_engine(src_path)
    dst_path = tmp_path / "cli_dst.db"
    dst = create_engine(f"sqlite:///{dst_path}", future=True)
    Base.metadata.create_all(dst)
    dst.dispose()

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--source-db-url", f"sqlite:///{src_path}", "--target-db-url", f"sqlite:///{dst_path}", "--dry-run"],
        capture_output=True, text=True, cwd=str(SCRIPT.parents[1]),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "DRY-RUN" in result.stdout
    assert "organizations" in result.stdout
    # nothing written
    check_engine = create_engine(f"sqlite:///{dst_path}", future=True)
    with check_engine.connect() as conn:
        assert conn.execute(select(Organization.__table__)).all() == []


def test_cli_refuses_a_non_empty_target_in_empty_target_mode(tmp_path):
    src_path = tmp_path / "cli_src2.db"
    _make_populated_engine(src_path)
    dst_path = tmp_path / "cli_dst2.db"
    _make_populated_engine(dst_path)  # target already has data

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--source-db-url", f"sqlite:///{src_path}", "--target-db-url", f"sqlite:///{dst_path}"],
        capture_output=True, text=True, cwd=str(SCRIPT.parents[1]),
    )
    assert result.returncode != 0
    assert "REFUSED" in result.stdout or "REFUSED" in result.stderr


def test_cli_refuses_identical_source_and_target(tmp_path):
    path = tmp_path / "same.db"
    _make_populated_engine(path)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--source-db-url", f"sqlite:///{path}", "--target-db-url", f"sqlite:///{path}", "--dry-run"],
        capture_output=True, text=True, cwd=str(SCRIPT.parents[1]),
    )
    assert result.returncode != 0
    assert "REFUSED" in result.stdout


def test_cli_test_mode_refuses_a_dangerous_target(tmp_path, monkeypatch):
    src_path = tmp_path / "guard_src.db"
    _make_populated_engine(src_path)
    monkeypatch.setenv("WM_DB_TEST_MODE", "1")
    monkeypatch.setenv("WM_TEST_DB_EXTRA_ROOTS", str(tmp_path))
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--source-db-url", f"sqlite:///{src_path}",
         "--target-db-url", f"sqlite:///{tmp_path / 'winmarket_local.db'}", "--dry-run"],
        capture_output=True, text=True, cwd=str(SCRIPT.parents[1]),
        env={**__import__("os").environ, "WM_DB_TEST_MODE": "1", "WM_TEST_DB_EXTRA_ROOTS": str(tmp_path)},
    )
    assert result.returncode != 0
    assert "REFUSED" in result.stdout or "REFUSED" in result.stderr or "Refus" in (result.stdout + result.stderr)
