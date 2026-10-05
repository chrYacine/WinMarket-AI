"""B05-T2 (DEFECT confirmed): the extraction prompt has always told the
model "null si absent" for `deadline_reponse`, but `AOContext.
deadline_reponse` was `str = ""` — a null response made the WHOLE
`AOContext(**data)` construction raise, silently discarding every OTHER
already-valid field (client, budget, durée, certifications...) and
forcing the full local-regex fallback for the entire AO. This module
replaces that all-or-nothing construction with a PER-FIELD resolver: each
field is validated independently, using a local fallback ONLY for that
one field when the model's own value is missing or invalid, and only
when the fallback actually finds something in the text — never
fabricating an absence into a guess. See src.core.models.AOContext for
the field_provenance/extraction_status/extraction_reason contract this
records.
"""
from __future__ import annotations

import math
import re
import unicodedata
from pathlib import Path
from typing import Callable, Optional

from src.core.models import AOContext, ExtractedFact
from src.agents.certification_scope import analyze_certification_mentions, extract_certifications as _extract_certifications, resolve_mandatory_certifications
from src.agents.llm_client import ClaudeClient
from src.core.content_preparation import wrap_untrusted_content
from src.core.config import LLM_TEMPERATURE_FACTUAL
from src.core.prompt_loader import PromptLoadError, load_prompt

_PROMPTS_DIR = Path(__file__).parent / "prompts"
_EXTRACTION_SYSTEM = (_PROMPTS_DIR / "ao_extraction_system.txt").read_text(encoding="utf-8")
_EXTRACTION_USER_PATH = _PROMPTS_DIR / "ao_extraction_user.txt"
# B05-T3: a SEPARATE, small prompt pair for the additive business-facts
# extraction call below — never merged into the primary extraction
# prompt/schema above, so this addition can never destabilize the
# existing, extensively-tested core extraction (titre/client/budget/...).
# Loaded via load_prompt (not read eagerly at import time like
# _EXTRACTION_SYSTEM above) since this call only ever happens when a
# policy actually configured custom criteria — see _extract_facts_via_llm.
_FACTS_SYSTEM_PATH = _PROMPTS_DIR / "ao_facts_extraction_system.txt"
_FACTS_USER_PATH = _PROMPTS_DIR / "ao_facts_extraction_user.txt"


def read_document(path: str) -> str:
    p = Path(path)
    if p.suffix.lower() == ".pdf":
        import fitz
        doc = fitz.open(path)
        return "\n".join(page.get_text("text") for page in doc)
    if p.suffix.lower() in [".txt", ".md"]:
        return p.read_text(encoding="utf-8", errors="ignore")
    if p.suffix.lower() == ".docx":
        from docx import Document
        d = Document(path)
        return "\n".join([para.text for para in d.paragraphs])
    return p.read_text(encoding="utf-8", errors="ignore")


def _find_budget(text: str):
    m = re.search(r"(\d{1,3}(?:[\s.]\d{3})+|\d{5,})\s*€", text)
    if not m:
        return None
    return float(re.sub(r"[\s.]", "", m.group(1)))


_TECH_VOCAB = [
    "React", "Angular", "Vue", "Java", "Spring", "Python", "Django", "FastAPI",
    ".NET", "C#", "Azure", "AWS", "GCP", "Docker", "Kubernetes", "Power BI",
    "PostgreSQL", "Oracle", "SQL Server", "IA", "RAG", "LLM", "Node", "NodeJS",
    "TypeScript", "DevOps", "Terraform", "Ansible", "SAP", "SharePoint"
]


def _extract_technologies(text: str) -> list[str]:
    low = text.lower()
    return sorted({v for v in _TECH_VOCAB if v.lower() in low})


_SECTOR_KEYWORDS = {
    "Finance/Assurance": ["mutuelle", "assurance", "banque", "financ", "crédit", "credit", "caisse", "axa", "maif", "maaf", "groupama"],
    "Santé": ["hôpital", "hopital", "santé", "sante", "médical", "medical", "clinique", "ehpad", "chu", "ars"],
    "Collectivité/Public": ["métropole", "metropole", "commune", "mairie", "région", "region", "département", "departement", "agglo", "intercommunal", "préfecture", "ministère"],
    "Éducation": ["université", "universite", "école", "ecole", "formation", "académie", "cfa", "lycée", "campus"],
    "Transport": ["transport", "sncf", "ratp", "aéroport", "aeroport", "autoroute", "mobilité"],
    "Énergie": ["énergie", "energie", "electricité", "electricite", "engie", "edf", "rte", "grdf"],
    "Industrie": ["industrie", "manufactur", "usine", "production", "logistique"],
    "Défense": ["défense", "defense", "armée", "armee", "militaire", "dga"],
}

