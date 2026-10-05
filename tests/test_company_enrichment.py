"""B07-T1: the AO buyer profile is either real or explicitly absent.

Every test here is fully isolated: no real network call, no real token
(tests/conftest.py already blanks PAPPERS_API_TOKEN and replaces
requests.get with a raising guard for the whole session; each test below
additionally installs its own explicit double so a failure is attributed
precisely).
"""
import pytest

from src.agents import company_enrichment as module
from src.agents.company_enrichment import CompanyEnrichmentAgent
from src.core.models import CompanyProfile

# The fabricated values the old fallback branch injected as if they were
# verified facts about the buyer. None of them may ever appear again.
_FABRICATED_LITERALS = ["250-500", "50M€ estimés", "Paris", "Services numériques / public", "10+ ans", "Bonne", "Client démo"]


class _Spy:
    """Records calls; raises if invoked when it must not be."""

    def __init__(self, payload=None, exc: Exception | None = None, forbidden: bool = False):
        self.payload, self.exc, self.forbidden = payload, exc, forbidden
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.forbidden:
            raise AssertionError("requests.get was called although no external lookup was authorized")
        if self.exc is not None:
            raise self.exc
        return _FakeResponse(self.payload)


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


@pytest.fixture()
def token(monkeypatch):
    """A configured (fake) provider token, set on the module's own imported
    reference — company_enrichment does `from src.core.config import
    PAPPERS_API_TOKEN`, so that module-level name is what the code reads."""
    monkeypatch.setattr(module, "PAPPERS_API_TOKEN", "fake-token-never-sent-anywhere")


def _install(monkeypatch, spy: _Spy) -> _Spy:
    monkeypatch.setattr(module.requests, "get", spy)
    return spy


def _assert_no_fabricated_facts(profile: CompanyProfile) -> None:
    defaults = CompanyProfile()
    assert profile.effectif == defaults.effectif
    assert profile.ca == defaults.ca
    assert profile.ville == defaults.ville
    assert profile.secteur == defaults.secteur
    assert profile.anciennete == defaults.anciennete
    assert profile.solidite_financiere == defaults.solidite_financiere
    assert profile.siret == defaults.siret
    rendered = profile.model_dump_json()
    for literal in _FABRICATED_LITERALS:
        assert literal not in rendered, f"fabricated literal {literal!r} resurfaced in the profile"


def test_disabled_makes_no_network_call_and_invents_nothing(monkeypatch, token):
    spy = _install(monkeypatch, _Spy(forbidden=True))

    profile = CompanyEnrichmentAgent().enrich("Ville de Nantes", external_enrichment_enabled=False)

    assert spy.calls == []
    assert profile.source == "unavailable"
    # The one real fact — the buyer name from the AO — kept verbatim.
    assert profile.raison_sociale == "Ville de Nantes"
    _assert_no_fabricated_facts(profile)


def test_enabled_without_token_makes_no_network_call(monkeypatch):
    monkeypatch.setattr(module, "PAPPERS_API_TOKEN", "")
    spy = _install(monkeypatch, _Spy(forbidden=True))

    profile = CompanyEnrichmentAgent().enrich("Ville de Nantes", external_enrichment_enabled=True)

    assert spy.calls == []
    assert profile.source == "unavailable"
    _assert_no_fabricated_facts(profile)


def test_provider_exception_falls_back_honestly(monkeypatch, token):
    _install(monkeypatch, _Spy(exc=ConnectionError("provider down")))

    profile = CompanyEnrichmentAgent().enrich("Ville de Nantes", external_enrichment_enabled=True)

    assert profile.source == "unavailable"
    assert profile.raison_sociale == "Ville de Nantes"
    _assert_no_fabricated_facts(profile)


def test_zero_results_is_unavailable_not_invented(monkeypatch, token):
    _install(monkeypatch, _Spy(payload={"resultats": []}))

    profile = CompanyEnrichmentAgent().enrich("Entreprise Inexistante SAS", external_enrichment_enabled=True)

    assert profile.source == "unavailable"
    _assert_no_fabricated_facts(profile)


