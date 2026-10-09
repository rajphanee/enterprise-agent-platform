"""
Claude-mode code tested WITHOUT network: we put a fake 'anthropic' module in
sys.modules so ClaudeLLM talks to it instead of the real API.
"""

import sys
import types
from types import SimpleNamespace

import pytest

from agent_platform.config import Settings
from agent_platform.errors import LLMError
from agent_platform.it_agent.policy import INTENTS


class _FakeErrors:
    class AuthenticationError(Exception): pass
    class APIConnectionError(Exception): pass
    class RateLimitError(Exception): pass
    class APIStatusError(Exception):
        status_code = 500


def _install_fake_anthropic(monkeypatch, reply):
    """reply: object returned by messages.create, or an exception to raise."""
    calls = []

    class FakeClient:
        def __init__(self, **kwargs):
            self.messages = SimpleNamespace(create=self.create)

        def create(self, **kwargs):
            calls.append(kwargs)
            if isinstance(reply, Exception):
                raise reply
            return reply

    module = types.ModuleType("anthropic")
    module.Anthropic = FakeClient
    for name in ("AuthenticationError", "APIConnectionError", "RateLimitError", "APIStatusError"):
        setattr(module, name, getattr(_FakeErrors, name))
    monkeypatch.setitem(sys.modules, "anthropic", module)
    return calls


def _llm():
    from agent_platform.llm import ClaudeLLM
    return ClaudeLLM(Settings(mode="claude", anthropic_api_key="test", llm_max_retries=0))


def test_classification_uses_forced_tool_call(monkeypatch):
    block = SimpleNamespace(type="tool_use", input={"intent": "vpn_issue", "confidence": 0.9, "reason": "vpn"})
    calls = _install_fake_anthropic(monkeypatch, SimpleNamespace(content=[block]))
    result = _llm().classify_ticket("vpn down", INTENTS)
    assert result["intent"] == "vpn_issue"
    assert calls[0]["tool_choice"] == {"type": "tool", "name": "classify_ticket"}
    assert calls[0]["tools"][0]["input_schema"]["properties"]["intent"]["enum"] == INTENTS


def test_out_of_list_intent_is_rejected(monkeypatch):
    block = SimpleNamespace(type="tool_use", input={"intent": "delete_everything", "confidence": 1, "reason": ""})
    _install_fake_anthropic(monkeypatch, SimpleNamespace(content=[block]))
    with pytest.raises(LLMError):
        _llm().classify_ticket("x", INTENTS)


def test_connection_failure_becomes_llm_error(monkeypatch):
    _install_fake_anthropic(monkeypatch, _FakeErrors.APIConnectionError("network down"))
    with pytest.raises(LLMError, match="failed after 1 attempts"):
        _llm().answer_question("q", [])
