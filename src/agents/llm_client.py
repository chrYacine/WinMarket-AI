"""Provider-neutral LLM facade with controlled technical fallback."""
import json
import os
import time
from typing import Optional
import httpx

from src.agents.llm_providers import AnthropicProvider, OpenAICompatibleProvider
from src.core import config
from src.core.error_handler import ErrorType, classify_exception
from src.core.logger import get_agent_logger

logger = get_agent_logger("llm_client")
_RETRY_SUFFIX = "\n\nRéponds UNIQUEMENT avec un objet JSON valide, sans markdown ni commentaire."

def is_fallback_eligible(exc: Exception) -> bool:
    """Only infrastructure/availability failures justify changing provider."""
    exception_name = type(exc).__name__.casefold()
    return classify_exception(exc) in {
        ErrorType.RATE_LIMIT, ErrorType.TIMEOUT, ErrorType.SERVER_ERROR, ErrorType.AUTH_ERROR,
    } or isinstance(exc, (ConnectionError, httpx.TransportError)) or any(
        marker in exception_name for marker in ("connection", "timeout", "unavailable")
    )

class LLMClient:
    def __init__(self, providers=None):
        if providers is None:
            timeout = float(config.LLM_TIMEOUT_SECONDS)
            available = {
                "anthropic": AnthropicProvider(os.getenv("ANTHROPIC_API_KEY", "") or config.ANTHROPIC_API_KEY,
                    os.getenv("ANTHROPIC_MODEL", "") or config.ANTHROPIC_MODEL, timeout),
                "openai": OpenAICompatibleProvider("openai", os.getenv("OPENAI_API_KEY", "") or config.OPENAI_API_KEY,
                    config.OPENAI_MODEL, "https://api.openai.com/v1", timeout),
                "mistral": OpenAICompatibleProvider("mistral", os.getenv("MISTRAL_API_KEY", "") or config.MISTRAL_API_KEY,
                    config.MISTRAL_MODEL, "https://api.mistral.ai/v1", timeout),
            }
            providers = [available[name] for name in config.LLM_PROVIDER_PRIORITY if name in available]
        self.providers = [provider for provider in providers if provider.enabled]
        self.enabled = bool(self.providers) and config.LLM_ENABLED
        self.last_provider_used = None

    def complete(self, prompt: str, system: str = None, temperature: float = None,
                 max_tokens: Optional[int] = None) -> Optional[str]:
        if not self.enabled:
            logger.warning("No configured LLM provider; using deterministic local behavior")
            return None
        temperature = config.LLM_TEMPERATURE_FACTUAL if temperature is None else temperature
        max_tokens = config.LLM_MAX_TOKENS if max_tokens is None else max_tokens
        for index, provider in enumerate(self.providers):
            started = time.time()
            try:
                result = provider.complete(prompt, system, temperature, max_tokens)
                self.last_provider_used = provider.name
                logger.info("LLM completion provider=%s duration_ms=%d", provider.name, (time.time()-started)*1000)
                return result
            except Exception as exc:
                eligible = is_fallback_eligible(exc)
                logger.error("LLM provider failed provider=%s error_type=%s fallback_eligible=%s",
                             provider.name, type(exc).__name__, eligible)
                if not eligible or index == len(self.providers) - 1:
                    raise
                logger.warning("LLM fallback from=%s to=%s", provider.name, self.providers[index+1].name)
        return None

    def json_complete(self, prompt: str, system: str = None, temperature: float = None,
                      max_tokens: Optional[int] = None, max_retries: int = 3) -> Optional[dict]:
        current_prompt = prompt
        for _ in range(max_retries):
            try:
                text = self.complete(current_prompt, system, temperature, max_tokens)
            except Exception as exc:
                logger.error("All eligible LLM providers unavailable error_type=%s", type(exc).__name__)
                return None
            if not text: return None
            parsed = self._parse_json_response(text)
            if parsed is not None: return parsed
            current_prompt = prompt + _RETRY_SUFFIX
        return None

    @staticmethod
    def _parse_json_response(text: str) -> Optional[dict]:
        text = text.strip()
        if "```" in text:
            start, end = text.find("```"), text.rfind("```")
            if start != end:
                text = text[start+3:end].strip()
                if text.startswith("json"): text = text[4:].strip()
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end >= 0: text = text[start:end+1]
        try: return json.loads(text)
        except json.JSONDecodeError:
            logger.warning("Invalid JSON response")
            return None

# Compatibility for existing imports; this facade is no longer Anthropic-only.
ClaudeClient = LLMClient
