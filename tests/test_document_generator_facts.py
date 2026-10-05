"""Ticket B19-T1 — fact grounding of the generated candidature content.

Covers the two confirmed defects and the new consistency guard in
src/livrables/document_generator.py:

1. the system prompt no longer hardcodes a FICTIONAL company
   specialization — the identity sentence is built from the account's own
   ProviderProfile, or stays specific-free when there is none;
2. the three decision prompts (NO-GO / GO SOUS RESERVE / GO) now live in
   src/livrables/prompts/*.txt and are loaded through the shared
   src/core/prompt_loader.py, each carrying an explicit anti-invention
   instruction;
3. a generated field naming a certification the server does not know to be
   real is dropped whole, so the caller's own neutral fallback text is used.

No network, no DB, no API key: the LLM is a fake object that records the
prompt/system it was handed and returns a dict the test constructs.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.core.models import AOContext, CriterionScore, ScoringResult
from src.livrables.document_generator import DocumentGenerator

# The exact literal removed from the old class-level _DOC_SYSTEM constant.
_FABRICATED_SPECIALIZATION = (
    "transformation digitale, développement applicatif, data/IA, cloud et cybersécurité"
)
_ANTI_INVENTION = "N'invente aucun chiffre, certification, référence client"


class FakeLLM:
    """Records what it was called with; returns a caller-supplied dict."""

    def __init__(self, response: dict | None = None, enabled: bool = True):
        self.enabled = enabled
        self.response = response if response is not None else {}
        self.prompt: str | None = None
        self.system: str | None = None
        self.calls = 0

    def json_complete(self, prompt, system=None, **kwargs):
        self.calls += 1
        self.prompt = prompt
        self.system = system
        return self.response


class FakeProviderProfile:
    """Duck-typed stand-in for src.web.database.models.ProviderProfile —
    only the three attributes this code path reads, no DB session."""

    def __init__(self, raison_sociale=None, competences=None, certifications=None):
        self.raison_sociale = raison_sociale
        self.competences = competences or []
        self.certifications = certifications or []


@pytest.fixture()
def generator(tmp_path) -> DocumentGenerator:
    # Isolated output dir — this ticket writes no document, but the
    # constructor creates its directory.
    return DocumentGenerator(output_dir=tmp_path / "outputs")


def _ao(**overrides) -> AOContext:
    base = dict(
        titre="Refonte du portail usagers",
        client="Métropole de Lyon",
        secteur="Secteur public",
        technologies_demandees=["python", "kubernetes"],
        livrables=["Portail", "Documentation"],
        certifications_obligatoires=[],
    )
    base.update(overrides)
    return AOContext(**base)


def _result(decision: str = "GO", score: float = 90.0) -> ScoringResult:
    return ScoringResult(
        decision=decision,
        score_global=score,
        criteres=[CriterionScore(nom="Adéquation technique", poids=20.0, score=85.0, justification="Stack maîtrisée.")],
        criteres_bloquants=[],
        recommandations=["Préparer le mémoire technique"],
    )


# ───────────────────────── system prompt identity ─────────────────────────

def test_system_prompt_without_profile_states_no_fabricated_specialization(generator):
    llm = FakeLLM({"resume_executif": "Une phrase neutre."})
    generator._generate_ai_content(_ao(), _result(), llm, provider_profile=None)

    assert llm.system, "a system prompt must still be sent"
    assert _FABRICATED_SPECIALIZATION not in llm.system
    assert "ESN française spécialisée" not in llm.system
    # still a coherent, usable instruction, not an empty shell
    # Lot 44: no assumed role ("rédacteur avant-vente senior") either — the intent (no invented
    # specialization) is unchanged and asserted just below.
    assert "avant-vente" not in llm.system and "ESN" not in llm.system
    assert "Tu rédiges la réponse du prestataire" in llm.system
    assert len(llm.system.strip()) > 200


def test_system_prompt_uses_the_accounts_real_raison_sociale(generator):
    profile = FakeProviderProfile(raison_sociale="Nova Digital", competences=["python", "aws"])
    llm = FakeLLM({"resume_executif": "Une phrase neutre."})
    generator._generate_ai_content(_ao(), _result(), llm, provider_profile=profile)

    assert "Nova Digital" in llm.system
    assert "python" in llm.system and "aws" in llm.system
    assert _FABRICATED_SPECIALIZATION not in llm.system


def test_system_prompt_with_profile_but_no_raison_sociale_falls_back_to_generic(generator):
    profile = FakeProviderProfile(raison_sociale=None, competences=[])
    llm = FakeLLM({})
    generator._generate_ai_content(_ao(), _result(), llm, provider_profile=profile)

    assert _FABRICATED_SPECIALIZATION not in llm.system
    assert "n'en invente aucune" in llm.system


def test_pipeline_call_signature_without_provider_profile_still_works(generator):
    """src/core/pipeline.py (frozen) calls this with three positional args
    and no keyword — that must keep working unchanged."""
    llm = FakeLLM({"conclusion": "Nous restons disponibles."})
    out = generator._generate_ai_content(_ao(), _result(), llm)
    assert out == {"conclusion": "Nous restons disponibles."}


# ───────────────────── the three file-based prompt bodies ─────────────────

@pytest.mark.parametrize(
    "decision, marker",
    [
        ("NO-GO", '**"note_refus"**'),
        ("GO SOUS RESERVE", '**"reserves_et_conditions"**'),
        ("GO", "Réaffirmation des enjeux"),
    ],
)
def test_each_decision_branch_builds_its_own_prompt(generator, decision, marker):
    llm = FakeLLM({})
    generator._generate_ai_content(_ao(), _result(decision=decision), llm)

    assert llm.calls == 1, "exactly one LLM call per invocation"
    assert marker in llm.prompt
    # the data context block is interpolated, not left as a raw placeholder
    assert "{{CONTEXT}}" not in llm.prompt
    assert "Métropole de Lyon" in llm.prompt
    assert f"Décision  : {decision}" in llm.prompt


@pytest.mark.parametrize("decision", ["NO-GO", "GO SOUS RESERVE", "GO"])
def test_anti_invention_instruction_present_in_every_prompt(generator, decision):
    llm = FakeLLM({})
    generator._generate_ai_content(_ao(), _result(decision=decision), llm)
    assert _ANTI_INVENTION in llm.prompt


def test_no_unresolved_placeholder_remains_in_any_prompt(generator):
    for decision in ("NO-GO", "GO SOUS RESERVE", "GO"):
        llm = FakeLLM({})
        generator._generate_ai_content(_ao(), _result(decision=decision), llm)
        assert "{{" not in llm.prompt, f"unfilled placeholder in the {decision} prompt"


def test_prompt_files_exist_where_the_contract_says(generator):
    prompts = Path(__file__).resolve().parents[1] / "src" / "livrables" / "prompts"
    for name in ("document_system.txt", "document_no_go.txt", "document_reserve.txt", "document_go.txt"):
        assert (prompts / name).is_file(), name


# ──────────────── certification consistency guard (core) ──────────────────

def test_invented_certification_drops_the_whole_field(generator):
    """CORE: SecNumCloud is neither required by the AO nor declared by a
    provider profile — the field is dropped entirely, so generate_docx /
    generate_pdf fall back to their own neutral text."""
    ao = _ao(certifications_obligatoires=["ISO 27001"])
    llm = FakeLLM({"valeur_ajoutee": ["Nous détenons la certification SecNumCloud, un atout clé."]})

    out = generator._generate_ai_content(ao, _result(), llm, provider_profile=None)

    assert "valeur_ajoutee" not in out
    assert "SecNumCloud" not in str(out)


def test_grounded_certification_from_the_ao_is_kept(generator):
    ao = _ao(certifications_obligatoires=["ISO 27001"])
    claim = "Notre équipe opère dans un périmètre certifié ISO 27001."
    llm = FakeLLM({"valeur_ajoutee": [claim], "conclusion": "Nous restons disponibles."})

    out = generator._generate_ai_content(ao, _result(), llm, provider_profile=None)

    assert out["valeur_ajoutee"] == [claim]
    assert out["conclusion"] == "Nous restons disponibles."


def test_grounded_certification_from_the_provider_profile_is_kept(generator):
    ao = _ao(certifications_obligatoires=[])
    profile = FakeProviderProfile(
        raison_sociale="Nova Digital",
        competences=["python"],
        certifications=[{"nom": "Qualiopi", "statut": "declaree", "preuve_reference": None}],
    )
    claim = "Notre organisme de formation est certifié Qualiopi."
    llm = FakeLLM({"valeur_ajoutee": [claim]})

    out = generator._generate_ai_content(ao, _result(), llm, provider_profile=profile)

    assert out["valeur_ajoutee"] == [claim]


def test_only_the_offending_field_is_dropped(generator):
    ao = _ao(certifications_obligatoires=["ISO 27001"])
    llm = FakeLLM({
        "resume_executif": "Un projet structurant pour la Métropole de Lyon.",
        "valeur_ajoutee": ["Bonne pratique ISO 27001.", "Nous sommes hébergeur HDS."],
        "conclusion": "Nous restons disponibles.",
    })

    out = generator._generate_ai_content(ao, _result(), llm, provider_profile=None)

    assert "valeur_ajoutee" not in out  # HDS is ungrounded -> whole field goes
    assert out["resume_executif"].startswith("Un projet structurant")
    assert out["conclusion"] == "Nous restons disponibles."


def test_string_field_mentioning_an_invented_certification_is_dropped(generator):
    ao = _ao(certifications_obligatoires=[])
    llm = FakeLLM({"methodologie": "Nos environnements sont qualifiés SecNumCloud."})

    out = generator._generate_ai_content(ao, _result(), llm, provider_profile=None)

    assert "methodologie" not in out


def test_free_typed_provider_certification_still_matches_the_canonical_name(generator):
    """A profile row's free-typed "iso27001" must ground a generated
    "ISO 27001" — the known-real set is canonicalized, not compared raw."""
    profile = FakeProviderProfile(
        raison_sociale="Nova Digital",
        certifications=[{"nom": "iso27001", "statut": "verifiee", "preuve_reference": "doc-1"}],
    )
    claim = "Nous sommes certifiés ISO 27001."
    llm = FakeLLM({"valeur_ajoutee": [claim]})

    out = generator._generate_ai_content(_ao(), _result(), llm, provider_profile=profile)

    assert out["valeur_ajoutee"] == [claim]


def test_common_french_word_pris_is_not_read_as_the_pris_qualification(generator):
    """`_NAME_PATTERNS["PRIS"]` is a case-insensitive `\\bpris\\b`, which
    also matches the ordinary past participle. A lowercase occurrence must
    not cost the account a whole generated section."""
    llm = FakeLLM({"conclusion": "Nous avons pris en compte chaque contrainte du CCTP."})

    out = generator._generate_ai_content(_ao(), _result(), llm, provider_profile=None)

    assert out["conclusion"].startswith("Nous avons pris")


def test_no_prose_field_is_dropped_when_no_certification_is_mentioned(generator):
    response = {
        "resume_executif": "Une réponse construite autour des livrables attendus.",
        "methodologie": "Trois phases, chacune adossée à un jalon de validation.",
        "valeur_ajoutee": ["Équipe stable", "Transfert de compétences"],
    }
    out = generator._generate_ai_content(_ao(), _result(), FakeLLM(dict(response)), provider_profile=None)
    assert out == response


def test_disabled_llm_returns_empty_and_never_calls_the_model(generator):
    llm = FakeLLM({"resume_executif": "x"}, enabled=False)
    assert generator._generate_ai_content(_ao(), _result(), llm) == {}
    assert llm.calls == 0


# ─────────────────────── server-controlled structured facts ───────────────

def test_structured_scoring_facts_are_untouched_by_generation(generator):
    """decision / score_global / criteres are the server's own values; the
    generation step must neither regenerate nor mutate them."""
    ao = _ao(certifications_obligatoires=["ISO 27001"])
    result = _result(decision="GO SOUS RESERVE", score=72.5)
    before = (result.decision, result.score_global, [(c.nom, c.poids, c.score, c.justification) for c in result.criteres])

    llm = FakeLLM({
        "decision": "GO",              # the model trying to restate a server fact
        "score_global": 99,
        "valeur_ajoutee": ["Certifié SecNumCloud."],
    })
    generator._generate_ai_content(ao, result, llm, provider_profile=None)

    after = (result.decision, result.score_global, [(c.nom, c.poids, c.score, c.justification) for c in result.criteres])
    assert before == after
    assert result.decision == "GO SOUS RESERVE"
    assert result.score_global == 72.5
