"""Lot 50 ter §2 — the whole-dossier SCOPE decision, shared by the legacy direct-submit intake
(`intake.py::receive_dossier`), the preview/confirm route (`dossier_service.py::check_admitted_scope`) and
the job itself (`src/web/jobs.py`) — one rule, never three drifting copies of it.

Before this: the "≥2 tender-vocabulary terms" lexical heuristic (`ContentSecurityGate`, its `out_of_scope`
reason code) was an ABSOLUTE VETO on the whole dossier's combined text, wherever it ran — contradicting the
very reason lot 50 built a smarter per-piece moderator in the first place ("un document sans le vocabulaire
classique d'un marché peut être pertinent", `document_moderator_agent.py`'s own docstring). A piece the
moderator had ALREADY judged genuinely relevant (shared client/site/reference/lot token, or a real LLM
judgment of sense) could still see the WHOLE dossier rejected — or, after lot 50 bis moved the check earlier,
accepted at confirmation and only THEN rejected inside the job — purely because the combined text happened
to fall under the lexical cutoff.

The fix: the lexical heuristic is demoted to a FALLBACK signal, consulted ONLY when no piece was
independently judged 'lie' by the (heuristic-or-real-LLM) per-piece moderator — never a veto against an
established, contextual relevance judgment. Security (prompt injection) is a SEPARATE, untouched concern:
still an absolute block, checked first, regardless of any piece's relevance. A dossier where NO piece is
judged relevant AND the lexical fallback also finds nothing market-like stays refused, exactly as before —
this only stops a false NEGATIVE (a genuinely relevant piece without classic vocabulary), never loosens the
security check or lets a truly empty/unusable/off-topic dossier through.
"""
from __future__ import annotations

from typing import Any, Iterable

from src.core.content_security import ContentSecurityGate, ModerationResult


def assess_dossier_scope(combined_text: str, pieces: Iterable[Any]) -> ModerationResult:
    """`pieces` is any iterable of objects exposing `.moderation_verdict` (an `IntakePiece`, an
    `AoDossierPiece` ORM row, or a `PieceText` — all three already carry it, directly or via the DB row)."""
    verdict = ContentSecurityGate().check(combined_text)
    if "prompt_injection" in verdict.reason_codes:
        return ModerationResult(False, ["prompt_injection"])
    if any(getattr(p, "moderation_verdict", None) == "lie" for p in pieces):
        return ModerationResult(True, [])
    if "out_of_scope" in verdict.reason_codes:
        return ModerationResult(False, ["out_of_scope"])
    return ModerationResult(True, [])
