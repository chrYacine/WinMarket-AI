"""Lot 54 §1 — the "nouvelle analyse documentaire" banner names the REAL piece change (added/removed/
modified), never "pièces ajoutées" unconditionally (the lot 53 wording) when a re-analysis only removed a
piece with nothing added.
"""
from __future__ import annotations

from src.web.ao_dossier.service import describe_piece_change
from tests.test_lot50_dossier_admission import _confirm, _decision, _make_account, _preview, _wait
from tests.test_lot50bis_add_pieces import BUDGET_PIECE, CCTP, RC, _add_pieces_preview
from tests.test_lot47bis_ao_dossier import _file


def test_describe_piece_change_pure_cases():
    a, b = {"h1", "h2"}, {"h1", "h2", "h3"}
    assert describe_piece_change(a, b) == "pieces_ajoutees"
    assert describe_piece_change(b, a) == "pieces_retirees"
    assert describe_piece_change({"h1"}, {"h2"}) == "pieces_modifiees"
    assert describe_piece_change(a, a) == "dossier_inchange"


def test_result_page_never_claims_pieces_added_when_only_a_piece_was_removed(client, db):
    csrf = _make_account(client, db, "l54-drop@example.com")
    files = [_file("rc", "rc.txt", RC), _file("cctp", "cctp.txt", CCTP), _file("autres", "budget.txt", BUDGET_PIECE)]
    r = _preview(client, csrf, files)
    body = r.json()
    r2 = _confirm(client, csrf, body["dossier_id"], [_decision(p) for p in body["pieces"]])
    original = _wait(r2.json()["job_id"])

    original_dossier = client.get(f"/api/analyze/{original.id}/dossier").json()
    budget_piece = next(p for p in original_dossier["pieces"] if p["nom"] == "budget.txt")
    keep = [p["id"] for p in original_dossier["pieces"] if p["id"] != budget_piece["id"]]

    r3 = _add_pieces_preview(client, csrf, original.id, files=None, keep_piece_ids=keep)
    preview2 = r3.json()
    r4 = _confirm(client, csrf, preview2["dossier_id"], [_decision(p) for p in preview2["pieces"]])
    reduced = _wait(r4.json()["job_id"])

    page = client.get(f"/app/resultats/{reduced.id}")
    assert page.status_code == 200
    assert "en ajoutant des pièces" not in page.text, "no piece was added — the old unconditional wording must be gone"
    assert "en retirant des pièces" in page.text

    origin_page = client.get(f"/app/resultats/{original.id}")
    assert "pièces retirées" in origin_page.text


def test_result_page_still_says_pieces_added_when_that_is_what_actually_happened(client, db):
    csrf = _make_account(client, db, "l54-add@example.com")
    files = [_file("rc", "rc.txt", RC), _file("cctp", "cctp.txt", CCTP)]
    r = _preview(client, csrf, files)
    body = r.json()
    r2 = _confirm(client, csrf, body["dossier_id"], [_decision(p) for p in body["pieces"]])
    original = _wait(r2.json()["job_id"])

    r3 = _add_pieces_preview(client, csrf, original.id, [_file("autres", "budget.txt", BUDGET_PIECE)])
    preview2 = r3.json()
    r4 = _confirm(client, csrf, preview2["dossier_id"], [_decision(p) for p in preview2["pieces"]])
    extended = _wait(r4.json()["job_id"])

    page = client.get(f"/app/resultats/{extended.id}")
    assert page.status_code == 200
    assert "en ajoutant des pièces" in page.text
