"""Lot 53 — NOT a correctness test (no assertions beyond "it saved"): exports a real revision's PDF (with a
sourced complement, accents, and a longer citation) to disk so it can be RENDERED and visually inspected —
per the ticket's explicit demand that text extraction alone does not validate layout (table breaks, accents,
long text, sources). Prefixed `zz` so it sorts last and is obviously not part of the normal targeted run;
delete-safe (writes only to the scratchpad-like docs/qa folder, never touches any tracked binary).
"""
from __future__ import annotations

from pathlib import Path

from tests.test_lot52_completion_facts_http import (
    FREQ_RESPONSE, _analyze, _complete, _make_prestataire_fact_account, _needs, _patch_llm, _search, _upload_doc, _wait,
)

OUT_DIR = Path(__file__).resolve().parents[1] / "docs" / "qa" / "lot_53_20260925" / "recette"

LONG_DOC = (
    "Notre société est spécialisée en accompagnement de la transformation numérique des collectivités "
    "territoriales et des établissements publics à caractère administratif. Nous intervenons notamment "
    "sur des missions de conseil en stratégie SI, d'urbanisation des systèmes d'information, et de conduite "
    "du changement auprès des agents. Notre fréquence de nettoyage standard est de 5 fois par semaine, "
    "ajustable selon les besoins spécifiques du client et les contraintes horaires du site d'intervention, "
    "avec une astreinte téléphonique disponible du lundi au vendredi de 7h à 20h, hors jours fériés légaux."
)


def test_export_a_real_revision_pdf_and_docx_for_visual_inspection(client, db, monkeypatch):
    csrf = _make_prestataire_fact_account(client, db, "l53-visual@example.com")
    _upload_doc(client, csrf, "conditions_generales_intervention.md", LONG_DOC)
    job = _analyze(
        client, csrf,
        "Appel d'offres. Prestation de nettoyage à Saint-Étienne-du-Rouvray, cahier des charges détaillé : "
        "3 fois par semaine minimum. Budget prévisionnel : 120 000 €. Certification qualité appréciée.",
    )
    state = _needs(client, job.id)
    need = next(n for n in state["needs"] if n["field_key"] == "frequence_nettoyage")
    response = dict(FREQ_RESPONSE)
    response["citation"] = "Notre fréquence de nettoyage standard est de 5 fois par semaine, ajustable selon les besoins spécifiques du client"
    _patch_llm(monkeypatch, response)
    proposal = _search(client, csrf, job.id, [need["id"]]).json()["results"][0]
    r = _complete(
        client, csrf, job.id, [{"need_id": need["id"], "value": proposal["value"], "source_proposal": proposal}],
        confirm_profile_write=True, expected_profile_version=state["profile_version"],
    )
    assert r.status_code == 200, r.text
    revision = _wait(r.json()["job_id"])
    assert revision.status == "done"

    pdf = client.get(f"/api/download/{revision.id}/pdf").content
    docx = client.get(f"/api/download/{revision.id}/docx").content
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "revision_visuelle.pdf").write_bytes(pdf)
    (OUT_DIR / "revision_visuelle.docx").write_bytes(docx)

    import fitz
    doc = fitz.open(stream=pdf, filetype="pdf")
    for i, page in enumerate(doc):
        pix = page.get_pixmap(dpi=150)
        pix.save(str(OUT_DIR / f"revision_visuelle_page{i+1}.png"))
    print(f"\nSaved {len(doc)} PDF page(s) as PNG + the DOCX to {OUT_DIR}")
