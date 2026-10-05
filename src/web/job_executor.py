"""B12-T1 — bounded background-analysis worker pool.

Replaces src/web/jobs.py::start_analysis's previous unbounded
`threading.Thread(target=_run_analysis, args=(job, text), daemon=True);
thread.start()` — a fresh, uncapped OS thread per submitted analysis, with
no queue and no durable trace that a job was even ACCEPTED until
_run_analysis got far enough to call _save_analysis_snapshot() (only after
the score was computed).

Stdlib only, per the ticket ("pas de broker/framework nouveau sans
nécessité démontrée"): a `queue.Queue(maxsize=...)` plus a small, fixed
pool of long-lived `threading.Thread` workers consuming it — the same
primitive the codebase already used, just bounded.

Design summary (see docs/api/B12_T1_JOB_EXECUTOR_CONTRACT.md for the full
contract the coordinator wires into main.py/routes_api.py):

1. submit(job, text) — called from jobs.start_analysis (the request-
   handling thread). Writes a durable 'queued' row synchronously FIRST,
   still inside this call — this is what survives a crash between "the
   HTTP response said accepted" and "a worker even looked at it" — and
   only THEN reserves a queue slot with a non-blocking put. If the queue
   is already full this raises JobQueueSaturatedError and rolls back the
   row it just wrote, so a refused submission never leaves a ghost row.
   This order (row, then enqueue) is load-bearing, not incidental — see
   submit()'s own docstring for the race it avoids.

2. A worker thread pulls the item, ATOMICALLY claims the durable row
   (analysis_jobs_repo.try_claim — a conditional UPDATE, real DB-level
   correctness, not just this pool's own in-memory queue serialization).
   If the claim fails (nothing to claim — e.g. the durable write above
   failed and jobs._fail() already marked the in-memory Job terminal) the
   worker silently skips it — never an error, per the ticket.

3. The claimed job runs src/web/jobs.py::_run_analysis completely
   unchanged — this module only gates/wraps that call, it does not
   reimplement or alter any of its scoring/persistence behavior.

4. A per-job heartbeat thread refreshes `last_heartbeat_at` while the run
   is in progress, stopped the moment _run_analysis returns. Afterwards
   the in-memory Job's terminal status is mirrored onto the durable row
   (done/error), in its own short-lived session — never held open across
   the pipeline run itself.

5. reconcile_on_startup() — meant to be called once by main.py, before the
   app starts accepting requests (see the contract doc for the exact
   snippet) — marks any row still 'queued' (always abandoned: the
   in-memory queue that would have held it cannot have survived a
   restart) or 'running' with a stale/missing heartbeat as 'interrupted'.
   No automatic retry, ever: an interrupted job just sits there reporting
   its own state; see src/web/jobs.py's new fallback branch for how that
   reaches /api/analyze/{job_id}/status.
"""
from __future__ import annotations

from src.web.auth.manual_access import ManualAccessDenied

import queue
import threading
import uuid
from dataclasses import dataclass
from typing import Optional

from src.core import config
from src.core.logger import get_agent_logger
from src.web import jobs
from src.web.auth.dependencies import evaluate_subscription_access
from src.web.database.repositories import analysis_jobs as analysis_jobs_repo
from src.web.database.repositories import users as users_repo
from src.web.database.session import is_database_configured, session_scope

logger = get_agent_logger("web_job_executor")

# B20-T1: a job can sit `queued` for a while (worker pool busy, or simply
# scheduling latency) between the moment /api/analyze accepted it (subscription
# was active then) and the moment a worker actually claims it — the
# subscription can lapse (expire/cancel) in that window. This is a NEW error
# code, defined here rather than in src/web/jobs.py (owned by another lot this
# round) — jobs._fail()'s `error_code` parameter is a plain string, it does
# not need to be a name that lives in jobs.py itself. Sibling of jobs.py's own
# WORKER_LAUNCH_FAILED_ERROR_CODE etc: stable, safe-to-expose, never a silent
# 404 and never an automatic retry/grace period.
SUBSCRIPTION_NO_LONGER_ACTIVE_ERROR_CODE = "subscription_no_longer_active"
_SUBSCRIPTION_NO_LONGER_ACTIVE_MESSAGE = (
    "Votre abonnement Starter n'est plus actif : cette analyse n'a pas été lancée (aucun appel "
    "IA n'a été effectué). Réactivez votre abonnement puis relancez une nouvelle analyse si besoin."
)

