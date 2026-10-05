"""Lot 55 — scripts/migrate_sqlite_to_postgresql.py fixes, all on disposable SQLite (dialect differences are
covered separately by the real pgserver rehearsal, tests/test_lot54_migration_rehearsal.py).

1. Resume mode: an existing PRIMARY KEY is no longer proof a row is identical — a genuine value divergence on
   a non-excluded column is refused (counted as an error), never silently skipped or overwritten.
2. The deferred-FK backfill never claims to have updated a row that isn't actually there on the target.
3. A schema-revision mismatch (real Alembic tracking on either side) is refused before any row is copied.
4. Source/target URLs may come from environment variables instead of CLI arguments (password-in-shell-
   history avoidance).
"""
from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

from sqlalchemy import create_engine, select

from scripts.migrate_sqlite_to_postgresql import (
    _assert_schema_revisions_compatible,
    _backfill_deferred_fk,
    _diverging_columns,
    copy_table,
)
from src.web.database.models import Base, KnowledgeCorpus, KnowledgeDocument, KnowledgeDocumentVersion, Organization, User

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


def test_diverging_columns_flags_a_real_value_change_ignoring_excluded_derived_columns():
    source_row = {"id": "x", "embedding_status": "ready", "embedding_indexed_at": "2026-01-01", "name": "Org A"}
    # embedding_status/embedding_indexed_at differ (documented derived transform) but the real "name" column
    # also differs — must still be reported as a genuine divergence.
    target_row = {"id": "x", "embedding_status": "pending", "embedding_indexed_at": None, "name": "Org B (changed)"}
    diverging = _diverging_columns(KnowledgeDocumentVersion.__tablename__, source_row, target_row)
    assert diverging == ["name"], "only the non-excluded, actually-different column must be reported"


def test_diverging_columns_is_silent_when_only_excluded_derived_columns_differ():
    source_row = {"id": "x", "embedding_status": "ready", "embedding_indexed_at": "2026-01-01", "name": "Org A"}
    target_row = {"id": "x", "embedding_status": "pending", "embedding_indexed_at": None, "name": "Org A"}
    assert _diverging_columns(KnowledgeDocumentVersion.__tablename__, source_row, target_row) == []


def test_copy_table_resume_mode_refuses_a_genuine_divergence_instead_of_overwriting_or_ignoring(tmp_path):
    """DEFECT this fixes: the old resume mode treated "primary key already exists" as proof the row was
    already correctly copied. Here the target row was copied once, then the SOURCE row changed (a name edit)
    before a second (resumed) run — the old code would silently skip it, leaving the target permanently
    wrong with no error raised. The new code must refuse this row and count it as an error."""
    src = _make_populated_engine(tmp_path / "src_div.db")
    dst = create_engine(f"sqlite:///{tmp_path / 'dst_div.db'}", future=True)
    Base.metadata.create_all(dst)
    copy_table(src, dst, Organization.__table__, mode="empty-target", batch_size=100)

    # Mutate the SOURCE row after the first (successful) copy — simulating "the target's copy is stale/wrong"
    # rather than "the target already has the exact same row".
    with src.begin() as conn:
        conn.execute(Organization.__table__.update().values(name="Org test — RENAMED after first copy"))

    report = copy_table(src, dst, Organization.__table__, mode="resume", batch_size=100)
    assert report["errors"] == 1, "a genuine value divergence on an existing PK must be counted as an error"
    assert report["copied"] == 0 and report["skipped_existing"] == 0

    with dst.connect() as conn:
        target_name = conn.execute(select(Organization.__table__.c.name)).scalar_one()
    assert target_name == "Org test", "the target row must NEVER be silently overwritten by a divergent resume"


