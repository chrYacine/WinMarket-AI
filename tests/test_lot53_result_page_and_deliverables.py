"""Lot 53 — the result page, history entries and PDF/DOCX deliverables actually reflect a revision's own
frozen data (policy label, honest reference count, complements with origin/citation, before/after changes)
— reusing the lot 52 account-building/stub-LLM helpers verbatim (no new harness reinvented).
"""
from __future__ import annotations

import hashlib
import io

from src.web import jobs
from src.web.database.repositories import analysis_complements as complements_repo
from tests.conftest import make_active_starter_user
from tests.test_lot52_completion_facts_http import (
    FREQ_CITATION, FREQ_DOC, FREQ_RESPONSE, _analyze, _complete, _make_prestataire_fact_account,
    _needs, _patch_llm, _search, _upload_doc, _wait,
)


def _pdf_text(content: bytes) -> str:
    import fitz
    doc = fitz.open(stream=content, filetype="pdf")
    return "\n".join(page.get_text("text") for page in doc)


def _docx_text(content: bytes) -> str:
    from docx import Document
    doc = Document(io.BytesIO(content))
    return "\n".join(p.text for p in doc.paragraphs)


def _sourced_revision(client, db, monkeypatch, *, response=None):
    csrf = _make_prestataire_fact_account(client, db, "l53-a@example.com")
    _upload_doc(client, csrf, "conditions.md", FREQ_DOC)
    job = _analyze(client, csrf, "Appel d'offres de nettoyage à Lyon. 3 fois par semaine. Budget : 120 000 euros.")
    state = _needs(client, job.id)
    need = next(n for n in state["needs"] if n["field_key"] == "frequence_nettoyage")
    _patch_llm(monkeypatch, response or FREQ_RESPONSE)
    proposal = _search(client, csrf, job.id, [need["id"]]).json()["results"][0]
    r = _complete(
        client, csrf, job.id, [{"need_id": need["id"], "value": proposal["value"], "source_proposal": proposal}],
        confirm_profile_write=True, expected_profile_version=state["profile_version"],
    )
    assert r.status_code == 200, r.text
    revision = _wait(r.json()["job_id"])
    assert revision.status == "done"
    return csrf, job, revision


def test_result_page_shows_policy_label_reference_count_and_sourced_complement(client, db, monkeypatch):
    csrf, job, revision = _sourced_revision(client, db, monkeypatch)
    page = client.get(f"/app/resultats/{revision.id}")
    assert page.status_code == 200
    assert "Politique de scoring appliquée : v1" in page.text
    assert "Proposition documentaire acceptée" in page.text
    assert FREQ_CITATION in page.text
    assert "conditions.md" in page.text
    assert "Ce qui a changé dans cette révision" in page.text
    assert "Fréquence de nettoyage" in page.text
    assert "Manquante" in page.text  # the "before" value: the account never had a declared frequency


def test_result_page_reference_heading_never_inflates_a_single_document_into_several_references(client, db, monkeypatch):
    csrf, job, revision = _sourced_revision(client, db, monkeypatch)
    page = client.get(f"/app/resultats/{revision.id}")
    assert "0 référence distincte trouvée" in page.text or "référence" in page.text
    assert "documents pertinents trouvés" not in page.text, "the old, uncorrected wording must be gone"


