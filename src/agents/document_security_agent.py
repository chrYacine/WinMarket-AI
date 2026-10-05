"""Lot 50 §2.A / Lot 50 bis §1 — the security role for ONE piece of a tender dossier or a private-knowledge
document.

Reuses `src.core.content_security.ContentSecurityGate` (the existing, already-tested injection regex) as the
one thing that can BLOCK a piece outright, and adds a middle "to_verify" state this ticket asks for: a
document that shows a SUSPICIOUS pattern (vocabulary about assistants/prompts/models near an imperative verb)
without matching the strict, deterministic injection regex. "No signal detected" is never presented as a
guarantee of innocuousness (§2.A) — `authorized` only ever means "no known pattern matched", stated as such.

This module never itself decides admission (the server does, per §2's own rule "un avis LLM ne contourne pas
les contrôles déterministes") — it only classifies. A `blocked` verdict can never be lifted by a client-side
confirmation OR by an LLM's opinion; only a corrected/replaced piece changes it (the caller enforces this, not
this module, and the LLM path below is never even consulted for an already-`blocked` piece — there is nothing
for a second opinion to lift).

Lot 50 bis §1 fixes a real false-negative in the lot 50 heuristic: `likely_citation` used to be set by a
WHOLE-DOCUMENT search for words like "exemple" — a single such word ANYWHERE in a long document (including far
from the suspicious passage) could wrongly soften the flag. It now only counts a citation marker found in the
SAME short window as the suspicious pattern, exactly the co-occurrence discipline already used for the
suspicious pattern itself (`_SUSPECT_SUBJECT`/`_SUSPECT_VERB`).

For a `to_verify` case only (never for `authorized` or `blocked`), a real second opinion from the account's
configured LLM adapter may be requested — citation-verified before being trusted. It can REFINE the reasoning
attached to the SAME `to_verify` state (leaning "citation_pedagogique" or "instruction_suspecte"), and it may
recommend HEIGHTENED vigilance, but it can never itself set `state="authorized"` and never lifts a `blocked`
verdict — an LLM approval or the mere word "exemple" in a piece never guarantees innocuousness or overrides a
deterministic control (ticket, verbatim).

OWASP RAG Security / LLM Prompt Injection Prevention cheat sheets were read as DESIGN references for this
module's shape (a graded verdict with cited spans, technical checks before content checks, no false sense of
completeness from a keyword match) — nothing here executes instructions found in a document, and nothing here
was copied verbatim from those pages.
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
_LLM_SYSTEM = (_PROMPTS_DIR / "document_security_review_system.txt").read_text(encoding="utf-8")
_LLM_USER_PATH = _PROMPTS_DIR / "document_security_review_user.txt"
_ASSESSMENTS = ("citation_pedagogique", "instruction_suspecte", "indetermine")

# A REAL pattern, short of the strict/confirmed injection regex, that deserves a human look rather than a
# silent pass: vocabulary about the assistant/model/prompt itself, in the same short window as an imperative
# verb. A single isolated word ("assistant", "IA") is NOT enough by itself — a legitimate document that simply
# MENTIONS an assistant (e.g. "le prestataire proposera un assistant IA pour le support") must not be flagged
# only for using that vocabulary; only the CO-OCCURRENCE with an imperative near it is a "to_verify" signal.
_SUSPECT_SUBJECT = r"(?:assistant|mod[eè]le|IA|prompt|syst[eè]me|instructions?|consignes?)"
_SUSPECT_VERB = r"(?:ignore|oublie|d[ée]sactive|contourne|r[ée]v[eè]le|agis|comporte-toi|r[ée]ponds|d[ée]sormais)"
_TO_VERIFY_PATTERNS = (
    re.compile(rf"(?i){_SUSPECT_SUBJECT}[^.\n]{{0,60}}{_SUSPECT_VERB}"),
    re.compile(rf"(?i){_SUSPECT_VERB}[^.\n]{{0,60}}{_SUSPECT_SUBJECT}"),
)
# A legitimate document CITING an attack as an example (a security clause, a case study, an awareness note)
# rather than addressing one to the model — never upgrades a to_verify signal to a block, but is recorded so
# the reason is honest about why the piece was still only flagged, not blocked (§2.A: "distinguer une citation
# d'une instruction"). Lot 50 bis: checked NEAR the suspicious span only (see module docstring) — a marker
# elsewhere in a long, unrelated document must never soften this flag.
_CITATION_MARKERS = (
    r"(?i)\bexemple\b", r"(?i)\bcas\s+v[ée]cu\b", r"(?i)\bclause\s+de\s+s[ée]curit[ée]\b",
    r"(?i)\bsensibilisation\b", r"(?i)\bne\s+doit\s+jamais\b", r"(?i)\bà\s+titre\s+illustratif\b",
)
_CITATION_WINDOW_CHARS = 120


@dataclass(frozen=True)
class SecuritySpan:
    start: int
    end: int


@dataclass(frozen=True)
class SecurityAssessment:
    state: str  # 'authorized' | 'blocked' | 'to_verify'
    code: str
    reason: str
    spans: list[SecuritySpan] = field(default_factory=list)
    likely_citation: bool = False
    source: str = "heuristic"  # "heuristic" | "llm" | "heuristic_llm_unavailable" | "heuristic_llm_invalid"
    heuristic_likely_citation: Optional[bool] = None  # kept alongside an "llm" result when the two disagree


class DocumentSecurityAgent:
    """No shell/file/network access beyond the ONE already-configured LLM call, no read of any other
    account's data: `assess` takes plain text and a display name already validated upstream (format/magic-
    bytes/size — this agent never re-parses binary)."""

    def __init__(self, gate: Optional[ContentSecurityGate] = None):
        self._gate = gate or ContentSecurityGate()

    def assess(self, text: str, *, display_name: str = "", llm=None) -> SecurityAssessment:
        content = text or ""
        verdict = self._gate.check(content)
        if "prompt_injection" in verdict.reason_codes:
            spans = [SecuritySpan(m.start(), m.end()) for m in self._matches(content, ContentSecurityGate._INJECTION_PATTERNS)]
            # No LLM second look here: a confirmed block is never reconsidered by an LLM opinion (ticket,
            # verbatim) — there is nothing left for a second opinion to usefully lift or refine.
            return SecurityAssessment(
                state="blocked", code="prompt_injection",
                reason="Instruction adressée à un assistant IA détectée dans le contenu : la pièce est bloquée. "
                       "Ce blocage ne peut pas être levé par une confirmation ni par un avis d'IA ; corrigez ou remplacez la pièce.",
                spans=spans,
            )
        to_verify_spans = self._matches(content, _TO_VERIFY_PATTERNS)
        if to_verify_spans:
            citation_near = self._citation_marker_near(content, to_verify_spans)
            reason = (
                "Vocabulaire évoquant un assistant/modèle/des instructions proche d'un verbe impératif : "
                "aucune règle stricte n'est déclenchée, mais une vérification humaine est recommandée avant "
                "d'admettre cette pièce."
            )
            if citation_near:
                reason += " Un indice de citation (exemple, clause de sécurité, sensibilisation) apparaît à proximité immédiate du passage suspect — non confirmé automatiquement."
            spans = [SecuritySpan(s.start(), s.end()) for s in to_verify_spans]
            heuristic_result = SecurityAssessment(
                state="to_verify", code="suspect_vocabulary", reason=reason, spans=spans, likely_citation=citation_near,
            )
            if llm is None:
                return heuristic_result
            return self._llm_second_look(content, spans=to_verify_spans, heuristic=heuristic_result, llm=llm)
        # "authorized" states the absence of a KNOWN pattern — never a promise of innocuousness (§2.A). No LLM
        # call here either: the ticket's "analyse complémentaire" is for SUSPICIONS (to_verify) specifically,
        # never a blanket second pass over every authorized piece (which would also never be allowed to grant
        # a guarantee it can't give).
        return SecurityAssessment(
            state="authorized", code="no_known_pattern",
            reason="Aucun motif de sécurité connu détecté dans ce contenu — cela ne garantit pas son innocuité.",
        )

    def _llm_second_look(self, content: str, *, spans: list["re.Match[str]"], heuristic: SecurityAssessment, llm) -> SecurityAssessment:
        first = spans[0]
        window_start, window_end = max(0, first.start() - 80), min(len(content), first.end() + 80)
        snippet = content[window_start:window_end]
        bounded_text, truncated = bound_for_llm(content)
        prompt = load_prompt(_LLM_USER_PATH, text=bounded_text, suspect_snippet=snippet)
        # The model sees BOTH the bounded text and the (possibly further-out) suspect snippet — a citation is
        # verified against their union, never against the full untruncated document, so a "verified" citation
        # always genuinely reflects what the model was actually shown.
        judgment = run_structured_judgment(
            llm, user_prompt=prompt, system_prompt=_LLM_SYSTEM, source_text=bounded_text + "\n" + snippet,
            allowed_values={"assessment": set(_ASSESSMENTS)},
        )
        if judgment.source != "llm":
            source = "heuristic_llm_unavailable" if judgment.source == "llm_unavailable" else "heuristic_llm_invalid"
            return SecurityAssessment(**{**heuristic.__dict__, "source": source})

        data = judgment.data
        assessment, llm_reason = data["assessment"], data["reason"]
        reason = heuristic.reason + f" Second avis (IA) : {llm_reason}"
        if truncated:
            reason += " (analyse limitée aux premiers caractères du document, hors passage suspect examiné en entier — couverture partielle, non dissimulée)"
        if assessment == "instruction_suspecte":
            reason += " — jugé comme une instruction probable : vigilance renforcée, admission déconseillée. Cet avis ne remplace pas un contrôle déterministe et n'ouvre aucun blocage automatique supplémentaire."
            likely_citation = False
        elif assessment == "citation_pedagogique":
            likely_citation = True
        else:
            likely_citation = heuristic.likely_citation
        # state stays "to_verify" in every case — an LLM opinion never grants "authorized" and this path is
        # never reached for an already-"blocked" piece (ticket, verbatim: no LLM approval lifts a restriction).
        return SecurityAssessment(
            state="to_verify", code="suspect_vocabulary", reason=reason, spans=heuristic.spans,
            likely_citation=likely_citation, source="llm", heuristic_likely_citation=heuristic.likely_citation,
        )

    @staticmethod
    def _citation_marker_near(text: str, spans: list["re.Match[str]"]) -> bool:
        for span in spans:
            window = text[max(0, span.start() - _CITATION_WINDOW_CHARS): span.end() + _CITATION_WINDOW_CHARS]
            if any(re.search(p, window) for p in _CITATION_MARKERS):
                return True
        return False

    @staticmethod
    def _matches(text: str, patterns) -> list["re.Match[str]"]:
        out: list["re.Match[str]"] = []
        for pattern in patterns:
            out.extend(re.finditer(pattern, text))
        return out