def test_backfill_deferred_fk_reports_missing_target_rows_never_claims_them_as_updated(tmp_path):
    """DEFECT this fixes: the old backfill counted `updated += 1` unconditionally for every source row with a
    non-NULL deferred column, even if the UPDATE affected zero rows on the target (e.g. that row failed to
    copy earlier). Reproduced here by deleting the target row before backfilling: the source still names it,
    but there is nothing to update — this must be reported as a missing target, never counted as a success."""
    src = create_engine(f"sqlite:///{tmp_path / 'src_fk.db'}", future=True)
    dst = create_engine(f"sqlite:///{tmp_path / 'dst_fk.db'}", future=True)
    Base.metadata.create_all(src)
    Base.metadata.create_all(dst)

    org_id, user_id, corpus_id, doc_id, version_id = (uuid.uuid4() for _ in range(5))
    for engine in (src, dst):
        with engine.begin() as conn:
            conn.execute(Organization.__table__.insert().values(id=org_id, name="Org"))
            conn.execute(User.__table__.insert().values(id=user_id, email="a@example.com", password_hash="x", first_name="A", last_name="B", status="active"))
            conn.execute(KnowledgeCorpus.__table__.insert().values(id=corpus_id, organization_id=org_id, owner_user_id=user_id, generation=1))
            conn.execute(KnowledgeDocumentVersion.__table__.insert().values(
                id=version_id, document_id=doc_id, organization_id=org_id, owner_user_id=user_id, version_number=1,
                storage_key="k", file_size=10, content_hash="h" * 64, extraction_status="ready", extractor_version="v1",
                embedding_status="not_applicable",
            ))
    with src.begin() as conn:
        conn.execute(KnowledgeDocument.__table__.insert().values(
            id=doc_id, corpus_id=corpus_id, organization_id=org_id, owner_user_id=user_id,
            original_filename="a.md", status="active", active_version_id=version_id,
        ))
    with dst.begin() as conn:
        # The target's knowledge_documents row was inserted WITHOUT the deferred column set (as the real
        # copy_table does), but is then deleted entirely — simulating "this row failed to copy".
        conn.execute(KnowledgeDocument.__table__.insert().values(
            id=doc_id, corpus_id=corpus_id, organization_id=org_id, owner_user_id=user_id,
            original_filename="a.md", status="active", active_version_id=None,
        ))
        conn.execute(KnowledgeDocument.__table__.delete().where(KnowledgeDocument.__table__.c.id == doc_id))

    result = _backfill_deferred_fk(src, dst, KnowledgeDocument.__tablename__, "active_version_id")
    assert result["updated"] == 0
    assert result["missing_targets"] == [doc_id], "a source row with no target counterpart must be reported, never silently counted as backfilled"