# Minted once per process — this is the identity a claim/heartbeat is
# recorded under (AnalysisJob.worker_instance_id), for observability only;
# reconciliation's abandonment decision is keyed on heartbeat staleness,
# never on comparing this id against "the current process's id" (a fresh
# process always has a NEW id, so that comparison would trivially and
# wrongly call every leftover row abandoned regardless of whether some
# other, still-alive instance actually owns it).
WORKER_INSTANCE_ID = uuid.uuid4().hex


class JobQueueSaturatedError(RuntimeError):
    """Raised by submit() when the bounded queue has no room at submit
    time. The coordinator maps this to an explicit HTTP status (429
    recommended — see docs/api/B12_T1_JOB_EXECUTOR_CONTRACT.md) at the
    /api/analyze route boundary. No durable row is ever created for the
    submission that raised this."""

    def __init__(self, job_id: str):
        super().__init__(f"Job queue is saturated (maxsize={config.JOB_QUEUE_MAX_DEPTH}); refusing job_id={job_id}")
        self.job_id = job_id


@dataclass
class _WorkItem:
    job: "jobs.Job"
    text: str = ""
    # Lot 47 bis: the id of a validated AO dossier. The worker reads the dossier from the database and the private
    # storage — the queue never carries the pieces' content, and a dossier survives a restart.
    dossier_id: Optional[uuid.UUID] = None
    # Lot 49: set instead of `text`/`dossier_id` for a completion revision — see jobs.RevisionSpec and
    # jobs._run_revision. Mutually exclusive with the analysis path in _process below.
    revision: Optional["jobs.RevisionSpec"] = None


_QUEUE: "queue.Queue[_WorkItem]" = queue.Queue(maxsize=max(1, config.JOB_QUEUE_MAX_DEPTH))
_workers_started = False
_workers_lock = threading.Lock()


def _ensure_workers_started() -> None:
    global _workers_started
    if _workers_started:
        return
    with _workers_lock:
        if _workers_started:
            return
        count = max(1, config.JOB_EXECUTOR_MAX_CONCURRENCY)
        for i in range(count):
            t = threading.Thread(target=_worker_loop, name=f"job-executor-worker-{i}", daemon=True)
            t.start()
        _workers_started = True
        logger.info("Job executor started worker_count=%d queue_maxsize=%d", count, config.JOB_QUEUE_MAX_DEPTH)


def submit(job: "jobs.Job", text: str, dossier_id: Optional[uuid.UUID] = None) -> None:
    """Entry point called by src/web/jobs.py::start_analysis. Raises
    JobQueueSaturatedError on saturation (and performs no lasting side
    effect in that case — see module docstring point 1 and the ordering
    note below).

    ORDERING IS LOAD-BEARING: the durable 'queued' row is written FIRST,
    and the item is only made visible to worker threads (the
    queue.Queue.put_nowait call) AFTER that write commits — never the
    other way around. This pool's own worker threads are long-lived and
    may already be polling the queue by the time this runs, so if the
    item were enqueued before the row existed, a worker could dequeue it
    and call try_claim() before the row was there to claim — a genuine
    inter-thread race this module hit during development (see the report)
    that would strand the job at 'running' forever with no one left to
    process it, since queue.Queue.get() delivers each item to exactly one
    consumer, once. Writing the row first means try_claim always has
    something to find once an item is actually dequeued.

    On saturation (queue.Full), a row written just above by this same
    call is rolled back (delete_if_queued) before raising — never left
    behind as a ghost 'queued' row for a submission that was, in the end,
    refused.
    """
    _enqueue(_WorkItem(job=job, text=text, dossier_id=dossier_id))


def submit_revision(job: "jobs.Job", spec: "jobs.RevisionSpec") -> None:
    """Lot 49 — same durable-row-then-enqueue contract as submit() above (see its docstring for why the
    ordering is load-bearing), for a completion revision instead of a fresh analysis. `_process` reads
    `item.revision` and calls jobs._run_revision instead of jobs._run_analysis; everything else (durable
    queue row, atomic claim, heartbeat, subscription re-check, saturation handling) is unchanged."""
    _enqueue(_WorkItem(job=job, revision=spec))


