"""Pure data-access functions for the `analysis_jobs` table (B12-T1).

This is the durable, private mirror of src/web/jobs.py's in-process
Job/_JOBS state machine — see src/web/database/models.py::AnalysisJob for
the full column rationale. src/web/job_executor.py is the only caller that
should normally need this module; src/web/jobs.py's read fallback
(get_job) also reads from it directly (get_by_id) for the same reason it
already reads the `analyses` table directly: reconstructing a servable Job
from whatever durable trace exists.

Every write here that must be safe under real concurrent access (try_claim
above all) is a single conditional UPDATE whose WHERE clause encodes the
precondition — the database's own row-level transaction handling is what
actually guarantees "exactly one caller wins", not an application-level
lock (see try_claim's docstring).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from src.web.database.models import AnalysisJob


def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_queued(
    db: Session,
    *,
    job_id: str,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    source_label: str = "",
) -> AnalysisJob:
    """Insert the initial `queued` row. Called synchronously by
    src/web/job_executor.py::submit, BEFORE the HTTP response returns
    "accepted" to the caller — this is what survives a crash between
    "accepted" and "a worker even looked at it". Plain INSERT: job_id is
    minted once per job (jobs.create_job) and is this table's primary key,
    so a genuine collision would be a bug upstream, not a case to
    reconcile — unlike `analyses.job_id`, there is no upsert-on-retry
    story here."""
    from src.web.auth.manual_access import check_job_quota
    check_job_quota(db, user_id=user_id, organization_id=organization_id)
    row = AnalysisJob(
        id=job_id, user_id=user_id, organization_id=organization_id,
        status="queued", source_label=(source_label or "")[:255] or None,
    )
    db.add(row)
    db.flush()
    return row


def delete_if_queued(db: Session, *, job_id: str) -> bool:
    """Rollback path for src/web/job_executor.py::submit: undoes the
    durable row written by create_queued when the immediately-following
    bounded-queue reservation turns out to be saturated (queue.Full). Only
    ever called BEFORE the corresponding item is made visible to any
    worker (submit's ordering: write the row, THEN attempt to enqueue —
    never the other way around, precisely to avoid a worker racing to
    claim a row before or after this decision), so there is no concurrent
    claimant to race against here: conditioning on `status = 'queued'`
    anyway is a defensive no-op guard, not a correctness requirement for
    this specific caller."""
    stmt = delete(AnalysisJob).where(AnalysisJob.id == job_id, AnalysisJob.status == "queued")
    result = db.execute(stmt)
    return result.rowcount == 1


def try_claim(db: Session, *, job_id: str, worker_instance_id: str) -> bool:
    """Atomically claim a queued job for `worker_instance_id`: an UPDATE
    conditioned on `status = 'queued'`, exactly one row affected at most.

    This is a real DB-level guarantee, not just an in-process lock: two
    concurrent callers (two threads, or — the untested-but-intended case —
    two separate instances against the same database) each issue this
    UPDATE inside their own transaction. Whichever commits first wins; the
    database serializes the second writer's UPDATE behind the first one's
    commit (a row-level lock on Postgres, SQLite's own writer-serialization
    for the file lock), and by the time the second one actually runs, the
    WHERE clause no longer matches (status is already 'running') — so it
    updates 0 rows. `rowcount == 1` is therefore a reliable "I won" signal
    ; `rowcount == 0` reliably means someone else won, or the row wasn't
    queued (already claimed, already terminal, or never durably written at
    all — see job_executor.submit for when that last case happens) —
    either way the caller's contract is simply "skip, don't error".

    Caller is expected to run this inside its own short-lived
    session_scope() and let that commit — the row lock is held from this
    statement's execution until that commit, which is what makes the
    "exactly one winner" guarantee real rather than advisory.
    """
    stmt = (
        update(AnalysisJob)
        .where(AnalysisJob.id == job_id, AnalysisJob.status == "queued")
        .values(status="running", claimed_at=_now(), last_heartbeat_at=_now(), worker_instance_id=worker_instance_id)
    )
    result = db.execute(stmt)
    return result.rowcount == 1


def update_heartbeat(db: Session, *, job_id: str, worker_instance_id: str) -> bool:
    """Refresh `last_heartbeat_at` for a job this worker instance still
    believes it owns. Conditioned on both `worker_instance_id` and
    `status = 'running'` so a heartbeat tick from a worker that has
    somehow lost its claim (e.g. a future admin action interrupted it
    manually) is a harmless no-op rather than resurrecting a row someone
    else already moved on from."""
    stmt = (
        update(AnalysisJob)
        .where(AnalysisJob.id == job_id, AnalysisJob.worker_instance_id == worker_instance_id, AnalysisJob.status == "running")
        .values(last_heartbeat_at=_now())
    )
    result = db.execute(stmt)
    return result.rowcount == 1


def _mark_terminal(
    db: Session, *, job_id: str, status: str, error_code: Optional[str], analysis_id: Optional[uuid.UUID],
) -> bool:
    """Shared by mark_done/mark_error/mark_interrupted: conditioned on the
    row NOT already being terminal ('done'/'error'/'interrupted'), mirroring
    jobs.py::_fail's own "never revive/overwrite an already-terminal job"
    rule for the in-memory Job — a late/duplicate terminal signal here must
    not overwrite an earlier, already-recorded terminal state either."""
    stmt = (
        update(AnalysisJob)
        .where(AnalysisJob.id == job_id, AnalysisJob.status.notin_(("done", "error", "interrupted")))
        .values(status=status, error_code=error_code, analysis_id=analysis_id, finished_at=_now())
    )
    result = db.execute(stmt)
    return result.rowcount == 1


def mark_done(db: Session, *, job_id: str, analysis_id: Optional[uuid.UUID] = None) -> bool:
    return _mark_terminal(db, job_id=job_id, status="done", error_code=None, analysis_id=analysis_id)


def mark_error(db: Session, *, job_id: str, error_code: Optional[str] = None) -> bool:
    return _mark_terminal(db, job_id=job_id, status="error", error_code=error_code, analysis_id=None)


def mark_interrupted(db: Session, *, job_id: str, error_code: str = "job_interrupted") -> bool:
    """Used directly by a single-row repair path if ever needed; startup
    reconciliation normally uses reconcile_stale_jobs (bulk, same
    conditional-UPDATE idiom) instead of calling this per row."""
    return _mark_terminal(db, job_id=job_id, status="interrupted", error_code=error_code, analysis_id=None)


def reconcile_stale_jobs(db: Session, *, staleness_seconds: int, error_code: str = "job_interrupted") -> int:
    """Startup-time abandonment scan (src/web/job_executor.py::
    reconcile_on_startup calls this once, before the app starts accepting
    traffic). Two conditional UPDATEs, each analogous to try_claim's
    single-statement atomicity:

    1. Every row still 'queued': unconditionally interrupted. A 'queued'
       row means "accepted, but no worker had claimed it yet" — the
       in-process bounded queue (a plain queue.Queue) that would have held
       it does not survive a process restart, so if this row is still
       'queued' at a fresh startup, the item it represents is provably
       gone from every possible in-memory queue; there is no scenario
       where it is still legitimately waiting somewhere.

    2. Every row still 'running' whose `last_heartbeat_at` is missing or
       older than `staleness_seconds`: interrupted. A missing heartbeat
       covers "claimed, then crashed before the first heartbeat tick" or
       the pre-heartbeat path is a live worker refreshing its OWN
       timestamp — see AnalysisJob's docstring for why this is what makes
       the check meaningful even for a (not exercised beyond one real
       single-process-restart test — see the report) different, still-
       alive instance: its heartbeat stays fresh, so this scan correctly
       leaves it alone.

    Returns the total number of rows transitioned, for the caller to log.
    Never touches a row already 'done'/'error'/'interrupted'.
    """
    cutoff = _now() - timedelta(seconds=staleness_seconds)

    queued_stmt = (
        update(AnalysisJob)
        .where(AnalysisJob.status == "queued")
        .values(status="interrupted", error_code=error_code, finished_at=_now())
    )
    queued_count = db.execute(queued_stmt).rowcount or 0

    running_stmt = (
        update(AnalysisJob)
        .where(
            AnalysisJob.status == "running",
            (AnalysisJob.last_heartbeat_at.is_(None)) | (AnalysisJob.last_heartbeat_at < cutoff),
        )
        .values(status="interrupted", error_code=error_code, finished_at=_now())
    )
    running_count = db.execute(running_stmt).rowcount or 0

    return queued_count + running_count


def get_by_id(db: Session, job_id: str) -> Optional[AnalysisJob]:
    """Unscoped read — internal server-side use only (src/web/jobs.py's
    get_job() fallback chain), exactly like analyses_repo.get_by_job_id:
    the row's own user_id/organization_id are carried onto the
    reconstructed Job, and the CALLER (the route) is what enforces
    ownership, same as every other branch of that fallback chain."""
    stmt = select(AnalysisJob).where(AnalysisJob.id == job_id)
    return db.execute(stmt).scalar_one_or_none()


def get_by_id_for_user(db: Session, job_id: str, user_id: uuid.UUID) -> Optional[AnalysisJob]:
    """Ownership-checked lookup — never returns another user's job row."""
    stmt = select(AnalysisJob).where(AnalysisJob.id == job_id, AnalysisJob.user_id == user_id)
    return db.execute(stmt).scalar_one_or_none()
