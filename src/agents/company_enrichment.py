"""B07-T1 (DEFECT confirmed): the buyer profile of an AO — never the ESN's
own identity (that is src/web/database/models.py::ProviderProfile, a
different object owned by a different part of the codebase; see its
docstring). `CompanyProfile` here describes EXCLUSIVELY the acheteur named
in the AO.

Two defects are fixed here, both instances of the lot's core rule — a
fact is either real or explicitly absent, never fabricated and never
presented as verified:

1. The old fallback branch invented a whole company (effectif "250-500",
   CA "50M€ estimés", ville "Paris", ancienneté "10+ ans", solidité
   "Bonne") and tagged it `source="mock"` — a marker nothing downstream
   ever checked, so this fiction reached scoring and the generated
   candidature documents as if it were verified public data. The
   fabrication is deleted outright. When nothing real is available this
   module now returns a CompanyProfile carrying ONLY the one fact it
   actually has — the buyer name as written in the AO, verbatim — and
   leaves every other field on the shared model's own defaults.

2. The external lookup was gated solely on PAPPERS_API_TOKEN being
   configured server-wide. A server-wide key is not an account's
   authorization to send that account's buyer names to a third party.
   The real SaaS caller (src/web/jobs.py) now passes the account's own
   explicit ProviderProfile.external_enrichment_enabled flag.

3. `resultats[0]` was taken as the buyer with no verification — "le
   premier homonyme assimilé automatiquement au client". A single
   candidate whose name actually matches is now required; anything else
   is reported as "ambiguous" rather than guessed.

`source` is therefore one of:
  "pappers"     — exactly one candidate, whose name matches the AO's
                  buyer name; the real fields come from that record.
  "ambiguous"   — several candidates, or a single candidate whose name
                  does not match. No candidate is picked.
  "unavailable" — lookup disabled for this account, no token, no buyer
                  name, zero results, or a provider/network failure.
"mock" is gone and is never produced again.
"""
import re
import unicodedata

import requests

from src.core.config import PAPPERS_API_TOKEN
from src.core.logger import get_agent_logger
from src.core.models import CompanyProfile

logger = get_agent_logger("company_enrichment")

_SEARCH_URL = "https://api.pappers.fr/v2/recherche"
# The placeholder src/agents/ao_extractor.py writes when no buyer could be
# identified in the AO text — a label, not a company to look up.
_UNKNOWN_CLIENT = "Client non identifié"
# Enough candidates for "is this name ambiguous?" to be answerable at all.
# The previous code requested par_page=1, which structurally hid every
# homonym and is precisely what made blindly trusting resultats[0] look
# safe. Kept small: this is an ambiguity check, not a candidate ranker.
_CANDIDATES_PER_PAGE = 5

SOURCE_PAPPERS = "pappers"
SOURCE_AMBIGUOUS = "ambiguous"
SOURCE_UNAVAILABLE = "unavailable"


def _normalize(value: str) -> str:
    """Casefold, drop accents, reduce to alphanumeric words. Deliberately
    minimal — no fuzzy matching infrastructure, just enough to tolerate
    case, accents (an AO writes "Nantes Métropole", the registry stores
    "NANTES METROPOLE"), punctuation and spacing differences."""
    decomposed = unicodedata.normalize("NFKD", str(value or "").casefold())
    unaccented = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"[^0-9a-z]+", " ", unaccented).strip()


def _names_match(queried: str, candidate: str) -> bool:
    left, right = _normalize(queried), _normalize(candidate)
    if not left or not right:
        return False
    return left in right or right in left


class CompanyEnrichmentAgent:
    def enrich(self, company_name: str, *, external_enrichment_enabled: bool = True) -> CompanyProfile:
        """Look up the AO's buyer, or honestly report that it is unknown.

        `external_enrichment_enabled` keeps its historical default (True:
        a configured token means a lookup is attempted) for direct callers
        and tests. Every SaaS caller (src/web/jobs.py,
        src/web/routes_scoring_policy.py::/simulate) passes the value
        explicitly, and it is False for any account that has not opted in —
        in which case NO network call of any kind is made."""
        if not external_enrichment_enabled:
            return self._unavailable(company_name)
        if not PAPPERS_API_TOKEN or not company_name or company_name == _UNKNOWN_CLIENT:
            return self._unavailable(company_name)

        try:
            # Integration fix: the whole provider interaction — the request
            # AND parsing its response shape — is inside this try. A
            # malformed candidate (e.g. a string instead of an object where
            # `.get` doesn't exist) used to raise past this method despite
            # the docstring's "aucune exception ne s'échappe de enrich()"
            # promise; it is now treated exactly like a network failure.
            response = requests.get(
                _SEARCH_URL,
                params={"api_token": PAPPERS_API_TOKEN, "q": company_name, "par_page": _CANDIDATES_PER_PAGE},
                timeout=8,
            )
            candidates = response.json().get("resultats") or []

            if not candidates:
                return self._unavailable(company_name)
            if len(candidates) > 1:
                # Several registered companies answer this name. Choosing
                # one would be a fabricated identification, so none is.
                logger.info("Company lookup returned %d candidates; reported as ambiguous", len(candidates))
                return self._ambiguous(company_name)

            entry = candidates[0]
            if not _names_match(company_name, entry.get("nom_entreprise", "")):
                logger.info("Company lookup returned a single non-matching candidate; reported as ambiguous")
                return self._ambiguous(company_name)

            siege = entry.get("siege") or {}
            return CompanyProfile(
                raison_sociale=entry.get("nom_entreprise", company_name),
                siret=siege.get("siret", ""),
                effectif=str(entry.get("effectif", "Non renseigné")),
                ca=str(entry.get("chiffre_affaires", "Non renseigné")),
                ville=siege.get("ville", ""),
                secteur=entry.get("domaine_activite", ""),
                anciennete=str(entry.get("date_creation", "")),
                solidite_financiere="À vérifier",
                source=SOURCE_PAPPERS,
            )
        except Exception:
            # Broad on purpose, exactly as before: a provider outage OR a
            # malformed response must never break an analysis. Nothing
            # sensitive is logged (no token, no response body) and nothing
            # is invented on catch.
            logger.warning("Company lookup unavailable for the AO buyer; returning an absent profile")
            return self._unavailable(company_name)

    @staticmethod
    def _unavailable(company_name: str) -> CompanyProfile:
        """The buyer name as written in the AO — the only verified fact
        available — and nothing else. Every other field keeps the shared
        CompanyProfile default, which reads as "Non renseigné"."""
        return CompanyProfile(raison_sociale=company_name or "", source=SOURCE_UNAVAILABLE)

    @staticmethod
    def _ambiguous(company_name: str) -> CompanyProfile:
        """Same as _unavailable, but says WHY nothing was filled in: the
        buyer name matched several registered companies (or none
        confidently), so no candidate's data is copied in."""
        return CompanyProfile(raison_sociale=company_name or "", source=SOURCE_AMBIGUOUS)