def _enqueue(item: "_WorkItem") -> None:
    job = item.job
    _ensure_workers_started()

    durable_row_written = False
    if job.user_id is not None and job.organization_id is not None and is_database_configured():
        try:
            with session_scope() as db:
                analysis_jobs_repo.create_queued(
                    db, job_id=job.id, user_id=job.user_id, organization_id=job.organization_id,
                    source_label=job.source_label,
                )
            durable_row_written = True
        except ManualAccessDenied:
            jobs._fail(job, message="Activation ou quota insuffisant.", error_code="MANUAL_ACCESS_DENIED")
            raise
        except Exception:
            # Nothing has been enqueued yet at this point — no worker can
            # possibly be racing to claim a row that was never written and
            # never made visible. Mark the in-memory job terminal right
            # here and stop: there is nothing left for a worker to do.
            logger.exception("Failed to write the durable queued row job_id=%s", job.id)
            jobs._fail(
                job,
                message="Impossible d'enregistrer la demande d'analyse. Réessayez ou contactez le support si le problème persiste.",
                error_code=jobs.WORKER_LAUNCH_FAILED_ERROR_CODE,
            )
            return
    # else: anonymous/ownerless job (e.g. a legacy caller, or a test built
    # directly with jobs.create_job()) or no database configured — jobs.
    # py's own _save_analysis_snapshot skips the `analyses` table the same
    # way for the same reason (no owner to file a row under, or nothing to
    # write to). The job still runs normally through this pool; it just
    # cannot have a durable queue row and therefore cannot survive a
    # restart while merely queued/running — an existing, documented
    # limitation, not a new one introduced by this ticket.

    try:
        _QUEUE.put_nowait(item)
    except queue.Full:
        if durable_row_written:
            try:
                with session_scope() as db:
                    analysis_jobs_repo.delete_if_queued(db, job_id=job.id)
            except Exception:
                logger.exception("Failed to roll back the durable row for a saturated submission job_id=%s", job.id)
        raise JobQueueSaturatedError(job.id)


def _worker_loop() -> None:
    while True:
        item = _QUEUE.get()
        try:
            _process(item)
        except Exception:
            logger.exception("Unhandled exception processing job_id=%s", item.job.id)
        finally:
            _QUEUE.task_done()


def _run_item(job: "jobs.Job", item: _WorkItem) -> None:
    """The one call that actually executes `item`, whichever kind it is — kept as a single line the two
    branches below share, so a fresh analysis and a lot 49 completion revision can never diverge in how
    they are dispatched once claimed."""
    if item.revision is not None:
        jobs._run_revision(job, item.revision)
    else:
        jobs._run_analysis(job, item.text, dossier_id=item.dossier_id)


def _process(item: _WorkItem) -> None:
    job = item.job

    if job.user_id is None or job.organization_id is None or not is_database_configured():
        # No durable row exists to claim for this job (see submit() above)
        # — run it directly, exactly as jobs.start_analysis always did for
        # any job before this ticket.
        _run_item(job, item)
        return

    try:
        with session_scope() as db:
            claimed = analysis_jobs_repo.try_claim(db, job_id=job.id, worker_instance_id=WORKER_INSTANCE_ID)
    except Exception:
        logger.exception("Failed to claim durable job row job_id=%s", job.id)
        claimed = False

    if not claimed:
        # Someone else claimed it first, it is no longer 'queued' (already
        # terminal), or the row never existed (submit()'s durable write
        # failed and already marked the in-memory job terminal). Skip,
        # never an error — this is the whole point of an atomic claim.
        logger.info("Skipping job_id=%s — claim unsuccessful (already claimed/terminal/missing row)", job.id)
        return

    # B20-T1: re-check the SAME subscription gate every route already
    # enforces at request time, right here at claim time, with a FRESH read
    # (never a cached/stale value from submission time — see
    # _user_subscription_still_active's own docstring). This runs strictly
    # AFTER the durable row is claimed (so it is never re-evaluated for a
    # job that already reached 'done'/'error') and strictly BEFORE
    # jobs._run_analysis is ever called — no LLM call, no scoring, no
    # document generation happens for a lapsed subscription. An
    # already-computed `analyses` row / already-generated documents for a
    # PRIOR job are never touched by this check.
    if not _user_subscription_still_active(job.user_id):
        logger.warning(
            "Refusing to run job_id=%s — subscription no longer active at claim time for user_id=%s",
            job.id, job.user_id,
        )
        jobs._fail(
            job,
            message=_SUBSCRIPTION_NO_LONGER_ACTIVE_MESSAGE,
            error_code=SUBSCRIPTION_NO_LONGER_ACTIVE_ERROR_CODE,
        )
        _finalize(job)
        return

    stop_heartbeat = threading.Event()
    hb_thread = threading.Thread(
        target=_heartbeat_loop, args=(job.id, stop_heartbeat), name=f"job-heartbeat-{job.id}", daemon=True,
    )
    hb_thread.start()
    try:
        _run_item(job, item)
    finally:
        stop_heartbeat.set()
        hb_thread.join(timeout=2.0)
        _finalize(job)


