"""B17-T1 (DEFECT confirmed): clause-scoped certification-obligation
analysis, isolated from src/agents/ao_extractor.py so it is independently
testable (per the ticket) and has a single place centralizing
certification names/aliases and the lexical rules used to judge them.

The audited bug this replaces: the previous `_extract_certifications`
looked for a negation ANYWHERE on the same LINE as a certification
mention. "ISO 27001 obligatoire ; Qualiopi non obligatoire" put both
mentions on one line, so Qualiopi's negation wrongly cancelled ISO 27001
too. A plain split on ';' is not enough either — a comma or "mais" can
each start a genuinely independent clause, while a comma can ALSO join a
coordinated list that shares a single verb/negation ("ISO 27001, HDS et
SecNumCloud sont obligatoires"). This module treats ';' and 'mais' as
clause boundaries (each clause gets its own verdict) while keeping commas
and coordinating conjunctions INSIDE a clause (so a shared verb/negation
correctly applies to every name it lists) — no heavy NLP dependency, a
small set of regexes over French AO phrasing.
"""
from __future__ import annotations

import re

from src.core.models import CertificationMention

# Certification NAME detection is intentionally separate from OBLIGATION
# detection (below) — this is what lets an N-way coordinated list ("ISO
# 27001, HDS et SecNumCloud sont obligatoires") work uniformly instead of
# needing one hand-tuned "name + nearby trigger" regex per certification.
_NAME_PATTERNS: dict[str, str] = {
    "SecNumCloud": r"secnumcloud",
    "HDS": r"\bhds\b|h[eé]bergeur\s+de\s+donn[eé]es\s+de\s+sant[eé]|h[eé]bergement\s+de\s+donn[eé]es\s+de\s+sant[eé]",
    "ISO 27001": r"iso\s*27001",
    "ISO 27701": r"iso\s*27701",
    "Qualiopi": r"qualiopi",
    "RGPD": r"\brgpd\b",
    "SOC 2": r"soc\s*2\b",
    "PASSI": r"\bpassi\b",
    "PRIS": r"\bpris\b",
    "RGS": r"\brgs\b",
}

_OBLIGATION_TRIGGERS = re.compile(
    r"(?i)\b(?:obligatoires?|requise?s?|exig[eé]e?s?|imp[eé]rati(?:fs?|ves?)|"
    r"n[eé]cessaires?|[eé]liminatoires?|sine\s+qua\s+non|doit\s+[eê]tre\s+certifi[eé]e?)\b"
)

# A "ni X ni Y ne sont/est requis(es)" construction negates WITHOUT ever
# using "pas"/"non" — the pre-existing negation list missed this shape
# entirely (a second, independent bug from the one named in the ticket's
# own headline example, caught while rewriting this module).
_NEGATION_PATTERNS = [
    r"non\s+requise?s?",
    r"non\s+obligatoires?",
    r"pas\s+requise?s?",
    r"pas\s+exig[eé]e?s?",
    r"appr[eé]ci[eé]e?s?\s+(?:mais\s+)?non",
    r"souhait[eé]e?s?\s+(?:mais\s+)?non",
    r"n'est\s+pas\s+(?:requise?|obligatoire)",
    r"ne\s+sont\s+pas\s+(?:requise?s?|obligatoires?)",
    r"\bni\b.{0,80}?\bne\s+(?:sont|est)\b",
]

# A cert named alongside one of these, with NO obligation trigger at all,
# is an explicit non-mandatory mention — distinct from a plain, uncommented
# name (which gets no verdict at all, ticket section 2: "ne pas convertir
# une simple mention... en obligation").
_OPTIONAL_MENTION_PATTERNS = [
    r"souhait[eé]e?s?", r"souhaitables?", r"appr[eé]ci[eé]e?s?", r"recommand[eé]e?s?",
    r"pr[eé]f[eé]rables?", r"optionnels?", r"si\s+possible", r"un\s+plus",
]

_AMBIGUOUS_MARKERS = [
    r"pourrait", r"pourraient", r"[eé]ventuellement", r"sous\s+r[eé]serve",
    r"le\s+cas\s+[eé]ch[eé]ant", r"dans\s+la\s+mesure\s+du\s+possible",
]

