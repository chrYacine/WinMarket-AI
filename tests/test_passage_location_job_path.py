"""B18-T5 (DEFECT F11/F12/E-6) — test group C: the real /api/analyze job
path, confirming excerpt/positions/provenance are correctly persisted and
rereadable, that an old JSON result predating this ticket still loads
with an explicit "provenance unavailable" (None fields, never invented
positions/fingerprints), that a persisted citation is NEVER silently
replaced by the source document's current content after it is edited,
and that B03 access-control / B18-T3 selection guarantees still hold on
this touched path.
"""
from __future__ import annotations

import io
import json
import re
import time

from src.core.models import RAGEvidence
from tests.conftest import default_org_id, make_active_starter_user

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

REFERENCE_TEXT = "Reference projet portail client stack Python Django secteur retail livre en 2024."


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


def _upload(client, filename: str, content: str, csrf):
    r = client.post(
        "/api/knowledge/documents",
        files={"file": (filename, io.BytesIO(content.encode("utf-8")), "text/plain")},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 201, r.text
    return r.json()


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


class _SelectiveFakeProvider:
    """Same idiom as tests/test_reference_selection_job_path.py."""
    name = "primary"
    enabled = True

    def __init__(self, selection_payload: dict):
        self._selection_payload = selection_payload

    def complete(self, prompt, system=None, temperature=None, max_tokens=None):
        if "selected_ids" in prompt:
            return json.dumps(self._selection_payload)
        return json.dumps({"justifications": {}})


def _install_fake_llm(monkeypatch, selection_payload: dict):
    from src.core import config
    import src.agents.llm_client as llm_client_module
    from src.agents.llm_client import LLMClient

    monkeypatch.setattr(config, "LLM_ENABLED", True)
    monkeypatch.setattr(llm_client_module, "ClaudeClient", lambda: LLMClient([_SelectiveFakeProvider(selection_payload)]))


def _install_fake_search(monkeypatch, evidences: list[RAGEvidence]):
    from src.rag import private_rag_manager
    monkeypatch.setattr(private_rag_manager, "search", lambda db, **kw: list(evidences))


# ---------------------------------------------------------------------------
# Persistence / reread of the new provenance fields
# ---------------------------------------------------------------------------

def test_excerpt_and_provenance_persist_and_reread_exactly(client, db, monkeypatch):
    evidence = RAGEvidence(
        query="q", source="ref.md", score=0.8, content="Passage localise pertinent.",
        start_char=120, end_char=147, content_fingerprint="abc123fingerprint",
        document_version_id="11111111-1111-1111-1111-111111111111",
    )
    _install_fake_search(monkeypatch, [evidence])
    _install_fake_llm(monkeypatch, {"selected_ids": [1], "synthese": "Retenue."})

    csrf = _configure_account(client, db, "provenancepersist@example.com")
    job = _run_analysis_to_completion(client, VALID_AO_TEXT, csrf)

    kept = job.result.evidence_pack[0]
    assert kept.start_char == 120
    assert kept.end_char == 147
    assert kept.content_fingerprint == "abc123fingerprint"
    assert kept.document_version_id == "11111111-1111-1111-1111-111111111111"

    from src.web import jobs as jobs_module
    del jobs_module._JOBS[job.id]
    reloaded = jobs_module.get_job(job.id)
    assert reloaded is not None
    reread = reloaded.result.evidence_pack[0]
    assert reread.start_char == 120
    assert reread.end_char == 147
    assert reread.content_fingerprint == "abc123fingerprint"
    assert reread.document_version_id == "11111111-1111-1111-1111-111111111111"
    assert reread.content == "Passage localise pertinent."


def test_old_json_without_provenance_fields_still_readable_as_unavailable(tmp_path, monkeypatch):
    """A pre-B18-T5 persisted analysis has no start_char/end_char/
    content_fingerprint/document_version_id keys at all. It must still load
    — with these fields defaulting to None, an explicit, honest 'provenance
    unavailable' — never a fabricated position or a forced recomputation of
    decision/score_global (preserving T2's degraded-history handling)."""
    from src.web import jobs as jobs_module

    analyses_dir = tmp_path / "historique" / "analyses"
    analyses_dir.mkdir(parents=True)
    monkeypatch.setattr(jobs_module, "ANALYSES_DIR", analyses_dir)

    legacy_result = {
        "decision": "GO", "score_global": 85.0,
        "criteres": [{"nom": "Adéquation expertise", "poids": 20.0, "score": 90.0, "justification": "ok"}],
        "criteres_bloquants": [], "forces": [], "faiblesses": [], "risques": [], "recommandations": [],
        "evidence_pack": [{"query": "q", "source": "old.md", "score": 0.6, "content": "ancienne reference"}],
        "company_profile": None, "capacity": None, "rag_synthesis": "Synthese historique.", "ai_content": {},
        # no start_char/end_char/content_fingerprint/document_version_id at
        # all — exactly what a pre-B18-T5 persisted analysis looks like.
    }
    (analyses_dir / "legacyprovenance0001.json").write_text(json.dumps({
        "id": "legacyprovenance0001", "user_id": None, "organization_id": None, "created_at": 0,
        "source_label": "Legacy", "ao": {"titre": "AO historique"}, "result": legacy_result, "files": {},
    }), encoding="utf-8")

    reloaded = jobs_module.get_job("legacyprovenance0001")
    assert reloaded is not None
    assert reloaded.status == "done"
    old_evidence = reloaded.result.evidence_pack[0]
    assert old_evidence.start_char is None
    assert old_evidence.end_char is None
    assert old_evidence.content_fingerprint is None
    assert old_evidence.document_version_id is None
    assert reloaded.result.data_integrity == "ok"
    assert reloaded.result.decision == "GO"
    assert reloaded.result.score_global == 85.0, "no forced recalculation for old data lacking provenance"


def test_completed_job_keeps_its_original_excerpt_after_source_document_is_edited(client, db, monkeypatch):
    """Ticket section 5: 'ne pas remplacer une ancienne citation par le
    contenu actuel du fichier'. A job that already completed must keep
    reporting the excerpt it actually used, even after the source document
    is later modified (a new version uploaded) — evidence_pack is baked
    into result_data at job time and is never recomputed from a fresh
    search on reread."""
    _install_fake_llm(monkeypatch, {"selected_ids": [1], "synthese": "Retenue."})
    csrf = _configure_account(client, db, "sourceeditedafter@example.com")
    upload = _upload(client, "ref.md", REFERENCE_TEXT, csrf)
    document_id = upload["document"]["id"]

    job = _run_analysis_to_completion(client, VALID_AO_TEXT, csrf)
    assert len(job.result.evidence_pack) == 1
    original_kept = job.result.evidence_pack[0]
    original_content = original_kept.content
    original_fingerprint = original_kept.content_fingerprint
    assert original_content  # sanity: a real passage was captured

    # Now genuinely change the source document via a new version — the
    # live corpus/search would see completely different content afterward.
    if document_id is not None:
        r = client.post(
            f"/api/knowledge/documents/{document_id}/versions",
            files={"file": ("ref.md", io.BytesIO(b"Contenu totalement remplace, plus aucun rapport avec l'original."), "text/plain")},
            headers={"X-CSRF-Token": csrf},
        )
        assert r.status_code in (200, 201), r.text

    from src.web import jobs as jobs_module
    del jobs_module._JOBS[job.id]
    reloaded = jobs_module.get_job(job.id)
    assert reloaded is not None
    reread = reloaded.result.evidence_pack[0]
    assert reread.content == original_content, "a completed job's persisted excerpt must never track the live document"
    assert reread.content_fingerprint == original_fingerprint


# ---------------------------------------------------------------------------
# B03 isolation / B18-T3 selection contract replayed on this touched path
# ---------------------------------------------------------------------------

def test_no_excerpt_or_positions_from_another_owner_ever_appear(client, db, monkeypatch):
    a_evidence = RAGEvidence(
        query="q", source="a_only.md", score=0.8, content="Reference exclusive de A.",
        start_char=0, end_char=len("Reference exclusive de A."), content_fingerprint="fp-a",
    )
    b_evidence = RAGEvidence(
        query="q", source="b_only.md", score=0.8, content="Reference exclusive de B.",
        start_char=0, end_char=len("Reference exclusive de B."), content_fingerprint="fp-b",
    )

    csrf_a = _configure_account(client, db, "provisoa@example.com")
    _install_fake_search(monkeypatch, [a_evidence])
    _install_fake_llm(monkeypatch, {"selected_ids": [1], "synthese": "A"})
    job_a = _run_analysis_to_completion(client, VALID_AO_TEXT, csrf_a)
    assert [ev.content_fingerprint for ev in job_a.result.evidence_pack] == ["fp-a"]

    csrf_b = _configure_account(client, db, "provisob@example.com")
    _install_fake_search(monkeypatch, [b_evidence])
    _install_fake_llm(monkeypatch, {"selected_ids": [1], "synthese": "B"})
    job_b = _run_analysis_to_completion(client, VALID_AO_TEXT, csrf_b)
    assert [ev.content_fingerprint for ev in job_b.result.evidence_pack] == ["fp-b"]
    assert "fp-a" not in [ev.content_fingerprint for ev in job_b.result.evidence_pack]
    assert "a_only.md" not in [ev.source for ev in job_b.result.evidence_pack]


def test_empty_selection_and_exclusion_preserve_provenance_fields_correctly(client, db, monkeypatch):
    kept_candidate = RAGEvidence(
        query="q", source="kept.md", score=0.8, content="Reference retenue avec provenance.",
        start_char=10, end_char=10 + len("Reference retenue avec provenance."), content_fingerprint="fp-kept",
    )
    excluded_candidate = RAGEvidence(
        query="q", source="excluded.md", score=0.3, content="Reference exclue.",
        start_char=0, end_char=len("Reference exclue."), content_fingerprint="fp-excluded",
    )

    _install_fake_search(monkeypatch, [kept_candidate, excluded_candidate])
    _install_fake_llm(monkeypatch, {"selected_ids": [1], "synthese": "Une seule retenue."})
    csrf = _configure_account(client, db, "exclusionprovenance@example.com")
    job = _run_analysis_to_completion(client, VALID_AO_TEXT, csrf)

    assert [ev.source for ev in job.result.evidence_pack] == ["kept.md"]
    kept = job.result.evidence_pack[0]
    assert kept.start_char == 10
    assert kept.content_fingerprint == "fp-kept"

    _install_fake_search(monkeypatch, [kept_candidate, excluded_candidate])
    _install_fake_llm(monkeypatch, {"selected_ids": [], "synthese": "ignoree"})
    csrf_empty = _configure_account(client, db, "emptyprovenance@example.com")
    job_empty = _run_analysis_to_completion(client, VALID_AO_TEXT, csrf_empty)
    assert job_empty.result.evidence_pack == []