def test_interrupted_and_resumed_transfer_of_the_deferred_fk_table_never_flags_a_false_divergence(tmp_path):
    """The exact scenario the ticket names explicitly: a transfer interrupted right after
    knowledge_documents was copied (active_version_id inserted as NULL, deferred) but BEFORE
    knowledge_document_versions and the backfill pass ran. A resumed run must recognize the existing
    knowledge_documents row as already-present (never a false divergence just because the deferred column
    is legitimately NULL there for now), then the backfill pass — run only once everything is copied — must
    correctly complete it to the real value, never leaving it NULL nor touching an unrelated row."""
    src = create_engine(f"sqlite:///{tmp_path / 'src_deferred.db'}", future=True)
    dst = create_engine(f"sqlite:///{tmp_path / 'dst_deferred.db'}", future=True)
    Base.metadata.create_all(src)
    Base.metadata.create_all(dst)

    org_id, user_id, corpus_id, doc_id, version_id = (uuid.uuid4() for _ in range(5))
    with src.begin() as conn:
        conn.execute(Organization.__table__.insert().values(id=org_id, name="Org"))
        conn.execute(User.__table__.insert().values(id=user_id, email="a@example.com", password_hash="x", first_name="A", last_name="B", status="active"))
        conn.execute(KnowledgeCorpus.__table__.insert().values(id=corpus_id, organization_id=org_id, owner_user_id=user_id, generation=1))
        conn.execute(KnowledgeDocumentVersion.__table__.insert().values(
            id=version_id, document_id=doc_id, organization_id=org_id, owner_user_id=user_id, version_number=1,
            storage_key="k", file_size=10, content_hash="h" * 64, extraction_status="ready", extractor_version="v1",
            embedding_status="ready",
        ))
        conn.execute(KnowledgeDocument.__table__.insert().values(
            id=doc_id, corpus_id=corpus_id, organization_id=org_id, owner_user_id=user_id,
            original_filename="a.md", status="active", active_version_id=version_id,
        ))

    # --- "First run", interrupted right after knowledge_documents (never reaches the version table or the
    # backfill pass) ---
    for table in (Organization.__table__, User.__table__, KnowledgeCorpus.__table__, KnowledgeDocument.__table__):
        report = copy_table(src, dst, table, mode="empty-target", batch_size=100)
        assert report["errors"] == 0
    with dst.connect() as conn:
        assert conn.execute(select(KnowledgeDocument.__table__.c.active_version_id)).scalar_one() is None

    # --- "Resumed run": every table re-attempted in resume mode ---
    for table in (Organization.__table__, User.__table__, KnowledgeCorpus.__table__):
        report = copy_table(src, dst, table, mode="resume", batch_size=100)
        assert report == {"copied": 0, "skipped_existing": 1, "errors": 0}
    doc_report = copy_table(src, dst, KnowledgeDocument.__table__, mode="resume", batch_size=100)
    assert doc_report == {"copied": 0, "skipped_existing": 1, "errors": 0}, (
        "the deferred column being NULL on the target must never be reported as a divergence"
    )
    version_report = copy_table(src, dst, KnowledgeDocumentVersion.__table__, mode="resume", batch_size=100)
    assert version_report["copied"] == 1 and version_report["errors"] == 0

    backfill_result = _backfill_deferred_fk(src, dst, KnowledgeDocument.__tablename__, "active_version_id")
    assert backfill_result == {"updated": 1, "missing_targets": []}

    with dst.connect() as conn:
        final_value = conn.execute(select(KnowledgeDocument.__table__.c.active_version_id)).scalar_one()
    assert final_value == version_id, "the deferred FK must be correctly completed after an interrupted+resumed transfer"


def test_assert_schema_revisions_compatible_is_a_no_op_when_neither_side_is_alembic_tracked(tmp_path):
    """The overwhelming majority of this project's disposable test/rehearsal databases are built via
    Base.metadata.create_all (never real Alembic) — this must remain completely unaffected."""
    src = create_engine(f"sqlite:///{tmp_path / 'src_noalembic.db'}", future=True)
    dst = create_engine(f"sqlite:///{tmp_path / 'dst_noalembic.db'}", future=True)
    Base.metadata.create_all(src)
    Base.metadata.create_all(dst)
    _assert_schema_revisions_compatible(src, dst)  # must not raise


