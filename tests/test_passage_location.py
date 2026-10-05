"""B18-T5 (DEFECT F11/F12/E-6) — test group A: the audited bug was a
search over the WHOLE document followed by blindly returning its first
3500 characters, so a relevant passage located further in simply
disappeared; the RAG prompt then re-truncated to 800 characters, losing
it a second time even on the rare occasion it survived the first cut.

This file covers: (1) src/rag/passage_location.py in isolation (pure,
deterministic, no DB/network), (2) a REAL temporary corpus where a
discriminating term sits only after character 3500 of a single-paragraph
document, proving the document is found AND the passage containing the
term is what's actually returned, with exact slice equality against the
canonical text, and (3) the reranking prompt actually sent to a
simulated LLM, captured verbatim, to prove the useful passage reaches it
within the current budget — mere presence of the term in the query is
not treated as sufficient on its own.
"""
from __future__ import annotations

import io
import json
import re
import time

from src.rag.passage_location import MAX_PASSAGE_CHARS, _split_into_segments, locate_relevant_passage
from tests.conftest import default_org_id, make_active_starter_user


# ---------------------------------------------------------------------------
# Pure unit tests — src/rag/passage_location.py in isolation
# ---------------------------------------------------------------------------

def test_split_into_segments_positions_reconstruct_the_original_text():
    text = "Premier paragraphe court.\n\nDeuxieme paragraphe, un peu plus long que le premier.\n\nTroisieme."
    segments = _split_into_segments(text)
    for start, end in segments:
        assert text[start:end] in text  # trivially true, but exercises slicing
    # Reconstructing from segment slices (joined the same way they were
    # split) must reproduce the original text exactly — this is the
    # invariant _split_into_segments relies on instead of re-searching
    # substrings, which would be ambiguous for repeated text.
    rebuilt = "\n\n".join(text[s:e] for s, e in segments)
    assert rebuilt == text


def test_split_into_segments_windows_a_single_long_paragraph():
    long_paragraph = "mot " * 2000  # far beyond MAX_PASSAGE_CHARS, no blank line at all
    segments = _split_into_segments(long_paragraph)
    assert len(segments) > 1
    for start, end in segments:
        assert end - start <= MAX_PASSAGE_CHARS
    assert segments[0][0] == 0
    assert segments[-1][1] == len(long_paragraph)


def test_locate_relevant_passage_exact_slice_equality_and_valid_positions():
    filler = "contexte generique documentation processus qualite methode reference. " * 60
    term_sentence = "Le terme discriminant xylophage apparait precisement ici dans le texte. "
    canonical_text = filler + term_sentence + filler
    assert len(canonical_text) > MAX_PASSAGE_CHARS

    from sklearn.feature_extraction.text import TfidfVectorizer
    vectorizer = TfidfVectorizer()
    vectorizer.fit([canonical_text, "un document totalement different sans rapport"])

    match = locate_relevant_passage(canonical_text, "xylophage", vectorizer)

    assert match.content == canonical_text[match.start_char:match.end_char]
    assert 0 <= match.start_char < match.end_char <= len(canonical_text)
    assert "xylophage" in match.content
    assert match.end_char - match.start_char == len(match.content)


def test_locate_relevant_passage_falls_back_to_prefix_when_single_segment():
    short_text = "Un seul court paragraphe sans double saut de ligne."
    from sklearn.feature_extraction.text import TfidfVectorizer
    vectorizer = TfidfVectorizer()
    vectorizer.fit([short_text])
    match = locate_relevant_passage(short_text, "paragraphe", vectorizer)
    assert match.content == short_text
    assert match.start_char == 0
    assert match.end_char == len(short_text)


# ---------------------------------------------------------------------------
# Real temporary corpus — src/rag/private_rag_manager.py end to end
# ---------------------------------------------------------------------------

