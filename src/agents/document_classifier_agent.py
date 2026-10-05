"""Lot 50 §2.B / Lot 50 bis §1 — the classification role for ONE piece of a tender dossier: proposes what KIND
of document a piece is (rc / cctp / ccap / acte_engagement / annexe / autre / indetermine) from its CONTENT
(and, weakly, its file name — never the name alone), keeping the user's DECLARED category, this PROPOSED
category and the eventually CONFIRMED (final) category distinct at every step (never overwritten into each
other).

Two judgment paths, always distinguished in the result (`source`):
- **heuristic** (lot 50): deterministic per-category vocabulary matching — no numeric confidence invented,
  "indetermine" (`category_proposed=None`) on any genuine ambiguity, always available (no network, no LLM).
- **llm** (lot 50 bis): a real call to the account's configured LLM adapter (`src.agents.llm_client`, the SAME
  one `ao_extractor.py` uses — no new provider), given the piece's own text plus its declared category and
  file name as CONTEXT (never an instruction the model should follow). Its citation is verified against the
  exact text the model was shown (`document_llm_support.verify_citation`) before being trusted at all; an
  unavailable provider or an invalid/unverifiable response falls back to the heuristic path, reported as such
  (`source="heuristic_llm_unavailable"` / `"heuristic_llm_invalid"`) — never silently, never presented as an
  LLM validation that did not actually happen.

Never a numeric confidence score in either path (the ticket forbids inventing one). No shell/file/network
access beyond the ONE already-configured LLM call; never moves, deletes or activates anything — this module
only proposes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from src.agents.document_llm_support import bound_for_llm, run_structured_judgment
from src.core.prompt_loader import load_prompt

_PROMPTS_DIR = Path(__file__).parent / "prompts"
_LLM_SYSTEM = (_PROMPTS_DIR / "document_classification_system.txt").read_text(encoding="utf-8")
_LLM_USER_PATH = _PROMPTS_DIR / "document_classification_user.txt"
_CATEGORIES = ("rc", "cctp", "ccap", "acte_engagement", "annexe", "autre", "indetermine")

# Vocabulary is intentionally over-inclusive within a category and DISJOINT enough between categories to be
# a meaningful (not just illustrative) signal — but this is still a heuristic reading, not a promise of
# correctness, and the caller must always let the user confirm/correct it (§2.B: "Aucun déplacement/
# suppression/activation automatique").
_VOCABULARY: dict[str, tuple[str, ...]] = {
    "rc": ("règlement de consultation", "reglement de consultation", "modalités de remise des offres",
           "critères de sélection des candidatures", "délai de remise des plis", "conditions de participation"),
    "cctp": ("cahier des clauses techniques particulières", "cctp", "spécifications techniques", "prestations attendues",
             "description technique", "exigences techniques"),
    "ccap": ("cahier des clauses administratives particulières", "ccap", "conditions de paiement", "pénalités de retard",
             "clauses contractuelles", "modalités de résiliation", "garanties financières"),
    "acte_engagement": ("acte d'engagement", "je soussigné", "m'engage sans réserve", "engagement du candidat",
                         "signature du représentant"),
    "annexe": ("annexe", "pièce jointe", "bordereau des prix", "détail quantitatif estimatif", "dqe"),
    "autre": ("planning", "calendrier prévisionnel", "questions/réponses", "questions-réponses", "rectificatif",
              "avenant", "détail des prix", "courrier de précision", "note de cadrage"),
}
_FILENAME_HINTS: dict[str, tuple[str, ...]] = {
    "rc": ("rc", "reglement", "règlement"), "cctp": ("cctp", "cahier_charges", "cahier-des-charges"),
    "ccap": ("ccap",), "acte_engagement": ("ae", "acte_engagement", "acte-engagement"),
    "annexe": ("annexe", "bpu", "dqe"), "autre": ("planning", "qr", "rectificatif", "avenant"),
}


@dataclass(frozen=True)
class ClassificationResult:
    category_declared: str
    category_proposed: Optional[str]  # None: no usable signal — "indetermine", never a guess presented as sure
    reasons: list[str] = field(default_factory=list)  # short, human-readable motifs — never a numeric confidence
    evidence: list[str] = field(default_factory=list)  # exact matched/cited phrases — for display, never a full-text dump
    agrees_with_declared: Optional[bool] = None  # None when no proposal was possible
    source: str = "heuristic"  # "heuristic" | "llm" | "heuristic_llm_unavailable" | "heuristic_llm_invalid"
    heuristic_category: Optional[str] = None  # kept alongside an "llm" result when the two disagree — never hidden

    @property
    def state(self) -> str:
        return "indetermine" if self.category_proposed is None else self.category_proposed


class DocumentClassifierAgent:
    def _heuristic(self, text: str, *, filename: str, declared_category: str) -> ClassificationResult:
        normalized = (text or "").casefold()
        name = (filename or "").casefold()
        scores: dict[str, list[str]] = {cat: [] for cat in _VOCABULARY}
        for category, terms in _VOCABULARY.items():
            for term in terms:
                if term in normalized:
                    scores[category].append(term)
        # A filename hint only ever ADDS weight to a category that ALSO has at least one content match, or
        # breaks a tie between two content-matched categories — a piece is never classified from its name
        # alone (a hostile or careless file name must not steer the outcome by itself).
        name_hints: dict[str, list[str]] = {cat: [] for cat in _VOCABULARY}
        for category, hints in _FILENAME_HINTS.items():
            for hint in hints:
                if hint and hint in name:
                    name_hints[category].append(hint)

        matched = {cat: terms for cat, terms in scores.items() if terms}
        if not matched:
            return ClassificationResult(category_declared=declared_category, category_proposed=None,
                                        reasons=["Aucun vocabulaire caractéristique reconnu dans le contenu."])

        def rank(cat: str) -> tuple[int, int]:
            return (len(scores[cat]), len(name_hints[cat]))

        best = max(matched, key=rank)
        ties = [c for c in matched if rank(c) == rank(best)]
        if len(ties) > 1:
            # A genuine ambiguity between two categories is reported as such — never resolved by an arbitrary
            # pick presented as certain.
            reasons = [f"Vocabulaire partagé entre {' et '.join(sorted(ties))} : proposition non tranchée."]
            return ClassificationResult(category_declared=declared_category, category_proposed=None, reasons=reasons,
                                        evidence=sorted({t for c in ties for t in scores[c]}))
        reasons = [f"Vocabulaire de « {best} » reconnu : " + ", ".join(scores[best][:3]) + ("…" if len(scores[best]) > 3 else "")]
        if name_hints[best]:
            reasons.append("Nom de fichier cohérent avec cette catégorie.")
        return ClassificationResult(
            category_declared=declared_category, category_proposed=best, reasons=reasons,
            evidence=list(dict.fromkeys(scores[best]))[:5], agrees_with_declared=(best == declared_category),
        )

    def classify(self, text: str, *, filename: str = "", declared_category: str = "autre", llm=None) -> ClassificationResult:
        heuristic = self._heuristic(text, filename=filename, declared_category=declared_category)
        if llm is None:
            return heuristic

        bounded_text, truncated = bound_for_llm(text)
        prompt = load_prompt(_LLM_USER_PATH, text=bounded_text, filename=filename or "(non fourni)", declared=declared_category)
        judgment = run_structured_judgment(
            llm, user_prompt=prompt, system_prompt=_LLM_SYSTEM, source_text=bounded_text,
            allowed_values={"category": set(_CATEGORIES)},
        )
        if judgment.source != "llm":
            source = "heuristic_llm_unavailable" if judgment.source == "llm_unavailable" else "heuristic_llm_invalid"
            return ClassificationResult(**{**heuristic.__dict__, "source": source})

        data = judgment.data
        category = data["category"]
        proposed = None if category == "indetermine" else category
        reasons = [data["reason"]]
        if heuristic.category_proposed and heuristic.category_proposed != proposed:
            reasons.append(f"Lecture par mots-clés en désaccord : proposait « {heuristic.category_proposed} ».")
        if truncated:
            reasons.append("Analyse limitée aux premiers caractères du document (pièce plus longue que la fenêtre d'analyse) — couverture partielle, non dissimulée.")
        evidence = [data["citation"]] if data.get("citation") else []
        return ClassificationResult(
            category_declared=declared_category, category_proposed=proposed, reasons=reasons, evidence=evidence,
            agrees_with_declared=(proposed == declared_category) if proposed else None,
            source="llm", heuristic_category=heuristic.category_proposed,
        )