_CLIENT_PATTERNS = [
    r"Client\s*:\s*(.+)", r"Acheteur\s*:\s*(.+)", r"Entreprise\s*:\s*(.+)", r"Pouvoir\s+adjudicateur\s*:\s*(.+)",
]

_DEADLINE_PATTERNS = [
    r"(?:date\s+limite(?:\s+de\s+remise(?:\s+des\s+offres)?)?|limite\s+de\s+remise\s+des\s+offres|deadline)"
    r"[^\n]{0,60}?(\d{1,2}[\/.\-]\d{1,2}[\/.\-]\d{2,4})",
]


# ---------------------------------------------------------------------------
# Per-field local fallback detectors — reused, per field, ONLY when the
# model's own value for that field is missing or invalid, and ONLY applied
# when they actually find something (an absence stays an absence rather
# than being guessed at, ticket section 3).
# ---------------------------------------------------------------------------

def _detect_title_locally(text: str) -> Optional[str]:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    return lines[0][:120] if lines else None


def _detect_client_locally(text: str) -> Optional[str]:
    for pat in _CLIENT_PATTERNS:
        m = re.search(pat, text, re.I)
        if m:
            return m.group(1).strip()[:100]
    return None


def _detect_sector_locally(text: str) -> Optional[str]:
    text_lower = text.lower()
    for secteur, keywords in _SECTOR_KEYWORDS.items():
        if any(kw in text_lower for kw in keywords):
            return secteur
    return None


def _detect_deadline_locally(text: str) -> Optional[str]:
    """Deliberately narrow: only an unambiguous DD/MM/YYYY-style date near
    an explicit deadline keyword is recognized. A relative phrasing ("dans
    3 semaines") is NOT reconstructed locally — that requires reading the
    surrounding sentence, which only the LLM does; recognizing it falls
    back to `None` (absent) rather than fabricating a date (ticket:
    "sans fabriquer de date")."""
    for pat in _DEADLINE_PATTERNS:
        m = re.search(pat, text, re.I)
        if m:
            return m.group(1)
    return None


def _detect_duration_locally(text: str) -> Optional[int]:
    m = re.search(r"(\d{1,3})\s*mois\b", text, re.I)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d{1,2})\s*ans?\b", text, re.I)
    if m:
        return int(m.group(1)) * 12
    return None


def _detect_questions_locally(text: str) -> list[str]:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    return [l for l in lines if "?" in l][:10]


def _detect_livrables_locally(text: str) -> list[str]:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    return [l for l in lines if "livrable" in l.lower() or "dossier" in l.lower()][:8]


def _detect_contraintes_locally(text: str) -> list[str]:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    return [l for l in lines if any(w in l.lower() for w in ["obligatoire", "imperatif", "exige", "deadline", "delai"])][:8]


# ---------------------------------------------------------------------------
# B05-T3: private, sector-neutral business-fact extraction — additive,
# separate from every fixed field above. `requested_facts` (built by
# src.agents.business_facts.requested_facts_from_criteria from the active
# ScoringPolicy's own custom_criteria) names exactly which facts THIS
# account's configuration actually needs; nothing is ever extracted beyond
# that list, and nothing here can influence the fixed fields above.
# ---------------------------------------------------------------------------

_CAPITALIZED = r"[A-ZÀ-ÖØ-Þ][\w'’\-]*"
# A connector that continues an enumeration: "Lyon, Marseille", "Lyon et
# Marseille", "Lyon / Marseille", "Lyon ainsi que Marseille", or a bullet
# list ("- Lyon\n- Marseille"). Lowercase words only (never re.I): the
# capitalized item that follows is what identifies a possible proper noun.
_ENUM_CONNECTOR = r"(?:[,;/&]|\bet\b|\bou\b|\bainsi\s+qu(?:e|['’])|\bplus\b|\n\s*[-•*]\s*)"
_ENUM_ARTICLE = r"(?:de\s+|d['’]\s*|du\s+|des\s+|la\s+|le\s+|les\s+)?"
_NON_EXHAUSTIVE = re.compile(r"\b(?:etc\b|notamment|entre\s+autres|et\s+autres|par\s+exemple)|…|\.\.\.", re.I)


def _list_mentions(text: str, vocabulary: list[str]) -> list[tuple[str, int, int]]:
    mentions: list[tuple[str, int, int]] = []
    for value in vocabulary:
        value = str(value).strip()
        if not value:
            continue
        for m in re.finditer(rf"(?<!\w){re.escape(value)}(?!\w)", text, re.I):
            mentions.append((value, m.start(), m.end()))
    return mentions