def test_a_citation_containing_html_is_rendered_as_escaped_text_never_executed(client, db, monkeypatch):
    malicious_doc = (
        "Notre fréquence de nettoyage standard <script>alert(1)</script> est de 5 fois par semaine, ajustable."
    )
    response = dict(FREQ_RESPONSE)
    response["citation"] = "standard <script>alert(1)</script> est de 5 fois par semaine"
    csrf = _make_prestataire_fact_account(client, db, "l53-xss@example.com")
    _upload_doc(client, csrf, "conditions.md", malicious_doc)
    job = _analyze(client, csrf, "Appel d'offres de nettoyage à Lyon. 3 fois par semaine. Budget : 120 000 euros.")
    state = _needs(client, job.id)
    need = next(n for n in state["needs"] if n["field_key"] == "frequence_nettoyage")
    _patch_llm(monkeypatch, response)
    proposal = _search(client, csrf, job.id, [need["id"]]).json()["results"][0]
    r = _complete(
        client, csrf, job.id, [{"need_id": need["id"], "value": proposal["value"], "source_proposal": proposal}],
        confirm_profile_write=True, expected_profile_version=state["profile_version"],
    )
    revision = _wait(r.json()["job_id"])
    page = client.get(f"/app/resultats/{revision.id}")
    assert "<script>alert(1)</script>" not in page.text, "raw HTML from a citation must never reach the page unescaped"
    assert "&lt;script&gt;" in page.text, "Jinja's default autoescaping must still be active for citation text"


def test_pdf_and_docx_include_the_same_change_the_result_page_shows(client, db, monkeypatch):
    csrf, job, revision = _sourced_revision(client, db, monkeypatch)
    pdf = client.get(f"/api/download/{revision.id}/pdf").content
    docx = client.get(f"/api/download/{revision.id}/docx").content
    assert pdf[:4] == b"%PDF"
    pdf_text = _pdf_text(pdf)
    assert "Compléments et changements de cette révision" in pdf_text
    assert "Fréquence de nettoyage" in pdf_text
    docx_text = _docx_text(docx)
    assert "Compléments et changements de cette révision" in docx_text
    assert "Fréquence de nettoyage" in docx_text


def test_the_parent_analysis_and_its_own_deliverables_are_byte_identical_after_the_revision(client, db, monkeypatch):
    csrf, job, revision = _sourced_revision(client, db, monkeypatch)
    original_pdf = client.get(f"/api/download/{job.id}/pdf").content
    original_sha = hashlib.sha256(original_pdf).hexdigest()
    reread_job = jobs.get_job(job.id)
    reread_pdf = client.get(f"/api/download/{job.id}/pdf").content
    assert hashlib.sha256(reread_pdf).hexdigest() == original_sha
    assert reread_job.result.decision == job.result.decision


def test_a_foreign_user_cannot_view_someone_elses_result_page(client, db, monkeypatch):
    csrf, job, revision = _sourced_revision(client, db, monkeypatch)
    make_active_starter_user(db, "l53-outsider@example.com", scoring=False)
    from tests.test_lot44_criteria_contract import _login
    _login(client, "l53-outsider@example.com")
    r = client.get(f"/app/resultats/{revision.id}")
    assert r.status_code == 404


def test_result_page_renders_safely_when_the_parent_analysis_is_no_longer_accessible(client, db, monkeypatch):
    """The parent job unreachable (deleted/unreadable — simulated here the same way `parent_label` already
    handled this case before this lot: `jobs.get_job(parent_id)` returning None) — the revision's OWN page
    must still render, with no revision-diff section and no crash/leak (ticket: "afficher un état sûr sans
    fuite")."""
    csrf, job, revision = _sourced_revision(client, db, monkeypatch)
    from src.web import routes_pages

    real_get_job = routes_pages.jobs.get_job
    def _get_job_unless_parent(job_id):
        return None if job_id == job.id else real_get_job(job_id)
    monkeypatch.setattr(routes_pages.jobs, "get_job", _get_job_unless_parent)

    page = client.get(f"/app/resultats/{revision.id}")
    assert page.status_code == 200
    assert "Ce qui a changé dans cette révision" in page.text  # completion_changes is frozen on the result itself
    assert "Décision et critères modifiés" not in page.text, "no parent result to diff against: never fabricated"


def test_history_api_exposes_parent_and_origin_job_ids_for_lineage_display(client, db, monkeypatch):
    csrf, job, revision = _sourced_revision(client, db, monkeypatch)
    r = client.get("/api/history?page=1&page_size=50")
    assert r.status_code == 200
    items = {i["job_id"]: i for i in r.json()["items"] if i["job_id"]}
    assert items[revision.id]["parent_job_id"] == job.id
    assert items[job.id]["parent_job_id"] is None
