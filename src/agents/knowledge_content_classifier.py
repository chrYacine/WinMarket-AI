"""Lot 50 bis §2 — content-type classification for a PRIVATE KNOWLEDGE BASE document (référence, certification,
présentation, autre, indéterminé). A DIFFERENT taxonomy from the AO-dossier classifier
(`document_classifier_agent.py`, rc/cctp/ccap/...): this one describes what a document IS for the account's
own professional use, never whether it names a specific tender. A "certification" proposal is NOT a verified
certification — it never updates the account's profile/scoring by itself (the caller enforces this, not this
module).

Two judgment paths, exactly like the AO-dossier agents (`source` field): "heuristic" (deterministic vocabulary
matching, always available), "llm" (a real, citation-verified call to the account's configured adapter), or a
"heuristic_llm_unavailable"/"heuristic_llm_invalid" fallback — never silently presented as an LLM validation
that did not happen.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from src.agents.document_llm_support import bound_for_llm, run_structured_judgment
from src.core.prompt_loader import load_prompt

_PROMPTS_DIR = Path(__file__).parent / "prompts"
_LLM_SYSTEM = (_PROMPTS_DIR / "knowledge_content_classification_system.txt").read_text(encoding="utf-8")
_LLM_USER_PATH = _PROMPTS_DIR / "knowledge_content_classification_user.txt"
CATEGORIES = ("reference", "certification", "presentation", "autre", "indetermine")

_VOCABULARY: dict[str, tuple[str, ...]] = {
    "reference": ("référence client", "étude de cas", "projet réalisé", "client satisfait", "témoignage client",
                  "mission réalisée pour", "cas client"),
    "certification": ("certification", "certifié", "qualification", "attestation", "label qualité", "accréditation",
                       "iso 9001", "iso 27001", "qualiopi"),
    "presentation": ("plaquette", "présentation de l'entreprise", "qui sommes-nous", "notre équipe", "notre histoire",
                      "organigramme", "curriculum vitae", "cv de"),
    "autre": ("méthodologie", "procédure interne", "mode opératoire", "note de synthèse"),
}


@dataclass(frozen=True)
class ContentClassificationResult:
    category_proposed: Optional[str]  # None: "indetermine" — no confident signal, never a guess presented as sure
    reasons: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    source: str = "heuristic"
    heuristic_category: Optional[str] = None


class KnowledgeContentClassifierAgent:
    def _heuristic(self, text: str) -> ContentClassificationResult:
        normalized = (text or "").casefold()
        scores = {cat: [term for term in terms if term in normalized] for cat, terms in _VOCABULARY.items()}
        matched = {cat: terms for cat, terms in scores.items() if terms}
        if not matched:
            return ContentClassificationResult(category_proposed=None, reasons=["Aucun vocabulaire caractéristique reconnu dans le contenu."])
        best = max(matched, key=lambda c: len(scores[c]))
        ties = [c for c in matched if len(scores[c]) == len(scores[best])]
        if len(ties) > 1:
            return ContentClassificationResult(
                category_proposed=None, reasons=[f"Vocabulaire partagé entre {' et '.join(sorted(ties))} : proposition non tranchée."],
                evidence=sorted({t for c in ties for t in scores[c]}),
            )
        return ContentClassificationResult(
            category_proposed=best, reasons=[f"Vocabulaire de « {best} » reconnu : " + ", ".join(scores[best][:3])],
            evidence=list(dict.fromkeys(scores[best]))[:5],
        )

    def classify(self, text: str, *, filename: str = "", llm=None) -> ContentClassificationResult:
        heuristic = self._heuristic(text)
        if llm is None:
            return heuristic

        bounded_text, truncated = bound_for_llm(text)
        prompt = load_prompt(_LLM_USER_PATH, text=bounded_text, filename=filename or "(non fourni)")
        judgment = run_structured_judgment(
            llm, user_prompt=prompt, system_prompt=_LLM_SYSTEM, source_text=bounded_text,
            allowed_values={"category": set(CATEGORIES)},
        )
        if judgment.source != "llm":
            source = "heuristic_llm_unavailable" if judgment.source == "llm_unavailable" else "heuristic_llm_invalid"
            return ContentClassificationResult(**{**heuristic.__dict__, "source": source})

        data = judgment.data
        category = data["category"]
        proposed = None if category == "indetermine" else category
        reasons = [data["reason"]]
        if heuristic.category_proposed and heuristic.category_proposed != proposed:
            reasons.append(f"Lecture par mots-clés en désaccord : proposait « {heuristic.category_proposed} ».")
        if truncated:
            reasons.append("Analyse limitée aux premiers caractères du document — couverture partielle, non dissimulée.")
        evidence = [data["citation"]] if data.get("citation") else []
        return ContentClassificationResult(
            category_proposed=proposed, reasons=reasons, evidence=evidence, source="llm", heuristic_category=heuristic.category_proposed,
        )
