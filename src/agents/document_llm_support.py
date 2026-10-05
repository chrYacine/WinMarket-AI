"""Lot 50 bis §1 — shared plumbing the three document agents use to call the SAME configured LLM adapter
(`src.agents.llm_client.LLMClient`, already used by `ao_extractor.py` — no new provider, no new client) and to
never trust an unverified citation.

A citation an LLM returns is DATA about the document, not a fact by itself: `verify_citation` checks it is an
actual (whitespace/case-tolerant) substring of the text the model was given. An unverified citation means the
model's whole answer is treated as untrustworthy for this call (§1: "vérifie les citations par rapport au
texte reçu") — the caller falls back to its own heuristic result, never a half-trusted LLM claim.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

# Every real call is logged with provider/duration by LLMClient itself; this module adds NO secret, NO raw
# document content and NO API key to any log — only counts/booleans/short reasons.
from src.core.logger import get_agent_logger

logger = get_agent_logger("document_llm_support")

_WHITESPACE_RE = re.compile(r"\s+")
# Lot 50 bis §1: "traite le contexte utile de toutes les pièces dans les limites techniques du modèle...
# aucune troncature cachée". A dossier piece is bounded upstream (DOSSIER_MAX_EXTRACTED_CHARS applies to the
# WHOLE dossier, not one piece), so a single piece can still exceed a sensible per-call size. Rather than
# silently cutting the text, this caps it at a bounded window and — when it actually had to cut — appends an
# explicit, visible note to the returned reason so the truncation is never hidden from whoever reads it.
MAX_LLM_INPUT_CHARS = 45_000  # 3 dossier windows (config.DOSSIER_WINDOW_CHARS) — covers the large majority of real pieces whole


def bound_for_llm(text: str) -> tuple[str, bool]:
    """(possibly-truncated text, was_truncated). Cuts at a whitespace boundary near the limit, never mid-word,
    so a truncated citation the model might quote is still a genuine substring of what it was shown."""
    content = text or ""
    if len(content) <= MAX_LLM_INPUT_CHARS:
        return content, False
    cut = content.rfind(" ", MAX_LLM_INPUT_CHARS - 200, MAX_LLM_INPUT_CHARS)
    end = cut if cut > 0 else MAX_LLM_INPUT_CHARS
    return content[:end], True


def _normalize(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text).strip().casefold()


def verify_citation(source_text: str, citation: str) -> bool:
    """True if `citation` (as the model returned it) is a genuine, whitespace/case-tolerant substring of
    `source_text` (what the model was actually shown) — never a fuzzy/semantic match, which would defeat the
    point of a citation check. An empty citation is never verified (nothing to check, nothing to trust)."""
    citation = (citation or "").strip()
    if not citation or len(citation) < 6:
        return False
    return _normalize(citation) in _normalize(source_text or "")


@dataclass(frozen=True)
class LLMJudgment:
    """The outcome of ONE structured LLM call for a document agent. `source` is always one of:
    - "llm": the provider answered, the JSON shape was valid AND the citation was verified against the text
      actually given to the model;
    - "llm_unavailable": no provider configured/enabled, or every eligible provider failed (network/timeout/
      rate-limit/auth) — `LLMClient.json_complete` already collapses these into `None`, indistinguishable
      further without duplicating its own retry logic (same acknowledged limit as `ao_extractor.py`);
    - "llm_invalid_response": the provider answered, but the JSON was malformed, missing an expected key, used
      a value outside the allowed set, or its citation could not be verified against the source text — treated
      as UNTRUSTWORTHY as a whole, never partially trusted.
    `data` is the raw validated dict for "llm" only; `None` otherwise — a caller must never read fields off a
    non-"llm" judgment."""
    source: str
    data: Optional[dict[str, Any]] = None
    reason: Optional[str] = None


def run_structured_judgment(
    llm, *, user_prompt: str, system_prompt: str, source_text: str, allowed_values: dict[str, set[str]],
    max_tokens: int = 600,
) -> LLMJudgment:
    """ONE structured call: `allowed_values` maps each required top-level key (besides "reason"/"citation",
    always required as non-empty strings — "citation" may be an empty string only when the schema allows it,
    checked by the caller, not here) to the set of values it may hold (e.g. {"category": {"rc","cctp",...}}).
    Never raises: a disabled/unreachable provider or a malformed answer is reported as a judgment, not an
    exception — the caller (an agent) always has a safe heuristic fallback."""
    if llm is None or not getattr(llm, "enabled", False):
        return LLMJudgment(source="llm_unavailable", reason="no_provider_configured")
    from src.core.config import LLM_TEMPERATURE_FACTUAL

    try:
        data = llm.json_complete(user_prompt, system=system_prompt, temperature=LLM_TEMPERATURE_FACTUAL, max_tokens=max_tokens)
    except Exception:
        logger.exception("Document agent LLM call raised unexpectedly")
        return LLMJudgment(source="llm_unavailable", reason="provider_exception")
    if data is None:
        return LLMJudgment(source="llm_unavailable", reason="no_content_or_unparseable")
    if not isinstance(data, dict):
        return LLMJudgment(source="llm_invalid_response", reason="not_an_object")
    for key, allowed in allowed_values.items():
        if data.get(key) not in allowed:
            logger.warning("Document agent LLM response used an unknown value key=%s", key)
            return LLMJudgment(source="llm_invalid_response", reason=f"unknown_value:{key}")
    if not isinstance(data.get("reason"), str) or not data["reason"].strip():
        return LLMJudgment(source="llm_invalid_response", reason="missing_reason")
    citation = data.get("citation", "")
    if not isinstance(citation, str):
        return LLMJudgment(source="llm_invalid_response", reason="citation_not_a_string")
    if citation and not verify_citation(source_text, citation):
        logger.warning("Document agent LLM citation could not be verified against the source text")
        return LLMJudgment(source="llm_invalid_response", reason="citation_not_verified")
    return LLMJudgment(source="llm", data=data)
