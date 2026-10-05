"""Lot 43 — legacy removal without breaking the SaaS, and account-dependent
scoring.

Sections:
A. The FastAPI application no longer needs Streamlit (nor pandas) and loads no
   demo corpus; the removed modules are really gone; the manifests are clean.
B. Scoring is unchanged by the cleanup: results recorded BEFORE the removal
   (tests/fixtures/lot43_scoring_golden_before_cleanup.json, produced by the
   pre-cleanup engine, both through its `policy=None` path and through an
   explicit snapshot) are reproduced exactly by the explicit snapshots.
C. A new analysis/simulation needs the account's own private configuration —
   two accounts never see each other's; a missing configuration is refused.
D. Omission control for lists extracted by the LLM (the lot 42 reserve): the
   recorded lot 42 response, then the same response with Marseille omitted.
E. Real HTTP path with a simulated LLM (adapter level): settings -> simulation
   -> activation -> analysis -> result / history / documents / persisted read.

No real provider, network, key or customer data is reachable: the LLM is a
recorded/replayed fake at the provider-adapter level.
"""
from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from src.agents import business_facts
from src.agents.ao_extractor import AOExtractor, _llm_list_incomplete_reason
from src.agents.llm_client import LLMClient
from src.agents.scoring_engine import ScoringEngine, ScoringPolicySnapshot
from src.core.models import AOContext, CapacityResult, CompanyProfile, ExtractedFact, RAGEvidence
from src.web import jobs
from tests.conftest import default_org_id, make_active_starter_user
from tests.synthetic_scoring import (
    SYNTHETIC_BUSINESS_RULES, SYNTHETIC_CERTS, SYNTHETIC_MASTERED, SYNTHETIC_THRESHOLD_GO,
    SYNTHETIC_THRESHOLD_SOUS_RESERVE, SYNTHETIC_WEIGHTS, synthetic_policy,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN = json.loads((FIXTURES / "lot43_scoring_golden_before_cleanup.json").read_text(encoding="utf-8"))
SCENARIO = json.loads((FIXTURES / "lot42_scenario.json").read_text(encoding="utf-8"))
RECORDED_RESPONSE = (FIXTURES / "lot42_sonnet46_facts_response.txt").read_text(encoding="utf-8")


# ===========================================================================
# A — Streamlit / demo corpus are gone
# ===========================================================================

def test_the_application_imports_without_streamlit_pandas_or_the_demo_corpus():
    """A fresh interpreter in which importing `streamlit` or `pandas` is an
    error (pandas: only the removed UI used it; scikit-learn treats it as
    optional) must still import `main`, build the per-job analysis services,
    and never touch `data/reg_docs` (the demo corpus)."""
    script = textwrap.dedent('''
        import importlib.abc, pathlib, sys

        class Blocker(importlib.abc.MetaPathFinder):
            def find_spec(self, name, path, target=None):
                if name.split(".")[0] in ("streamlit", "pandas"):
                    raise ImportError(name + " is blocked by the lot 43 test")
                return None
        sys.meta_path.insert(0, Blocker())

        touched = []
        for method in ("read_text", "read_bytes", "open", "rglob", "glob", "iterdir", "exists", "is_file"):
            original = getattr(pathlib.Path, method)
            def make(original, method):
                def wrapper(self, *a, **k):
                    if "reg_docs" in str(self).replace("\\\\", "/"):
                        touched.append(method + ":" + str(self))
                    return original(self, *a, **k)
                return wrapper
            setattr(pathlib.Path, method, make(original, method))

        import main
        from src.web.analysis_services import build_analysis_services
        services = build_analysis_services()
        assert services.reranker is not None and services.scoring is not None
        assert "streamlit" not in sys.modules and "pandas" not in sys.modules
        assert not touched, touched
        print("STARTUP_OK")
    ''')
    result = subprocess.run([sys.executable, "-c", script], cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "STARTUP_OK" in result.stdout


def test_the_app_starts_and_serves_a_page_in_process(client):
    """The real lifespan (config validation, job reconciliation) and the page
    routes work on the isolated database, with the legacy modules gone."""
    assert client.get("/login").status_code == 200


@pytest.mark.parametrize("module", [
    "src.ui", "src.ui.app", "src.core.pipeline", "src.web.pipeline_singleton", "src.rag.rag_manager",
    "src.core.capacity_repository", "src.web.historique_service", "src.web.knowledge_service",
])
def test_removed_legacy_modules_are_really_gone(module):
    try:
        spec = importlib.util.find_spec(module)
    except ModuleNotFoundError:  # the parent package itself no longer exists
        spec = None
    assert spec is None


def test_no_python_file_of_the_project_imports_streamlit():
    pattern = re.compile(r"^\s*(?:import|from)\s+streamlit\b", re.M)
    offenders = []
    for folder in ("src", "scripts", "migrations"):
        for path in (REPO_ROOT / folder).rglob("*.py"):
            if pattern.search(path.read_text(encoding="utf-8", errors="ignore")):
                offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not pattern.search((REPO_ROOT / "main.py").read_text(encoding="utf-8"))
    assert offenders == []


def test_manifests_and_ci_no_longer_reference_streamlit():
    for name in ("requirements.txt", "requirements.lock.txt", "requirements-test.txt", ".github/workflows/ci.yml"):
        text = (REPO_ROOT / name).read_text(encoding="utf-8").lower()
        # The lock header may EXPLAIN what was removed; a pinned/required entry is what must not exist.
        entries = [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
        assert not any(line.startswith(("streamlit", "pandas", "altair", "pydeck", "watchdog")) for line in entries), name
        assert "streamlit run" not in text, name


def test_the_shared_services_that_survived_the_removal_are_all_importable():
    from src.core.capacity_plan import CapacityPlan
    from src.rag.private_rag_manager import search  # noqa: F401 — imports FRENCH_STOP_WORDS from the new module
    from src.rag.semantic_rerank import FRENCH_STOP_WORDS, SemanticReranker
    assert "le" in FRENCH_STOP_WORDS
    assert SemanticReranker().last_selection_status == "not_attempted"
    assert CapacityPlan(charge_globale_pct=10, disponibilite_minimum_pct=5).capacites_par_pole == {}


def test_the_global_json_history_is_not_written_by_a_real_job(client, db, monkeypatch):
    """Lot 43: the cross-account `historique_ao.json` is no longer written;
    the durable per-job JSON and the database remain (reading an old
    analysis is covered in section E)."""
    _install_no_op_search(monkeypatch)
    csrf = _configure_cleaning_account(client, db, "noglobaljson@example.com")
    job = _run_job(client, csrf, "Appel d'offres - Prestations de nettoyage. Acheteur : Collectivité Exemple. Site de Lyon, 2 fois par semaine. Budget : 80 000 euros.")
    assert job.status == "done", job.error
    assert not (Path(jobs.ANALYSES_DIR).parent / "historique_ao.json").exists()
    assert list(Path(jobs.ANALYSES_DIR).glob(f"{job.id}.json"))


# ===========================================================================
# B — scoring results are unchanged by the cleanup (golden recorded before)
# ===========================================================================

def _snapshot(spec: dict, **extra) -> ScoringPolicySnapshot:
    fields = dict(spec)
    fields["mastered_technologies"] = frozenset(fields["mastered_technologies"])
    fields["certifications_held"] = frozenset(fields["certifications_held"])
    fields.update(extra)
    return ScoringPolicySnapshot.from_legacy(**fields)


def _stable(blockers: list[str]) -> list[str]:
    """The "Trop de technologies non maitrisees : a, b, c" wording lists the
    members of a Python set, so its ORDER differs between interpreter runs
    (hash randomization) — a pre-existing cosmetic instability, reported in the
    lot 43 report, not changed here. The set of technologies is what counts."""
    prefix = "Trop de technologies non maitrisees : "
    # Lot 44: the historical budget blocker wording ("… seuil minimal de
    # rentabilite ESN …") presumed an ESN and a profitability rule; new results
    # say "… minimum fixé par la politique …". Same blocker, same threshold —
    # the only TEXT difference between the pre-cleanup record and today.
    legacy_budget = "Budget inferieur au seuil minimal de rentabilite ESN (50 000 EUR)"
    new_budget = "Budget inférieur au minimum fixé par la politique (50 000)"
    return [prefix + ", ".join(sorted(b[len(prefix):].split(", "))) if b.startswith(prefix) else (new_budget if b == legacy_budget else b)
            for b in blockers]


def _run_case(inputs: dict, policy: ScoringPolicySnapshot) -> dict:
    facts = {k: ExtractedFact(**v) for k, v in (inputs.get("facts") or {}).items()}
    ao = AOContext(**inputs["ao"], extracted_facts=facts)
    result = ScoringEngine().score(
        ao, CompanyProfile(**inputs["company"]), [RAGEvidence(**e) for e in inputs["evidences"]],
        CapacityResult(**inputs["capacity"]), policy=policy,
    )
    return dict(
        decision=result.decision, score_global=result.score_global, blockers=_stable(list(result.criteres_bloquants)),
        completeness=result.scoring_completeness, missing=list(result.scoring_missing),
        criteres=[[c.nom, c.poids, c.score] for c in result.criteres],
    )


@pytest.mark.parametrize("name", sorted(GOLDEN["legacy_cases"]))
def test_explicit_policy_reproduces_the_results_the_policy_none_engine_returned_before(name):
    case = GOLDEN["legacy_cases"][name]
    assert _run_case(case["inputs"], _snapshot(GOLDEN["demo_policy"])) == {**case["expected"], "blockers": _stable(case["expected"]["blockers"])}


@pytest.mark.parametrize("name", sorted(GOLDEN["custom_cases"]))
def test_custom_criteria_results_recorded_before_the_cleanup_are_reproduced(name):
    case = GOLDEN["custom_cases"][name]
    assert _run_case(case["inputs"], _snapshot(case["policy"])) == {**case["expected"], "blockers": _stable(case["expected"]["blockers"])}


def test_the_test_helper_policy_carries_exactly_the_values_recorded_from_the_old_engine():
    demo = GOLDEN["demo_policy"]
    assert dict(SYNTHETIC_WEIGHTS) == demo["weights"]
    assert sorted(SYNTHETIC_MASTERED) == demo["mastered_technologies"]
    assert sorted(SYNTHETIC_CERTS) == demo["certifications_held"]
    assert (SYNTHETIC_THRESHOLD_GO, SYNTHETIC_THRESHOLD_SOUS_RESERVE) == (demo["threshold_go"], demo["threshold_sous_reserve"])
    for key, value in SYNTHETIC_BUSINESS_RULES.items():
        assert demo[key] == value


def test_the_it_lyon_scenario_of_lot_42_keeps_its_observed_result():
    """66.5 / GO SOUS RESERVE — observed in lot 42 with the real model's
    extraction, replayed here from the recorded normalized extraction."""
    case = GOLDEN["custom_cases"]["it_lyon_recorded"]["expected"]
    assert (case["decision"], case["score_global"]) == ("GO SOUS RESERVE", 66.5)


# ===========================================================================
# C — private configuration is required; two accounts never mix
# ===========================================================================

_VALID_AO_TEXT = "Appel d'offres. Acheteur : Ville de Test. Budget : 250 000 euros. Technologies : Python."


def _login(client, email):
    client.cookies.clear()
    page = client.get("/login")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    client.post("/login", data={"email": email, "password": "Sup3rSecret!", "next": "/app", "csrf_token": csrf})
    return csrf


def _install_no_op_search(monkeypatch):
    from src.rag import private_rag_manager
    monkeypatch.setattr(private_rag_manager, "search", lambda db, **kw: [])


def _wait_terminal(job_id):
    for _ in range(300):
        job = jobs.get_job(job_id)
        if job is not None and job.status != "running":
            return job
        time.sleep(0.1)
    raise AssertionError("job did not reach a terminal state")


def _run_job(client, csrf, text):
    response = client.post("/api/analyze", data={"mode": "paste", "text": text}, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200, response.text
    return _wait_terminal(response.json()["job_id"])


CLEANING_PROFILE = {
    "raison_sociale": "Nettoyage Pro (test lot 43)", "competences": [], "certifications": [],
    "business_facts": SCENARIO["provider_business_facts"],
}
CLEANING_POLICY = {
    "weights": SCENARIO["fixed_weights"], "threshold_go": SCENARIO["threshold_go"],
    "threshold_sous_reserve": SCENARIO["threshold_sous_reserve"], "business_rules": SCENARIO["business_rules"],
    "custom_criteria": SCENARIO["custom_criteria"],
}
IT_POLICY = {
    "weights": dict(SYNTHETIC_WEIGHTS), "threshold_go": 88, "threshold_sous_reserve": 60,
    "business_rules": dict(SYNTHETIC_BUSINESS_RULES),
}


def _save_capacity(client, csrf, *, charge):
    r = client.post("/api/capacity", json={
        "charge_globale_pct": charge, "nombre_projets_en_cours": 1, "projets_en_cours": ["Projet"],
        "capacites_par_pole": {"Pôle": 100 - charge}}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200, r.text


def _activate(client, csrf):
    r = client.post("/api/scoring-config/policy/activate", json={"expected_active_version": None}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200, r.text


def _configure(client, csrf, *, profile, policy, charge, activate=True):
    _save_capacity(client, csrf, charge=charge)
    r = client.put("/api/scoring-config/profile", json=profile, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200, r.text
    r = client.put("/api/scoring-config/policy", json=policy, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200, r.text
    r = client.post("/api/scoring-config/policy/validate", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200 and r.json()["valid"], r.text
    if activate:
        _activate(client, csrf)


def _configure_cleaning_account(client, db, email, *, activate=True, charge=40):
    make_active_starter_user(db, email, scoring=False)
    csrf = _login(client, email)
    _configure(client, csrf, profile=CLEANING_PROFILE, policy=CLEANING_POLICY, charge=charge, activate=activate)
    return csrf


def _simulate(client, csrf, text):
    return client.post("/api/scoring-config/simulate", data={"mode": "paste", "text": text}, headers={"X-CSRF-Token": csrf})


def test_two_accounts_are_scored_on_their_own_configuration_only(client, db, monkeypatch):
    """Same AO text, two accounts: the cleaning account (custom criteria,
    40 % load) and an IT account (no custom criteria, 90 % load, its own
    skills). Analysis AND simulation of each use only that account's profile,
    facts, policy and capacity; nothing leaks across, and one cannot read the
    other's job."""
    _install_no_op_search(monkeypatch)
    text = (
        "Appel d'offres - Prestations de nettoyage et maintenance applicative. Acheteur : Collectivité Exemple. "
        "Site de Lyon, 2 fois par semaine. Budget : 250 000 euros. Technologies : Python."
    )

    make_active_starter_user(db, "lot43-clean@example.com", scoring=False)
    csrf_a = _login(client, "lot43-clean@example.com")
    _configure(client, csrf_a, profile=CLEANING_PROFILE, policy=CLEANING_POLICY, charge=40, activate=False)
    sim_a = _simulate(client, csrf_a, text)
    assert sim_a.status_code == 200, sim_a.text
    _activate(client, csrf_a)
    job_a = _run_job(client, csrf_a, text)

    make_active_starter_user(db, "lot43-it@example.com", scoring=False)
    csrf_b = _login(client, "lot43-it@example.com")
    _configure(client, csrf_b, profile={"raison_sociale": "ESN test", "competences": ["python"], "certifications": []},
               policy=IT_POLICY, charge=90, activate=False)
    sim_b = _simulate(client, csrf_b, text)
    assert sim_b.status_code == 200, sim_b.text
    _activate(client, csrf_b)
    job_b = _run_job(client, csrf_b, text)
    status_of_a_seen_by_b = client.get(f"/api/analyze/{job_a.id}/status")

    custom_labels = {c["label"] for c in SCENARIO["custom_criteria"]}
    for label, job, simulation, charge in (("A", job_a, sim_a.json(), 40), ("B", job_b, sim_b.json(), 90)):
        assert job.status == "done", (label, job.error)
        assert job.result.capacity.charge_actuelle_pct == charge, label
        assert simulation["capacity"]["charge_actuelle_pct"] == charge, label
        assert simulation["simulation"] is True
    names_a = {c.nom for c in job_a.result.criteres}
    names_b = {c.nom for c in job_b.result.criteres}
    assert custom_labels <= names_a and len(job_a.result.criteres) == 15
    assert not (custom_labels & names_b) and len(job_b.result.criteres) == 12
    assert {c["nom"] for c in sim_b.json()["criteres"]} == names_b
    assert {c["nom"] for c in sim_a.json()["criteres"]} == names_a
    # each result carries the weights of ITS account
    assert {c.nom: c.poids for c in job_a.result.criteres}["Disponibilité équipe"] == 15
    assert {c.nom: c.poids for c in job_b.result.criteres}["Disponibilité équipe"] == 10
    assert status_of_a_seen_by_b.status_code == 404, "an account must never read another account's job"


def test_a_job_without_a_private_capacity_plan_stops_and_never_uses_a_demo_capacity(client, db, monkeypatch):
    _install_no_op_search(monkeypatch)
    user = make_active_starter_user(db, "lot43-nocap@example.com", capacity=False, scoring=True)
    job = jobs.create_job(source_label="t", user_id=user.id, organization_id=default_org_id(db, user))
    jobs._run_analysis(job, _VALID_AO_TEXT)
    assert job.status == "error" and job.result is None
    assert job.error_code == jobs.UNEXPECTED_ERROR_CODE


def test_a_job_without_an_active_policy_stops_and_never_creates_one(client, db, monkeypatch):
    from src.web.database.repositories import scoring_policy as scoring_policy_repo
    _install_no_op_search(monkeypatch)
    user = make_active_starter_user(db, "lot43-nopolicy@example.com", capacity=True, scoring=False)
    org = default_org_id(db, user)
    job = jobs.create_job(source_label="t", user_id=user.id, organization_id=org)
    jobs._run_analysis(job, _VALID_AO_TEXT)
    assert job.status == "error" and job.result is None
    db.expire_all()
    assert scoring_policy_repo.get_active(db, organization_id=org, owner_user_id=user.id) is None
    assert scoring_policy_repo.get_draft(db, organization_id=org, owner_user_id=user.id) is None


def test_the_api_refuses_analysis_and_simulation_without_configuration(client, db):
    make_active_starter_user(db, "lot43-fresh@example.com", capacity=False, scoring=False)
    csrf = _login(client, "lot43-fresh@example.com")
    body = client.post("/api/analyze", data={"mode": "paste", "text": _VALID_AO_TEXT}, headers={"X-CSRF-Token": csrf})
    assert body.status_code == 409 and body.json()["detail"]["error_code"] == "CAPACITY_NOT_CONFIGURED"
    _save_capacity(client, csrf, charge=40)
    body = client.post("/api/analyze", data={"mode": "paste", "text": _VALID_AO_TEXT}, headers={"X-CSRF-Token": csrf})
    assert body.status_code == 409 and body.json()["detail"]["error_code"] == "SCORING_NOT_CONFIGURED"
    assert _simulate(client, csrf, _VALID_AO_TEXT).status_code == 404, "no draft: the simulation never invents one"
    assert client.get("/api/scoring-config").json()["policy"]["active"] is None


# ===========================================================================
# D — omission control for LLM-extracted lists (lot 42 reserve)
# ===========================================================================

ZONE_SPEC = {"label": "Sites d'intervention", "type": "list", "unit": None}
LYON_MARSEILLE_TEXT = SCENARIO["ao_text"]


@pytest.mark.parametrize("text,values,expected", [
    # complete list, several formulations
    ("Prestations sur les sites de Lyon et de Marseille.", ["Lyon", "Marseille"], None),
    ("Sites d'intervention :\n- Lyon\n- Marseille", ["Lyon", "Marseille"], None),
    ("Sites d'intervention : Lyon (69), Marseille (13).", ["Lyon", "Marseille"], None),
    ("Interventions sur les sites de LYON et de SAINT-ÉTIENNE.", ["Lyon", "Saint-Etienne"], None),
    ("Interventions sur les sites de Lyon et de Saint-Étienne.", ["Lyon", "saint etienne"], None),
    # an omission is detected
    ("Prestations sur les sites de Lyon et de Marseille.", ["Lyon"], "llm_list_may_omit: Marseille"),
    ("Prestations sur les sites de Lyon, Marseille et Nice.", ["Lyon", "Marseille"], "llm_list_may_omit: Nice"),
    ("Sites d'intervention :\n- Lyon\n- Marseille\n- Villeurbanne", ["Lyon", "Marseille"], "llm_list_may_omit: Villeurbanne"),
    ("Sites d'intervention : Lyon (69), Marseille (13), Nice.", ["Lyon", "Marseille"], "llm_list_may_omit: Nice"),
    ("Interventions sur les sites de Lyon et d'Aix-en-Provence.", ["Lyon"], "llm_list_may_omit: Aix-en-Provence"),
    # a list announced as non-exhaustive is never taken as complete
    ("Les sites d'intervention sont Lyon, Marseille, etc.", ["Lyon", "Marseille"], "llm_list_non_exhaustive_marker"),
    # a value the document never states
    ("Sites d'intervention : Lyon.", ["Lyon", "Paris"], "llm_value_not_in_document: Paris"),
    # a place cited OUTSIDE a passage about the requested fact is not a requirement
    ("Acheteur : Mairie de Marseille, 13001 Marseille.\nLes prestations sont réalisées sur le site de Lyon.", ["Lyon"], None),
    ("L'acheteur, basé à Marseille, demande des interventions sur le site de Lyon.", ["Lyon"], None),
    ("Siège du titulaire : Nice. Interventions sur le site de Lyon uniquement.", ["Lyon"], None),
    # documented limit: a passage that shares no word with the fact's label is not cross-checked
    ("Périmètre géographique : Lyon et Marseille.", ["Lyon"], None),
])
def test_llm_list_is_reconciled_with_the_documents_own_enumerations(text, values, expected):
    reason = _llm_list_incomplete_reason(text, "zone_intervention", ZONE_SPEC, values)
    if expected is None:
        assert reason is None
    else:
        assert reason == expected


def test_a_lowercase_enumeration_after_the_labels_colon_is_reconciled_too():
    spec = {"label": "Certifications exigées", "type": "list", "unit": None}
    text = "Certifications exigées : ISO 27001, HDS et SecNumCloud."
    assert _llm_list_incomplete_reason(text, "certifications", spec, ["ISO 27001", "HDS", "SecNumCloud"]) is None
    assert _llm_list_incomplete_reason(text, "certifications", spec, ["ISO 27001", "HDS"]) == "llm_list_may_omit: SecNumCloud"


class _ReplayProvider:
    """Adapter-level fake: replays a recorded answer for the facts prompt,
    nothing (=> local fallback) for every other prompt. Counts sends."""
    name = "recorded"
    enabled = True

    def __init__(self, facts_response: str):
        self.facts_response = facts_response
        self.facts_sends = 0
        self.other_sends = 0
        self._facts_marker = (
            REPO_ROOT / "src" / "agents" / "prompts" / "ao_facts_extraction_system.txt"
        ).read_text(encoding="utf-8").strip()[:60]

    def complete(self, prompt, system, temperature, max_tokens):
        if system and self._facts_marker in system:
            self.facts_sends += 1
            return self.facts_response
        self.other_sends += 1
        return ""


def _install_provider(monkeypatch, facts_response: str) -> _ReplayProvider:
    import src.agents.ao_extractor as extractor_module
    import src.agents.llm_client as llm_client_module
    from src.core import config

    provider = _ReplayProvider(facts_response)
    monkeypatch.setattr(config, "LLM_ENABLED", True)
    monkeypatch.setattr(extractor_module, "ClaudeClient", lambda: LLMClient([provider]))
    monkeypatch.setattr(llm_client_module, "ClaudeClient", lambda: LLMClient([provider]))
    return provider


def _omitting_marseille(response: str) -> str:
    payload = json.loads(response.strip().removeprefix("```json").removesuffix("```").strip())
    payload["zone_intervention"]["value"] = ["Lyon"]
    return "```json\n" + json.dumps(payload) + "\n```"


def _extract(text, monkeypatch, facts_response):
    provider = _install_provider(monkeypatch, facts_response)
    requested = business_facts.requested_facts_from_criteria(
        SCENARIO["custom_criteria"], known_facts=SCENARIO["provider_business_facts"])
    facts = AOExtractor()._resolve_requested_facts(text, requested, allow_llm=True)
    return facts, provider


def _score_facts(facts, **policy_overrides):
    rules = SCENARIO["business_rules"]
    snapshot = ScoringPolicySnapshot.from_legacy(
        weights=dict(SCENARIO["fixed_weights"]), threshold_go=SCENARIO["threshold_go"],
        threshold_sous_reserve=SCENARIO["threshold_sous_reserve"], budget_minimum_eur=rules["budget_minimum_eur"],
        max_charge_pct=rules["max_charge_pct"], max_unmastered_technologies=rules["max_unmastered_technologies"],
        certification_penalty_score=rules["certification_penalty_score"],
        custom_criteria=SCENARIO["custom_criteria"], declared_facts=SCENARIO["provider_business_facts"], **policy_overrides,
    )
    ao = AOContext(titre="Nettoyage (synthétique)", client="Client", budget_estime=120000.0,
                   texte_source=SCENARIO["ao_text"], extracted_facts=facts)
    return ScoringEngine().score(ao, CompanyProfile(), [], CapacityResult(**SCENARIO["capacity"]), policy=snapshot)


def test_the_recorded_lot_42_response_still_gives_the_lot_42_result(monkeypatch):
    facts, provider = _extract(LYON_MARSEILLE_TEXT, monkeypatch, RECORDED_RESPONSE)
    assert provider.facts_sends == 1
    zone = facts["zone_intervention"]
    assert zone.status == "found" and zone.provenance == "llm" and sorted(zone.value) == ["Lyon", "Marseille"]
    result = _score_facts(facts)
    by_name = {c.nom: c for c in result.criteres}
    assert result.decision == "NO-GO" and any("Sites couverts" in b for b in result.criteres_bloquants)
    assert by_name["Fréquence compatible"].score == 20, "4 > 3, unit 'par semaine' == 'par_semaine' after spelling normalization"
    assert by_name["Travail de nuit cohérent"].score == 100
    assert result.scoring_missing == [] and result.scoring_completeness == "complete"


def test_the_same_response_with_marseille_omitted_is_ambiguous_and_incomplete_never_favorable(monkeypatch):
    facts, provider = _extract(LYON_MARSEILLE_TEXT, monkeypatch, _omitting_marseille(RECORDED_RESPONSE))
    assert provider.facts_sends == 1, "no additional verification call"
    zone = facts["zone_intervention"]
    assert zone.status == "ambiguous" and zone.provenance == "llm" and zone.value is None
    assert zone.reason == "llm_list_may_omit: Marseille"
    result = _score_facts(facts)
    by_name = {c.nom: c for c in result.criteres}
    assert result.decision == "INCOMPLET"
    assert "custom:zone_couverte" in result.scoring_missing
    assert by_name["Sites couverts"].score == 0.0, "an unresolved criterion contributes 0, never a favorable score"
    assert "llm_list_may_omit: Marseille" in by_name["Sites couverts"].justification
    assert result.decision not in ("GO", "GO SOUS RESERVE")
    # the other, resolvable facts are still evaluated (unit equivalence intact)
    assert by_name["Fréquence compatible"].score == 20 and by_name["Travail de nuit cohérent"].score == 100


def test_a_place_cited_outside_the_requirement_adds_no_false_blocker_and_no_false_incomplete(monkeypatch):
    text = (
        "Acheteur : Mairie de Marseille, 13001 Marseille.\n"
        "Les prestations de nettoyage sont réalisées sur le site de Lyon.\n"
        "Quatre interventions par semaine. Le travail de nuit n'est pas requis."
    )
    response = json.dumps({
        "zone_intervention": {"value": ["Lyon"], "unit": None, "found": True},
        "frequence_nettoyage": {"value": 4, "unit": "par semaine", "found": True},
        "travail_de_nuit": {"value": False, "unit": None, "found": True},
    })
    facts, _ = _extract(text, monkeypatch, response)
    assert facts["zone_intervention"].status == "found" and facts["zone_intervention"].value == ["Lyon"]
    result = _score_facts(facts)
    assert result.criteres_bloquants == [], "Marseille (buyer's address) is not a required site"
    assert {c.nom: c for c in result.criteres}["Sites couverts"].score == 100
    assert result.scoring_completeness == "complete"


def test_an_absent_fact_stays_absent_and_zero_or_false_are_real_values(monkeypatch):
    # absent: the LLM found nothing and the local fallback finds nothing -> INCOMPLET, not a favorable default
    facts, _ = _extract("Texte sans aucune information utile.", monkeypatch, json.dumps({
        "zone_intervention": {"found": False}, "frequence_nettoyage": {"found": False}, "travail_de_nuit": {"found": False},
    }))
    assert {k: v.status for k, v in facts.items()} == {k: "absent" for k in facts}
    assert _score_facts(facts).decision == "INCOMPLET"
    # 0 and false are values: never dropped, never turned into "absent"
    threshold = {"id": "f", "operator": "numeric_threshold", "comparison": "provider_gte_ao", "pass_score": 100, "fail_score": 0}
    zero = ExtractedFact(value=0.0, unit="par semaine", status="found")
    assert business_facts.evaluate_custom_criterion(threshold, ao_fact=zero, provider_value=3, provider_unit="par_semaine")[0] == 100.0
    equality = {"id": "n", "operator": "equality", "pass_score": 100, "fail_score": 0}
    no_night = ExtractedFact(value=False, status="found")
    assert business_facts.evaluate_custom_criterion(equality, ao_fact=no_night, provider_value=False, provider_unit=None)[0] == 100.0
    assert business_facts.evaluate_custom_criterion(equality, ao_fact=ExtractedFact(status="absent"), provider_value=False, provider_unit=None)[0] is None


def test_equivalent_units_match_and_different_units_never_do():
    threshold = {"id": "f", "operator": "numeric_threshold", "comparison": "provider_gte_ao", "pass_score": 100, "fail_score": 0}
    for written in ("par semaine", "Par  Semaine", "par-semaine", "PAR_SEMAINE"):
        fact = ExtractedFact(value=2, unit=written, status="found")
        assert business_facts.evaluate_custom_criterion(threshold, ao_fact=fact, provider_value=3, provider_unit="par_semaine")[0] == 100.0
    for other in ("par mois", "par jour", None):
        fact = ExtractedFact(value=2, unit=other, status="found")
        assert business_facts.evaluate_custom_criterion(threshold, ao_fact=fact, provider_value=3, provider_unit="par_semaine")[0] is None


# ===========================================================================
# E — real HTTP path, simulated LLM: settings -> simulation -> analysis ->
#     result / history / documents / persisted read
# ===========================================================================

def test_settings_to_simulation_to_analysis_to_result_history_documents_and_persisted_read(client, db, monkeypatch):
    provider = _install_provider(monkeypatch, RECORDED_RESPONSE)
    _install_no_op_search(monkeypatch)
    csrf = _configure_cleaning_account(client, db, "lot43-flow@example.com", activate=False)

    # simulation on the DRAFT: deterministic, no provider call, nothing recorded
    simulation = _simulate(client, csrf, LYON_MARSEILLE_TEXT)
    assert simulation.status_code == 200, simulation.text
    assert simulation.json()["simulation"] is True
    assert provider.facts_sends == 0 and provider.other_sends == 0, "the simulation never calls a provider"
    assert client.get("/api/history").json()["total"] == 0, "the simulation writes nothing to the history"

    _activate(client, csrf)

    # real analysis through the job pipeline, with the recorded model answer
    job = _run_job(client, csrf, LYON_MARSEILLE_TEXT)
    assert job.status == "done", (job.error, job.error_code)
    assert provider.facts_sends == 1
    result = job.result
    assert result.decision == "NO-GO" and any("Sites couverts" in b for b in result.criteres_bloquants)
    by_name = {c.nom: c for c in result.criteres}
    assert by_name["Fréquence compatible"].score == 20
    assert job.scoring_policy_version == 1

    # result page, status, history, documents
    assert client.get(f"/api/analyze/{job.id}/status").json()["redirect_url"] == f"/app/resultats/{job.id}"
    assert client.get(f"/app/resultats/{job.id}").status_code == 200
    history = client.get("/api/history").json()
    assert history["total"] == 1 and history["items"][0]["job_id"] == job.id and history["items"][0]["decision"] == "NO-GO"
    pdf = client.get(f"/api/download/{job.id}/pdf")
    docx = client.get(f"/api/download/{job.id}/docx")
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf" and pdf.content[:4] == b"%PDF"
    assert docx.status_code == 200 and docx.content[:2] == b"PK"

    # persisted read: forget the in-memory job, the analysis is read back from
    # the durable record, and its documents are still retrievable
    score, decision = result.score_global, result.decision
    jobs._JOBS.pop(job.id, None)
    reread = jobs.get_job(job.id)
    assert reread is not None and reread.status == "done"
    assert (reread.result.score_global, reread.result.decision) == (score, decision)
    assert client.get(f"/api/download/{job.id}/pdf").status_code == 200
