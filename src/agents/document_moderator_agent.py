"""Lot 50 §2.C / Lot 50 bis §1 — the documentary moderation role for ONE piece: is it PLAUSIBLY RELEVANT to
this dossier (or, in the private knowledge base, to the account's own professional usage), independent of
security and of classification? Verdict: `lie` (relevant), `incertain` (not enough signal either way) or
`hors_sujet` (no shared signal at all) — always with a sourced reason, never a bare label.

This deliberately does NOT reuse the blunt "≥2 tender-vocabulary terms" scope gate
(`src.core.content_security.ContentSecurityGate`, still used for the whole-dossier/whole-document SECURITY
scope check elsewhere, unchanged): a document without the classic vocabulary of a tender (a planning, a
list of questions, a price detail) can still be genuinely relevant, and the ticket is explicit about this
(§2.C: "un document sans le vocabulaire classique d'un marché peut être pertinent").

Two judgment paths, always distinguished (`source`):
- **heuristic** (lot 50): looks for TOKENS shared with the rest of the dossier (or with the account's own
  declared link note) — a planning that names the same client, site or lot reference as the RC is "lié" even
  without a single word of tender vocabulary. A SHARED WORD ALONE is a coarse, mechanical signal — lot 50 bis
  §1 explicitly warns it is not by itself proof of a real link (two pieces can share a common word — "projet",
  a city name used generically — without one being about the other).
- **llm** (lot 50 bis): a real call to the account's configured LLM adapter, asked to judge the SENSE of the
  piece against a short summary of the other pieces (never their full text — bounded context) and any
  user-declared link note, not just shared vocabulary. Citation-verified before being trusted; unavailable or
  invalid falls back to the heuristic result, reported as such.

A `hors_sujet`/`incertain` verdict here is advisory: it feeds the admission table (§3) for the user (or, for
an already-confirmed piece, for the record), it is never itself a security block and never itself changes a
scoring decision. Pertinence, security and business conflicts stay three separate concerns (§2.C). A relevant
but CONTRADICTORY piece (a different amount, a different date than another piece) must still be judged "lié"
here — moderation is about topical relevance, never about resolving or hiding a business contradiction (that
stays `src/agents/dossier_consolidation.py`'s job, untouched).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from src.agents.document_llm_support import bound_for_llm, run_structured_judgment
from src.core.content_security import ContentSecurityGate
from src.core.prompt_loader import load_prompt

_PROMPTS_DIR = Path(__file__).parent / "prompts"
_LLM_SYSTEM = (_PROMPTS_DIR / "document_relevance_system.txt").read_text(encoding="utf-8")
_LLM_USER_PATH = _PROMPTS_DIR / "document_relevance_user.txt"
_VERDICTS = ("lie", "hors_sujet", "incertain")
_OTHERS_SUMMARY_CHARS = 4000  # bounded context — never the other pieces' full text (§1: within model limits)

_MIN_SIGNIFICANT_LEN = 5
_STOPWORDS = frozenset((
    "dans", "pour", "avec", "cette", "sont", "être", "leur", "leurs", "notre", "votre", "nous", "vous",
    "elle", "ils", "elles", "aussi", "ainsi", "donc", "mais", "plus", "moins", "tout", "tous", "toute",
    "toutes", "sans", "sous", "chaque", "entre", "avant", "après", "comme", "ceci", "cela", "dont", "où",
))
_TOKEN_RE = re.compile(r"[a-zàâäéèêëïîôöùûüçñ0-9]{%d,}" % _MIN_SIGNIFICANT_LEN, re.IGNORECASE)


def _significant_tokens(text: str) -> set[str]:
    return {t for t in (m.group(0).casefold() for m in _TOKEN_RE.finditer(text or "")) if t not in _STOPWORDS}


@dataclass(frozen=True)
class RelevanceAssessment:
    verdict: str  # 'lie' | 'incertain' | 'hors_sujet'
    reason: str
    shared_terms: list[str] = field(default_factory=list)
    source: str = "heuristic"  # "heuristic" | "llm" | "heuristic_llm_unavailable" | "heuristic_llm_invalid"
    heuristic_verdict: Optional[str] = None  # kept alongside an "llm" result when the two disagree — never hidden


class DocumentModeratorAgent:
    _gate_terms = tuple(ContentSecurityGate._AO_TERMS)  # reused vocabulary ONLY, never the gate's 2-term hard cutoff

    def _heuristic(self, text: str, *, other_pieces_text: str, user_link_note: Optional[str]) -> RelevanceAssessment:
        content = text or ""
        normalized = content.casefold()
        if len(content.strip()) < 20:
            return RelevanceAssessment("incertain", "Contenu trop court pour établir un lien avec le dossier.")

        own_tokens = _significant_tokens(content)
        other_tokens = _significant_tokens(other_pieces_text)
        note_tokens = _significant_tokens(user_link_note or "")
        shared_with_dossier = sorted(own_tokens & other_tokens)[:8]
        shared_with_note = sorted(own_tokens & note_tokens)[:8]
        ao_terms_found = [t for t in self._gate_terms if t in normalized]

        if shared_with_dossier:
            return RelevanceAssessment(
                "lie",
                "Éléments communs avec d'autres pièces du dossier (même client, site, référence ou lot) : "
                + ", ".join(shared_with_dossier[:5]) + " — un mot partagé seul reste un indice mécanique, pas une preuve de sens.",
                shared_terms=shared_with_dossier,
            )
        if shared_with_note:
            return RelevanceAssessment(
                "lie",
                "Correspond au lien déclaré par l'utilisateur avec ce dossier : " + ", ".join(shared_with_note[:5]) + ".",
                shared_terms=shared_with_note,
            )
        if ao_terms_found:
            return RelevanceAssessment(
                "lie", "Vocabulaire de marché public reconnu : " + ", ".join(ao_terms_found[:5]) + ".",
                shared_terms=ao_terms_found,
            )
        if len(own_tokens) < 5:
            return RelevanceAssessment("incertain", "Ni vocabulaire de marché ni élément commun identifié avec le reste du dossier — contenu trop pauvre pour trancher.")
        return RelevanceAssessment(
            "hors_sujet",
            "Aucun vocabulaire de marché ni élément (client, site, référence, lot) commun avec les autres pièces du dossier n'a été trouvé.",
        )

    def assess_relevance(
        self, text: str, *, other_pieces_text: str = "", user_link_note: Optional[str] = None, llm=None,
    ) -> RelevanceAssessment:
        heuristic = self._heuristic(text, other_pieces_text=other_pieces_text, user_link_note=user_link_note)
        if llm is None:
            return heuristic

        bounded_text, truncated = bound_for_llm(text)
        prompt = load_prompt(
            _LLM_USER_PATH, text=bounded_text,
            others=(other_pieces_text or "(aucune autre pièce)")[:_OTHERS_SUMMARY_CHARS],
            user_note=user_link_note or "(aucun)",
        )
        judgment = run_structured_judgment(
            llm, user_prompt=prompt, system_prompt=_LLM_SYSTEM, source_text=bounded_text,
            allowed_values={"verdict": set(_VERDICTS)},
        )
        if judgment.source != "llm":
            source = "heuristic_llm_unavailable" if judgment.source == "llm_unavailable" else "heuristic_llm_invalid"
            return RelevanceAssessment(**{**heuristic.__dict__, "source": source})

        data = judgment.data
        verdict = data["verdict"]
        reason = data["reason"]
        if heuristic.verdict != verdict:
            reason += f" (lecture par mots-clés en désaccord : proposait « {heuristic.verdict} »)."
        if truncated:
            reason += " Analyse limitée aux premiers caractères du document — couverture partielle, non dissimulée."
        shared_terms = [data["citation"]] if data.get("citation") else heuristic.shared_terms
        return RelevanceAssessment(
            verdict=verdict, reason=reason, shared_terms=shared_terms, source="llm", heuristic_verdict=heuristic.verdict,
        )
