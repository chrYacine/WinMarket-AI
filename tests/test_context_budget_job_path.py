"""B18-T6 (complement to B18-T5, related to B15) — test group C: the real
/api/analyze job path, confirming the bounded excerpt actually presented
to the LLM is exactly what gets persisted/reread (no contradiction),
that full-document identity/deduplication/scores stay untouched by the
extra bounding pass, that T3's selection contract (unpresented id,
empty selection, disabled LLM) still holds unchanged, and that T6 adds
no additional LLM call or JSON-repair attempt of its own.
"""
from __future__ import annotations

import json
import re
import time

from src.core.models import RAGEvidence
from src.core.reference_identity import deduplicate_evidences
from src.rag.context_budget import get_context_budget
from tests.conftest import make_active_starter_user

VALID_WEIGHTS = {
    "Adequation expertise": 20, "References similaires": 15, "Disponibilite equipe": 10,
    "Rentabilite estimee": 10, "Faisabilite delai": 10, "Certifications requises": 10,
    "Complexite technique": 5, "Connaissance secteur": 5, "Potentiel commercial": 5,
    "Risque contractuel": 5, "Solidite client": 3, "Valeur strategique": 2,
}

VALID_AO_TEXT = (
    "Appel d'offres - Portail client\n"
    "Acheteur : Collectivite Exemple\n"
    "Le prestataire realisera le portail et ses livrables.\n"
    "Budget : 250 000 euros. Date limite : 30/11/2026.\n"
    "Exigences : Python et Django.\n"
)

LONG_CONTENT = ("Reference projet portail client stack Python Django secteur retail. " * 60)


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


class _CountingFakeProvider:
    """Same shared-call-site idiom as tests/test_reference_selection_job_path.py,
    extended to COUNT every call made — this ticket must not introduce any
    additional LLM call (a retry, a JSON-repair attempt) beyond what T3
    already made for reference selection."""
    name = "primary"
    enabled = True

    def __init__(self, selection_payload):
        self._selection_payload = selection_payload
        self.total_calls = 0
        self.selection_calls = 0

    def complete(self, prompt, system=None, temperature=None, max_tokens=None):
        self.total_calls += 1
        if "selected_ids" in prompt:
            self.selection_calls += 1
            return json.dumps(self._selection_payload)
        return json.dumps({"justifications": {}})


def _install_fake_llm(monkeypatch, selection_payload):
    from src.core import config
    import src.agents.llm_client as llm_client_module
    from src.agents.llm_client import LLMClient

    provider = _CountingFakeProvider(selection_payload)
    monkeypatch.setattr(config, "LLM_ENABLED", True)
    monkeypatch.setattr(llm_client_module, "ClaudeClient", lambda: LLMClient([provider]))
    return provider


def _install_fake_search(monkeypatch, evidences: list[RAGEvidence]):
    from src.rag import private_rag_manager
    monkeypatch.setattr(private_rag_manager, "search", lambda db, **kw: list(evidences))


# ---------------------------------------------------------------------------
# Presented context == persisted/reread evidence, no contradiction.
# ---------------------------------------------------------------------------

def test_persisted_evidence_matches_exactly_what_was_bounded_and_presented(client, db, monkeypatch):
    evidence = RAGEvidence(
        query="q", source="ref.md", score=0.8, content=LONG_CONTENT,
        start_char=0, end_char=len(LONG_CONTENT), content_fingerprint="fp-fixed", document_version_id="v1",
        duplicate_sources=["copy.md"],
    )
    _install_fake_search(monkeypatch, [evidence])
    provider = _install_fake_llm(monkeypatch, {"selected_ids": [1], "synthese": "Retenue."})
    csrf = _configure_account(client, db, "budgetpersist@example.com")
    job = _run_analysis_to_completion(client, VALID_AO_TEXT, csrf)

    assert provider.selection_calls == 1
    kept = job.result.evidence_pack[0]
    budget = get_context_budget()
    assert len(kept.content) <= budget.max_excerpt_chars
    assert len(kept.content) < len(LONG_CONTENT), "sanity: bounding actually reduced the excerpt in this test"
    assert kept.end_char - kept.start_char == len(kept.content)
    assert kept.content == LONG_CONTENT[kept.start_char:kept.end_char]
    # Full-document identity/traceability untouched by bounding.
    assert kept.content_fingerprint == "fp-fixed"
    assert kept.document_version_id == "v1"
    assert kept.duplicate_sources == ["copy.md"]
    assert kept.score == 0.8

    from src.web import jobs as jobs_module
    del jobs_module._JOBS[job.id]
    reloaded = jobs_module.get_job(job.id)
    assert reloaded is not None
    reread = reloaded.result.evidence_pack[0]
    assert reread.content == kept.content
    assert reread.start_char == kept.start_char
    assert reread.end_char == kept.end_char
    assert reread.content_fingerprint == "fp-fixed"


# ---------------------------------------------------------------------------
# Dedup/scoring preserved at fixed candidates/selection despite bounding.
# ---------------------------------------------------------------------------