def _user_subscription_still_active(user_id: uuid.UUID) -> bool:
    """B20-T1: a FRESH re-check of src.web.auth.dependencies.
    evaluate_subscription_access — the exact same shared gate
    require_active_starter_user (API) and resolve_app_access (pages)
    enforce at request time — performed in its own short-lived
    session_scope(), never reusing a session/value captured back at
    submission time. Fails CLOSED: any error verifying access (DB hiccup,
    missing user) is treated as "not active", never as "access granted" —
    mirrors evaluate_subscription_access's own ambiguous-state rule
    ("never invent a right") one level up."""
    try:
        with session_scope() as db:
            user = users_repo.get_by_id(db, user_id)
            return evaluate_subscription_access(db, user).granted
    except Exception:
        logger.exception(
            "Failed to re-check subscription access at claim time for user_id=%s — treating as inactive", user_id,
        )
        return False


def _heartbeat_loop(job_id: str, stop: threading.Event) -> None:
    """Ticks every JOB_HEARTBEAT_INTERVAL_SECONDS while the job runs. The
    first tick only fires one interval after the claim (try_claim already
    stamps an initial last_heartbeat_at at claim time, so there is no
    window where a freshly-claimed row looks heartbeat-less)."""
    while not stop.wait(max(1, config.JOB_HEARTBEAT_INTERVAL_SECONDS)):
        try:
            with session_scope() as db:
                analysis_jobs_repo.update_heartbeat(db, job_id=job_id, worker_instance_id=WORKER_INSTANCE_ID)
        except Exception:
            logger.exception("Failed to update heartbeat job_id=%s", job_id)


def _finalize(job: "jobs.Job") -> None:
    """Mirror the in-memory Job's terminal outcome onto the durable row —
    short-lived session, never held open across the pipeline run (that
    already finished by the time this runs)."""
    try:
        with session_scope() as db:
            if job.status == "done":
                analysis_jobs_repo.mark_done(db, job_id=job.id, analysis_id=job.analysis_id)
            else:
                analysis_jobs_repo.mark_error(db, job_id=job.id, error_code=job.error_code)
    except Exception:
        logger.exception("Failed to mirror terminal state to analysis_jobs job_id=%s", job.id)


def reconcile_on_startup(staleness_seconds: Optional[int] = None) -> int:
    """Meant to be called exactly once by main.py, at process startup,
    BEFORE the app starts accepting requests (see
    docs/api/B12_T1_JOB_EXECUTOR_CONTRACT.md for the exact call site) —
    this module does not wire itself into main.py.

    Marks every row still 'queued' (unconditionally — a queue.Queue does
    not survive a restart, so a 'queued' row at a fresh startup cannot
    possibly still be waiting anywhere) or 'running' with a stale/missing
    heartbeat as 'interrupted'. Returns the count of rows transitioned (0
    if no database is configured, or on any internal error — never raises,
    since a reconciliation problem must not prevent the app from starting).
    """
    if not is_database_configured():
        return 0
    staleness = staleness_seconds if staleness_seconds is not None else config.JOB_HEARTBEAT_STALE_SECONDS
    try:
        with session_scope() as db:
            count = analysis_jobs_repo.reconcile_stale_jobs(db, staleness_seconds=staleness)
        if count:
            logger.warning("Startup reconciliation marked %d job(s) as interrupted", count)
        return count
    except Exception:
        logger.exception("Startup job reconciliation failed")
        return 0