def _enumeration_may_include_unrecognized_items(text: str, mentions: list[tuple[str, int, int]], vocabulary: list[str]) -> bool:
    """True when a recognized mention sits inside what looks like a list
    whose OTHER items are not in the recognition vocabulary (or the list is
    announced as non-exhaustive). The local fallback can only recognize the
    provider's own declared names — it cannot know an AO's requirement list
    is complete, so it must never present a partial recognition as the
    whole requirement (lot 41, D3-02: "Lyon et Marseille" against a
    provider covering only Lyon)."""
    vocab_folded = [v.strip().casefold() for v in vocabulary if isinstance(v, str) and v.strip()]
    for _value, start, end in mentions:
        after = text[end:end + 80]
        if _NON_EXHAUSTIVE.search(after[:40]):
            return True
        m = re.match(rf"\s*{_ENUM_CONNECTOR}\s*{_ENUM_ARTICLE}({_CAPITALIZED})", after)
        if m:
            following = after[m.start(1):].casefold()
            if not any(following.startswith(v) for v in vocab_folded):
                return True
        before = text[max(0, start - 80):start]
        m = re.search(rf"({_CAPITALIZED})\s*{_ENUM_CONNECTOR}\s*{_ENUM_ARTICLE}$", before)
        if m:
            word = m.group(1).casefold()
            if not any(v.endswith(word) for v in vocab_folded):
                return True
    return False


def _boolean_polarity(text: str, label: str) -> tuple[str, str]:
    """Polarity of a boolean requirement, from what surrounds each mention
    of its label: returns (state, reason) where state is "true", "false",
    "ambiguous" or "absent". Presence of the label alone is NOT an
    affirmation ("Travail de nuit : non." must never become true), and
    contradictory mentions are reported as ambiguous instead of picking a
    side (lot 41, D3-02)."""
    if not label:
        return "absent", "no_label"
    verdicts: list[str] = []
    for m in re.finditer(rf"(?<!\w){re.escape(label)}(?!\w)", text, re.I):
        after = re.split(r"[.\n;]", text[m.end():m.end() + 80], maxsplit=1)[0]
        before = re.split(r"[.\n;]", text[max(0, m.start() - 60):m.start()])[-1]
        negative = (
            re.match(r"\s*(?:[:=\-–]\s*)?(?:non|no|faux|false|aucun|aucune|pas)\b", after, re.I)
            or re.match(r"\s*(?:\w+\s+)?(?:n['’]est|ne\s+sera|ne\s+sont|ne\s+seront)\s+(?:pas|plus)\b", after, re.I)
            or re.search(r"(?:\bpas\s+d(?:e|['’])\s*|\baucune?\s+|\bsans\s+|\bhors\s+)$", before, re.I)
        )
        positive = (
            re.match(r"\s*(?:[:=\-–]\s*)?(?:oui|yes|true|vrai)\b", after, re.I)
            or re.match(r"\s*(?:\w+\s+)?(?:(?:est|sera|sont|seront)\s+)?(?:exig|requis|obligatoire|demand|n[ée]cessaire|pr[ée]vu)", after, re.I)
            or re.search(
                r"\b(?:exig\w*|requis\w*|obligatoire\w*|pr[ée]voi\w*|demand\w*|n[ée]cessit\w*|avec)\s+"
                r"(?:d['’]\s*|de\s+|du\s+|des\s+|le\s+|la\s+|les\s+|un\s+|une\s+)?$",
                before, re.I,
            )
        )
        # A negation marker wins inside ONE mention ("Aucun travail de
        # nuit ne sera demandé" also contains an affirmative verb, but it
        # is a negation); genuine contradictions are between mentions.
        if negative:
            verdicts.append("false")
        elif positive:
            verdicts.append("true")
        else:
            verdicts.append("ambiguous")
    if not verdicts:
        return "absent", "label_not_found"
    if len(set(verdicts)) == 1 and verdicts[0] != "ambiguous":
        return verdicts[0], "ok"
    return "ambiguous", "polarity_unclear_or_contradictory"


