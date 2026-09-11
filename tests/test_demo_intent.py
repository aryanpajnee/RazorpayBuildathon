"""Hermetic tests for demo/intent.py — understanding a request into a category.

No network: the LLM entry point is injected. The fallback path is exercised by
injecting a callable that raises, standing in for a missing key / outage.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage

import config
import demo.intent as di
from buyer import llm
from demo.intent import _fallback_category, understand_request


def _fake_invoke(text):
    def invoke(messages, *, purpose):  # noqa: ARG001
        return AIMessage(content=text)
    return invoke


def test_understand_uses_and_normalizes_the_model_label():
    out = understand_request("some noise cancelling cans", invoke=_fake_invoke("  Headphones  "))
    assert out == "headphones"


def test_understand_falls_back_when_model_is_chatty():
    # A model that ignores the "1-3 words" instruction and writes a sentence:
    chatty = _fake_invoke("You are looking to buy a nice pair of wireless headphones today")
    out = understand_request("wireless headphones", invoke=chatty)
    # too many words → deterministic fallback from the request text
    assert out and len(out.split()) <= 3
    assert "headphone" in out


def test_understand_falls_back_when_model_raises():
    def boom(messages, *, purpose):  # noqa: ARG001
        raise RuntimeError("no API key")
    out = understand_request("buy me a foam roller", invoke=boom)
    assert out and "roller" in out


def test_fallback_strips_price_and_filler():
    assert "headphone" in _fallback_category("buy me wireless headphones under 5000")
    # price tokens and filler are gone; result is a clean short label
    label = _fallback_category("i want the best running shoes for ₹3,000")
    assert label and not any(ch.isdigit() for ch in label)


def test_empty_request_is_general():
    assert understand_request("", invoke=_fake_invoke("")) == "general"


# --- GroqCloud wiring (hermetic — no network) --------------------------------

def test_groq_is_a_registered_provider_that_needs_a_key(monkeypatch):
    assert "groq" in llm._VALID_PROVIDERS
    assert config.MODELS.get("groq")  # a Groq model id is configured
    monkeypatch.setattr(config, "GROQ_API_KEY", "", raising=False)
    with pytest.raises(llm.MissingAPIKeyError):
        llm.get_chat_model(provider="groq")  # key checked before any network/import


def test_understand_falls_back_when_groq_key_missing(monkeypatch):
    # No invoke injected → understand_request tries to build the Groq gateway;
    # with no key it degrades to the deterministic fallback, never a crash and
    # never a silent reroute to another model.
    monkeypatch.setattr(config, "INTENT_PROVIDER", "groq", raising=False)
    monkeypatch.setattr(config, "GROQ_API_KEY", "", raising=False)
    monkeypatch.setattr(di, "_intent_invoke", None, raising=False)
    out = understand_request("buy me wireless headphones under 5000")
    assert out and "headphone" in out


def test_purpose_clause_does_not_become_part_of_the_scope():
    """"a coffee machine for my kitchen" must scope to the machine, not the
    kitchen. The label is shown to a human and then signed, so a trailing
    purpose phrase leaking into it changes what they are agreeing to."""
    assert _fallback_category("a coffee machine for my kitchen") == "coffee machine"
    assert _fallback_category("buy me a coffee machine") == "coffee machine"
    assert _fallback_category("a foam roller for my back") == "foam roller"
    assert _fallback_category("cheap running shoes under 5000") == "running shoes"


def test_a_request_that_is_only_qualifiers_still_yields_a_scope():
    """Degenerate input must not produce an empty scope: an empty category
    would match nothing and the refusal would read as a crypto failure."""
    assert _fallback_category("for my kitchen")
    assert _fallback_category("") == "general"
