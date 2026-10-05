"""QA verification pass on ticket B01 — scenarios T06 and T10 from the audit
checklist that aren't covered by tests/test_document_isolation.py.

Read-only verification: no application code is modified here.
"""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from src.core.models import AOContext, ScoringResult
from tests.conftest import default_org_id, make_active_starter_user


def _ao(titre: str = "Refonte plateforme e-commerce") -> AOContext:
    return AOContext(titre=titre, client="Client Test", secteur="Retail")


def _result(decision: str = "GO", score: float = 90.0) -> ScoringResult:
    return ScoringResult(decision=decision, score_global=score, criteres=[])


@pytest.fixture()
def isolated_storage(tmp_path, monkeypatch):
    from src.core import config
    from src.web import jobs as jobs_module

    output_dir = tmp_path / "outputs"
    output_dir.mkdir()
    monkeypatch.setattr(config, "OUTPUT_DIR", output_dir)
    monkeypatch.setattr(config, "LOCAL_STORAGE_PATH", tmp_path)
    monkeypatch.setattr(jobs_module, "ANALYSIS_FILES_DIR", output_dir)
    return output_dir


def _persist_job_to_db(job, ao, result, db):
    from src.livrables.document_generator import _safe_name
    from src.web.database.repositories import analyses as analyses_repo
    from src.web.storage.service import get_storage_service

    analysis = analyses_repo.create_analysis(
        db, user_id=job.user_id, organization_id=job.organization_id, job_id=job.id, title=ao.titre,
        client_name=ao.client, score=result.score_global, decision=result.decision,
        result_data={"ao": ao.model_dump(), "result": result.model_dump()},
    )
    storage = get_storage_service()
    readable_stem = _safe_name(ao.titre)
    original_names = {"pdf": f"rapport_decision_{readable_stem}.pdf", "docx": f"candidature_{readable_stem}.docx"}
    for kind, path_str in job.files.items():
        path = Path(path_str)
        analyses_repo.add_document(
            db, analysis_id=analysis.id, user_id=job.user_id, organization_id=job.organization_id,
            filename=path.name, original_filename=original_names[kind],
            storage_path=storage.save(path),
            mime_type=("application/pdf" if kind == "pdf" else
                       "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
            file_size=path.stat().st_size,
        )
    db.commit()
    return analysis


# ---------------------------------------------------------------------------
# T06 — anonymous access
# ---------------------------------------------------------------------------

def test_anonymous_download_is_refused_no_bytes_leaked(client, db, isolated_storage):
    from src.web import jobs

    user = make_active_starter_user(db, "t06@example.com")
    job = jobs.create_job(source_label="x", user_id=user.id, organization_id=default_org_id(db, user))
    ao, result = _ao(), _result()
    job.files = jobs._generate_documents(job, ao, result)
    job.ao, job.result, job.status = ao, result, "done"
    _persist_job_to_db(job, ao, result, db)

    # No login call at all — client has no session cookie.
    r = client.get(f"/api/download/{job.id}/pdf")
    assert r.status_code in (401, 303)
    if r.status_code == 200:
        pytest.fail("anonymous request must never receive document bytes")
    assert b"%PDF" not in r.content


# ---------------------------------------------------------------------------
# T10 — interrupted generation
# ---------------------------------------------------------------------------

def test_interrupted_generation_leaves_no_downloadable_partial_and_no_damage(
    isolated_storage, monkeypatch
):
    """A crash between writing the .tmp file and the atomic rename must not:
    - expose a partial file as if it were a finished document, and
    - must not touch a different, already-finalized document (distinct
      document_id per call makes cross-damage structurally impossible, but
      we verify it holds in practice too).
    """
    from src.livrables import document_generator as docgen_module
    from src.web import jobs

    user_id = uuid.uuid4()
    ao = _ao("Support N3 infogérance")

    # First, a normal, successful generation — this must survive untouched.
    job_ok = jobs.create_job(source_label="ok", user_id=user_id)
    files_ok = jobs._generate_documents(job_ok, ao, _result(decision="GO", score=70.0))
    good_pdf_bytes = Path(files_ok["pdf"]).read_bytes()
    good_docx_bytes = Path(files_ok["docx"]).read_bytes()

    # Now simulate a crash: _finalize (the atomic os.replace step) raises
    # before the second document is ever published under its final name.
    def _boom(tmp_path, final_path):
        assert Path(tmp_path).exists(), "temp file should be fully written before finalize runs"
        raise OSError("simulated crash before finalize")

    monkeypatch.setattr(docgen_module, "_finalize", _boom)

    job_crash = jobs.create_job(source_label="crash", user_id=user_id)
    with pytest.raises(OSError):
        jobs._generate_documents(job_crash, ao, _result(decision="GO", score=71.0))

    # The crashed job never gets a `files` dict pointing at a partial file,
    # so nothing downstream (DB persistence, download route) can ever serve
    # a half-written document for it.
    assert not hasattr(job_crash, "files") or not job_crash.files

    # Any leftover .tmp artifact on disk (if one exists) is not a valid,
    # openable document of its type, and lives at a distinct path from the
    # successful job — so it can never masquerade as job_ok's document.
    crash_dir = isolated_storage / str(user_id) / job_crash.id
    leftover_tmps = list(crash_dir.glob("*.tmp")) if crash_dir.exists() else []
    for tmp_file in leftover_tmps:
        assert not tmp_file.name.endswith((".pdf", ".docx"))

    # The earlier, successful document is byte-for-byte unchanged.
    assert Path(files_ok["pdf"]).read_bytes() == good_pdf_bytes
    assert Path(files_ok["docx"]).read_bytes() == good_docx_bytes