def _extract_fact_locally(text: str, spec: dict) -> ExtractedFact:
    """Deterministic, no-LLM fallback — deliberately narrow per type,
    exactly like every other local detector in this module: an absence
    stays an absence rather than being guessed at, and a partial or
    contradictory recognition is reported as "ambiguous" (with a `reason`),
    never promoted to a complete, favorable-looking value."""
    fact_type = spec.get("type")
    label = str(spec.get("label") or "").strip()

    if fact_type == "number":
        # A number is only ever trusted when it is anchored to the
        # REQUESTED UNIT itself (a label-only match once returned the year
        # 2025 as a "capacity"). No unit declared means no safe local
        # signal exists: "absent" rather than a guess.
        unit = spec.get("unit")
        if not unit:
            return ExtractedFact(status="absent", provenance="absent")
        # A unit is a technical slug the ACCOUNT typed (e.g. "par_semaine")
        # — real AO text says "par semaine". Every underscore is a
        # flexible word separator (space or nothing).
        unit_pattern = re.escape(unit).replace("_", r"[\s_]*")
        # Up to a short handful of other words between the number and the
        # unit ("3 fois par semaine"); `\D` never bridges into another digit.
        candidates = re.findall(rf"(\d+(?:[.,]\d+)?)\D{{0,15}}?{unit_pattern}\b", text, re.I)
        if not candidates:
            candidates = re.findall(rf"\b{unit_pattern}\D{{0,15}}?(\d+(?:[.,]\d+)?)", text, re.I)
        values: set[float] = set()
        for raw in candidates:
            try:
                value = float(raw.replace(",", "."))
            except ValueError:
                continue
            if math.isfinite(value):  # float("9" * 400) is inf
                values.add(value)
        if len(values) > 1:
            return ExtractedFact(status="ambiguous", provenance="fallback", reason="conflicting_values")
        if values:
            return ExtractedFact(value=values.pop(), unit=unit, status="found", provenance="fallback")
        return ExtractedFact(status="absent", provenance="absent")

    if fact_type == "list":
        # Recognition vs requirement (lot 41, D3-02): the provider's own
        # declared names (`recognition_vocabulary`) are only a set of
        # names the fallback CAN recognize — they are not the AO's
        # requirement list, and recognizing some of them does not prove the
        # requirement is limited to them.
        vocabulary = list(spec.get("recognition_vocabulary") or spec.get("known_values") or [])
        mentions = _list_mentions(text, vocabulary)
        if not mentions:
            return ExtractedFact(status="absent", provenance="absent")
        if _enumeration_may_include_unrecognized_items(text, mentions, vocabulary):
            return ExtractedFact(status="ambiguous", provenance="fallback", reason="partial_list_possible")
        first_seen: dict[str, int] = {}
        for value, start, _end in mentions:
            first_seen[value] = min(start, first_seen.get(value, start))
        found = sorted(first_seen, key=first_seen.get)
        return ExtractedFact(value=found, status="found", provenance="fallback")

    if fact_type == "boolean":
        state, reason = _boolean_polarity(text, label)
        if state == "true":
            return ExtractedFact(value=True, status="found", provenance="fallback")
        if state == "false":
            return ExtractedFact(value=False, status="found", provenance="fallback")
        if state == "ambiguous":
            return ExtractedFact(status="ambiguous", provenance="fallback", reason=reason)
        return ExtractedFact(status="absent", provenance="absent")

    # "text": free-form — no safe deterministic recognition without an LLM.
    return ExtractedFact(status="absent", provenance="absent")


# ---------------------------------------------------------------------------
# Lot 43: omission control for a LIST fact reported by the LLM.
#
# The local fallback was hardened in lot 41 (a recognized item inside an
# enumeration with other items is "ambiguous"), but a list the LLM returns
# was accepted as-is: a response that dropped "Marseille" from "sites de Lyon
# et de Marseille" made the coverage look complete. The check below reconciles
# the model's list with the explicit enumerations found in the document,
# WITHOUT a second LLM call, and reports any mismatch as "ambiguous" (which
# the engine turns into INCOMPLET — never a favorable fallback).
#
# What it covers (and what it does not) is documented in
# docs/api/B06_SCORING_CONFIG_CONTRACT.md ("Contrôle d'omission des listes").
# Requirements are NEVER built from the account's own declared vocabulary,
# and a city that appears outside a passage about the requested fact (a
# buyer's address, a head-office) is never turned into a required item.
# ---------------------------------------------------------------------------

_LABEL_STOP_WORDS = frozenset({
    "dans", "pour", "avec", "sont", "entre", "leur", "leurs", "cette", "tous", "tout", "plus", "vers", "chez", "sans",
})
_BULLET = re.compile(r"^\s*(?:[-•*–]|\d{1,2}[.)])\s+")
_ITEM = rf"{_CAPITALIZED}(?:\s+{_CAPITALIZED})*"
# Capitalized items joined by an enumeration connector: "Lyon et de
# Marseille", "Lyon, Villeurbanne, Vénissieux", "- Lyon\n- Marseille".
_ENUM_RUN = re.compile(rf"(?<!\w)({_ITEM})((?:\s*{_ENUM_CONNECTOR}\s*{_ENUM_ARTICLE}{_ITEM})+)")
_LOWER_ENUM_SPLIT = re.compile(r"\s*(?:[,;/&]|\bet\b|\bou\b|\bainsi\s+qu(?:e|['’]))\s*", re.I)
_LEADING_ARTICLE = re.compile(r"^(?:de\s+|d['’]\s*|du\s+|des\s+|la\s+|le\s+|les\s+|l['’]\s*)", re.I)


