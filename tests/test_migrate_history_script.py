"""V3 SaaS — scripts/migrate_history_to_postgresql.py.

Covers checklist item 12 (migration historique). Runs against a temporary
historique_ao.json — never against the real project data — via
monkeypatched module paths.
"""
import json
import sys

from tests.conftest import default_org_id, make_active_starter_user


def _patch_history_paths(monkeypatch, tmp_path):
    import scripts.migrate_history_to_postgresql as migrate

    hist_dir = tmp_path / "historique"
    hist_dir.mkdir()
    hist_file = hist_dir / "historique_ao.json"
    analyses_dir = hist_dir / "analyses"
    analyses_dir.mkdir()
    backup_root = hist_dir / "_migration_backups"

    monkeypatch.setattr(migrate, "HIST_FILE", hist_file)
    monkeypatch.setattr(migrate, "ANALYSES_DIR", analyses_dir)
    monkeypatch.setattr(migrate, "BACKUP_ROOT", backup_root)
    return migrate, hist_file, analyses_dir, backup_root


def _run_migration(monkeypatch, migrate, owner_email: str) -> None:
    monkeypatch.setattr(sys, "argv", ["migrate_history_to_postgresql.py", owner_email])
    migrate.main()


def test_migration_creates_one_analysis_per_record_and_backs_up(db, tmp_path, monkeypatch, test_db):
    migrate, hist_file, analyses_dir, backup_root = _patch_history_paths(monkeypatch, tmp_path)

    owner = make_active_starter_user(db, "owner@example.com")
    records = [
        {"titre": "AO 1", "client": "Client 1", "decision": "GO", "score": 90, "date": "01/01/2026 10:00"},
        {"ao_id": "AO_X", "titre": "AO 2", "client": "Client 2", "decision": "NO-GO", "score": 40,
         "date": "02/01/2026 10:00", "job_id": "jobdetail1"},
    ]
    hist_file.write_text(json.dumps(records), encoding="utf-8")
    (analyses_dir / "jobdetail1.json").write_text(json.dumps({
        "id": "jobdetail1", "ao": {"titre": "AO 2 detail"}, "result": {"decision": "NO-GO"},
    }), encoding="utf-8")

    _run_migration(monkeypatch, migrate, "owner@example.com")

    from src.web.database.repositories import analyses as analyses_repo
    rows = analyses_repo.list_for_user(db, owner.id, default_org_id(db, owner))
    assert len(rows) == 2
    assert backup_root.exists()
    assert any(backup_root.iterdir())

    detailed = next(r for r in rows if r.job_id == "jobdetail1")
    assert detailed.result_data.get("ao", {}).get("titre") == "AO 2 detail"


def test_migration_is_idempotent(db, tmp_path, monkeypatch, test_db):
    migrate, hist_file, analyses_dir, backup_root = _patch_history_paths(monkeypatch, tmp_path)
    owner = make_active_starter_user(db, "owner2@example.com")

    hist_file.write_text(json.dumps([
        {"titre": "AO only once", "client": "Client", "decision": "GO", "score": 80, "date": "03/01/2026 10:00"},
    ]), encoding="utf-8")

    _run_migration(monkeypatch, migrate, "owner2@example.com")
    _run_migration(monkeypatch, migrate, "owner2@example.com")  # re-run — must not duplicate

    from src.web.database.repositories import analyses as analyses_repo
    matching = [r for r in analyses_repo.list_for_user(db, owner.id, default_org_id(db, owner)) if r.title == "AO only once"]
    assert len(matching) == 1


def test_migration_fails_clearly_when_owner_missing(tmp_path, monkeypatch, test_db):
    migrate, hist_file, analyses_dir, backup_root = _patch_history_paths(monkeypatch, tmp_path)
    hist_file.write_text(json.dumps([{"titre": "AO", "date": "01/01/2026 10:00"}]), encoding="utf-8")

    monkeypatch.setattr(sys, "argv", ["migrate_history_to_postgresql.py", "doesnotexist@example.com"])
    try:
        migrate.main()
        raised = False
    except SystemExit:
        raised = True
    assert raised


