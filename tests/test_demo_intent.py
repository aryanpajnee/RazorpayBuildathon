"""Hermetic tests for demo/intent.py — understanding a request into a category.

No network: the LLM entry point is injected. The fallback path is exercised by
injecting a callable that raises, standing in for a missing key / outage.
"""

from __future__ import annotations

from langchain_core.messages import AIMessage

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