def test_single_matching_result_is_used(monkeypatch, token):
    _install(monkeypatch, _Spy(payload={"resultats": [{
        "nom_entreprise": "NANTES METROPOLE HABITAT",
        "siege": {"siret": "27440001600015", "ville": "NANTES"},
        "effectif": "500 à 999 salariés",
        "chiffre_affaires": 128000000,
        "domaine_activite": "Location de logements",
        "date_creation": "1913-01-01",
    }]}))

    profile = CompanyEnrichmentAgent().enrich("Nantes Métropole Habitat", external_enrichment_enabled=True)

    assert profile.source == "pappers"
    assert profile.raison_sociale == "NANTES METROPOLE HABITAT"
    assert profile.siret == "27440001600015"
    assert profile.ville == "NANTES"
    assert profile.effectif == "500 à 999 salariés"
    assert profile.ca == "128000000"
    assert profile.secteur == "Location de logements"
    assert profile.anciennete == "1913-01-01"
    assert profile.solidite_financiere == "À vérifier"


def test_multiple_candidates_are_ambiguous_never_the_first_one(monkeypatch, token):
    _install(monkeypatch, _Spy(payload={"resultats": [
        {"nom_entreprise": "MARTIN CONSEIL", "siege": {"siret": "11111111100011", "ville": "LYON"}, "effectif": "10"},
        {"nom_entreprise": "MARTIN CONSEIL", "siege": {"siret": "22222222200022", "ville": "LILLE"}, "effectif": "800"},
    ]}))

    profile = CompanyEnrichmentAgent().enrich("Martin Conseil", external_enrichment_enabled=True)

    assert profile.source == "ambiguous"
    # Nothing from resultats[0] was copied in — no SIRET, no city, no size.
    assert profile.siret == ""
    assert profile.raison_sociale == "Martin Conseil"  # the AO's own wording, not a candidate's
    _assert_no_fabricated_facts(profile)


def test_single_non_matching_candidate_is_ambiguous(monkeypatch, token):
    _install(monkeypatch, _Spy(payload={"resultats": [
        {"nom_entreprise": "BOULANGERIE DUPONT", "siege": {"siret": "33333333300033", "ville": "BREST"}},
    ]}))

    profile = CompanyEnrichmentAgent().enrich("Conseil Départemental du Finistère", external_enrichment_enabled=True)

    assert profile.source == "ambiguous"
    assert profile.siret == ""
    _assert_no_fabricated_facts(profile)


def test_frozen_pipeline_default_still_attempts_the_lookup(monkeypatch, token):
    """src/core/pipeline.py calls enrich(ao.client) with no keyword at all.
    The default must preserve its exact prior gating: token configured ->
    a lookup is attempted."""
    spy = _install(monkeypatch, _Spy(payload={"resultats": []}))

    CompanyEnrichmentAgent().enrich("Ville de Nantes")

    assert len(spy.calls) == 1
    _args, kwargs = spy.calls[0]
    assert kwargs["params"]["q"] == "Ville de Nantes"


def test_unidentified_buyer_placeholder_is_never_looked_up(monkeypatch, token):
    spy = _install(monkeypatch, _Spy(forbidden=True))

    profile = CompanyEnrichmentAgent().enrich("Client non identifié")

    assert spy.calls == []
    assert profile.source == "unavailable"
    _assert_no_fabricated_facts(profile)


def test_mock_source_is_gone_for_every_path(monkeypatch, token):
    _install(monkeypatch, _Spy(payload={"resultats": []}))
    agent = CompanyEnrichmentAgent()
    produced = [
        agent.enrich("X", external_enrichment_enabled=False),
        agent.enrich("X", external_enrichment_enabled=True),
        agent.enrich(""),
    ]
    assert all(p.source in {"pappers", "ambiguous", "unavailable"} for p in produced)
    assert all(p.source != "mock" for p in produced)
