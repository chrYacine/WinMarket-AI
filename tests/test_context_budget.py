"""B18-T6 (complement to B18-T5, related to B15) — test groups A and B:
the reference-selection prompt's candidate/document-text assembly must
stay within a centralized, validated character budget (6 candidates x
800 chars/excerpt, 4800 chars total block by default), without
reintroducing the audited "lost passage" defect this bounding replaces
(a blind `content[:800]` head-cut). Group A exercises the FULL real
job/search/reranking path end to end (not just isolated helpers) with
six real long documents and a long source name; Group B proves a late
discriminating term survives its negation/qualifier intact through the
final bounded excerpt.
"""
from __future__ import annotations

import io
import json
import re
import time

import pytest

from src.core.models import RAGEvidence
from src.rag.context_budget import (
    InvalidContextBudgetError,
    allocate_excerpt_budgets,
    bound_candidate_excerpt,
    build_candidates_block,
    build_header,
    get_context_budget,
    truncate_label,
)
from src.rag.passage_location import fit_window_to_budget
from tests.conftest import default_org_id, make_active_starter_user

_PROMPT_PATH = None  # resolved lazily below to avoid importing at collection time on unrelated failures


def _extract_candidates_block(prompt: str) -> str:
    """Locates the exact substring that reached `{{CANDIDATES_TEXT}}` in
    the REAL template file (src/rag/prompts/reference_selection.txt),
    using short, ao_text/candidates_text-independent anchors taken from
    the template itself — never a hardcoded guess at its wording."""
    from src.rag.reference_selection import _PROMPT_PATH as prompt_path

    template = prompt_path.read_text(encoding="utf-8")
    raw_prefix, raw_suffix = template.split("{{CANDIDATES_TEXT}}")
    prefix_anchor = raw_prefix[-60:]
    suffix_anchor = raw_suffix[:60]
    start = prompt.index(prefix_anchor) + len(prefix_anchor)
    end = prompt.index(suffix_anchor, start)
    return prompt[start:end]


def _split_candidate_chunks(candidates_block: str) -> list[str]:
    """Splits the assembled block back into one chunk per candidate
    (header + excerpt), using the exact header pattern
    context_budget.build_header produces."""
    header_re = re.compile(r"--- Référence \d+ \([^\n]*\) ---\n")
    starts = [m.start() for m in header_re.finditer(candidates_block)]
    starts.append(len(candidates_block))
    return [candidates_block[starts[i]:starts[i + 1]] for i in range(len(starts) - 1)]


# ---------------------------------------------------------------------------
# Unit level — src/rag/context_budget.py in isolation
# ---------------------------------------------------------------------------

def test_default_budget_matches_ticket_values():
    budget = get_context_budget()
    assert budget.max_candidates == 6
    assert budget.max_excerpt_chars == 800
    assert budget.max_document_block_chars == 4800


@pytest.mark.parametrize("attr,bad_value", [
    ("RAG_SELECTION_MAX_CANDIDATES", 0),
    ("RAG_SELECTION_MAX_CANDIDATES", -3),
    ("RAG_SELECTION_MAX_EXCERPT_CHARS", 0),
    ("RAG_SELECTION_MAX_DOCUMENT_BLOCK_CHARS", 0),
])
def test_non_positive_config_values_are_rejected(monkeypatch, attr, bad_value):
    from src.core import config
    monkeypatch.setattr(config, attr, bad_value)
    with pytest.raises(InvalidContextBudgetError):
        get_context_budget()


def test_block_smaller_than_excerpt_cap_is_rejected(monkeypatch):
    from src.core import config
    monkeypatch.setattr(config, "RAG_SELECTION_MAX_DOCUMENT_BLOCK_CHARS", 100)
    monkeypatch.setattr(config, "RAG_SELECTION_MAX_EXCERPT_CHARS", 800)
    with pytest.raises(InvalidContextBudgetError):
        get_context_budget()


def test_excerpt_cap_larger_than_max_passage_chars_is_rejected(monkeypatch):
    from src.core import config
    monkeypatch.setattr(config, "RAG_SELECTION_MAX_EXCERPT_CHARS", 4000)
    monkeypatch.setattr(config, "RAG_SELECTION_MAX_DOCUMENT_BLOCK_CHARS", 24000)
    with pytest.raises(InvalidContextBudgetError):
        get_context_budget()


def test_truncate_label_bounds_long_source_names_without_touching_short_ones():
    short = "reference.md"
    assert truncate_label(short) == short
    long_name = "un_nom_de_fichier_extremement_long_" * 5 + ".md"
    truncated = truncate_label(long_name)
    assert len(truncated) <= 60
    assert truncated.endswith("…")


