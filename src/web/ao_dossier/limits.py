"""The categories and the effective limits of an AO dossier — ONE place, served to the API validation, the
intake and the page (`/app/analyser`). Sizes are DECIMAL (1 Mo = 1 000 000 octets) and never mixed with the
Mio of the RAG corpus limits."""
from __future__ import annotations

from src.core import config
from src.web.knowledge import extraction

CATEGORIES = ("rc", "cctp", "ccap", "acte_engagement", "annexe", "autre")
MAIN_CATEGORIES = ("rc", "cctp", "ccap", "acte_engagement")  # the 4 guided slots — none of them mandatory (lot 50 §4)

# The multipart field carrying each category. Four named GUIDED slots of one file each (all optional, lot 50
# §4 — a dossier is never required to cover all of RC/CCTP/CCAP/acte), plus `annexes`, plus (lot 50 §1) a
# free-form `autres` slot for pieces that do not fit a guided label (planning, questions/réponses, rectificatif,
# détail des prix, courrier de précision, ...) — an open example list, never a closed business list and never a
# new accepted FORMAT. Every slot shares the SAME total-file/byte/char budgets (config.DOSSIER_MAX_*); a free-
# form piece never gets an allocation of its own.
SLOTS = (
    {"field": "rc", "category": "rc", "label": "RC – Règlement de consultation", "max_files": 1},
    {"field": "cctp", "category": "cctp", "label": "CCTP / Cahier des charges", "max_files": 1},
    {"field": "ccap", "category": "ccap", "label": "CCAP / Conditions contractuelles", "max_files": 1},
    {"field": "acte_engagement", "category": "acte_engagement", "label": "Acte d'engagement", "max_files": 1},
    {"field": "annexes", "category": "annexe", "label": "Annexes", "max_files": None},  # bounded by DOSSIER_MAX_ANNEXES
    {"field": "autres", "category": "autre", "label": "Autres pièces liées à cet appel d'offres", "max_files": None},  # bounded by the total DOSSIER_MAX_FILES only
)
FIELD_TO_CATEGORY = {slot["field"]: slot["category"] for slot in SLOTS}
CATEGORY_SHORT = {"rc": "RC", "cctp": "CCTP", "ccap": "CCAP", "acte_engagement": "Acte d'engagement", "annexe": "Annexe", "autre": "Autre pièce"}
CATEGORY_LABEL = {slot["category"]: slot["label"] for slot in SLOTS}
# Suggested labels shown as EXAMPLES next to the free-form slot — illustrative only, never a closed list the
# server enforces (a piece named/classified differently is never rejected for that reason alone, lot 50 §1).
AUTRE_EXAMPLES = ("planning", "questions/réponses", "rectificatif", "détail des prix", "courrier de précision")


def format_bytes(n: int) -> str:
    """Decimal, French notation: 12,3 Ko / 4,5 Mo — the same convention as the page."""
    if n < 1_000_000:
        return f"{n / 1000:.1f} Ko".replace(".", ",")
    return f"{n / 1_000_000:.1f} Mo".replace(".", ",")


_UNBOUNDED_SLOT_CAP = {"annexe": config.DOSSIER_MAX_ANNEXES, "autre": config.DOSSIER_MAX_FILES}


def dossier_limits() -> dict:
    """The effective limits, as served to the interface (`data-*` attributes of the page). A slot without its
    own `max_files` (currently `annexe` and `autre`) is bounded by its own category cap where one exists
    (annexes: DOSSIER_MAX_ANNEXES) or otherwise only by the dossier's total file count (DOSSIER_MAX_FILES) —
    never a NEW, separate allocation for free-form pieces (lot 50 §1)."""
    slots = [{**slot, "max_files": slot["max_files"] if slot["max_files"] is not None else _UNBOUNDED_SLOT_CAP.get(slot["category"], config.DOSSIER_MAX_FILES)} for slot in SLOTS]
    return {
        "max_total_bytes": config.DOSSIER_MAX_TOTAL_BYTES,
        "max_total_label": f"{config.DOSSIER_MAX_TOTAL_BYTES / 1_000_000:g} Mo",
        "max_files": config.DOSSIER_MAX_FILES,
        "max_annexes": config.DOSSIER_MAX_ANNEXES,
        "slots": slots,
        "formats": sorted(extraction.SUPPORTED_SUFFIXES),
        "max_extracted_chars": config.DOSSIER_MAX_EXTRACTED_CHARS,
        "autre_examples": list(AUTRE_EXAMPLES),
        "main_categories": list(MAIN_CATEGORIES),
    }
