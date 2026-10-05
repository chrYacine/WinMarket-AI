"""Lot 47 — the download follows the ACTIVE version's real format; a failed upload / a full corpus never traps the user.

A. after a replacement in another format the download carries the name, extension and Content-Type of the version
   that is actually served (reproduced at lot 45: it kept the creation name/extension, e.g. `rapport.pdf` for DOCX bytes);
   a failed replacement keeps serving the previous valid version with ITS metadata; nothing is derived from a
   user-supplied path and no file is renamed on disk; an unknown format gets a neutral name without a made-up extension;
B. the quota rule is unchanged (a document created in failure occupies a place until deleted) and the journey has no dead
   end: full corpus -> refusal -> deletion -> addition; replacing a valid document never needs a free place.
"""
from __future__ import annotations

import io
import re
from types import SimpleNamespace

import pytest
from docx import Document as DocxDocument

from src.web.knowledge import documents_service
from tests.conftest import make_active_starter_user
from tests.test_b03_private_knowledge import _login, _upload

PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _pdf_bytes(text: str) -> bytes:
    import fitz

    doc = fitz.open()
    doc.new_page().insert_text((72, 72), text)
    return doc.tobytes()


def _docx_bytes(*paragraphs: str) -> bytes:
    doc = DocxDocument()
    for p in paragraphs:
        doc.add_paragraph(p)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _put_version(client, csrf, doc_id, filename, content):
    return client.post(f"/api/knowledge/documents/{doc_id}/versions", files={"file": (filename, io.BytesIO(content), "application/octet-stream")},
                       headers={"X-CSRF-Token": csrf})


def _disposition_filename(response) -> str:
    header = response.headers["content-disposition"]
    star = re.search(r"filename\*=utf-8''([^;]+)", header, re.IGNORECASE)
    if star:
        from urllib.parse import unquote
        return unquote(star.group(1))
    return re.search(r'filename="([^"]*)"', header).group(1)


# ---------------------------------------------------------------------------
# A — name / extension / Content-Type of the version really served
# ---------------------------------------------------------------------------

def test_a_download_name_extension_and_type_follow_the_active_version_after_a_change_of_format(client, db):
    make_active_starter_user(db, "l47-format@example.com")
    csrf = _login(client, "l47-format@example.com")
    pdf = _pdf_bytes("MARQUEUR_L47_PDF rapport de synthese.")
    doc_id = _upload(client, "rapport.pdf", pdf, csrf=csrf).json()["document"]["id"]
    first = client.get(f"/api/knowledge/documents/{doc_id}/download")
    assert (first.content, first.headers["content-type"].split(";")[0], _disposition_filename(first)) == (pdf, PDF, "rapport.pdf")

    docx = _docx_bytes("Introduction", "MARQUEUR_L47_DOCX memoire technique.")
    assert _put_version(client, csrf, doc_id, "memoire.docx", docx).status_code == 200
    second = client.get(f"/api/knowledge/documents/{doc_id}/download")
    assert second.content == docx, "the bytes are the ACTIVE version's"
    assert second.headers["content-type"].split(";")[0] == DOCX
    assert _disposition_filename(second) == "rapport.docx", "the extension is the version's, not the creation name's"
    assert second.headers["content-disposition"].startswith("attachment")
    assert second.headers.get("x-content-type-options") == "nosniff"

    md = b"# Note\n\nMARQUEUR_L47_MD contenu.\n"
    assert _put_version(client, csrf, doc_id, "note.md", md).status_code == 200
    third = client.get(f"/api/knowledge/documents/{doc_id}/download")
    assert (third.content, third.headers["content-type"].split(";")[0], _disposition_filename(third)) == (md, "text/markdown", "rapport.md")
    assert _put_version(client, csrf, doc_id, "note.txt", b"MARQUEUR_L47_TXT brut").status_code == 200
    fourth = client.get(f"/api/knowledge/documents/{doc_id}/download")
    assert (fourth.headers["content-type"].split(";")[0], _disposition_filename(fourth)) == ("text/plain", "rapport.txt")


