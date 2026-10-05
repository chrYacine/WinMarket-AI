"""B17-T1 (DEFECT confirmed): a negation applied to an ENTIRE LINE could
cancel a certification obligation stated in a DIFFERENT clause on that
same line. Audited example: "ISO 27001 obligatoire ; Qualiopi non
obligatoire" wrongly dropped ISO 27001 too. src/agents/
certification_scope.py isolates clause-scoped analysis (never a whole
line, never an arbitrary shared window) — this file proves the exact
audited case plus the ticket's essential test groups.

Test groups, per the ticket:
A. A mixed obligatory/optional case, both orders, on one line with
   punctuation or "mais" — only the correct obligation survives.
B. Collective scope: "X et Y obligatoires" / "ni X ni Y ne sont requises"
   — correct COLLECTIVE scope; a simple mention stays distinct from an
   obligation.
C. Two contradictory/distinct-context clauses — sources kept, conflict
   visible, no fabricated resolution. The real AOExtractor fallback with
   a disabled LLM, verifying the job receives correct obligations and
   diagnostics.
"""
from __future__ import annotations

import re
import time

from src.agents.ao_extractor import AOExtractor
from src.agents.certification_scope import analyze_certification_mentions, extract_certifications, resolve_mandatory_certifications
from tests.conftest import make_active_starter_user

AUDITED_EXAMPLE = "ISO 27001 obligatoire ; Qualiopi non obligatoire"


# ---------------------------------------------------------------------------
# Group A — mixed obligatory/optional on one line, both orders, both
# separators.
# ---------------------------------------------------------------------------

def test_audited_example_keeps_iso_and_excludes_qualiopi():
    result = extract_certifications(AUDITED_EXAMPLE)
    assert "ISO 27001" in result
    assert "Qualiopi" not in result


def test_audited_example_reversed_order_gives_the_same_correct_result():
    result = extract_certifications("Qualiopi non obligatoire ; ISO 27001 obligatoire")
    assert "ISO 27001" in result
    assert "Qualiopi" not in result


def test_mais_separator_both_orders():
    result_a = extract_certifications("ISO 27001 obligatoire, mais Qualiopi n'est pas obligatoire")
    assert result_a == ["ISO 27001"]

    result_b = extract_certifications("Qualiopi n'est pas obligatoire, mais ISO 27001 est obligatoire")
    assert result_b == ["ISO 27001"]


def test_semicolon_with_three_independent_clauses():
    text = "ISO 27001 obligatoire ; Qualiopi non obligatoire ; RGPD exige"
    result = extract_certifications(text)
    assert set(result) == {"ISO 27001", "RGPD"}


# ---------------------------------------------------------------------------
# Group B — collective (coordinated) scope, and simple mentions staying
# distinct from obligations.
# ---------------------------------------------------------------------------

def test_coordinated_positive_list_applies_to_every_name():
    result = extract_certifications("ISO 27001 et Qualiopi obligatoires")
    assert set(result) == {"ISO 27001", "Qualiopi"}


def test_coordinated_negation_excludes_every_name():
    result = extract_certifications("ni ISO 27001 ni Qualiopi ne sont requises")
    assert result == []
    mentions = analyze_certification_mentions("ni ISO 27001 ni Qualiopi ne sont requises")
    verdicts = {m.name: m.verdict for m in mentions}
    assert verdicts["ISO 27001"] == "non_obligatoire"
    assert verdicts["Qualiopi"] == "non_obligatoire"


def test_three_way_coordinated_positive_list():
    result = extract_certifications("ISO 27001, HDS et SecNumCloud sont obligatoires")
    assert set(result) == {"ISO 27001", "HDS", "SecNumCloud"}


def test_bare_mention_without_any_trigger_word_is_not_an_obligation():
    text = "Le prestataire actuel dispose d'une certification ISO 27001."
    assert extract_certifications(text) == []
    mentions = analyze_certification_mentions(text)
    assert mentions == [], "a bare mention with no obligation language must not even register as a mention"


