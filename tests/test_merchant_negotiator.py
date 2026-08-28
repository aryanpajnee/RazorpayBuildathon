"""Tests for Agent #4 - Merchant Negotiator (`merchant/agents/negotiator.py`).

Every `llm.invoke` call is monkeypatched - none of these tests touch the
network or a real Gemini key. Each fake returns a `FakeMsg` whose `.content`
is the JSON text a real model call would have produced, so the module's own
parsing/validation logic is what's actually under test. SKUs used come
straight from `data/catalog.json` (real footwear products) so "unknown sku"
tests have a genuine contrast between a real and an invented sku.
"""

from __future__ import annotations

import json

from merchant.agents import negotiator


class FakeMsg:
    """Stand-in for the LangChain message `llm.invoke` returns."""

    def __init__(self, content: str) -> None:
        self.content = content


def fake_invoke(payload):
    """Build a fake `llm.invoke(messages, *, purpose=...)` that always
    returns `payload` (already JSON-encoded or a raw string) regardless of
    the messages/purpose it's called with."""
    content = payload if isinstance(payload, str) else json.dumps(payload)

    def _invoke(messages, *, purpose):
        return FakeMsg(content)

    return _invoke


_INTENT = {
    "category": "footwear",
    "max_paise": 900000,
    "max_purchases": 2,
    "currency": "INR",
    "expires_at": 9999999999,
    "merchant_id": None,
}

_BUYER_CART = [{"sku": "NW-SHOE-002", "qty": 1}]  # ₹5,199 real sku


# --- concede -------------------------------------------------------------


def test_concede_returns_cheaper_valid_catalog_cart(monkeypatch):
    monkeypatch.setattr(
        "merchant.agents.negotiator.llm.invoke",
        fake_invoke({
            "action": "concede",
            "cart": [{"sku": "NW-SHOE-007", "qty": 1}],  # ₹1,499 real sku
            "message": "Here's a lighter-weight alternative that costs less.",
        }),
    )
    result = negotiator.counter(
        buyer_cart=_BUYER_CART,
        buyer_message="can you do better on price?",
        intent=_INTENT,
        turn=1,
    )
    assert result["action"] == "concede"
    assert result["cart"] == [{"sku": "NW-SHOE-007", "qty": 1}]
    assert result["message"]


# --- hold ------------------------------------------------------------------


def test_hold_echoes_standing_offer(monkeypatch):
    monkeypatch.setattr(
        "merchant.agents.negotiator.llm.invoke",
        fake_invoke({
            "action": "hold",
            "cart": _BUYER_CART,
            "message": "This is already our best offer for this item.",
        }),
    )
    result = negotiator.counter(
        buyer_cart=_BUYER_CART,
        buyer_message="any discount available?",
        intent=_INTENT,
        turn=2,
    )
    assert result["action"] == "hold"
    assert result["cart"] == _BUYER_CART
    assert result["message"]


# --- walk_away ---------------------------------------------------------------


def test_walk_away_returns_empty_cart(monkeypatch):
    monkeypatch.setattr(
        "merchant.agents.negotiator.llm.invoke",
        fake_invoke({
            "action": "walk_away",
            "cart": [{"sku": "NW-SHOE-999", "qty": 1}],  # should be ignored
            "message": "We can't offer anything cheaper than this.",
        }),
    )
    result = negotiator.counter(
        buyer_cart=_BUYER_CART,
        buyer_message="I need it much cheaper",
        intent=_INTENT,
        turn=4,
    )
    assert result["action"] == "walk_away"
    assert result["cart"] == []
    assert result["message"]


# --- invented sku dropped ----------------------------------------------------


def test_concede_drops_invented_sku(monkeypatch):
    monkeypatch.setattr(
        "merchant.agents.negotiator.llm.invoke",
        fake_invoke({
            "action": "concede",
            "cart": [
                {"sku": "NW-SHOE-007", "qty": 1},   # real
                {"sku": "NW-MADE-UP-999", "qty": 1},  # invented, must be dropped
            ],
            "message": "Better deal here.",
        }),
    )
    result = negotiator.counter(
        buyer_cart=_BUYER_CART,
        buyer_message="better price please",
        intent=_INTENT,
        turn=1,
    )
    assert result["action"] == "concede"
    assert result["cart"] == [{"sku": "NW-SHOE-007", "qty": 1}]


# --- unrecognised action -> hold --------------------------------------------


def test_unrecognised_action_treated_as_hold(monkeypatch):
    monkeypatch.setattr(
        "merchant.agents.negotiator.llm.invoke",
        fake_invoke({
            "action": "give_free_stuff",
            "cart": _BUYER_CART,
            "message": "Sure, here's a freebie.",
        }),
    )
    result = negotiator.counter(
        buyer_cart=_BUYER_CART,
        buyer_message="throw in something free?",
        intent=_INTENT,
        turn=1,
    )
    assert result["action"] == "hold"
    assert result["cart"] == _BUYER_CART


# --- malformed output raises NodeError --------------------------------------


def test_malformed_output_raises_node_error(monkeypatch):
    monkeypatch.setattr(
        "merchant.agents.negotiator.llm.invoke",
        fake_invoke("this is not json at all, sorry"),
    )
    try:
        negotiator.counter(
            buyer_cart=_BUYER_CART,
            buyer_message="better price?",
            intent=_INTENT,
            turn=1,
        )
        assert False, "expected NodeError"
    except negotiator.NodeError:
        pass


def test_non_object_output_raises_node_error(monkeypatch):
    monkeypatch.setattr(
        "merchant.agents.negotiator.llm.invoke",
        fake_invoke(["hold", "concede"]),  # a JSON array, not an object
    )
    try:
        negotiator.counter(
            buyer_cart=_BUYER_CART,
            buyer_message="better price?",
            intent=_INTENT,
            turn=1,
        )
        assert False, "expected NodeError"
    except negotiator.NodeError:
        pass


# --- money discipline: no price field ---------------------------------------


def test_result_has_no_price_field(monkeypatch):
    monkeypatch.setattr(
        "merchant.agents.negotiator.llm.invoke",
        fake_invoke({
            "action": "concede",
            "cart": [{"sku": "NW-SHOE-007", "qty": 1}],
            "message": "Here's a cheaper option.",
        }),
    )
    result = negotiator.counter(
        buyer_cart=_BUYER_CART,
        buyer_message="better price?",
        intent=_INTENT,
        turn=1,
    )
    assert "price" not in result
    assert "price_paise" not in result
    assert "total" not in result
    for item in result["cart"]:
        assert set(item.keys()) == {"sku", "qty"}
