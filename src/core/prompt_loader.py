"""Minimal, dependency-free prompt-template loader — the ONE shared
mechanism for every prompt kept in its own text file (CLAUDE.md's own
convention keeps most prompts inline in Python; a few tickets have asked
for a SPECIFIC prompt to live in a dedicated file instead — this is the
shared loading mechanism for all of them, first introduced for
src/rag/prompts/reference_selection.txt (B18-T3), reused as-is for
src/agents/prompts/ao_extraction_*.txt (B05-T2) — never a second ad hoc
loader for the next one).

Plain string replacement, not str.format() — a prompt's own JSON example
commonly contains literal `{`/`}` that .format() would misinterpret as
fields.
"""
from __future__ import annotations

from pathlib import Path


class PromptLoadError(Exception):
    """B16-T2 (DEFECT confirmed): a prompt template failing to load (file
    missing, unreadable — a deployment/packaging problem) used to surface
    as a bare `FileNotFoundError`/`OSError` from `path.read_text(...)`,
    indistinguishable at every call site from any other exception a
    caller's own `except Exception` might be watching for. Reproduced via
    a real job (see tests/test_36_etat_des_lieux_validation.py, campaign
    36): a missing scoring-enrichment prompt file surfaced as
    `error_code="scoring_failed"` — the SAME code a genuine scoring-engine
    bug produces, making the two indistinguishable to whoever reads the
    error code.

    ONE centralized, safe exception type for this ONE failure mode — never
    a path, never file content, so it is safe to let propagate and safe to
    log without redaction anywhere it is caught. A caller (e.g.
    `ScoringEngine.enrich_with_llm`) catches THIS specific type to
    translate it into its own domain-appropriate signal (e.g.
    `enrichment_reason="prompt_missing"`) — never broadening to catch every
    `FileNotFoundError` in sight, which could just as easily come from an
    unrelated file operation and get mislabeled as a prompt problem."""


def load_prompt(path: Path, **replacements: str) -> str:
    """Replaces `{{KEY}}` placeholders (uppercased) in the template file at
    `path` with the given keyword arguments — e.g. `text="..."` replaces
    `{{TEXT}}`. Every placeholder present in the template must have a
    matching keyword argument; an unmatched `{{...}}` is left as-is rather
    than silently dropped, so a typo in either the template or the caller
    surfaces as visibly wrong prompt text instead of vanishing.

    Raises `PromptLoadError` (never a bare `FileNotFoundError`/`OSError`)
    if the template file cannot be read — see that class's own docstring
    for why this is centralized here rather than left to each caller to
    reinvent."""
    try:
        template = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PromptLoadError("Le gabarit de prompt demandé est introuvable ou illisible.") from exc
    for key, value in replacements.items():
        template = template.replace("{{" + key.upper() + "}}", value)
    return template