def test_allocate_excerpt_budgets_never_assumes_n_times_cap_always_fits():
    """Six candidates with a completely normal source name: the naive
    assumption "6 x 800 = 4800 always fits" ignores header overhead —
    this must not silently overflow the block."""
    budget = get_context_budget()
    candidates = [
        RAGEvidence(query="q", source=f"reference_{i}.md", score=0.5, content="x" * 3500)
        for i in range(6)
    ]
    allocations = allocate_excerpt_budgets(candidates, budget)
    assert len(allocations) == 6
    headers_total = sum(len(build_header(i + 1, ev.source)) for i, ev in enumerate(candidates))
    assert headers_total + sum(allocations) <= budget.max_document_block_chars
    for a in allocations:
        assert 0 <= a <= budget.max_excerpt_chars


def test_allocate_excerpt_budgets_a_very_long_source_name_does_not_crush_the_others():
    budget = get_context_budget()
    long_name = "nom_de_fichier_extremement_long_" * 10 + ".md"
    candidates = [
        RAGEvidence(query="q", source=long_name, score=0.5, content="x" * 3500),
        RAGEvidence(query="q", source="normal.md", score=0.5, content="x" * 3500),
    ]
    allocations = allocate_excerpt_budgets(candidates, budget)
    # The long name's header is truncated (build_header/truncate_label), so
    # its own budget line stays reasonable and doesn't starve the other
    # candidate down to zero.
    assert allocations[1] > 0


def test_bound_candidate_excerpt_returns_same_object_when_already_within_budget():
    ev = RAGEvidence(query="q", source="short.md", score=0.5, content="Un contenu court.")
    result = bound_candidate_excerpt(ev, 800)
    assert result is ev


def test_bound_candidate_excerpt_translates_positions_to_canonical_offsets():
    content = ("Contexte non pertinent. " * 40) + "Le terme cible important apparait ici precisement dans le texte."
    ev = RAGEvidence(query="terme cible", source="doc.md", score=0.6, content=content, start_char=1000, end_char=1000 + len(content))
    bounded = bound_candidate_excerpt(ev, 100)
    assert len(bounded.content) <= 100
    assert "terme" in bounded.content or "cible" in bounded.content
    assert bounded.start_char is not None and bounded.end_char is not None
    assert bounded.end_char - bounded.start_char == len(bounded.content)
    # Canonical text here is `content` itself, offset by the evidence's own
    # start_char (1000) — the translation this function is responsible for.
    canonical = "x" * 1000 + content
    assert bounded.content == canonical[bounded.start_char:bounded.end_char]


def test_bound_candidate_excerpt_keeps_positions_none_when_evidence_has_none():
    ev = RAGEvidence(query="q", source="doc.md", score=0.5, content="x" * 3500)
    bounded = bound_candidate_excerpt(ev, 100)
    assert bounded.start_char is None
    assert bounded.end_char is None


def test_build_candidates_block_enforces_all_three_limits_at_once():
    budget = get_context_budget()
    candidates = [
        RAGEvidence(query="q", source=f"reference_{i}.md", score=0.5, content=("phrase pertinente ici. " * 200))
        for i in range(6)
    ]
    candidates_text, bounded = build_candidates_block(candidates, budget)
    assert len(candidates_text) <= budget.max_document_block_chars
    assert len(bounded) == 6
    for ev in bounded:
        assert len(ev.content) <= budget.max_excerpt_chars


# ---------------------------------------------------------------------------
# fit_window_to_budget — negation/short-clause boundary (Group B, unit level)
# ---------------------------------------------------------------------------

def test_negation_clause_that_a_naive_cut_would_split_is_kept_whole():
    prefix = "Introduction generale du contexte technique avant la clause importante. "
    clause = "Le module n'est pas compatible avec l'existant."
    suffix = " Puis la suite continue sur un tout autre sujet sans rapport direct avec le reste."
    text = prefix + clause + suffix
    naive_cut_point = len(prefix) + 10
    assert len(prefix) < naive_cut_point < len(prefix) + len(clause), "sanity: a blind slice would land mid-clause"

    start, end = fit_window_to_budget(text, "compatible", max_chars=len(clause) + 5)
    excerpt = text[start:end]
    assert clause in excerpt, "the whole negation clause must survive together, never cut through its middle"


