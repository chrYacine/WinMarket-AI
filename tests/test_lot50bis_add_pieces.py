"""Lot 50 bis §3 — "Ajouter les pièces restantes": a NEW documentary re-analysis of an existing dossier-based
job, distinct from the lot-49-bis declarative revision. Real routes/engine; LLM disabled in this environment.
"""
from __future__ import annotations

import json

from src.web import jobs
from tests.test_lot44_criteria_contract import _put
from tests.test_lot50_dossier_admission import PROFILE, _confirm, _decision, _make_account, _preview, _wait
from tests.test_lot47bis_ao_dossier import _file
from tests.conftest import make_active_starter_user


def _add_pieces_preview(client, csrf, job_id, files, keep_piece_ids=None):
    data = {}
    if keep_piece_ids is not None:
        data["keep_piece_ids"] = json.dumps(keep_piece_ids)
    return client.post(f"/api/analyze/{job_id}/add-pieces/preview", files=files or None, data=data, headers={"X-CSRF-Token": csrf})


RC = "Règlement de consultation. Appel d'offres - Prestations de nettoyage. Site de Lyon, projet Gerland-2026. Travail de nuit : non.".encode()
CCTP = "Cahier des charges techniques. 3 fois par semaine.".encode()
BUDGET_PIECE = "Note de budget pour le projet Gerland-2026 : 150 000 € retenus, budget validé.".encode()


def test_adding_a_new_piece_produces_a_fresh_linked_analysis_original_untouched(client, db):
    csrf = _make_account(client, db, "l50bis-add1@example.com")
    files = [_file("rc", "rc.txt", RC), _file("cctp", "cctp.txt", CCTP)]
    r = _preview(client, csrf, files)
    assert r.status_code == 200, r.text
    body = r.json()
    r2 = _confirm(client, csrf, body["dossier_id"], [_decision(p) for p in body["pieces"]])
    assert r2.status_code == 200, r2.text
    original = _wait(r2.json()["job_id"])
    assert original.result.decision == "INCOMPLET", original.result.scoring_missing  # no budget stated anywhere yet
    original_pdf = client.get(f"/api/download/{original.id}/pdf").content

    r3 = _add_pieces_preview(client, csrf, original.id, [_file("autres", "budget.txt", BUDGET_PIECE)])
    assert r3.status_code == 200, r3.text
    preview2 = r3.json()
    assert preview2["origin_job_id"] == original.id
    names = {p["nom"] for p in preview2["pieces"]}
    assert {"rc.txt", "cctp.txt", "budget.txt"} <= names, preview2["pieces"]

    r4 = _confirm(client, csrf, preview2["dossier_id"], [_decision(p) for p in preview2["pieces"]])
    assert r4.status_code == 200, r4.text
    extended = _wait(r4.json()["job_id"])
    assert extended.origin_job_id == original.id
    assert extended.parent_job_id is None, "this is a documentary re-analysis, never a declarative revision"
    assert extended.result.decision == "GO", extended.result.scoring_missing

    # the ORIGINAL job/result/deliverables are byte-for-byte untouched
    reread = jobs.get_job(original.id)
    assert reread.result.decision == "INCOMPLET"
    assert client.get(f"/api/download/{original.id}/pdf").content == original_pdf
    assert extended.files.get("pdf") != original.files.get("pdf")


def test_dropping_an_original_piece_without_adding_new_files_still_works(client, db):
    csrf = _make_account(client, db, "l50bis-add2@example.com")
    files = [_file("rc", "rc.txt", RC), _file("cctp", "cctp.txt", CCTP), _file("autres", "budget.txt", BUDGET_PIECE)]
    r = _preview(client, csrf, files)
    body = r.json()
    r2 = _confirm(client, csrf, body["dossier_id"], [_decision(p) for p in body["pieces"]])
    original = _wait(r2.json()["job_id"])
    assert original.result.decision == "GO"

    original_dossier = client.get(f"/api/analyze/{original.id}/dossier").json()
    budget_piece = next(p for p in original_dossier["pieces"] if p["nom"] == "budget.txt")
    keep = [p["id"] for p in original_dossier["pieces"] if p["id"] != budget_piece["id"]]

    r3 = _add_pieces_preview(client, csrf, original.id, files=None, keep_piece_ids=keep)
    assert r3.status_code == 200, r3.text
    preview2 = r3.json()
    names = {p["nom"] for p in preview2["pieces"]}
    assert "budget.txt" not in names, "explicitly dropped original piece must not reappear"

    r4 = _confirm(client, csrf, preview2["dossier_id"], [_decision(p) for p in preview2["pieces"]])
    assert r4.status_code == 200, r4.text
    reduced = _wait(r4.json()["job_id"])
    assert reduced.result.decision == "INCOMPLET", "dropping the budget piece must genuinely remove the budget fact"