def test_a_a_failed_replacement_keeps_serving_the_previous_valid_version_with_its_own_metadata(client, db):
    make_active_starter_user(db, "l47-failed@example.com")
    csrf = _login(client, "l47-failed@example.com")
    doc_id = _upload(client, "offre.pdf", _pdf_bytes("MARQUEUR_L47_A"), csrf=csrf).json()["document"]["id"]
    docx = _docx_bytes("MARQUEUR_L47_B version valide")
    assert _put_version(client, csrf, doc_id, "offre.docx", docx).status_code == 200
    for bad_name, bad_bytes in (("casse.docx", b"PK\x03\x04pas une archive"), ("vide.md", b"  \n "), ("scan.pdf", _blank_pdf())):
        assert _put_version(client, csrf, doc_id, bad_name, bad_bytes).status_code == 422
        served = client.get(f"/api/knowledge/documents/{doc_id}/download")
        assert served.status_code == 200 and served.content == docx, bad_name
        assert served.headers["content-type"].split(";")[0] == DOCX and _disposition_filename(served) == "offre.docx", bad_name
    detail = client.get(f"/api/knowledge/documents/{doc_id}").json()
    assert detail["active_version_number"] == 2 and [v["status"] for v in detail["versions"]] == ["ready", "ready", "failed", "failed", "failed"]


def _blank_pdf() -> bytes:
    import fitz

    doc = fitz.open()
    doc.new_page()
    return doc.tobytes()


def test_a_hostile_creation_names_never_reach_the_header_as_a_path_or_a_quote(client, db):
    make_active_starter_user(db, "l47-hostile@example.com")
    csrf = _login(client, "l47-hostile@example.com")
    # the test client already percent-encodes quotes and control characters in a multipart file name, so those are
    # pinned on the function itself; the HTTP loop pins what still reaches the server verbatim
    assert documents_service.safe_download_stem('a"b\\c/d".md') == "d_" and documents_service.safe_download_stem('x"y.md') == "x_y"
    assert documents_service.safe_download_stem("tab\tnew\nline.md") == "tab_new_line" and documents_service.safe_download_stem("\x00\x1f.md") == "__"
    for name, expected in (("..\\..\\evil'; x=1.md", "evil'; x=1.md"), ("../../etc/passwd.md", "passwd.md"), (" .md", "document.md"),
                           ("é<b>o.md", "é_b_o.md")):
        doc_id = _upload(client, name, b"# H\n\nMARQUEUR_L47_HOSTILE contenu.", csrf=csrf).json()["document"]["id"]
        served = client.get(f"/api/knowledge/documents/{doc_id}/download")
        got = _disposition_filename(served)
        assert served.status_code == 200 and got == expected, (name, got)
        assert not re.search(r'[\\/"<>\x00-\x1f]', got)


def test_a_an_unknown_or_inconsistent_format_gets_a_neutral_name_without_a_made_up_extension():
    doc = SimpleNamespace(original_filename="Contrat.pdf")
    def meta(suffix, key):
        return documents_service.download_metadata(doc, SimpleNamespace(content_type_detected=suffix, storage_key=key))
    assert meta(".pdf", "knowledge/o/u/d/v/abc.pdf") == ("Contrat.pdf", PDF)
    assert meta(".docx", "knowledge/o/u/d/v/abc.docx") == ("Contrat.docx", DOCX)
    for unknown in (None, "", ".exe", "application/pdf"):
        assert meta(unknown, "knowledge/o/u/d/v/abc.bin") == ("Contrat", "application/octet-stream")
    assert meta(".pdf", "knowledge/o/u/d/v/abc.docx") == ("Contrat", "application/octet-stream"), "detected format contradicts the stored file"
    assert documents_service.download_metadata(SimpleNamespace(original_filename=""), SimpleNamespace(content_type_detected=".md", storage_key="k/a.md")) == ("document.md", "text/markdown")
    long_name = SimpleNamespace(original_filename="x" * 400 + ".md")
    assert len(documents_service.download_metadata(long_name, SimpleNamespace(content_type_detected=".md", storage_key="k/a.md"))[0]) <= 123