def test_assert_schema_revisions_compatible_refuses_a_stale_target_revision(tmp_path, monkeypatch):
    """The real-cutover case: the source is genuinely Alembic-tracked at head, but the target was stamped at
    an OLDER revision (a stale/incomplete `alembic upgrade` on the target) — table existence alone would not
    catch this; the revision check must refuse before any row is copied."""
    from alembic.config import Config
    from alembic import command as alembic_command

    src_path = tmp_path / "src_stale.db"
    dst_path = tmp_path / "dst_stale.db"
    src = create_engine(f"sqlite:///{src_path}", future=True)
    dst = create_engine(f"sqlite:///{dst_path}", future=True)
    Base.metadata.create_all(src)
    Base.metadata.create_all(dst)

    repo_root = SCRIPT.parents[1]
    cfg = Config(str(repo_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(repo_root / "migrations"))

    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{src_path}")
    alembic_command.stamp(cfg, "head")

    # Target stamped at a real, EARLIER revision instead of head (an older migration in this project's own
    # history) — never fabricated, always one of the project's actual revision ids.
    from alembic.script import ScriptDirectory
    script = ScriptDirectory.from_config(cfg)
    revisions = list(script.walk_revisions())
    older_revision = revisions[-1].revision if len(revisions) > 1 else revisions[0].revision
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{dst_path}")
    alembic_command.stamp(cfg, older_revision)

    try:
        _assert_schema_revisions_compatible(src, dst)
        raised = False
    except SystemExit:
        raised = True
    assert raised, "a target stamped at an older-than-head revision must be refused before any transfer"


def test_cli_accepts_source_and_target_urls_from_environment_variables_not_only_cli_flags(tmp_path):
    """A password embedded in a literal --target-db-url argument lands in shell history/process listings;
    the env-var path must work identically for a real (non-dry-run) transfer."""
    src_path = tmp_path / "env_src.db"
    dst_path = tmp_path / "env_dst.db"
    _make_populated_engine(src_path)
    dst = create_engine(f"sqlite:///{dst_path}", future=True)
    Base.metadata.create_all(dst)
    dst.dispose()

    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True, text=True, cwd=str(SCRIPT.parents[1]),
        env={**os.environ, "WM_MIGRATE_SOURCE_DB_URL": f"sqlite:///{src_path}", "WM_MIGRATE_TARGET_DB_URL": f"sqlite:///{dst_path}"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    check = create_engine(f"sqlite:///{dst_path}", future=True)
    with check.connect() as conn:
        assert conn.execute(select(Organization.__table__)).all(), "the env-var-supplied URLs must be used exactly like their CLI-flag equivalents"


def test_cli_marks_a_queued_or_running_target_job_as_interrupted_after_transfer(tmp_path):
    """DEFECT this fixes: a source job still 'queued'/'running' at write-stop time was copied onto the
    target as-is and left there forever — nothing on the target would ever claim/finish it, since the
    process that would have processed it is retired with the source. The script must reconcile it to
    'interrupted' itself, the same way scripts/backup_restore.py's own restore path already does."""
    src_path = tmp_path / "src_jobs.db"
    dst_path = tmp_path / "dst_jobs.db"
    src = _make_populated_engine(src_path)
    dst = create_engine(f"sqlite:///{dst_path}", future=True)
    Base.metadata.create_all(dst)
    dst.dispose()

    from src.web.database.repositories import analysis_jobs as analysis_jobs_repo
    from sqlalchemy.orm import sessionmaker
    Session = sessionmaker(bind=src)
    session = Session()
    org_id = session.execute(select(Organization.__table__.c.id)).scalar_one()
    user_id = session.execute(select(User.__table__.c.id)).scalar_one()
    analysis_jobs_repo.create_queued(session, job_id="stuck-cutover-job", user_id=user_id, organization_id=org_id, source_label="x")
    session.commit()
    session.close()
    src.dispose()

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--source-db-url", f"sqlite:///{src_path}", "--target-db-url", f"sqlite:///{dst_path}"],
        capture_output=True, text=True, cwd=str(SCRIPT.parents[1]),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "interrupted" in result.stdout.lower() or "réattribué" in result.stdout.lower()

    check = create_engine(f"sqlite:///{dst_path}", future=True)
    CheckSession = sessionmaker(bind=check)
    check_session = CheckSession()
    row = analysis_jobs_repo.get_by_id(check_session, "stuck-cutover-job")
    assert row.status == "interrupted", "a queued/running job copied onto the target must never be left claimable forever"
    check_session.close()


def test_cli_refuses_cleanly_when_neither_flag_nor_env_var_supplies_a_url(tmp_path):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--dry-run"],
        capture_output=True, text=True, cwd=str(SCRIPT.parents[1]),
        env={k: v for k, v in os.environ.items() if not k.startswith("WM_MIGRATE_")},
    )
    assert result.returncode != 0
    assert "REFUSED" in result.stdout
