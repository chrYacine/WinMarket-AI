import pytest
from src.agents.llm_client import LLMClient

@pytest.fixture(autouse=True)
def enable_in_memory_providers(monkeypatch):
    from src.core import config
    monkeypatch.setattr(config, "LLM_ENABLED", True)


class FakeProvider:
    def __init__(self, name, result=None, error=None):
        self.name, self.result, self.error, self.calls = name, result, error, 0
        self.enabled = True
    def complete(self, *args):
        self.calls += 1
        if self.error: raise self.error
        return self.result

def test_normal_operation_does_not_fallback():
    primary, fallback = FakeProvider("primary", "ok"), FakeProvider("fallback", "unused")
    client = LLMClient([primary, fallback])
    assert client.complete("test") == "ok"
    assert primary.calls == 1 and fallback.calls == 0
    assert client.last_provider_used == "primary"

def test_timeout_uses_next_provider():
    primary = FakeProvider("primary", error=TimeoutError("timed out"))
    fallback = FakeProvider("openai", "ok")
    client = LLMClient([primary, fallback])
    assert client.complete("test") == "ok"
    assert primary.calls == fallback.calls == 1
    assert client.last_provider_used == "openai"

def test_provider_specific_connection_error_uses_fallback():
    class APIConnectionError(Exception):
        pass
    primary = FakeProvider("anthropic", error=APIConnectionError("connection failed"))
    fallback = FakeProvider("openai", "ok")
    client = LLMClient([primary, fallback])
    assert client.complete("test") == "ok"
    assert fallback.calls == 1

def test_validation_error_does_not_fallback():
    primary = FakeProvider("primary", error=ValueError("invalid payload"))
    fallback = FakeProvider("fallback", "no")
    client = LLMClient([primary, fallback])
    with pytest.raises(ValueError): client.complete("test")
    assert fallback.calls == 0
