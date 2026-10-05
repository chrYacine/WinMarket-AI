"""Lot 50 ter §4 — a GENUINE, controlled interleaving proof for the sweeper/confirm race, not just a
before-state check. `tests/test_lot50bis_expiry_sweeper.py::test_sweep_is_safe_against_a_confirm_that_happened_first`
already proved a row flipped OUT of 'staging' BEFORE the sweep even reads it is safe — that only exercises
the WHERE clause's `status` half against a row the sweep's own SELECT never even returns as a candidate.

The scenario the ticket asks for is stricter: the sweeper's SELECT reads a row as an expired candidate FIRST,
a confirm() then races in and wins BEFORE the sweeper's own DELETE for that same candidate runs, and the
DELETE must still refuse to touch it (0 rows deleted, storage untouched) even though the sweeper "knew about"
the row before the confirm. `sweep_expired_staging`'s `_after_select` parameter (lot 50 ter §4, test-only)
lets this test inject the real confirm at exactly that point deterministically, instead of hoping a sleep()
lands the right way.

Deliberately built with ONLY the `db` fixture (repository calls directly, no HTTP `client`) — `src.web.jobs`'s
own import chain never touches `src.rag.private_rag_manager`/sklearn (only `main.py`'s route wiring does), so
this suite runs independently of that unrelated environment issue.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from src.web.ao_dossier import expiry_sweeper, storage
from src.web.database.models import AoDossier
from src.web.database.repositories import ao_dossiers as dossiers_repo
from tests.conftest import default_org_id, make_active_starter_user


def _piece(name: str) -> dict:
    return {
        "category": "rc", "display_name": name, "file_format": ".txt", "size_bytes": 10,
        "content_hash": "0" * 64, "storage_key": f"nonexistent/{name}",
    }


def _make_staging_dossier(db, *, organization_id, user_id, seconds_ago: int = 60) -> uuid.UUID:
    dossier_id = uuid.uuid4()
    expires_at = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    dossiers_repo.create_staging(
        db, dossier_id=dossier_id, organization_id=organization_id, user_id=user_id,
        total_bytes=10, pieces=[_piece("rc.txt")], expires_at=expires_at,
    )
    db.commit()
    return dossier_id


def test_a_confirm_that_wins_the_race_after_the_sweep_already_selected_the_row_is_never_deleted(db):
    user = make_active_starter_user(db, "l50ter-race1@example.com", scoring=False, capacity=False)
    org_id = default_org_id(db, user)
    dossier_id = _make_staging_dossier(db, organization_id=org_id, user_id=user.id)

    confirmed_in_callback = {"done": False}

    def _confirm_races_in(candidates):
        # Exactly the window the ticket describes: the sweep has ALREADY selected this row as an expired
        # candidate (it is in `candidates`); a confirm() now races in and wins BEFORE the sweep's own
        # DELETE for it runs below.
        ids = {row[0] for row in candidates}
        assert dossier_id in ids, "the interleaving test is meaningless if the sweep never saw this row"
        dossier = dossiers_repo.get_staging_for_owner(db, dossier_id=dossier_id, organization_id=org_id, user_id=user.id)
        dossiers_repo.confirm_staging(db, dossier, confirmed_by_user_id=user.id, categories_missing=[], scope_limited=False)
        dossiers_repo.link_job(db, dossier, job_id="fake-job-racing-in")
        db.commit()
        confirmed_in_callback["done"] = True

    removed = expiry_sweeper.sweep_expired_staging(db, _after_select=_confirm_races_in)

    assert confirmed_in_callback["done"], "the injected confirm never actually ran"
    assert removed == 0, "the sweep deleted a dossier a confirm won the race to admit"
    row = db.get(AoDossier, dossier_id)
    assert row is not None, "the confirmed dossier row was deleted despite winning the race"
    assert row.status == "submitted"
    assert row.job_id == "fake-job-racing-in", "the confirm's own write must survive, not be rolled back by the sweep"


def test_a_confirm_that_loses_the_race_still_lets_the_sweep_clean_up_normally(db):
    """Sanity companion: when NOTHING races in, the sweep still removes the expired row exactly as before —
    the new `_after_select` seam must not change ordinary behaviour when it does nothing."""
    user = make_active_starter_user(db, "l50ter-race2@example.com", scoring=False, capacity=False)
    org_id = default_org_id(db, user)
    dossier_id = _make_staging_dossier(db, organization_id=org_id, user_id=user.id)

    removed = expiry_sweeper.sweep_expired_staging(db, _after_select=lambda candidates: None)

    assert removed == 1
    assert db.get(AoDossier, dossier_id) is None


def test_storage_removal_never_runs_for_a_row_the_delete_did_not_actually_remove(db, monkeypatch):
    """The stronger, direct proof of 'no accepted job left with erased pieces': if the DELETE affects 0 rows
    (because a confirm won the race), `storage.remove_dossier_dir` — which would delete the pieces' files —
    must never even be called for that dossier."""
    user = make_active_starter_user(db, "l50ter-race3@example.com", scoring=False, capacity=False)
    org_id = default_org_id(db, user)
    dossier_id = _make_staging_dossier(db, organization_id=org_id, user_id=user.id)

    storage_calls = []
    monkeypatch.setattr(storage, "remove_dossier_dir", lambda *a, **k: storage_calls.append(a))

    def _confirm_races_in(candidates):
        dossier = dossiers_repo.get_staging_for_owner(db, dossier_id=dossier_id, organization_id=org_id, user_id=user.id)
        dossiers_repo.confirm_staging(db, dossier, confirmed_by_user_id=user.id, categories_missing=[], scope_limited=False)
        dossiers_repo.link_job(db, dossier, job_id="fake-job-racing-in-2")
        db.commit()

    expiry_sweeper.sweep_expired_staging(db, _after_select=_confirm_races_in)

    assert storage_calls == [], "storage.remove_dossier_dir ran for a dossier the confirm had already won"


def test_a_disk_deletion_failure_never_leaves_a_db_row_pointing_at_deleted_files(db, monkeypatch):
    """Ticket §4: 'traite explicitement les échecs de suppression disque après transaction sans supprimer de
    fichier référencé'. The DB row is deleted (and committed) BEFORE storage cleanup is even attempted — so a
    disk-removal failure can only ever leave an ORPHAN directory (nothing references it any more), never a
    dangling DB row that still points at files the sweep then destroyed. This proves that ordering holds even
    when the disk step genuinely raises."""
    user = make_active_starter_user(db, "l50ter-race4@example.com", scoring=False, capacity=False)
    org_id = default_org_id(db, user)
    dossier_id = _make_staging_dossier(db, organization_id=org_id, user_id=user.id)

    def _boom(*a, **k):
        raise OSError("simulated disk failure removing the dossier directory")

    monkeypatch.setattr(storage, "remove_dossier_dir", _boom)

    removed = expiry_sweeper.sweep_expired_staging(db)  # must not raise despite the disk failure

    assert removed == 1, "the DB row must still be counted removed — the failure is on disk cleanup only"
    assert db.get(AoDossier, dossier_id) is None, "the DB row must be gone regardless of the disk failure"