def test_a_no_file_is_renamed_on_disk_and_isolation_is_unchanged(client, db):
    from pathlib import Path

    from src.core import config

    make_active_starter_user(db, "l47-owner@example.com")
    make_active_starter_user(db, "l47-other@example.com")
    csrf = _login(client, "l47-owner@example.com")
    doc_id = _upload(client, "mien.pdf", _pdf_bytes("MARQUEUR_L47_PRIVE"), csrf=csrf).json()["document"]["id"]
    assert _put_version(client, csrf, doc_id, "mien.docx", _docx_bytes("MARQUEUR_L47_PRIVE v2")).status_code == 200
    before = sorted(p.name for p in (Path(config.LOCAL_STORAGE_PATH) / "knowledge").rglob("*") if p.is_file())
    client.get(f"/api/knowledge/documents/{doc_id}/download")
    assert sorted(p.name for p in (Path(config.LOCAL_STORAGE_PATH) / "knowledge").rglob("*") if p.is_file()) == before
    assert all(re.fullmatch(r"[0-9a-f]{32}\.(pdf|docx)", n) for n in before), "opaque names on disk, never the user's"
    _login(client, "l47-other@example.com")
    assert client.get(f"/api/knowledge/documents/{doc_id}/download").status_code == 404


# ---------------------------------------------------------------------------
# B — quota rule unchanged; no dead end
# ---------------------------------------------------------------------------

def test_b_a_failed_upload_keeps_its_place_until_deleted_then_a_new_document_fits(client, db, monkeypatch):
    from src.core import config

    monkeypatch.setattr(config, "KNOWLEDGE_MAX_ACTIVE_DOCUMENTS_PER_CORPUS", 3)
    make_active_starter_user(db, "l47-quota@example.com")
    csrf = _login(client, "l47-quota@example.com")
    good = [_upload(client, f"ok{i}.md", f"# {i}\n\nMARQUEUR_L47_OK{i} contenu.".encode(), csrf=csrf).json()["document"]["id"] for i in (1, 2)]
    failed = _upload(client, "vide.md", b"  \n  ", csrf=csrf)
    assert failed.status_code == 422, "the failed upload is a document..."
    failed_id = failed.json()["detail"]["document"]["id"]
    full = _upload(client, "nouveau.md", b"# N\n\nMARQUEUR_L47_NEW", csrf=csrf)
    assert full.status_code == 409 and full.json()["detail"]["error_code"] == "CORPUS_FULL", "...that occupies its place (rule unchanged)"
    assert client.get("/api/knowledge/documents").json()["documents"].__len__() == 3
    # replacing a VALID document never needs a free place, and a failed replacement never frees or takes one
    assert _put_version(client, csrf, good[0], "ok1.md", b"# 1b\n\nMARQUEUR_L47_OK1B nouvelle version.").status_code == 200
    assert _put_version(client, csrf, good[0], "ok1.md", b"   ").status_code == 422
    assert len(client.get("/api/knowledge/documents").json()["documents"]) == 3
    # deleting the failed document is what frees the place — server count, nothing automatic
    assert client.delete(f"/api/knowledge/documents/{failed_id}", headers={"X-CSRF-Token": csrf}).status_code == 200
    again = _upload(client, "nouveau.md", b"# N\n\nMARQUEUR_L47_NEW contenu.", csrf=csrf)
    assert again.status_code == 201, again.text
    assert _upload(client, "encore.md", b"# E\n\nMARQUEUR_L47_ENCORE", csrf=csrf).status_code == 409, "and the corpus is full again"
    names = sorted(d["original_filename"] for d in client.get("/api/knowledge/documents").json()["documents"])
    assert names == ["nouveau.md", "ok1.md", "ok2.md"], "nothing else was removed on the way"
