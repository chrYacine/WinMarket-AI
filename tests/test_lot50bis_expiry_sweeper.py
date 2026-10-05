"""Lot 50 bis §4 — real expiration of abandoned dossier-preview staging rows, even if nobody ever revisits
them (`src/web/ao_dossier/expiry_sweeper.py`). Direct unit tests of `sweep_expired_staging`: real staging
dossiers are created through the real `POST /api/analyze/dossier-preview` route (real files, real storage),
then their `staging_expires_at` is pushed into the past directly in the database to simulate abandonment —
the sweep itself is exercised as a plain function, independent of the background thread / lifespan wiring.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from src.web.ao_dossier import expiry_sweeper, storage
from src.web.database.models import AoDossier
from tests.test_lot47bis_ao_dossier import _file
from tests.test_lot50_dossier_admission import _confirm, _decision, _make_account, _preview

RC = b"Reglement de consultation. Appel d'offres - Prestations de nettoyage. Site de Lyon."


def _expire(db, dossier_id, *, seconds_ago: int = 60) -> None:
    dossier = db.get(AoDossier, dossier_id)
    dossier.staging_expires_at = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    db.commit()


def _exists(db, dossier_id) -> bool:
    db.expire_all()
    return db.get(AoDossier, dossier_id) is not None


def test_sweep_removes_an_expired_staging_dossier_and_its_storage_files(client, db):
    csrf = _make_account(client, db, "l50bis-sweep1@example.com")
    r = _preview(client, csrf, [_file("rc", "rc.txt", RC)])
    assert r.status_code == 200, r.text
    body = r.json()
    dossier_id = uuid.UUID(body["dossier_id"])
    row = db.get(AoDossier, dossier_id)
    directory = storage.dossier_dir(row.organization_id, row.user_id, dossier_id)
    assert directory.exists()

    _expire(db, dossier_id)
    removed = expiry_sweeper.sweep_expired_staging(db)

    assert removed == 1
    assert not _exists(db, dossier_id)
    assert not directory.exists()


def test_sweep_never_touches_a_non_expired_staging_dossier(client, db):
    csrf = _make_account(client, db, "l50bis-sweep2@example.com")
    r = _preview(client, csrf, [_file("rc", "rc.txt", RC)])
    body = r.json()
    dossier_id = uuid.UUID(body["dossier_id"])

    removed = expiry_sweeper.sweep_expired_staging(db)

    assert removed == 0
    assert _exists(db, dossier_id)


def test_sweep_never_touches_a_confirmed_dossier_even_if_it_were_somehow_flagged_expired(client, db):
    csrf = _make_account(client, db, "l50bis-sweep3@example.com")
    r = _preview(client, csrf, [_file("rc", "rc.txt", RC)])
    body = r.json()
    r2 = _confirm(client, csrf, body["dossier_id"], [_decision(p) for p in body["pieces"]])
    assert r2.status_code == 200, r2.text
    dossier_id = uuid.UUID(body["dossier_id"])
    row = db.get(AoDossier, dossier_id)
    assert row.status == "submitted"  # confirmed and linked to a job

    removed = expiry_sweeper.sweep_expired_staging(db, now=datetime.now(timezone.utc) + timedelta(days=3650))

    assert removed == 0
    assert _exists(db, dossier_id)


def test_sweep_is_safe_against_a_confirm_that_happened_first(client, db):
    """A row a concurrent confirm() has ALREADY flipped OUT OF 'staging' between the sweep reading it and
    deleting it must never be removed — the guarded DELETE's WHERE clause (`status='staging' AND
    staging_expires_at < now`) is the atomicity boundary (see module docstring). Simulated directly (a real
    confirm() also clears `staging_expires_at`, which would already exclude the row on its own — this
    isolates the `status` half of the guard specifically, as if the sweep read a stale `staging_expires_at`
    a split second before confirm()'s own commit cleared it)."""
    csrf = _make_account(client, db, "l50bis-sweep4@example.com")
    r = _preview(client, csrf, [_file("rc", "rc.txt", RC)])
    body = r.json()
    dossier_id = uuid.UUID(body["dossier_id"])
    row = db.get(AoDossier, dossier_id)
    row.status = "validated"
    row.staging_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()

    removed = expiry_sweeper.sweep_expired_staging(db)

    assert removed == 0
    assert _exists(db, dossier_id)


def test_sweep_batch_size_bounds_a_single_pass_and_drains_the_rest_over_further_passes(client, db):
    csrf = _make_account(client, db, "l50bis-sweep5@example.com")
    ids = []
    for i in range(3):
        r = _preview(client, csrf, [_file("rc", f"rc{i}.txt", RC + str(i).encode())])
        dossier_id = uuid.UUID(r.json()["dossier_id"])
        _expire(db, dossier_id)
        ids.append(dossier_id)

    first_pass = expiry_sweeper.sweep_expired_staging(db, batch_size=2)
    assert first_pass == 2
    remaining = sum(1 for i in ids if _exists(db, i))
    assert remaining == 1

    second_pass = expiry_sweeper.sweep_expired_staging(db, batch_size=2)
    assert second_pass == 1
    assert all(not _exists(db, i) for i in ids)


def test_sweep_is_idempotent_when_called_again_with_nothing_left_to_remove(client, db):
    csrf = _make_account(client, db, "l50bis-sweep6@example.com")
    r = _preview(client, csrf, [_file("rc", "rc.txt", RC)])
    dossier_id = uuid.UUID(r.json()["dossier_id"])
    _expire(db, dossier_id)

    assert expiry_sweeper.sweep_expired_staging(db) == 1
    assert expiry_sweeper.sweep_expired_staging(db) == 0  # nothing left; never an error, never a re-deletion


def test_sweep_logs_no_piece_name_or_content(client, db, caplog):
    csrf = _make_account(client, db, "l50bis-sweep7@example.com")
    secret_name = "confidentiel-secret-marker.txt"
    r = _preview(client, csrf, [_file("rc", secret_name, RC)])
    dossier_id = uuid.UUID(r.json()["dossier_id"])
    _expire(db, dossier_id)

    with caplog.at_level(logging.DEBUG):
        expiry_sweeper.sweep_expired_staging(db)

    all_messages = "\n".join(record.getMessage() for record in caplog.records)
    assert secret_name not in all_messages
    assert RC.decode() not in all_messages


def test_start_background_sweeper_is_idempotent(monkeypatch):
    calls = {"n": 0}
    original = expiry_sweeper._sweep_once_safely

    def counting():
        calls["n"] += 1
        original()

    monkeypatch.setattr(expiry_sweeper, "_sweep_once_safely", counting)
    monkeypatch.setattr(expiry_sweeper, "_sweeper_started", False)
    expiry_sweeper.start_background_sweeper()
    expiry_sweeper.start_background_sweeper()
    expiry_sweeper.start_background_sweeper()
    assert calls["n"] == 1