def _fold(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", str(text)) if not unicodedata.combining(c)).casefold()


def _words(text: str) -> str:
    """Accent/case-insensitive, punctuation-insensitive form used to compare a
    value with the document ("Saint-Étienne" == "saint etienne")."""
    return re.sub(r"[\W_]+", " ", _fold(text)).strip()


def _stem(word: str) -> str:
    word = _fold(word)
    return word[:-1] if len(word) > 4 and word.endswith(("s", "x")) else word


def _anchor_stems(key: str, spec: dict) -> set[str]:
    """Words of the fact's OWN label/identifier ("Sites d'intervention",
    `zone_intervention`) — the only thing that tells a passage is about this
    fact. Nothing is taken from the account's declared values."""
    words = re.findall(r"[^\W\d_]{4,}", _fold(f"{spec.get('label') or ''} {str(key).replace('_', ' ')}"))
    return {_stem(w) for w in words if w not in _LABEL_STOP_WORDS}


def _passages(text: str) -> list[str]:
    """Lines/sentences of the document; a line ending with ':' is kept
    together with the bullet lines that follow it (one passage)."""
    lines = text.splitlines()
    passages: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        i += 1
        if not line:
            continue
        block = line
        if line.endswith(":"):
            while i < len(lines) and _BULLET.match(lines[i]):
                block += "\n" + lines[i].strip()
                i += 1
        if "\n" in block:
            passages.append(block)
        else:
            passages.extend(s for s in re.split(r"(?<=[.!?])\s+(?=[A-ZÀ-ÖØ-Þ0-9])", block) if s.strip())
    return passages


def _enumerated_items(passage: str, anchors: set[str]) -> list[str]:
    """Items an explicit enumeration of `passage` lists: bullet lines under
    the passage's header, capitalized items joined by connectors, and the
    short comma/`et`-separated items following the label's colon."""
    clean = re.sub(r"\([^)]*\)", " ", passage)
    items: list[str] = []
    if "\n" in clean:
        for line in clean.splitlines()[1:]:
            item = _BULLET.sub("", line).strip(" .;,")
            if item and len(item.split()) <= 6:
                items.append(item)
    for m in _ENUM_RUN.finditer(clean):
        items.append(m.group(1))
        items.extend(re.findall(_ITEM, m.group(2)))
    head, colon, tail = clean.partition(":")
    if colon and anchors & {_stem(w) for w in re.findall(r"[^\W\d_]{4,}", head)}:
        for raw in _LOWER_ENUM_SPLIT.split(tail.split("\n")[0]):
            item = _LEADING_ARTICLE.sub("", raw.strip(" .;,"))
            if item and len(item.split()) <= 3:
                items.append(item)
    return items


def _llm_list_incomplete_reason(text: str, key: str, spec: dict, values: list[str]) -> Optional[str]:
    """None when the model's list is consistent with the document; else a
    short machine-readable reason. Only "list" facts; only passages that
    reuse a word of the fact's label are cross-checked."""
    padded_text = f" {_words(text)} "
    folded_values = [_words(v) for v in values]
    for original, folded in zip(values, folded_values):
        if not folded or f" {folded} " not in padded_text:
            return f"llm_value_not_in_document: {original}"
    anchors = _anchor_stems(key, spec)
    if not anchors:
        return None
    for passage in _passages(text):
        passage_stems = {_stem(w) for w in re.findall(r"[^\W\d_]{4,}", passage)}
        if not anchors & passage_stems:
            continue
        if _NON_EXHAUSTIVE.search(passage):
            return "llm_list_non_exhaustive_marker"
        for item in _enumerated_items(passage, anchors):
            folded_item = _words(item)
            if not any(folded_item == v or folded_item in v or v in folded_item for v in folded_values):
                return f"llm_list_may_omit: {item}"
    return None


def _validate_extracted_fact_value(raw: object, fact_type: str) -> tuple[object, bool]:
    """Type-checks ONE LLM-supplied fact value against its declared type —
    mirrors this module's _validate_str/_validate_float/_validate_str_list
    idiom, kept separate since a fact's type is chosen at runtime (not a
    fixed AOContext field)."""
    if fact_type == "number":
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
            return None, False
        return float(raw), True
    if fact_type == "list":
        if not isinstance(raw, list) or not all(isinstance(x, str) and not isinstance(x, bool) for x in raw):
            return None, False
        cleaned = [x.strip() for x in raw if x.strip()]
        return cleaned, bool(cleaned)
    if fact_type == "boolean":
        if not isinstance(raw, bool):
            return None, False
        return raw, True
    if fact_type == "text":
        if not isinstance(raw, str) or isinstance(raw, bool) or not raw.strip():
            return None, False
        return raw.strip(), True
    return None, False


# ---------------------------------------------------------------------------
# Field-level validation — each function returns the cleaned value or
# _INVALID; never raises, never partially mutates the raw LLM response.
# ---------------------------------------------------------------------------

_INVALID = object()
_MISSING = object()


def _validate_str(raw: object) -> object:
    if isinstance(raw, bool) or not isinstance(raw, str):
        return _INVALID
    stripped = raw.strip()
    return stripped if stripped else _INVALID


def _validate_float(raw: object) -> object:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return _INVALID
    value = float(raw)
    return value if math.isfinite(value) else _INVALID


def _validate_int_like(raw: object) -> object:
    if isinstance(raw, bool):
        return _INVALID
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float) and math.isfinite(raw) and raw.is_integer():
        return int(raw)
    return _INVALID


