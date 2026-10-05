"""Lot 50 bis §1 / lot 50 ter §2 — the whole-dossier scope decision at confirm time.

Lot 50 bis closed a real gap: the legacy direct-submit route already refused a dossier whose combined text
lacked market vocabulary at INTAKE, but the preview/confirm route never ran the same check — a dossier could
confirm with 200 and only fail LATER, inside the job. `dossier_service.check_admitted_scope` closed that by
running the same whole-text check at confirm time (`src/web/routes_api.py`).

Lot 50 ter §2 found that the check it shared was ITSELF wrong: the lexical "≥2 market-vocabulary-terms"
heuristic was an absolute veto, contradicting the very reason `document_moderator_agent.py` exists (a piece
without classic vocabulary can still be genuinely relevant via a shared client/site/reference/lot token, or a
real LLM reading). `test_confirm_refuses_a_dossier_whose_final_admitted_text_is_out_of_scope` below used to
assert a 422 for a piece that happens to carry exactly one market-vocabulary word ("budget") — which the
per-piece moderator ALSO independently judges 'lie' (it shares that same vocabulary check, just with a lower
bar). Under the corrected contract (`src/web/ao_dossier/scope.py`), a piece the moderator judges relevant is
never re-vetoed by the lexical fallback — so this exact case is now CORRECTLY ADMITTED, and the test is
renamed/rewritten accordingly (ticket, verbatim: "remplace cette attente par le nouveau contrat explicite,
sans supprimer les assertions de sécurité"). A genuinely signal-less dossier (see
`tests/test_lot50ter_scope_relevance.py` for the fuller suite) still gets refused.
"""
from __future__ import annotations

from tests.test_lot47bis_ao_dossier import _file
from tests.test_lot50_dossier_admission import _confirm, _decision, _make_account, _preview, _wait

RC = b"Reglement de consultation. Appel d'offres - Prestations de nettoyage. Site de Lyon."
CCTP = b"Cahier des charges techniques. 3 fois par semaine."
# Exactly ONE tender-vocabulary term ("budget") — the per-piece moderator ALREADY judges this 'lie' on that
# same vocabulary signal (its own heuristic's third branch), so lot 50 ter's corrected scope decision must
# ADMIT it: the lexical gate is a fallback for when NO piece is otherwise judged relevant, never a veto
# against one that already is.
SPARSE_PIECE = b"Le budget indique dans ce fichier reste a confirmer avant la fin du mois par les equipes concernees."


def test_confirm_admits_a_piece_the_moderator_judges_relevant_even_if_lexically_sparse(client, db):
    csrf = _make_account(client, db, "l50bis-scope1@example.com")
    files = [_file("rc", "rc.txt", RC), _file("autres", "sparse.txt", SPARSE_PIECE)]
    r = _preview(client, csrf, files)
    assert r.status_code == 200, r.text
    body = r.json()

    rc_piece = next(p for p in body["pieces"] if p["nom"] == "rc.txt")
    sparse_piece = next(p for p in body["pieces"] if p["nom"] == "sparse.txt")
    assert sparse_piece["pertinence"] == "lie", sparse_piece  # the moderator's OWN vocabulary signal, not shared tokens
    decisions = [
        _decision(rc_piece, include=False, reason="Test : exclue volontairement."),
        _decision(sparse_piece, include=True),
    ]
    r2 = _confirm(client, csrf, body["dossier_id"], decisions)
    assert r2.status_code == 200, r2.text
    job = _wait(r2.json()["job_id"])
    assert job.status == "done", "a piece the moderator judged relevant must not be re-vetoed by the lexical fallback"


def test_confirm_still_accepts_a_dossier_whose_admitted_text_has_market_vocabulary(client, db):
    """Same shape of request, but the retained pieces together carry enough market vocabulary — the new
    gate must not become an over-eager regression on ordinary, legitimate dossiers."""
    csrf = _make_account(client, db, "l50bis-scope2@example.com")
    files = [_file("rc", "rc.txt", RC), _file("cctp", "cctp.txt", CCTP)]
    r = _preview(client, csrf, files)
    assert r.status_code == 200, r.text
    body = r.json()
    r2 = _confirm(client, csrf, body["dossier_id"], [_decision(p) for p in body["pieces"]])
    assert r2.status_code == 200, r2.text
    job = _wait(r2.json()["job_id"])
    assert job.status == "done"