def test_dedup_by_full_fingerprint_survives_bounding_to_different_excerpts():
    """Two evidences sharing the same content_fingerprint (same full
    document) but bounded to DIFFERENT excerpts (as would happen if their
    localized T5 passages differed) must still be recognized as one
    document by src.core.reference_identity.deduplicate_evidences — T6
    must never recompute identity on the reduced excerpt."""
    fp = "fp-shared-full-document"
    ev1 = RAGEvidence(query="q", source="original.md", score=0.6, content="Premier extrait borne differemment.", content_fingerprint=fp)
    ev2 = RAGEvidence(query="q", source="renamed.md", score=0.9, content="Second extrait borne autrement, plus loin.", content_fingerprint=fp)
    deduped = deduplicate_evidences([ev1, ev2])
    assert len(deduped) == 1
    assert deduped[0].source == "renamed.md"  # best score kept
    assert deduped[0].duplicate_sources == ["original.md"]


def test_score_and_decision_unchanged_when_only_excerpt_length_shrinks(client, db, monkeypatch):
    """Fixed candidate list, fixed (simulated) selection — reducing the
    bounded excerpt length must not perturb score_global/decision, which
    depend only on `.score`/selection, never on `.content` length."""
    short_evidence = RAGEvidence(query="q", source="ref.md", score=0.8, content="Extrait deja court.")
    long_evidence = RAGEvidence(query="q", source="ref.md", score=0.8, content=LONG_CONTENT)

    _install_fake_search(monkeypatch, [short_evidence])
    _install_fake_llm(monkeypatch, {"selected_ids": [1], "synthese": "Retenue."})
    csrf_short = _configure_account(client, db, "budgetshort@example.com")
    job_short = _run_analysis_to_completion(client, VALID_AO_TEXT, csrf_short)

    _install_fake_search(monkeypatch, [long_evidence])
    _install_fake_llm(monkeypatch, {"selected_ids": [1], "synthese": "Retenue."})
    csrf_long = _configure_account(client, db, "budgetlong@example.com")
    job_long = _run_analysis_to_completion(client, VALID_AO_TEXT, csrf_long)

    assert job_short.result.score_global == job_long.result.score_global
    assert job_short.result.decision == job_long.result.decision
    assert len(job_short.result.evidence_pack[0].content) != len(job_long.result.evidence_pack[0].content)


# ---------------------------------------------------------------------------
# T3 contract replayed unchanged: unpresented id, empty selection,
# disabled LLM.
# ---------------------------------------------------------------------------

def test_id_beyond_presented_candidates_still_falls_back(client, db, monkeypatch):
    evidence = RAGEvidence(query="q", source="ref.md", score=0.8, content=LONG_CONTENT)
    _install_fake_search(monkeypatch, [evidence])
    _install_fake_llm(monkeypatch, {"selected_ids": [5], "synthese": "invalide"})
    csrf = _configure_account(client, db, "budgetunknownid@example.com")
    job = _run_analysis_to_completion(client, VALID_AO_TEXT, csrf)

    assert job.result.rag_selection_status == "fallback"
    assert job.result.rag_selection_reason == "unknown_id"
    # Fallback keeps the ORIGINAL, unbounded evidence (existing T3/T5
    # behavior, deliberately untouched by T6).
    assert job.result.evidence_pack[0].content == LONG_CONTENT


def test_empty_selection_still_yields_no_evidence_at_all(client, db, monkeypatch):
    evidence = RAGEvidence(query="q", source="ref.md", score=0.8, content=LONG_CONTENT)
    _install_fake_search(monkeypatch, [evidence])
    _install_fake_llm(monkeypatch, {"selected_ids": [], "synthese": "ignoree"})
    csrf = _configure_account(client, db, "budgetempty@example.com")
    job = _run_analysis_to_completion(client, VALID_AO_TEXT, csrf)

    assert job.result.evidence_pack == []
    assert job.result.rag_selection_status == "applied"


def test_disabled_llm_skips_budgeting_entirely_and_keeps_original_evidence(client, db, monkeypatch):
    from src.core import config
    from src.rag import private_rag_manager

    evidence = RAGEvidence(query="q", source="ref.md", score=0.8, content=LONG_CONTENT)
    monkeypatch.setattr(private_rag_manager, "search", lambda db, **kw: [evidence])
    monkeypatch.setattr(config, "LLM_ENABLED", False)
    csrf = _configure_account(client, db, "budgetdisabled@example.com")
    job = _run_analysis_to_completion(client, VALID_AO_TEXT, csrf)

    assert job.result.rag_selection_status == "not_attempted"
    assert job.result.evidence_pack[0].content == LONG_CONTENT, "not_attempted returns evidences untouched, never bounded"


# ---------------------------------------------------------------------------
# No additional LLM call introduced by T6.
# ---------------------------------------------------------------------------

def test_context_budgeting_adds_no_extra_llm_call(client, db, monkeypatch):
    evidence = RAGEvidence(query="q", source="ref.md", score=0.8, content=LONG_CONTENT)
    _install_fake_search(monkeypatch, [evidence])
    provider = _install_fake_llm(monkeypatch, {"selected_ids": [1], "synthese": "Retenue."})
    csrf = _configure_account(client, db, "budgetcallcount@example.com")
    _run_analysis_to_completion(client, VALID_AO_TEXT, csrf)

    assert provider.selection_calls == 1, "exactly one reference-selection call, no retry/repair introduced by T6"