def test_recommendation_wording_is_not_promoted_to_a_strict_obligation():
    text = "Une certification Qualiopi est recommandée pour ce marché."
    assert extract_certifications(text) == []
    mentions = analyze_certification_mentions(text)
    assert mentions and mentions[0].verdict == "non_obligatoire"


def test_ou_equivalent_alternative_wording_is_preserved_in_the_clause():
    text = "La certification ISO 27001 ou équivalent est obligatoire."
    result = extract_certifications(text)
    assert "ISO 27001" in result
    mentions = analyze_certification_mentions(text)
    mention = next(m for m in mentions if m.name == "ISO 27001")
    assert "ou équivalent" in mention.clause, "the alternative-acceptance wording must survive in the recorded clause"


# ---------------------------------------------------------------------------
# Group C — contradiction across clauses, and the real AOExtractor
# fallback end to end.
# ---------------------------------------------------------------------------

def test_contradiction_across_two_lines_is_flagged_never_resolved():
    text = "ISO 27001 obligatoire.\nPlus loin dans le document, ISO 27001 est non obligatoire."
    mentions = analyze_certification_mentions(text)
    mandatory, contradictions = resolve_mandatory_certifications(mentions)
    assert "ISO 27001" not in mandatory, "a real contradiction must never be silently resolved either way"
    assert contradictions == ["ISO 27001"]
    # Both original clauses are preserved, not erased.
    clauses = {m.clause for m in mentions if m.name == "ISO 27001"}
    assert len(clauses) == 2


def test_repeated_consistent_mentions_of_the_same_name_are_not_erased():
    text = "ISO 27001 obligatoire.\nRappel : ISO 27001 reste obligatoire pour ce lot."
    mentions = analyze_certification_mentions(text)
    iso_mentions = [m for m in mentions if m.name == "ISO 27001"]
    assert len(iso_mentions) == 2
    mandatory, contradictions = resolve_mandatory_certifications(mentions)
    assert mandatory == ["ISO 27001"]
    assert contradictions == []


def test_ambiguous_conditional_wording_is_neither_mandatory_nor_a_bare_absence():
    text = "Une certification SecNumCloud pourrait être obligatoire selon le lot retenu."
    mentions = analyze_certification_mentions(text)
    assert mentions and mentions[0].verdict == "ambigu"
    mandatory, _ = resolve_mandatory_certifications(mentions)
    assert mandatory == []


SAMPLE_TEXT_WITH_SCOPE_BUG = (
    "Appel d'offres - Portail client\n"
    "Acheteur : Collectivite Exemple\n"
    "Budget : 300 000 euros.\n"
    "Duree : 12 mois.\n"
    "ISO 27001 obligatoire ; Qualiopi non obligatoire\n"
)


def test_real_ao_extractor_fallback_with_disabled_llm_gets_correct_obligations_and_diagnostics(monkeypatch):
    """Exercises the REAL AOExtractor.extract fallback path (LLM
    disabled) end to end — not just the isolated certification_scope
    functions — proving B05-T2's field-resolution contract and B17-T1's
    clause scoping compose correctly."""
    import src.agents.ao_extractor as ao_extractor_module
    from src.core import config
    from src.agents.llm_client import LLMClient

    monkeypatch.setattr(config, "LLM_ENABLED", False)
    monkeypatch.setattr(ao_extractor_module, "ClaudeClient", lambda: LLMClient([]))

    ao = AOExtractor().extract(SAMPLE_TEXT_WITH_SCOPE_BUG)

    assert ao.extraction_status == "fallback_local"
    assert ao.extraction_reason == "llm_disabled"
    assert ao.certifications_obligatoires == ["ISO 27001"]
    assert ao.field_provenance["certifications_obligatoires"] == "fallback"
    names_verdicts = {m.name: m.verdict for m in ao.certification_mentions}
    assert names_verdicts["ISO 27001"] == "obligatoire"
    assert names_verdicts["Qualiopi"] == "non_obligatoire"
    assert ao.certification_contradictions == []