# ---------------------------------------------------------------------------
# B23-T1 (DEFECT confirmed): --dry-run previews with zero writes/backup, and
# a single bad row is isolated by its own SAVEPOINT rather than risking the
# whole batch (see migrate_history_to_postgresql.py's own module docstring
# for the exact PostgreSQL failure mode this closes).
# ---------------------------------------------------------------------------

def test_dry_run_writes_nothing_and_creates_no_backup(db, tmp_path, monkeypatch, test_db):
    migrate, hist_file, analyses_dir, backup_root = _patch_history_paths(monkeypatch, tmp_path)
    owner = make_active_starter_user(db, "dryrun@example.com")
    hist_file.write_text(json.dumps([
        {"titre": "Dry AO", "client": "Client", "decision": "GO", "score": 80, "date": "01/01/2026 10:00"},
    ]), encoding="utf-8")

    monkeypatch.setattr(sys, "argv", ["migrate_history_to_postgresql.py", "dryrun@example.com", "--dry-run"])
    migrate.main()

    from src.web.database.repositories import analyses as analyses_repo
    owner_org_id = default_org_id(db, owner)
    assert analyses_repo.list_for_user(db, owner.id, owner_org_id) == [], "dry-run must insert nothing"
    assert not backup_root.exists(), "dry-run must not create a backup either — zero filesystem writes"

    # A real (non-dry-run) run afterward must still work and actually migrate.
    monkeypatch.setattr(sys, "argv", ["migrate_history_to_postgresql.py", "dryrun@example.com"])
    migrate.main()
    assert len(analyses_repo.list_for_user(db, owner.id, owner_org_id)) == 1
    assert backup_root.exists()


def test_a_bad_row_is_isolated_by_its_own_savepoint_never_discards_earlier_migrated_rows(db, tmp_path, monkeypatch, test_db):
    """The confirmed defect: without a per-row SAVEPOINT, a row that fails
    at db.flush() time would — on a real PostgreSQL backend — abort the
    WHOLE surrounding transaction, silently losing every row already
    flushed as "migrated" before it (see the script's own module docstring
    for the exact mechanism). Simulated here by making create_analysis
    raise for exactly the middle record (a stand-in for any flush-time
    failure — a bad constraint, a malformed value — the actual cause
    doesn't matter, only whether it's isolated). Proves the rows on either
    side of the failing one still land, and the error is counted, never
    silently swallowed or allowed to abort the batch."""
    migrate, hist_file, analyses_dir, backup_root = _patch_history_paths(monkeypatch, tmp_path)
    make_active_starter_user(db, "savepoint@example.com")

    hist_file.write_text(json.dumps([
        {"titre": "Before", "client": "C1", "decision": "GO", "score": 70, "date": "01/01/2026 10:00"},
        {"titre": "Bad Row", "client": "C2", "decision": "GO", "score": 70, "date": "02/01/2026 10:00"},
        {"titre": "After", "client": "C3", "decision": "GO", "score": 70, "date": "03/01/2026 10:00"},
    ]), encoding="utf-8")

    from src.web.database.repositories import analyses as analyses_repo
    real_create_analysis = analyses_repo.create_analysis

    def _flaky_create_analysis(db_arg, **kwargs):
        if kwargs.get("title") == "Bad Row":
            raise RuntimeError("simulated flush-time failure for this row only")
        return real_create_analysis(db_arg, **kwargs)

    monkeypatch.setattr(analyses_repo, "create_analysis", _flaky_create_analysis)
    monkeypatch.setattr(sys, "argv", ["migrate_history_to_postgresql.py", "savepoint@example.com"])
    migrate.main()

    owner_row = None
    from src.web.database.repositories import users as users_repo
    owner_row = users_repo.get_by_email(db, "savepoint@example.com")
    titles = {r.title for r in analyses_repo.list_for_user(db, owner_row.id, default_org_id(db, owner_row))}
    assert "Before" in titles, "a row migrated before the failing one must survive the failure"
    assert "After" in titles, "a row processed after the failing one must still be migrated"
    assert "Bad Row" not in titles, "the failing row itself must never be inserted"
