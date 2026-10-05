"""Provider adapters sharing one completion contract."""
from abc import ABC, abstractmethod
import inspect
from typing import Optional
import httpx

class LLMProvider(ABC):
    name: str
    @property
    @abstractmethod
    def enabled(self) -> bool: ...
    @abstractmethod
    def complete(self, prompt: str, system: Optional[str], temperature: float, max_tokens: int) -> str: ...

class AnthropicProvider(LLMProvider):
    name = "anthropic"
    def __init__(self, api_key: str, model: str, timeout: float):
        self.model, self.client = model, None
        if api_key and not api_key.startswith("sk-ant-xxxx"):
            import anthropic
            self.client = anthropic.Anthropic(api_key=api_key, timeout=timeout)
    @property
    def enabled(self): return self.client is not None
    def complete(self, prompt, system, temperature, max_tokens):
        kwargs = {"model": self.model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]}
        if "temperature" in inspect.signature(self.client.messages.create).parameters:
            kwargs["temperature"] = temperature
        if system: kwargs["system"] = system
        message = self.client.messages.create(**kwargs)
        return "\n".join(block.text for block in message.content if hasattr(block, "text"))

class OpenAICompatibleProvider(LLMProvider):
    def __init__(self, name, api_key, model, base_url, timeout):
        self.name, self.api_key, self.model = name, api_key, model
        self.url, self.timeout = base_url.rstrip("/") + "/chat/completions", timeout
    @property
    def enabled(self): return bool(self.api_key)
    def complete(self, prompt, system, temperature, max_tokens):
        messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
        response = httpx.post(self.url, headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens},
            timeout=self.timeout)
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]