def test_fit_window_to_budget_prefers_clause_containing_the_query_term():
    text = (
        "Premiere phrase sans rapport avec la recherche menee ici. "
        "Deuxieme phrase, elle aussi hors sujet et sans grand interet. "
        "Troisieme phrase qui mentionne enfin le terme xylophage recherche. "
        "Quatrieme phrase de conclusion, hors sujet egalement."
    )
    start, end = fit_window_to_budget(text, "xylophage", max_chars=80)
    assert "xylophage" in text[start:end]


# ---------------------------------------------------------------------------
# Real job path helpers (shared with tests/test_passage_location.py idiom)
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


def _upload(client, filename: str, content: str, csrf):
    r = client.post(
        "/api/knowledge/documents",
        files={"file": (filename, io.BytesIO(content.encode("utf-8")), "text/plain")},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 201, r.text


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
    """Same shared-call-site idiom as tests/test_passage_location.py —
    answers extraction, reference-selection and scoring-enrichment calls
    differently, records every reference-selection prompt AND counts every
    call made (ticket section: "vérifier qu'aucune nouvelle tentative de
    réparation JSON ni aucun appel additionnel n'est introduit par T6")."""
    name = "primary"
    enabled = True

    def __init__(self, technologies):
        self._technologies = technologies
        self.selection_prompts: list[str] = []
        self.total_calls = 0

    def complete(self, prompt, system=None, temperature=None, max_tokens=None):
        self.total_calls += 1
        if "selected_ids" in prompt:
            self.selection_prompts.append(prompt)
            return json.dumps({"selected_ids": [1], "synthese": "Reference retenue."})
        if "technologies_demandees" in prompt:
            return json.dumps({
                "titre": "Portail client", "client": "Collectivite Exemple", "secteur": "Collectivité/Public",
                "budget_estime": 250000.0, "deadline_reponse": "30/11/2026", "duree_projet_mois": 12,
                "technologies_demandees": self._technologies, "competences_requises": self._technologies,
                "questions_client": [], "livrables": [], "contraintes": [], "certifications_obligatoires": [],
            })
        return json.dumps({"justifications": {}})


def _install_fake_llm(monkeypatch, technologies):
    from src.core import config
    import src.agents.llm_client as llm_client_module
    import src.agents.ao_extractor as ao_extractor_module
    from src.agents.llm_client import LLMClient

    provider = _CapturingFakeProvider(technologies)
    monkeypatch.setattr(config, "LLM_ENABLED", True)
    monkeypatch.setattr(llm_client_module, "ClaudeClient", lambda: LLMClient([provider]))
    monkeypatch.setattr(ao_extractor_module, "ClaudeClient", lambda: LLMClient([provider]))
    return provider


# ---------------------------------------------------------------------------
# Group A — six real long documents + one long source name, real search,
# real reranking, captured real prompt.
# ---------------------------------------------------------------------------

_TOPIC_TERMS = ["portail", "facturation", "logistique", "comptabilite", "ressources", "cybersecurite"]


def _long_document(term: str) -> str:
    filler = f"reference projet interne relative au theme {term} avec beaucoup de details operationnels. " * 45
    assert len(filler) > 3500
    return filler


LONG_SOURCE_NAME = "dossier_reference_client_tres_detaille_avec_un_nom_de_fichier_particulierement_long_" * 3 + ".md"


def test_six_long_documents_and_a_long_source_name_respect_all_caps_end_to_end(client, db, monkeypatch):
    provider = _install_fake_llm(monkeypatch, _TOPIC_TERMS)
    csrf = _configure_account(client, db, "budgetgroupa@example.com")

    for i, term in enumerate(_TOPIC_TERMS):
        filename = LONG_SOURCE_NAME if i == 0 else f"reference_{term}.md"
        _upload(client, filename, _long_document(term), csrf)

    ao_text = (
        "Appel d'offres - Modernisation SI\n"
        "Acheteur : Collectivite Exemple\n"
        "Exigences : " + " ".join(_TOPIC_TERMS) + ".\n"
        "Budget : 250 000 euros. Date limite : 30/11/2026.\n"
    )
    job = _run_analysis_to_completion(client, ao_text, csrf)
    assert job.result.rag_selection_status in ("applied", "fallback")

    assert len(provider.selection_prompts) == 1
    prompt = provider.selection_prompts[0]
    candidates_block = _extract_candidates_block(prompt)
    chunks = _split_candidate_chunks(candidates_block)

    from src.rag.context_budget import get_context_budget
    budget = get_context_budget()

    assert len(chunks) <= budget.max_candidates, (
        f"expected at most {budget.max_candidates} candidates presented, got {len(chunks)}"
    )
    assert len(candidates_block) <= budget.max_document_block_chars, (
        f"assembled document block is {len(candidates_block)} chars, budget is {budget.max_document_block_chars} "
        "— header overhead must not push the block over the cap"
    )
    for chunk in chunks:
        header_end = chunk.index("\n") + 1
        excerpt = chunk[header_end:]
        assert len(excerpt) <= budget.max_excerpt_chars, (
            f"a single candidate excerpt is {len(excerpt)} chars, budget is {budget.max_excerpt_chars}"
        )


# ---------------------------------------------------------------------------
# Group B — late discriminating term, far from the T5 window's own start,
# with an adjacent negation, must survive the T6 bounding intact.
# ---------------------------------------------------------------------------

_FILLER_SENTENCE = "Contexte generique et sans rapport avec la clause suivante, uniquement du remplissage. "
NEGATION_CLAUSE = "Le module de facturation n'est pas compatible avec l'ancien systeme existant."

# Enough filler BEFORE the target to push it past the first T5 window
# (3500 chars) and, within the SECOND window, still well past its own
# start (not at offset 0) — this is the "loin du debut de la fenetre T5"
# requirement, verified explicitly below rather than assumed.
_LEAD_FILLER = _FILLER_SENTENCE * 65  # > 3500 + comfortably into the 2nd window
_TRAIL_FILLER = _FILLER_SENTENCE * 20
LONG_DOCUMENT_WITH_NEGATION = _LEAD_FILLER + NEGATION_CLAUSE + " " + _TRAIL_FILLER
assert len(_LEAD_FILLER) > 3500


def test_late_negation_reaches_the_bounded_prompt_excerpt_with_exact_positions(client, db, monkeypatch):
    from src.rag import private_rag_manager

    user = make_active_starter_user(db, "negationbudget@example.com", scoring=False)
    org_id = default_org_id(db, user)
    csrf = _login(client, "negationbudget@example.com")
    _upload(client, "facturation.md", LONG_DOCUMENT_WITH_NEGATION, csrf)
    _upload(client, "unrelated.md", "Un document totalement different sur un tout autre sujet, sans aucun rapport avec ce qui precede.", csrf)

    results = private_rag_manager.search(
        db, organization_id=org_id, owner_user_id=user.id, query="facturation compatible", top_k=6,
    )
    evidence = next(ev for ev in results if ev.source == "facturation.md")
    assert NEGATION_CLAUSE in evidence.content, "sanity: the T5 located passage must contain the negation at all"
    local_offset = evidence.content.index(NEGATION_CLAUSE)
    assert local_offset > 500, (
        f"the clause sits at offset {local_offset} inside the T5 window — too close to its start to exercise "
        "the 'loin du debut de la fenetre T5' requirement"
    )
    assert evidence.start_char is not None and evidence.start_char > 0, (
        "the T5 window itself must not start at character 0 of the document for this test to be meaningful"
    )

    # Now the full job path — capture what actually reaches the LLM.
    provider = _install_fake_llm(monkeypatch, ["facturation", "compatible"])
    csrf = _configure_account(client, db, "negationbudgetjob@example.com")
    _upload(client, "facturation.md", LONG_DOCUMENT_WITH_NEGATION, csrf)
    _upload(client, "unrelated.md", "Un document totalement different sur un tout autre sujet, sans aucun rapport avec ce qui precede.", csrf)

    ao_text = (
        "Appel d'offres - Modernisation facturation\n"
        "Acheteur : Collectivite Exemple\n"
        "Exigences : facturation compatible avec l'existant.\n"
        "Budget : 100 000 euros. Date limite : 30/11/2026.\n"
    )
    job = _run_analysis_to_completion(client, ao_text, csrf)
    assert len(provider.selection_prompts) == 1
    candidates_block = _extract_candidates_block(provider.selection_prompts[0])
    assert NEGATION_CLAUSE in candidates_block, (
        "the negation must survive intact into the BOUNDED prompt excerpt — presence of the query term alone "
        "is not sufficient, the actual clause with its negation must be inspected"
    )

    kept = next((ev for ev in job.result.evidence_pack if ev.source == "facturation.md"), None)
    assert kept is not None, "the selection LLM was told to pick id=1 for this single-candidate job"
    assert NEGATION_CLAUSE in kept.content
    assert len(kept.content) <= get_context_budget().max_excerpt_chars
    assert kept.start_char is not None and kept.end_char is not None
    assert kept.end_char - kept.start_char == len(kept.content)
    assert kept.content == LONG_DOCUMENT_WITH_NEGATION[kept.start_char:kept.end_char]
