"""Lot 50 bis §4 — actual expiration of abandoned dossier-preview staging rows.

Before this module, an expired `AoDossier(status='staging')` was only ever cleaned up the NEXT time a confirm
was attempted against it (`routes_api.py::api_dossier_preview_confirm`) — an account that opens the preview
and never comes back left the row (and its private-storage files) sitting forever. This module adds a REAL
sweep that runs even if nobody ever revisits it: once at process startup (catching up on anything that
expired while the process was down) and then periodically on a background thread, reusing the SAME
startup-lifecycle hook `job_executor.reconcile_on_startup()` already uses (`main.py`'s `_lifespan`) — no new
scheduler/framework, per the ticket ("réutilise le cycle de maintenance existant").

Safety, by construction (no explicit locking needed):
- the DELETE that removes a row is itself the atomicity boundary: `WHERE status='staging' AND
  staging_expires_at < :now` is re-evaluated by the database at the moment of the delete. If a concurrent
  confirm() has ALREADY flipped the row to 'validated' (or discarded it) between this sweep reading it and
  deleting it, the DELETE simply matches zero rows for that id — never a race that corrupts a dossier a user
  is actively confirming. Symmetrically, if the sweep deletes a row a split second before a confirm() call
  reads it, that confirm() finds nothing (`get_staging_for_owner` returns None) and answers 404 — the EXACT
  same outcome an already-expired-and-swept-later row would have produced anyway;
- storage (the pieces' actual files) is only ever removed AFTER the corresponding DB row is committed
  deleted — a crash between the two leaves an orphan DIRECTORY, never a DB row pointing at deleted files;
- only `status='staging'` rows are ever touched — a 'validated'/'submitted' dossier (confirmed, or a plain
  direct-submit one) is never a sweep candidate, whatever `staging_expires_at` a legacy row might carry
  (NULL for every one of those by construction, so the `IS NOT NULL AND < now` guard excludes them anyway —
  belt and suspenders);
- bounded batches (`config.DOSSIER_STAGING_SWEEP_BATCH_SIZE` per pass): a huge backlog is drained over several
  passes, never one unbounded transaction;
- logs carry only ids/counts — never a piece's name or content.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

from sqlalchemy import delete, select

from src.core import config
from src.core.logger import get_agent_logger
from src.web.ao_dossier import storage
from src.web.database.models import AoDossier

logger = get_agent_logger("dossier_expiry_sweeper")

_sweeper_lock = threading.Lock()
_sweeper_started = False


def sweep_expired_staging(
    db, *, now: datetime | None = None, batch_size: int | None = None, _after_select=None,
) -> int:
    """ONE bounded pass: deletes up to `batch_size` expired staging dossiers (and their private-storage
    files) for ALL accounts — a maintenance task, not a per-request/per-owner operation. Returns how many were
    actually removed (0 is a normal, frequent result, not an error). Idempotent and safe to call concurrently
    with itself or with a confirm attempt (see module docstring).

    `_after_select` (lot 50 ter §4, test-only — production code, including `main.py`'s own callers, never
    passes it): an optional callback invoked with the candidate list right after the SELECT above and before
    the DELETE loop below, so a test can deterministically interleave a real `confirm()` INTO that exact
    window — proving the guarded DELETE (not just "confirm happened first, then sweep found nothing") is what
    actually protects a dossier a confirm is racing to admit, not merely a same-transaction illusion."""
    now = now or datetime.now(timezone.utc)
    batch_size = batch_size or config.DOSSIER_STAGING_SWEEP_BATCH_SIZE
    candidates = db.execute(
        select(AoDossier.id, AoDossier.organization_id, AoDossier.user_id)
        .where(AoDossier.status == "staging", AoDossier.staging_expires_at.is_not(None), AoDossier.staging_expires_at < now)
        .limit(batch_size)
    ).all()
    if _after_select is not None:
        _after_select(candidates)
    removed = 0
    for dossier_id, organization_id, user_id in candidates:
        result = db.execute(
            delete(AoDossier).where(AoDossier.id == dossier_id, AoDossier.status == "staging", AoDossier.staging_expires_at < now)
        )
        db.commit()
        if result.rowcount:
            removed += 1
            try:
                storage.remove_dossier_dir(organization_id, user_id, dossier_id)
            except Exception:
                logger.exception("Failed to remove storage for an expired staging dossier dossier_id=%s", dossier_id)
    if removed:
        logger.info("Expired staging dossier sweep removed=%d candidates=%d", removed, len(candidates))
    return removed


def _sweep_once_safely() -> None:
    try:
        from src.web.database.session import session_scope

        with session_scope() as db:
            sweep_expired_staging(db)
    except Exception:
        # Never let a transient DB hiccup take down the sweeper thread — the next tick (or the next process
        # restart's catch-up pass) retries. An abandoned staging row is not urgent to delete: correctness
        # never depends on this ever running (a confirm attempt on an expired row still refuses and cleans up
        # on its own, per the pre-existing behavior this module supplements, never replaces).
        logger.exception("Staging dossier sweep pass failed — will retry on the next tick")


def _sweeper_loop() -> None:
    interval = max(60, config.DOSSIER_STAGING_SWEEP_INTERVAL_SECONDS)
    while True:
        time.sleep(interval)
        _sweep_once_safely()


def start_background_sweeper() -> None:
    """Idempotent: safe to call more than once (e.g. from tests or a reload) — only ever starts ONE thread.
    Runs one immediate catch-up pass synchronously (so a long-idle process's backlog is not left until the
    first `interval` elapses) before handing off to the periodic daemon thread."""
    global _sweeper_started
    with _sweeper_lock:
        if _sweeper_started:
            return
        _sweeper_started = True
    _sweep_once_safely()
    thread = threading.Thread(target=_sweeper_loop, name="dossier-staging-sweeper", daemon=True)
    thread.start()
    logger.info("Dossier staging sweeper started interval_seconds=%d batch_size=%d",
                config.DOSSIER_STAGING_SWEEP_INTERVAL_SECONDS, config.DOSSIER_STAGING_SWEEP_BATCH_SIZE)
