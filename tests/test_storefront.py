"""Tests for Agent #1 Storefront (`merchant/agents/storefront.py`).

Mirrors `tests/test_nodes.py`'s pattern: a `FakeMsg` stand-in for the
LangChain message `llm.invoke` returns, and `llm.invoke` monkeypatched so
nothing here touches the network or a real API key.

Storefront's one behavioural difference from the buyer nodes is the point
under test here: it must NEVER propagate an LLM failure. Every other node in
this codebase raises `NodeError` on bad output and lets its caller retry;
storefront instead falls back to a deterministic string, because its
availability must not depend on the LLM (see the module docstring).
"""

from __future__ import annotations

import pytest

from merchant.agents import storefront


class FakeMsg:
    """Stand-in for the LangChain message `llm.invoke` returns."""

    def __init__(self, content: str) -> None:
        self.content = content


def fake_invoke(text: str):
    """Build a fake `llm.invoke(messages, *, purpose=...)` that always
    returns `text` as a `FakeMsg`, regardless of the messages/purpose."""

    def _invoke(messages, *, purpose):
        return FakeMsg(text)

    return _invoke


def fake_invoke_raises(exc: Exception):
    """Build a fake `llm.invoke` that raises `exc` instead of returning."""

    def _invoke(messages, *, purpose):
        raise exc

    return _invoke


# --- reply(): happy path -----------------------------------------------------


def test_reply_returns_nonempty_prose(monkeypatch):
    monkeypatch.setattr(
        "merchant.agents.storefront.llm.invoke",
        fake_invoke("Welcome to Northwind! We carry footwear, apparel, and more."),
    )
    result = storefront.reply(buyer_message="what do you sell?")
    assert isinstance(result, str)
    assert result.strip() != ""
    assert "Northwind" in result


def test_reply_passes_optional_context_without_requiring_it(monkeypatch):
    seen_prompts = []

    def _invoke(messages, *, purpose):
        # messages is a list of (role, text) tuples; capture the human turn.
        seen_prompts.append(messages)
        return FakeMsg("Sure, here's what we have.")

    monkeypatch.setattr("merchant.agents.storefront.llm.invoke", _invoke)

    # No context supplied - must not raise or require the kwarg.
    result_no_context = storefront.reply(buyer_message="hello")
    assert result_no_context.strip() != ""

    # Context supplied - should be threaded into the human prompt somewhere.
    result_with_context = storefront.reply(
        buyer_message="hello", context="catalog has 30 products"
    )
    assert result_with_context.strip() != ""
    assert any("catalog has 30 products" in str(msg) for msg in seen_prompts[-1])


def test_reply_uses_storefront_purpose(monkeypatch):
    captured_purpose = {}

    def _invoke(messages, *, purpose):
        captured_purpose["purpose"] = purpose
        return FakeMsg("hi there")

    monkeypatch.setattr("merchant.agents.storefront.llm.invoke", _invoke)
    storefront.reply(buyer_message="hi")
    assert captured_purpose["purpose"] == "storefront"


# --- reply(): LLM failure falls back deterministically -----------------------


def test_reply_falls_back_on_llm_exception(monkeypatch):
    monkeypatch.setattr(
        "merchant.agents.storefront.llm.invoke",
        fake_invoke_raises(RuntimeError("transient LLM failure")),
    )
    result = storefront.reply(buyer_message="what do you sell?")
    assert result == storefront._FALLBACK_REPLY


def test_reply_falls_back_on_empty_llm_output(monkeypatch):
    monkeypatch.setattr("merchant.agents.storefront.llm.invoke", fake_invoke("   "))
    result = storefront.reply(buyer_message="hello?")
    assert result == storefront._FALLBACK_REPLY


def test_reply_falls_back_on_blank_buyer_message(monkeypatch):
    # Should not even reach the LLM for empty input.
    def _invoke(messages, *, purpose):
        raise AssertionError("llm.invoke should not be called for blank input")

    monkeypatch.setattr("merchant.agents.storefront.llm.invoke", _invoke)
    assert storefront.reply(buyer_message="   ") == storefront._FALLBACK_REPLY
    assert storefront.reply(buyer_message="") == storefront._FALLBACK_REPLY


# --- greeting(): deterministic, no LLM call -----------------------------------


def test_greeting_is_deterministic_and_nonempty(monkeypatch):
    def _invoke(messages, *, purpose):
        raise AssertionError("greeting() must not call the LLM")

    monkeypatch.setattr("merchant.agents.storefront.llm.invoke", _invoke)

    first = storefront.greeting()
    second = storefront.greeting()
    assert first == second
    assert isinstance(first, str)
    assert first.strip() != ""


def test_greeting_matches_fallback_reply():
    # greeting() doubles as reply()'s fallback text - keep them identical so
    # a mid-conversation LLM outage degrades to the same known-good string.
    assert storefront.greeting() == storefront._FALLBACK_REPLY
