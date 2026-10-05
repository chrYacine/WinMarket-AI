"""Lot 54 §1 — exports a richer revision (several criteria, a sourced AND a declared complement, a long
citation, accented text) as DOCX + PDF for REAL visual inspection (Word via COM, see
docs/qa/lot_54_20260926/recette/docx_to_pdf.py) — not a correctness test, no assertions beyond "it saved".
"""
from __future__ import annotations

from pathlib import Path

from tests.test_lot44_criteria_contract import _capacity, _login, _put, crit
from tests.conftest import make_active_starter_user
from tests.test_lot52_completion_facts_http import _patch_llm, _search, _upload_doc

OUT_DIR = Path(__file__).resolve().parents[1] / "docs" / "qa" / "lot_54_20260926" / "recette"

PROFILE = {
    "raison_sociale": "Nettoyage et Accompagnement Numérique des Collectivités Territoriales (test lot 54)",
    "competences": [], "certifications": [],
    "business_facts": {
        "zone_intervention": {"key": "zone_intervention", "label": "Zone d'intervention", "type": "list", "unit": None, "value": ["Lyon"]},
        "frequence_nettoyage": {"key": "frequence_nettoyage", "label": "Fréquence de nettoyage", "type": "number", "unit": "par_semaine", "value": 5},
    },
}
BUDGET_TIERS = {"source": "budget", "fact_key": None, "tiers": [{"at_least": 100000, "score": 100}], "below_score": 0, "zero_score": None, "minimum_blocking": None}

LONG_DOC = (
    "Notre société intervient depuis plus de quinze ans auprès des collectivités territoriales et des "
    "établissements publics à caractère administratif, sur des missions d'entretien, de maintenance générale "
    "et d'accompagnement à la transformation numérique des services. Notre fréquence de nettoyage standard "
    "pour un site administratif de taille moyenne est de 5 fois par semaine, ajustable selon les besoins "
    "spécifiques du site d'intervention et les contraintes horaires du client, avec une astreinte "
    "téléphonique disponible du lundi au vendredi de 7h à 20h, hors jours fériés légaux, et une équipe "
    "encadrante présente sur site au moins une fois par quinzaine pour le contrôle qualité."
)


def test_export_a_richer_revision_docx_and_pdf_for_word_based_visual_inspection(client, db, monkeypatch):
    make_active_starter_user(db, "l54-visual@example.com", scoring=False)
    csrf = _login(client, "l54-visual@example.com")
    _capacity(client, csrf)
    assert _put(client, csrf, "/api/scoring-config/profile", PROFILE).status_code == 200
    criteria = [
        crit("zone", "list_coverage", {"fact_key": "zone_intervention", "pass_score": 100, "fail_score": 0}, 30, label="Sites couverts", blocking=True),
        crit("frequence", "numeric_threshold", {"fact_key": "frequence_nettoyage", "comparison": "provider_gte_ao", "pass_score": 100, "fail_score": 20}, 20, label="Fréquence compatible"),
        crit("budget", "numeric_tiers", BUDGET_TIERS, 50, label="Budget estimé"),
    ]
    assert _put(client, csrf, "/api/scoring-config/policy", {"criteria": criteria, "threshold_go": 80, "threshold_sous_reserve": 55}).status_code == 200
    assert client.post("/api/scoring-config/policy/activate", json={"expected_active_version": None}, headers={"X-CSRF-Token": csrf}).status_code == 200
    cleared = dict(PROFILE)
    cleared["business_facts"] = {**PROFILE["business_facts"], "frequence_nettoyage": {**PROFILE["business_facts"]["frequence_nettoyage"], "value": None}}
    assert _put(client, csrf, "/api/scoring-config/profile", cleared).status_code == 200

    _upload_doc(client, csrf, "conditions_generales_intervention_Saint-Étienne.md", LONG_DOC)

    ao_text = (
        "Appel d'offres. Prestation d'entretien et de nettoyage des locaux administratifs de la Métropole de "
        "Saint-Étienne-du-Rouvray, cahier des charges détaillé, site de Lyon, 3 fois par semaine minimum. "
        "Budget prévisionnel non chiffré dans le présent extrait."
    )
    r = client.post("/api/analyze", data={"mode": "paste", "text": ao_text}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200, r.text
    from src.web import jobs
    import time
    def wait(job_id):
        for _ in range(300):
            job = jobs.get_job(job_id)
            if job is not None and job.status != "running":
                return job
            time.sleep(0.05)
        raise AssertionError("job did not finish")
    job = wait(r.json()["job_id"])
    assert job.result.decision == "INCOMPLET"

    state = client.get(f"/api/analyze/{job.id}/completion").json()
    freq_need = next(n for n in state["needs"] if n["field_key"] == "frequence_nettoyage")
    budget_need = next(n for n in state["needs"] if n["field_key"] == "budget_estime")

    response = {"found": True, "passage_number": 1, "value": 5, "unit": "par semaine",
                "citation": "Notre fréquence de nettoyage standard pour un site administratif de taille moyenne est de 5 fois par semaine, ajustable selon les besoins spécifiques du site d'intervention et les contraintes horaires du client",
                "reason": "valeur explicite trouvée, avec contexte détaillé"}
    _patch_llm(monkeypatch, response)
    proposal = _search(client, csrf, job.id, [freq_need["id"]]).json()["results"][0]

    r2 = client.post(
        f"/api/analyze/{job.id}/complete",
        json={
            "items": [
                {"need_id": freq_need["id"], "value": proposal["value"], "source_proposal": proposal},
                {"need_id": budget_need["id"], "value": 150000},
            ],
            "confirm_profile_write": True, "expected_profile_version": state["profile_version"],
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert r2.status_code == 200, r2.text
    revision = wait(r2.json()["job_id"])
    assert revision.status == "done" and revision.result.decision == "GO"

    pdf = client.get(f"/api/download/{revision.id}/pdf").content
    docx = client.get(f"/api/download/{revision.id}/docx").content
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "revision_riche.pdf").write_bytes(pdf)
    (OUT_DIR / "revision_riche.docx").write_bytes(docx)
    print(f"\nSaved revision_riche.pdf/.docx to {OUT_DIR}")