def _validate_str_list(raw: object) -> object:
    if not isinstance(raw, list):
        return _INVALID
    if not all(isinstance(item, str) and not isinstance(item, bool) for item in raw):
        return _INVALID
    return [item.strip() for item in raw if item.strip()]


def _is_empty(value: object) -> bool:
    return value is None or value == [] or value == ""


def _resolve_field(
    data: dict, key: str, validate: Callable[[object], object], local_fn: Optional[Callable[[], object]],
) -> tuple[object, str]:
    """Resolves ONE AOContext field to (value, provenance_code) — see
    src.core.models.AOContext's field_provenance docstring for the exact
    meaning of each of the four codes. `local_fn` is called at most once,
    and ONLY when the model's own value for this field is missing or
    invalid — a valid model value is never second-guessed by a local
    heuristic (that comparison, when it happens at all, is a deliberate,
    separate conflict check — see `_conflicts_with_local`)."""
    raw = data.get(key, _MISSING)
    if raw is _MISSING or raw is None:
        local_value = local_fn() if local_fn else None
        return (None, "absent") if _is_empty(local_value) else (local_value, "fallback")
    value = validate(raw)
    if value is not _INVALID:
        return value, "llm"
    local_value = local_fn() if local_fn else None
    return (None, "rejected") if _is_empty(local_value) else (local_value, "fallback")


def _conflicts_with_local(llm_value: list[str], local_value: list[str]) -> bool:
    """A soft, explicit disagreement signal between a validated LLM list
    and an existing narrow local detector for the SAME field — never used
    to override either value (ticket section 3: "signalé, pas fusionné en
    silence"). Case-insensitive set comparison; not meant to be precise,
    only to flag that the two sources disagree and a human/B17-T1 should
    look."""
    return {v.lower() for v in llm_value} != {v.lower() for v in local_value}


