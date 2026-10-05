"""Deterministic security gate for untrusted tender documents."""

from dataclasses import dataclass, field
import hashlib
import re
from typing import List

from src.core.logger import get_agent_logger

logger = get_agent_logger("content_security")


class ContentSecurityError(ValueError):
    """Raised when untrusted content must not enter the analysis pipeline."""

    user_message = (
        "Le contenu n'a pas passé les contrôles de sécurité et ne peut pas être analysé. "
        "Vérifiez qu'il s'agit bien d'un document d'appel d'offres sans instructions parasites."
    )

    def __init__(self, reason_codes: List[str]):
        self.reason_codes = reason_codes
        super().__init__(self.user_message)


@dataclass(frozen=True)
class ModerationResult:
    allowed: bool
    reason_codes: List[str] = field(default_factory=list)


class ContentSecurityGate:
    """Fail-closed checks that never execute or forward document instructions."""

    _INJECTION_PATTERNS = (
        r"(?i)ignore\s+(?:all\s+|toutes?\s+les\s+|les\s+)?(?:previous|pr[eé]c[eé]dentes?|system|syst[eè]me)\s+instructions?",
        r"(?i)(?:oublie|ignore|contourne|remplace|modifie)\s+(?:les?\s+)?(?:instructions?|r[eè]gles?|consignes?)\s+(?:syst[eè]me|pr[eé]c[eé]dentes?)",
        r"(?i)(?:system|assistant|developer)\s*(?:prompt|message)\s*:",
        r"(?i)r[eé]v[eè]le\s+(?:ton|le)\s+(?:prompt|message\s+syst[eè]me|secret)",
        r"(?i)you\s+are\s+now\s+(?:an?|the)|tu\s+es\s+maintenant",
        r"(?i)do\s+not\s+follow\s+(?:the\s+)?(?:system|developer|previous)",
        r"(?i)<\s*/?\s*(?:system|assistant|developer|tool)[^>]*>",
    )
    _AO_TERMS = (
        "appel d'offres", "appel offre", "marché", "consultation", "cahier des charges",
        "cctp", "ccap", "règlement de consultation", "acheteur", "soumissionnaire",
        "prestataire", "lot", "budget", "livrable", "date limite", "offre technique",
        "critère", "exigence", "client", "projet", "contrat", "délai",
    )

    def check(self, content: str) -> ModerationResult:
        text = content or ""
        reasons: List[str] = []
        if not text.strip():
            reasons.append("empty_content")
        if any(re.search(pattern, text) for pattern in self._INJECTION_PATTERNS):
            reasons.append("prompt_injection")
        normalized = text.casefold()
        ao_signal_count = sum(term in normalized for term in self._AO_TERMS)
        if len(text.strip()) < 40 or ao_signal_count < 2:
            reasons.append("out_of_scope")

        result = ModerationResult(not reasons, reasons)
        if reasons:
            fingerprint = hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:12]
            logger.warning(
                "Untrusted content blocked reason_codes=%s fingerprint=%s length=%d",
                ",".join(reasons), fingerprint, len(text),
            )
        else:
            logger.info("Untrusted content accepted length=%d", len(text))
        return result

    def validate(self, content: str) -> str:
        result = self.check(content)
        if not result.allowed:
            raise ContentSecurityError(result.reason_codes)
        return content