_CLAUSE_BREAKER = re.compile(r"\s*;\s*|\s+mais\s+", re.IGNORECASE)


def _split_clauses(text: str) -> list[str]:
    """Independent clauses: split on ';' and standalone 'mais', but NEVER
    on every comma — a comma more often joins a coordinated list sharing
    one verb than it starts an unrelated clause in this kind of AO
    phrasing. Line boundaries are always a clause boundary too."""
    clauses: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        for clause in _CLAUSE_BREAKER.split(line):
            clause = clause.strip()
            if clause:
                clauses.append(clause)
    return clauses


def _names_in_clause(clause: str) -> list[str]:
    return [name for name, pattern in _NAME_PATTERNS.items() if re.search(pattern, clause, re.IGNORECASE)]


def _verdict_for_clause(clause: str) -> str | None:
    """One verdict for the WHOLE clause, shared by every name found in it
    — correct for a coordinated list ("ISO 27001 et Qualiopi obligatoires"
    / "ni ISO 27001 ni Qualiopi ne sont requises") since a clause, by
    construction, never crosses a ';'/'mais' boundary. Returns None when
    the clause carries no obligation-relevant signal at all (a bare name
    with no surrounding language) — a simple mention is never treated as
    an obligation, ticket section 2."""
    has_trigger = bool(_OBLIGATION_TRIGGERS.search(clause))
    is_negated = any(re.search(pat, clause, re.IGNORECASE) for pat in _NEGATION_PATTERNS)
    is_ambiguous = any(re.search(pat, clause, re.IGNORECASE) for pat in _AMBIGUOUS_MARKERS)
    is_optional_mention = any(re.search(pat, clause, re.IGNORECASE) for pat in _OPTIONAL_MENTION_PATTERNS)

    if has_trigger and is_negated:
        return "non_obligatoire"
    if has_trigger and is_ambiguous:
        return "ambigu"
    if has_trigger:
        return "obligatoire"
    if is_optional_mention:
        return "non_obligatoire"
    return None


def analyze_certification_mentions(text: str) -> list[CertificationMention]:
    """Every (name, verdict, exact source clause) found in `text` — never
    merged by name, never deduplicated; see resolve_mandatory_
    certifications for how these are collapsed into the flat, backward-
    compatible certifications_obligatoires list."""
    mentions: list[CertificationMention] = []
    for clause in _split_clauses(text):
        names = _names_in_clause(clause)
        if not names:
            continue
        verdict = _verdict_for_clause(clause)
        if verdict is None:
            continue
        for name in names:
            mentions.append(CertificationMention(name=name, verdict=verdict, clause=clause))
    return mentions


def resolve_mandatory_certifications(mentions: list[CertificationMention]) -> tuple[list[str], list[str]]:
    """Collapses per-clause mentions into:
    - the flat certifications_obligatoires list — a name qualifies ONLY
      when EVERY one of its mentions says "obligatoire"; a single
      contradicting or ambiguous mention anywhere excludes it (ticket
      section 3: "ne pas inventer sa résolution").
    - the list of names with a genuine disagreement across mentions
      (contradiction OR undetermined/ambiguous scope alongside an
      "obligatoire" mention) — surfaced, never silently resolved either
      way."""
    verdicts_by_name: dict[str, set[str]] = {}
    for mention in mentions:
        verdicts_by_name.setdefault(mention.name, set()).add(mention.verdict)

    mandatory: list[str] = []
    contradictions: list[str] = []
    for name, verdicts in verdicts_by_name.items():
        if verdicts == {"obligatoire"}:
            mandatory.append(name)
        elif "obligatoire" in verdicts and len(verdicts) > 1:
            contradictions.append(name)
    return sorted(mandatory), sorted(contradictions)


def extract_certifications(text: str) -> list[str]:
    """Backward-compatible entry point — same name/shape
    src/agents/ao_extractor.py relies on for its own local fallback and
    LLM-vs-local conflict check on `certifications_obligatoires`. A
    contradictory name is deliberately EXCLUDED here rather than guessed
    either way (see AOContext.certification_contradictions for visibility
    into exactly which names and clauses disagreed)."""
    mandatory, _ = resolve_mandatory_certifications(analyze_certification_mentions(text))
    return mandatory
