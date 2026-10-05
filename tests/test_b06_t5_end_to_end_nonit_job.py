"""B06-T5 / B05-T3 — the REAL end-to-end path (POST /api/analyze -> real
job -> real AOExtractor.extract -> real ScoringEngine.score, via
jobs.py::_run_analysis) for two synthetic, explicitly non-IT accounts
(cleaning, construction) — never an AOContext constructed by hand to
"prove" extraction, per the ticket's own explicit instruction.

LLM disabled in this environment (no real API key) — every extraction here
exercises the DETERMINISTIC LOCAL fallback path
(src/agents/ao_extractor.py::_extract_fact_locally), not the LLM path
(already covered in isolation by tests/test_b05_t3_facts_extraction.py's
fake-provider tests). A real LLM-backed extraction run is a separate,
explicitly authorized recette this ticket defers (real API key/network
required, out of scope here).
"""
from __future__ import annotations

import re
import time

from src.web import jobs
from tests.conftest import make_active_starter_user


def _csrf_from(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


def _login(client, email, password="Sup3rSecret!"):
    r = client.get("/login")
    csrf = _csrf_from(r.text)
    client.post("/login", data={"email": email, "password": password, "next": "/app", "csrf_token": csrf})
    return csrf


CLEANING_WEIGHTS = {
    "Adequation expertise": 0, "References similaires": 10, "Disponibilite equipe": 15,
    "Rentabilite estimee": 15, "Faisabilite delai": 10, "Certifications requises": 10,
    "Complexite technique": 0, "Connaissance secteur": 10, "Potentiel commercial": 5,
    "Risque contractuel": 5, "Solidite client": 5, "Valeur strategique": 0,
}  # sums to 85 — 15 left for the two custom criteria below

PERMISSIVE_BUSINESS_RULES = {
    "budget_minimum_eur": 0, "max_charge_pct": 100, "max_unmastered_technologies": 999, "certification_penalty_score": 20,
}


def _configure_cleaning_account(client, csrf, *, zone_covered: list[str]):
    client.post("/api/capacity", json={
        "charge_globale_pct": 40, "nombre_projets_en_cours": 1,
        "projets_en_cours": ["Chantier test"], "capacites_par_pole": {"Nettoyage": 40},
    }, headers={"X-CSRF-Token": csrf})
    r = client.put("/api/scoring-config/profile", json={
        "raison_sociale": "Nettoyage Pro Synthétique", "effectif": "10-50", "competences": [], "certifications": [],
        "business_facts": {
            "zone_intervention": {"key": "zone_intervention", "label": "Zone d'intervention", "type": "list", "unit": None, "value": zone_covered},
            "frequence_nettoyage": {"key": "frequence_nettoyage", "label": "Fréquence de nettoyage", "type": "number", "unit": "par_semaine", "value": 3},
        },
    }, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200, r.text
    r = client.put("/api/scoring-config/policy", json={
        "weights": CLEANING_WEIGHTS, "threshold_go": 80, "threshold_sous_reserve": 55,
        "business_rules": PERMISSIVE_BUSINESS_RULES,
        "custom_criteria": [
            {
                "id": "zone_couverte", "label": "Zone d'intervention couverte", "fact_key": "zone_intervention",
                "operator": "list_coverage", "weight": 10, "blocking": True, "pass_score": 100, "fail_score": 0,
            },
            {
                "id": "frequence_ok", "label": "Fréquence de nettoyage compatible", "fact_key": "frequence_nettoyage",
                "operator": "numeric_threshold", "comparison": "provider_gte_ao",
                "weight": 5, "blocking": False, "pass_score": 100, "fail_score": 30,
            },
        ],
    }, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200, r.text
    r = client.post("/api/scoring-config/policy/validate", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200 and r.json()["valid"], r.text
    r = client.post("/api/scoring-config/policy/activate", json={"expected_active_version": None}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200, r.text


def _run_job_and_wait(client, csrf, *, text: str) -> str:
    r = client.post("/api/analyze", data={"mode": "paste", "text": text}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    for _ in range(100):
        job = jobs.get_job(job_id)
        if job.status != "running":
            break
        time.sleep(0.1)
    return job_id


def _criterion(result, label):
    match = next((c for c in result.criteres if c.nom == label), None)
    assert match is not None, f"critère {label!r} introuvable parmi {[c.nom for c in result.criteres]}"
    return match


def test_cleaning_account_zone_covered_via_real_job(client, db):
    """The AO text genuinely mentions a zone the account declared it
    covers — the REAL extractor (local fallback, LLM disabled here) must
    recognize it and the REAL engine must score it, through the real HTTP
    -> job -> extraction -> scoring path, no hand-built AOContext."""
    make_active_starter_user(db, "cleaningzoneok@example.com", scoring=False)
    csrf = _login(client, "cleaningzoneok@example.com")
    _configure_cleaning_account(client, csrf, zone_covered=["Lyon", "Villeurbanne"])

    job_id = _run_job_and_wait(client, csrf, text=(
        "Marché de prestation de nettoyage de locaux. Acheteur : Mairie de Lyon. "
        "Budget : 80000 euros. Intervention sur le site de Lyon, 3 fois par semaine."
    ))
    job = jobs.get_job(job_id)
    assert job.status == "done", f"expected a completed job, got status={job.status!r} error={job.error!r}"

    zone = _criterion(job.result, "Zone d'intervention couverte")
    freq = _criterion(job.result, "Fréquence de nettoyage compatible")
    assert zone.score == 100.0, "the real extractor must have recognized 'Lyon' via its local fallback"
    assert freq.score == 100.0
    assert not job.result.criteres_bloquants


def test_cleaning_account_ao_mentions_an_unrecognized_zone_is_incomplete_never_fabricated(client, db):
    """The AO requires a zone the account does NOT cover (Marseille), but
    the deterministic LOCAL fallback (no LLM key here) can only ever
    recognize a zone that's already in the PROVIDER's own declared
    vocabulary (list_coverage's only safe source of truth without an
    LLM/gazetteer — see src/agents/ao_extractor.py::_extract_fact_locally).
    It genuinely cannot tell "Marseille" apart from "nothing mentioned at
    all" on its own. The honest outcome here is therefore INCOMPLET
    (extraction limitation, `scoring_missing` names the criterion),
    NEVER a fabricated NO-GO pretending the extractor determined a real
    mismatch it did not actually observe — ticket: 'aucun résultat
    favorable fabriqué', which cuts both ways: no fabricated rejection
    either."""
    make_active_starter_user(db, "cleaningzoneunknown@example.com", scoring=False)
    csrf = _login(client, "cleaningzoneunknown@example.com")
    _configure_cleaning_account(client, csrf, zone_covered=["Lyon", "Villeurbanne"])

    job_id = _run_job_and_wait(client, csrf, text=(
        "Marché de prestation de nettoyage de locaux. Acheteur : Mairie de Marseille. "
        "Budget : 80000 euros. Intervention sur le site de Marseille, 3 fois par semaine."
    ))
    job = jobs.get_job(job_id)
    assert job.status == "done"
    assert job.result.decision == "INCOMPLET"
    assert "custom:zone_couverte" in job.result.scoring_missing
    zone = _criterion(job.result, "Zone d'intervention couverte")
    assert zone.score == 0.0, "an unresolvable criterion contributes 0, never a fabricated favorable score"


def test_cleaning_account_real_frequency_mismatch_via_local_fallback_is_a_confirmed_no_go(client, db):
    """Unlike the list-type zone fact above, a NUMBER genuinely found in
    the AO text (any number near the unit, not restricted to a known
    vocabulary) lets the deterministic local fallback recognize a REAL,
    determined value — proving the full extraction -> scoring path can
    reach a real, non-fabricated blocking failure without an LLM too."""
    make_active_starter_user(db, "cleaningfreqbad@example.com", scoring=False)
    csrf = _login(client, "cleaningfreqbad@example.com")
    # Re-declare frequence_nettoyage as the BLOCKING criterion for this
    # test (zone stays non-blocking) so a real, recognized mismatch on a
    # NUMBER can be observed producing a confirmed NO-GO end to end.
    client.post("/api/capacity", json={
        "charge_globale_pct": 40, "nombre_projets_en_cours": 1,
        "projets_en_cours": ["Chantier test"], "capacites_par_pole": {"Nettoyage": 40},
    }, headers={"X-CSRF-Token": csrf})
    client.put("/api/scoring-config/profile", json={
        "raison_sociale": "Nettoyage Pro Synthétique 2", "effectif": "10-50", "competences": [], "certifications": [],
        "business_facts": {
            "zone_intervention": {"key": "zone_intervention", "label": "Zone d'intervention", "type": "list", "unit": None, "value": ["Lyon"]},
            "frequence_nettoyage": {"key": "frequence_nettoyage", "label": "Fréquence de nettoyage", "type": "number", "unit": "par_semaine", "value": 3},
        },
    }, headers={"X-CSRF-Token": csrf})
    client.put("/api/scoring-config/policy", json={
        "weights": CLEANING_WEIGHTS, "threshold_go": 80, "threshold_sous_reserve": 55,
        "business_rules": PERMISSIVE_BUSINESS_RULES,
        "custom_criteria": [
            {
                "id": "zone_couverte", "label": "Zone d'intervention couverte", "fact_key": "zone_intervention",
                "operator": "list_coverage", "weight": 10, "blocking": False, "pass_score": 100, "fail_score": 50,
            },
            {
                "id": "frequence_ok", "label": "Fréquence de nettoyage compatible", "fact_key": "frequence_nettoyage",
                "operator": "numeric_threshold", "comparison": "provider_gte_ao",
                "weight": 5, "blocking": True, "pass_score": 100, "fail_score": 0,
            },
        ],
    }, headers={"X-CSRF-Token": csrf})
    r = client.post("/api/scoring-config/policy/activate", json={"expected_active_version": None}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200, r.text

    job_id = _run_job_and_wait(client, csrf, text=(
        "Marché de prestation de nettoyage de locaux. Acheteur : Mairie de Lyon. "
        "Budget : 80000 euros. Intervention sur le site de Lyon, 10 fois par semaine."
    ))
    job = jobs.get_job(job_id)
    assert job.status == "done"
    freq = _criterion(job.result, "Fréquence de nettoyage compatible")
    assert freq.score == 0.0, "the real extractor must have recognized the number 10 near 'par semaine'"
    assert job.result.decision == "NO-GO"
    assert any("Fréquence de nettoyage compatible" in b for b in job.result.criteres_bloquants)


def test_second_it_account_is_completely_unaffected_by_the_cleaning_accounts_configuration(client, db):
    """Ticket: 'sans effet sur l'autre compte' — a normal, fully IT
    account (zero custom criteria) must see none of the cleaning
    account's custom criteria/facts leak in, and must still reach a
    normal GO/NO-GO/RESERVE decision through the SAME real job path.

    Uses scoring=False + an explicit, fully permissive business_rules
    configuration rather than the shared scoring=True fixture: that
    fixture's synthetic policy (tests/conftest.py::
    configure_synthetic_scoring_for_owner) leaves business_rules at their
    default {} (never configured), which — per the real B06-T4 contract —
    makes every real job through it legitimately resolve to INCOMPLET,
    unrelated to this test's own point."""
    make_active_starter_user(db, "itaccountunaffected@example.com", scoring=False)
    csrf = _login(client, "itaccountunaffected@example.com")
    client.post("/api/capacity", json={
        "charge_globale_pct": 40, "nombre_projets_en_cours": 1,
        "projets_en_cours": ["Projet test"], "capacites_par_pole": {"Software Engineering": 40},
    }, headers={"X-CSRF-Token": csrf})
    client.put("/api/scoring-config/profile", json={
        "raison_sociale": "ESN test", "effectif": "10-50", "competences": ["python", "react", "aws"], "certifications": [],
    }, headers={"X-CSRF-Token": csrf})
    client.put("/api/scoring-config/policy", json={
        "weights": {
            "Adequation expertise": 20, "References similaires": 15, "Disponibilite equipe": 10,
            "Rentabilite estimee": 10, "Faisabilite delai": 10, "Certifications requises": 10,
            "Complexite technique": 5, "Connaissance secteur": 5, "Potentiel commercial": 5,
            "Risque contractuel": 5, "Solidite client": 3, "Valeur strategique": 2,
        },
        "threshold_go": 88, "threshold_sous_reserve": 60,
        "business_rules": PERMISSIVE_BUSINESS_RULES,
    }, headers={"X-CSRF-Token": csrf})
    r = client.post("/api/scoring-config/policy/activate", json={"expected_active_version": None}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200, r.text
    job_id = _run_job_and_wait(client, csrf, text=(
        "Marché de développement logiciel. Acheteur : Ville de Test. Budget : 250000 euros. "
        "Technologies : Python, React, AWS."
    ))
    job = jobs.get_job(job_id)
    assert job.status == "done"
    assert job.result.decision in ("GO", "GO SOUS RESERVE", "NO-GO")
    assert not any("Zone d'intervention" in c.nom or "nettoyage" in c.nom.lower() for c in job.result.criteres)
    assert job.result.scoring_completeness == "complete"