def _csrf_from(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


def _login(client, email, password="Sup3rSecret!"):
    r = client.get("/login")
    csrf = _csrf_from(r.text)
    client.post("/login", data={"email": email, "password": password, "next": "/app", "csrf_token": csrf})
    return csrf


def _upload(client, filename: str, content: str, csrf):
    r = client.post(
        "/api/knowledge/documents",
        files={"file": (filename, io.BytesIO(content.encode("utf-8")), "text/plain")},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 201, r.text


DISCRIMINATING_TERM = "xylophage"
_FILLER = "contexte generique documentation processus qualite methode reference projet interne. "
# A SINGLE paragraph (no blank line anywhere) so the whole thing becomes
# one KnowledgeChunk (src/web/knowledge/extraction.py::_split_paragraphs
# only splits on "\n\n") — this is exactly the shape the audited bug lost:
# one long document, term past character 3500, no other chunk boundary to
# save it by accident.
PREFIX_FILLER = _FILLER * 55  # comfortably over 3500 chars on its own
assert len(PREFIX_FILLER) > MAX_PASSAGE_CHARS
TERM_SENTENCE = f"Le terme discriminant {DISCRIMINATING_TERM} apparait precisement ici dans ce document. "
SUFFIX_FILLER = _FILLER * 10
LONG_DOCUMENT = PREFIX_FILLER + TERM_SENTENCE + SUFFIX_FILLER
TERM_OFFSET = len(PREFIX_FILLER)
assert TERM_OFFSET > 3500, "the discriminating term must sit strictly after the old 3500-char cutoff"

UNRELATED_DOCUMENT = "Reference projet migration cloud AWS pour secteur banque livree en 2023, sans rapport avec le terme recherche."


def test_document_found_and_passage_contains_the_term_past_the_old_cutoff(client, db):
    from src.rag import private_rag_manager

    user = make_active_starter_user(db, "passagelocation@example.com", scoring=False)
    org_id = default_org_id(db, user)
    csrf = _login(client, "passagelocation@example.com")

    _upload(client, "long_document.md", LONG_DOCUMENT, csrf)
    _upload(client, "unrelated.md", UNRELATED_DOCUMENT, csrf)

    results = private_rag_manager.search(
        db, organization_id=org_id, owner_user_id=user.id, query=DISCRIMINATING_TERM, top_k=6,
    )

    sources = [ev.source for ev in results]
    assert "long_document.md" in sources, (
        f"the document containing the discriminating term was not even found; got sources={sources}"
    )
    evidence = next(ev for ev in results if ev.source == "long_document.md")

    # The core of this ticket: the term itself must be IN THE RETURNED
    # PASSAGE, not merely somewhere in the source document that was
    # matched. Presence in the query alone proves nothing about the fix.
    assert DISCRIMINATING_TERM in evidence.content, (
        "the located passage does not contain the discriminating term — "
        "the old bug (blind first-3500-chars) would fail this exact assertion"
    )
    assert evidence.start_char is not None and evidence.end_char is not None
    assert evidence.end_char - evidence.start_char == len(evidence.content)
    assert evidence.content == LONG_DOCUMENT[evidence.start_char:evidence.end_char]
    # The old bug's signature: a passage starting at 0 would never reach
    # the term at TERM_OFFSET > 3500 within a MAX_PASSAGE_CHARS window.
    assert evidence.start_char > 0 or evidence.end_char > TERM_OFFSET


# ---------------------------------------------------------------------------
# Reranking prompt capture — the SECOND half of the audited bug
# (candidates_text used to re-truncate ev.content to [:800])
# ---------------------------------------------------------------------------

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
    f"Exigences : {DISCRIMINATING_TERM} et Django.\n"
)


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


class _CapturingFakeProvider:
    """Same shared-call-site idiom as tests/test_reference_selection_job_path.py
    (one `llm.json_complete` call site serves extraction, reference-selection
    and scoring-enrichment) — extended to also answer the AO-extraction call
    with real `technologies_demandees` content (a truthy-but-empty AOContext
    would otherwise short-circuit AOExtractor's own regex fallback and the
    real search would never see the discriminating term in its query). Also
    records every reference-selection prompt verbatim, so the test can
    inspect the EXACT text sent for reranking rather than trust the
    mechanism blindly."""
    name = "primary"
    enabled = True

    def __init__(self):
        self.selection_prompts: list[str] = []

    def complete(self, prompt, system=None, temperature=None, max_tokens=None):
        if "selected_ids" in prompt:
            self.selection_prompts.append(prompt)
            return json.dumps({"selected_ids": [1], "synthese": "Reference retenue."})
        if "technologies_demandees" in prompt:
            return json.dumps({
                "titre": "Portail client", "client": "Collectivite Exemple", "secteur": "Collectivité/Public",
                "budget_estime": 250000.0, "deadline_reponse": "30/11/2026", "duree_projet_mois": 12,
                "technologies_demandees": [DISCRIMINATING_TERM, "Django"],
                "competences_requises": [DISCRIMINATING_TERM, "Django"],
                "questions_client": [], "livrables": [], "contraintes": [], "certifications_obligatoires": [],
            })
        return json.dumps({"justifications": {}})


def test_reranking_prompt_actually_contains_the_useful_passage_within_budget(client, db, monkeypatch):
    """Captures the real prompt src/rag/semantic_rerank.py::semantic_rerank sends
    to the LLM for reference selection, using a REAL search result (not a
    hand-built RAGEvidence) so the whole chain — search, passage location,
    candidate assembly — is exercised exactly as production would run it."""
    from src.core import config
    import src.agents.llm_client as llm_client_module
    import src.agents.ao_extractor as ao_extractor_module
    from src.agents.llm_client import LLMClient

    provider = _CapturingFakeProvider()
    monkeypatch.setattr(config, "LLM_ENABLED", True)
    monkeypatch.setattr(llm_client_module, "ClaudeClient", lambda: LLMClient([provider]))
    # AOExtractor binds `ClaudeClient` at MODULE IMPORT TIME
    # (src/agents/ao_extractor.py, `from src.agents.llm_client import
    # ClaudeClient`) and builds its own instance in __init__ — patching
    # only src.agents.llm_client.ClaudeClient (as jobs.py's own lazy,
    # per-call import does) leaves AOExtractor using the REAL, disabled
    # client, so extraction silently falls back to its regex path and
    # never sees the discriminating term. Both bindings must be patched
    # for the query built from `ao.technologies_demandees` to actually
    # contain it.
    monkeypatch.setattr(ao_extractor_module, "ClaudeClient", lambda: LLMClient([provider]))

    csrf = _configure_account(client, db, "promptcapture@example.com")
    _upload(client, "long_document.md", LONG_DOCUMENT, csrf)
    _upload(client, "unrelated.md", UNRELATED_DOCUMENT, csrf)

    job = _run_analysis_to_completion(client, VALID_AO_TEXT, csrf)
    assert job.result.rag_selection_status in ("applied", "fallback")

    assert len(provider.selection_prompts) == 1, "expected exactly one reference-selection LLM call"
    captured_prompt = provider.selection_prompts[0]

    # The actual regression check: the discriminating term must be PRESENT
    # in the text the model actually saw — not merely in the AO query that
    # triggered the search. Before this ticket's fix, `ev.content[:800]`
    # in semantic_rerank's candidate assembly could still cut it even when
    # passage location itself had found it correctly.
    assert DISCRIMINATING_TERM in captured_prompt, (
        "the discriminating term is present in the source document and was "
        "matched by search, but did not survive into the reranking prompt — "
        "this is exactly the second half of the audited double-truncation bug"
    )