class AOExtractor:
    def __init__(self):
        self.llm = ClaudeClient()

    def extract(self, text: str, requested_facts: Optional[dict] = None, *, allow_llm: bool = True) -> AOContext:
        """Lot 41 (D3-01): `allow_llm=False` forces the deterministic local
        path for EVERYTHING (primary extraction and requested facts) even
        when a provider is configured and enabled — used by the scoring
        simulation, which by contract never calls an external provider.
        Default True: every existing caller is unchanged.

        B05-T3: `requested_facts` (default None -> {}) is additive and
        never changes any existing behavior/return path below — every
        pre-existing caller (src/core/analysis_service.py::extract_ao with
        no third argument) is byte-for-byte unaffected. When non-empty
        (the active ScoringPolicy configured custom criteria — see
        src.agents.business_facts.requested_facts_from_criteria), the
        resulting AOContext additionally carries `extracted_facts` for
        exactly those keys, resolved by `_resolve_requested_facts` below —
        completely independent of whether the PRIMARY extraction above
        used the LLM or its local fallback."""
        if not allow_llm or not self.llm.enabled:
            ao = self._build_ao(text, data=None, status="fallback_local", reason="llm_disabled")
        else:
            prompt = load_prompt(_EXTRACTION_USER_PATH, text=wrap_untrusted_content(text[:25000]))
            data = self.llm.json_complete(prompt, system=_EXTRACTION_SYSTEM, temperature=LLM_TEMPERATURE_FACTUAL, max_tokens=8000)
            # json_complete() already collapses "provider exception", "empty
            # response" and "response never parsed as JSON" into a single
            # `None` — a more granular reason would require reimplementing its
            # retry/parsing logic here, which would duplicate rules the ticket
            # asks not to duplicate. `None` is treated as one fallback reason.
            if data is None:
                ao = self._build_ao(text, data=None, status="fallback_local", reason="no_content")
            # A response that parsed as JSON but is not an object (a bare
            # list/string/number) must never reach `.get()`/`.strip()` — this
            # is exactly the "réponse JSON de forme inattendue" the ticket
            # calls out.
            elif not isinstance(data, dict):
                ao = self._build_ao(text, data=None, status="fallback_local", reason="invalid_response_shape")
            else:
                ao = self._build_ao(text, data=data, status=None, reason=None)

        if requested_facts:
            ao.extracted_facts = self._resolve_requested_facts(text, requested_facts, allow_llm=allow_llm)
        return ao

    def _resolve_requested_facts(self, text: str, requested_facts: dict, *, allow_llm: bool = True) -> dict[str, ExtractedFact]:
        llm_results = (self._extract_facts_via_llm(text, requested_facts) if allow_llm else None) or {}
        resolved: dict[str, ExtractedFact] = {}
        for key, spec in requested_facts.items():
            result = llm_results.get(key)
            if result is not None and result.status == "found":
                if spec.get("type") == "list":
                    # Lot 43: a list the model returned is cross-checked
                    # against the document's own enumerations. A mismatch is
                    # "ambiguous" and deliberately does NOT fall through to
                    # the local fallback below (which could rebuild a
                    # favorable-looking list from the account's vocabulary).
                    reason = _llm_list_incomplete_reason(text, key, spec, result.value)
                    if reason is not None:
                        resolved[key] = ExtractedFact(status="ambiguous", provenance="llm", reason=reason)
                        continue
                resolved[key] = result
                continue
            # The LLM found nothing usable for this one fact (or was never
            # called at all, e.g. disabled) — deterministic local fallback,
            # never fabricated (ticket: "repli déterministe sur les
            # informations effectivement reconnues").
            resolved[key] = _extract_fact_locally(text, spec)
        return resolved

    def _extract_facts_via_llm(self, text: str, requested_facts: dict) -> Optional[dict[str, ExtractedFact]]:
        """Returns None if the call itself could not produce anything
        usable at all (LLM disabled, provider failure, malformed top-level
        response, missing prompt file) — the caller then falls back to the
        local heuristic for EVERY requested fact, never a half-applied
        result. A per-fact "found: false" or a value that fails its
        declared type check is a normal "absent"/"rejected" entry within
        an otherwise-usable response, not a reason to discard the whole
        call."""
        if not self.llm.enabled:
            return None
        facts_list = "\n".join(
            f"- {key} ({spec.get('label') or key}) : type={spec.get('type')}"
            + (f", unite={spec['unit']}" if spec.get("unit") else "")
            for key, spec in requested_facts.items()
        )
        try:
            prompt = load_prompt(_FACTS_USER_PATH, text=wrap_untrusted_content(text[:15000]), facts_list=facts_list)
            system = load_prompt(_FACTS_SYSTEM_PATH)
        except PromptLoadError:
            return None
        data = self.llm.json_complete(prompt, system=system, temperature=LLM_TEMPERATURE_FACTUAL, max_tokens=2000)
        if not isinstance(data, dict):
            return None
        results: dict[str, ExtractedFact] = {}
        for key, spec in requested_facts.items():
            entry = data.get(key)
            if not isinstance(entry, dict) or not entry.get("found"):
                results[key] = ExtractedFact(status="absent", provenance="llm")
                continue
            value, ok = _validate_extracted_fact_value(entry.get("value"), spec.get("type"))
            if not ok:
                # The model claimed to have found this fact ("found": true)
                # but the value it sent doesn't match the declared type —
                # a real disagreement, not a plain absence.
                results[key] = ExtractedFact(status="ambiguous", provenance="rejected")
                continue
            # Reviewer-caught (confirmed): using `spec.get("unit")` (the
            # REQUESTED unit) here — rather than the unit the model
            # actually reported for what it found in the AO — silently
            # stamped every LLM-extracted value as "same unit as
            # requested" by construction, making evaluate_custom_
            # criterion's unit-mismatch guard unable to ever fire even
            # when the AO genuinely expressed the value in a different
            # unit (a silent, undetectable conversion — exactly what the
            # ticket forbids). The prompt (ao_facts_extraction_system.txt)
            # now explicitly asks for the unit AS WRITTEN in the document;
            # a non-string reported unit is treated as none stated.
            reported_unit = entry.get("unit")
            unit = reported_unit if isinstance(reported_unit, str) and reported_unit.strip() else None
            results[key] = ExtractedFact(value=value, unit=unit, status="found", provenance="llm")
        return results

    def _build_ao(self, text: str, *, data: Optional[dict], status: Optional[str], reason: Optional[str]) -> AOContext:
        """Builds ONE AOContext from `data` (the raw, UNMODIFIED LLM
        response dict, or None when there is none at all) plus per-field
        local fallbacks — never raises on a single malformed field (each
        field is validated independently, ticket section 2), never
        mutates `data` itself."""
        source = data if data is not None else {}

        resolved: dict[str, tuple[object, str]] = {
            "titre": _resolve_field(source, "titre", _validate_str, lambda: _detect_title_locally(text)),
            "client": _resolve_field(source, "client", _validate_str, lambda: _detect_client_locally(text)),
            "secteur": _resolve_field(source, "secteur", _validate_str, lambda: _detect_sector_locally(text)),
            "budget_estime": _resolve_field(source, "budget_estime", _validate_float, lambda: _find_budget(text)),
            "deadline_reponse": _resolve_field(source, "deadline_reponse", _validate_str, lambda: _detect_deadline_locally(text)),
            "duree_projet_mois": _resolve_field(source, "duree_projet_mois", _validate_int_like, lambda: _detect_duration_locally(text)),
            "technologies_demandees": _resolve_field(source, "technologies_demandees", _validate_str_list, lambda: _extract_technologies(text)),
            # No local fallback for competences_requises: technologies and
            # business/methodology competences are NOT the same thing —
            # reusing the tech vocabulary here would misrepresent an
            # absence as a real answer (ticket section 3).
            "competences_requises": _resolve_field(source, "competences_requises", _validate_str_list, None),
            "questions_client": _resolve_field(source, "questions_client", _validate_str_list, lambda: _detect_questions_locally(text)),
            "livrables": _resolve_field(source, "livrables", _validate_str_list, lambda: _detect_livrables_locally(text)),
            "contraintes": _resolve_field(source, "contraintes", _validate_str_list, lambda: _detect_contraintes_locally(text)),
            "certifications_obligatoires": _resolve_field(source, "certifications_obligatoires", _validate_str_list, lambda: _extract_certifications(text)),
        }

        candidate: dict = {"texte_source": text}
        field_provenance: dict[str, str] = {}
        for field_name, (value, code) in resolved.items():
            field_provenance[field_name] = code
            if not _is_empty(value):
                candidate[field_name] = value

        # Strip common AO prefixes the LLM sometimes adds to the title —
        # cosmetic normalization only, applied after validation/resolution.
        if "titre" in candidate:
            titre = candidate["titre"]
            for prefix in ["Appel d'offres : ", "Appel d'offres: ", "APPEL D'OFFRES : ",
                           "Objet : ", "Objet: ", "Marché : ", "Marché: "]:
                if titre.lower().startswith(prefix.lower()):
                    titre = titre[len(prefix):].strip()
            candidate["titre"] = titre

        conflicts = []
        if resolved["certifications_obligatoires"][1] == "llm":
            local_certs = _extract_certifications(text)
            if local_certs and _conflicts_with_local(resolved["certifications_obligatoires"][0], local_certs):
                conflicts.append("certifications_obligatoires")
        if resolved["technologies_demandees"][1] == "llm":
            local_tech = _extract_technologies(text)
            if local_tech and _conflicts_with_local(resolved["technologies_demandees"][0], local_tech):
                conflicts.append("technologies_demandees")
        candidate["extraction_conflicts"] = conflicts
        candidate["field_provenance"] = field_provenance

        # B17-T1: the local, clause-scoped certification analysis is
        # ALWAYS recorded as a diagnostic overlay — it never overwrites
        # `certifications_obligatoires` itself (whatever provenance that
        # field ended up with above), it only makes the underlying
        # per-clause evidence inspectable (ticket section 4: "ne pas
        # écraser un champ LLM valable avec un repli plus pauvre").
        mentions = analyze_certification_mentions(text)
        _, contradictions = resolve_mandatory_certifications(mentions)
        candidate["certification_mentions"] = mentions
        candidate["certification_contradictions"] = contradictions

        if status is not None:
            candidate["extraction_status"] = status
            candidate["extraction_reason"] = reason
        else:
            codes = set(field_provenance.values())
            if codes <= {"llm"}:
                candidate["extraction_status"] = "llm_full"
                candidate["extraction_reason"] = None
            else:
                candidate["extraction_status"] = "llm_partial"
                candidate["extraction_reason"] = "some_fields_rejected"

        return AOContext(**candidate)
