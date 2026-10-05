"""B26-T1 — minimal structured job logging, health/readiness distinguishing
liveness from dependency availability, and backup/restore (the backup/
restore proof lives in test_b26_t1_backup_restore.py, kept separate since
it drives real subprocess scripts against temporary files).
"""
from __future__ import annotations

import logging

from src.core.logger import WARNING, log_job_event


def test_log_job_event_refuses_a_field_shaped_like_a_secret_or_prompt():
    logger = logging.getLogger("test_b26")
    for bad_key in ("prompt", "system_prompt", "api_token", "password", "document_content", "session_cookie"):
        try:
            log_job_event(logger, WARNING, "job_terminal_error", job_id="j1", **{bad_key: "whatever"})
            assert False, f"expected ValueError for forbidden field {bad_key!r}"
        except ValueError:
            pass


def test_log_job_event_accepts_safe_fields_and_reaches_the_real_logger(caplog):
    logger = logging.getLogger("test_b26_safe")
    with caplog.at_level(logging.WARNING, logger="test_b26_safe"):
        log_job_event(logger, WARNING, "job_terminal_error", job_id="job-123", step="scoring", error_code="scoring_failed")
    assert any("job_terminal_error" in r.message for r in caplog.records)


def test_healthz_is_unauthenticated_and_never_touches_the_database(client, monkeypatch):
    from src.web.database import session as db_session_module

    def _boom(*a, **kw):
        raise AssertionError("healthz must never touch the database")

    monkeypatch.setattr(db_session_module, "get_engine", _boom)
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_readyz_reports_ok_when_database_is_reachable(client):
    r = client.get("/readyz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["checks"]["database"] == "ok"


def test_readyz_reports_degraded_without_leaking_connection_details_when_db_is_down(client, monkeypatch):
    from src.web.database import session as db_session_module

    import contextlib

    @contextlib.contextmanager
    def _fake_session_scope():
        raise RuntimeError("connection to server at 'super-secret-host.internal' failed: password authentication failed for user 'admin'")
        yield  # pragma: no cover — unreachable, keeps this a generator function

    monkeypatch.setattr(db_session_module, "session_scope", _fake_session_scope)
    r = client.get("/readyz")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "degraded"
    assert body["checks"]["database"] == "unreachable"
    assert "super-secret-host" not in r.text
    assert "password" not in r.text.lower()
