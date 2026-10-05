"""Campagne 36 — État des lieux après le lot 37, puis suites données par le
lot fonctionnel qui a suivi :

1. Historique/export pour le MÊME utilisateur appartenant à DEUX
   organisations (pas seulement deux user_id différents) — reste un
   CONSTAT tel quel (comportement actuel : agrégation par user_id, sans
   séparation par organisation active). B22-T2 doit adapter précisément ce
   scénario (sélection A → uniquement A, sélection B → uniquement B) une
   fois le filtrage par organisation livré ; non fait dans ce fichier.
2. Comportement réel d'un prompt manquant à la FRONTIÈRE DU JOB (via le
   vrai chemin src/web/jobs.py::_run_analysis) — CONVERTI en régression
   positive par B16-T2 (voir PromptLoadError dans
   src/core/prompt_loader.py et son traitement dans
   src/agents/scoring_engine.py::enrich_with_llm) : un prompt manquant est
   maintenant un enrichment_reason="prompt_missing" avec le score déjà
   calculé conservé, jamais une perte de résultat ni un code
   "scoring_failed" indiscernable d'un vrai bug de scoring.
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from src.web.database.repositories import analyses as analyses_repo
from src.web.database.repositories import memberships as memberships_repo
from src.web.database.repositories import organizations as organizations_repo
from src.web.services import history_service
from tests.conftest import default_org_id, make_active_starter_user


def _csrf_from(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


def _login(client, email, password="Sup3rSecret!"):
    r = client.get("/login")
    csrf = _csrf_from(r.text)
    client.post("/login", data={"email": email, "password": password, "next": "/app", "csrf_token": csrf})
    return csrf


def test_same_user_two_organizations_history_is_scoped_to_the_selected_organization(client, db):
    """B22-T2 — régression positive (ex-constat de la campagne 36).

    Un même utilisateur (même user_id) est membre actif de DEUX
    organisations et a une analyse sous chacune. Le constat original
    (history_service filtrait UNIQUEMENT par user_id, jamais par
    organization_id, agrégeant les deux espaces) est désormais corrigé :
    analyses_repo.list_for_user/list_for_user_page/count_for_user/
    decision_counts_for_user filtrent par (user_id ET organization_id), et
    /api/history résout cette organisation via get_access_context (B02) —
    jamais un id transmis par le client sans vérification.

    Trois cas exigés par le ticket : sélection A → uniquement A ; sélection
    B → uniquement B ; sélection absente/ambiguë ou non autorisée → refus
    sûr (jamais un repli global agrégeant tout)."""
    user = make_active_starter_user(db, "twospaces@example.com", scoring=False)
    org_a = default_org_id(db, user)
    org_b = organizations_repo.create_organization(db, name="Deuxième organisation de twospaces")
    memberships_repo.create_membership(db, user_id=user.id, organization_id=org_b.id, role="organization_admin")
    db.commit()

    analyses_repo.create_analysis(
        db, user_id=user.id, organization_id=org_a, job_id="space-a-job",
        title="Analyse espace A", result_data={"ao": {}, "result": {}},
    )
    analyses_repo.create_analysis(
        db, user_id=user.id, organization_id=org_b.id, job_id="space-b-job",
        title="Analyse espace B", result_data={"ao": {}, "result": {}},
    )
    db.commit()

    _login(client, "twospaces@example.com")

    # Sélection absente, deux organisations actives → refus sûr (409),
    # jamais un repli implicite qui agrégerait les deux espaces.
    r_ambiguous = client.get("/api/history?page=1&page_size=50")
    assert r_ambiguous.status_code == 409

    # Sélection A → uniquement l'analyse de l'espace A.
    r_a = client.get(f"/api/history?page=1&page_size=50&organization_id={org_a}")
    assert r_a.status_code == 200
    titles_a = {item["titre"] for item in r_a.json()["items"]}
    assert titles_a == {"Analyse espace A"}

    # Sélection B → uniquement l'analyse de l'espace B.
    r_b = client.get(f"/api/history?page=1&page_size=50&organization_id={org_b.id}")
    assert r_b.status_code == 200
    titles_b = {item["titre"] for item in r_b.json()["items"]}
    assert titles_b == {"Analyse espace B"}

    stats_a = history_service.sidebar_stats(db, user.id, org_a)
    assert stats_a["total"] == 1, "les statistiques doivent être scopées à l'organisation sélectionnée, jamais agrégées"
    stats_b = history_service.sidebar_stats(db, user.id, org_b.id)
    assert stats_b["total"] == 1

    # Sélection d'une organisation non autorisée pour ce compte → refus sûr.
    unrelated_org = organizations_repo.create_organization(db, name="Organisation sans lien avec twospaces")
    db.commit()
    r_unauthorized = client.get(f"/api/history?page=1&page_size=50&organization_id={unrelated_org.id}")
    assert r_unauthorized.status_code == 403

    # Appartenance révoquée pour org_b → cette organisation n'est plus
    # sélectionnable, jamais un repli vers "toutes les organisations".
    membership_b = memberships_repo.get_active(db, user_id=user.id, organization_id=org_b.id)
    memberships_repo.revoke(db, membership_b)
    db.commit()
    r_revoked = client.get(f"/api/history?page=1&page_size=50&organization_id={org_b.id}")
    assert r_revoked.status_code == 403
    # org_a reste accessible (seule organisation active restante) — sans
    # préciser organization_id, la résolution automatique s'applique.
    r_a_still_ok = client.get("/api/history?page=1&page_size=50")
    assert r_a_still_ok.status_code == 200
    assert {item["titre"] for item in r_a_still_ok.json()["items"]} == {"Analyse espace A"}

    # Un AUTRE utilisateur, même s'il appartient à org_a, ne doit voir NI
    # l'une ni l'autre — isolation par PROPRIÉTAIRE, jamais par organisation
    # seule (non régressé par ce correctif).
    intruder = make_active_starter_user(db, "twospacesintruder@example.com", scoring=False)
    memberships_repo.create_membership(db, user_id=intruder.id, organization_id=org_a, role="viewer")
    db.commit()
    r_intruder = history_service.list_for_user_page(db, intruder.id, org_a, page=1, page_size=50)
    assert r_intruder[0] == [], "un collègue de la même organisation ne doit jamais voir les analyses privées d'autrui"


def test_missing_prompt_file_at_the_real_job_boundary_is_a_labeled_degraded_success(client, db, monkeypatch, tmp_path):
    """B16-T2 — régression positive (ex-constat de la campagne 36).

    Le prompt d'enrichissement (src/agents/prompts/scoring_enrichment_
    user.txt) était chargé par ScoringEngine.enrich_with_llm HORS de tout
    bloc try/except : un fichier manquant faisait perdre le score déjà
    calculé (job.result jamais assigné) et remontait error_code=
    "scoring_failed" côté jobs.py — indiscernable d'un vrai bug de scoring.

    Correctif (src/core/prompt_loader.py::PromptLoadError, src/agents/
    scoring_engine.py) : le chargement des DEUX prompts (système et
    utilisateur) est maintenant protégé ; un PromptLoadError est traduit en
    enrichment_status="failed"/enrichment_reason="prompt_missing" et le
    résultat algorithmique déjà calculé est conservé tel quel, à travers le
    VRAI chemin de job (jobs.py::_run_analysis), jamais un appel unitaire
    isolé — le job doit terminer "done", sans jamais exposer le détail de
    l'exception à l'API."""
    from src.rag import private_rag_manager
    from src.web import jobs

    monkeypatch.setattr(jobs, "ANALYSES_DIR", tmp_path / "analyses")
    monkeypatch.setattr(jobs, "ANALYSIS_FILES_DIR", tmp_path / "outputs")
    monkeypatch.setattr(private_rag_manager, "search", lambda db, **kw: [])

    from src.agents import scoring_engine
    monkeypatch.setattr(scoring_engine, "_SCORING_ENRICHMENT_USER_PATH", Path("/does/not/exist/prompt.txt"))

    # enrich_with_llm's very first line is `if not llm.enabled: return result`
    # (the LLM-disabled fallback) — in this test environment (no real API
    # key) that gate fires BEFORE the missing-file read is ever reached, so
    # a fake LLM reporting enabled=True is required to actually exercise
    # the vulnerable code path. Its json_complete raises if ever called —
    # the missing-file read happens strictly before that point in
    # enrich_with_llm, so this must never be invoked for real.
    class _FakeEnabledLLM:
        enabled = True

        def json_complete(self, *a, **kw):
            raise AssertionError("must never be reached — the missing prompt file read happens first")

    import src.agents.llm_client as llm_client_module
    monkeypatch.setattr(llm_client_module, "ClaudeClient", lambda: _FakeEnabledLLM())

    make_active_starter_user(db, "missingprompt@example.com", scoring=False)
    csrf = _login(client, "missingprompt@example.com")
    client.post("/api/capacity", json={
        "charge_globale_pct": 40, "nombre_projets_en_cours": 1,
        "projets_en_cours": ["Projet test"], "capacites_par_pole": {"Software Engineering": 40},
    }, headers={"X-CSRF-Token": csrf})
    client.put("/api/scoring-config/profile", json={
        "raison_sociale": "ESN test", "effectif": "10-50", "competences": ["python"], "certifications": [],
    }, headers={"X-CSRF-Token": csrf})
    client.put("/api/scoring-config/policy", json={
        "weights": {
            "Adequation expertise": 20, "References similaires": 15, "Disponibilite equipe": 10,
            "Rentabilite estimee": 10, "Faisabilite delai": 10, "Certifications requises": 10,
            "Complexite technique": 5, "Connaissance secteur": 5, "Potentiel commercial": 5,
            "Risque contractuel": 5, "Solidite client": 3, "Valeur strategique": 2,
        },
        "threshold_go": 88, "threshold_sous_reserve": 60,
        "business_rules": {
            "budget_minimum_eur": 0, "max_charge_pct": 100,
            "max_unmastered_technologies": 999, "certification_penalty_score": 20,
        },
    }, headers={"X-CSRF-Token": csrf})
    client.post("/api/scoring-config/policy/activate", json={"expected_active_version": None}, headers={"X-CSRF-Token": csrf})

    r = client.post("/api/analyze", data={
        "mode": "paste",
        "text": "Appel d'offres test. Acheteur: X. Budget: 200000 euros. Date limite: 30/11/2026. Python Django.",
    }, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200
    job_id = r.json()["job_id"]

    job = None
    for _ in range(80):
        job = jobs.get_job(job_id)
        if job.status != "running":
            break
        time.sleep(0.1)

    # Régression positive : un prompt manquant est un incident d'enrichissement
    # DÉGRADÉ, jamais un échec de job ni une perte du score déjà calculé.
    assert job.status == "done", "un prompt d'enrichissement manquant ne doit jamais faire échouer le job"
    assert job.error_code is None
    assert job.result is not None, "le score algorithmique déjà calculé ne doit jamais être perdu"
    assert job.result.enrichment_status == "failed"
    assert job.result.enrichment_reason == "prompt_missing", (
        "un prompt manquant doit être distinguable d'un vrai défaut de scoring "
        "(scoring_failed) ou d'un échec LLM générique (provider_exception)"
    )
    assert job.result.score_global is not None, "le score déjà calculé doit être conservé, jamais remplacé"

    status_response = client.get(f"/api/analyze/{job_id}/status")
    assert status_response.status_code == 200
    body = status_response.json()
    assert "does/not/exist" not in (body.get("error") or ""), "aucun chemin de fichier ne doit fuiter dans la réponse API"
    assert "FileNotFoundError" not in (body.get("error") or "")