# ---------------------------------------------------------------------------
# Real job path — the AO scope fix reaches a completed job's persisted AO.
# ---------------------------------------------------------------------------

VALID_WEIGHTS = {
    "Adequation expertise": 20, "References similaires": 15, "Disponibilite equipe": 10,
    "Rentabilite estimee": 10, "Faisabilite delai": 10, "Certifications requises": 10,
    "Complexite technique": 5, "Connaissance secteur": 5, "Potentiel commercial": 5,
    "Risque contractuel": 5, "Solidite client": 3, "Valeur strategique": 2,
}


def _csrf_from(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


def _login(client, email, password="Sup3rSecret!"):
    r = client.get("/login")
    csrf = _csrf_from(r.text)
    client.post("/login", data={"email": email, "password": password, "next": "/app", "csrf_token": csrf})
    return csrf


def _configure_capacity(client, csrf, charge: int = 40):
    return client.post("/api/capacity", json={
        "charge_globale_pct": charge, "nombre_projets_en_cours": 1,
        "projets_en_cours": ["Projet test"], "capacites_par_pole": {"Software Engineering": 40},
    }, headers={"X-CSRF-Token": csrf})


def _save_profile(client, csrf):
    return client.put("/api/scoring-config/profile", json={
        "raison_sociale": "ESN de test", "effectif": "10-50", "competences": ["python", "django"], "certifications": [],
    }, headers={"X-CSRF-Token": csrf})


def _save_draft(client, csrf):
    return client.put("/api/scoring-config/policy", json={
        "weights": dict(VALID_WEIGHTS), "threshold_go": 88, "threshold_sous_reserve": 60,
    }, headers={"X-CSRF-Token": csrf})


def _activate(client, csrf):
    return client.post("/api/scoring-config/policy/activate", json={"expected_active_version": None}, headers={"X-CSRF-Token": csrf})


def _configure_account(client, db, email):
    make_active_starter_user(db, email, scoring=False)
    csrf = _login(client, email)
    _configure_capacity(client, csrf)
    _save_profile(client, csrf)
    _save_draft(client, csrf)
    _activate(client, csrf)
    return csrf


def _run_analysis_to_completion(client, text: str, csrf):
    r = client.post("/api/analyze", data={"mode": "paste", "text": text}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    from src.web import jobs
    for _ in range(50):
        job = jobs.get_job(job_id)
        if job.status != "running":
            break
        time.sleep(0.1)
    assert job.status == "done", job.error
    return job


def _install_no_op_search(monkeypatch):
    from src.rag import private_rag_manager
    monkeypatch.setattr(private_rag_manager, "search", lambda db, **kw: [])


def test_real_job_receives_correct_obligations_with_llm_disabled(client, db, monkeypatch):
    from src.core import config
    monkeypatch.setattr(config, "LLM_ENABLED", False)
    _install_no_op_search(monkeypatch)
    csrf = _configure_account(client, db, "certscope@example.com")
    job = _run_analysis_to_completion(client, SAMPLE_TEXT_WITH_SCOPE_BUG, csrf)

    assert job.ao.certifications_obligatoires == ["ISO 27001"]
    names_verdicts = {m.name: m.verdict for m in job.ao.certification_mentions}
    assert names_verdicts["ISO 27001"] == "obligatoire"
    assert names_verdicts["Qualiopi"] == "non_obligatoire"

    from src.web import jobs as jobs_module
    del jobs_module._JOBS[job.id]
    reloaded = jobs_module.get_job(job.id)
    assert reloaded is not None
    assert reloaded.ao.certifications_obligatoires == ["ISO 27001"]
    assert {m.name: m.verdict for m in reloaded.ao.certification_mentions} == names_verdicts