def test_a_modified_original_file_is_never_silently_reconstructed(client, db, monkeypatch):
    csrf = _make_account(client, db, "l50bis-add3@example.com")
    files = [_file("rc", "rc.txt", RC), _file("cctp", "cctp.txt", CCTP), _file("autres", "budget.txt", BUDGET_PIECE)]
    r = _preview(client, csrf, files)
    body = r.json()
    r2 = _confirm(client, csrf, body["dossier_id"], [_decision(p) for p in body["pieces"]])
    original = _wait(r2.json()["job_id"])

    from src.web.ao_dossier import storage as dossier_storage
    original_dossier = client.get(f"/api/analyze/{original.id}/dossier").json()
    budget_piece = next(p for p in original_dossier["pieces"] if p["nom"] == "budget.txt")
    # tamper with the stored file directly, simulating external corruption/modification
    from src.web.database.repositories import ao_dossiers as dossiers_repo
    from src.web.database.session import session_scope
    with session_scope() as sdb:
        dossier_row = dossiers_repo.get_by_job(sdb, job_id=original.id, organization_id=original.organization_id, user_id=original.user_id)
        piece_row = next(p for p in dossier_row.pieces if p.display_name == "budget.txt")
        path = dossier_storage.resolve(piece_row.storage_key)
        path.write_bytes(b"contenu completement different")

    r3 = _add_pieces_preview(client, csrf, original.id, [_file("autres", "extra.txt", b"Une piece supplementaire quelconque pour ce test.")])
    assert r3.status_code == 200, r3.text
    preview2 = r3.json()
    assert any(rej["error_code"] == "ORIGINAL_PIECE_MODIFIED" for rej in preview2["rejetees"]), preview2["rejetees"]
    names = {p["nom"] for p in preview2["pieces"]}
    assert "budget.txt" not in names, "a modified original piece must never be silently carried over"


def test_add_pieces_requires_a_dossier_based_analysis(client, db):
    csrf = _make_account(client, db, "l50bis-add4@example.com")
    r = client.post("/api/analyze", data={"mode": "paste", "text": "Appel d'offres de nettoyage à Lyon, cahier des charges habituel."}, headers={"X-CSRF-Token": csrf})
    job_id = r.json()["job_id"]
    _wait(job_id)
    r2 = _add_pieces_preview(client, csrf, job_id, [_file("autres", "x.txt", b"Un contenu quelconque pour ce test.")])
    assert r2.status_code == 404 and r2.json()["detail"]["error_code"] == "NOT_A_DOSSIER_ANALYSIS"


def test_another_account_cannot_add_pieces_to_a_foreign_job(client, db, monkeypatch):
    from src.rag import private_rag_manager
    monkeypatch.setattr(private_rag_manager, "search", lambda db_, **kw: [])
    csrf_a = _make_account(client, db, "l50bis-add5a@example.com")
    files = [_file("rc", "rc.txt", RC)]
    r = _preview(client, csrf_a, files)
    body = r.json()
    r2 = _confirm(client, csrf_a, body["dossier_id"], [_decision(p) for p in body["pieces"]])
    job = _wait(r2.json()["job_id"])

    csrf_b = _make_account(client, db, "l50bis-add5b@example.com")
    r3 = _add_pieces_preview(client, csrf_b, job.id, [_file("autres", "x.txt", b"Un contenu quelconque appartenant a un autre compte.")])
    assert r3.status_code == 404


def test_add_pieces_preview_requires_csrf(client, db):
    csrf = _make_account(client, db, "l50bis-add6@example.com")
    files = [_file("rc", "rc.txt", RC)]
    r = _preview(client, csrf, files)
    body = r.json()
    r2 = _confirm(client, csrf, body["dossier_id"], [_decision(p) for p in body["pieces"]])
    job = _wait(r2.json()["job_id"])
    r3 = client.post(f"/api/analyze/{job.id}/add-pieces/preview", files=[_file("autres", "x.txt", b"Contenu quelconque.")])
    assert r3.status_code in (401, 403)
