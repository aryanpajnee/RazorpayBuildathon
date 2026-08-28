"""Tests for Agent #11 — Buyer Negotiator (`buyer/negotiator.py`).

Mirrors `tests/test_nodes.py`: every `llm.invoke` call is monkeypatched with
a `FakeMsg`, so nothing here touches the network or a real Gemini key. The
negotiator's own parsing/validation logic is what's under test.
"""

from __future__ import annotations

import json

import pytest

from buyer import negotiator
from buyer.nodes_common import NodeError


class FakeMsg:
    """Stand-in for the LangChain message `llm.invoke` returns."""

    def __init__(self, content: str) -> None:
        self.content = content


def fake_invoke(payload):
    """Build a fake `llm.invoke(messages, *, purpose=...)` that always
    returns `payload` (already JSON-encoded or a raw string)."""
    content = payload if isinstance(payload, str) else json.dumps(payload)

    def _invoke(messages, *, purpose):
        return FakeMsg(content)

    return _invoke


_INTENT = {
    "category": "footwear",
    "max_paise": 500000,
    "max_purchases": 2,
    "currency": "INR",
    "expires_at": 9999999999,
    "merchant_id": None,
}

_MERCHANT_CART = [{"sku": "NW-SHOE-001", "qty": 1}]


def test_accept_echoes_merchant_cart(monkeypatch):
    monkeypatch.setattr(
        "buyer.negotiator.llm.invoke",
        fake_invoke({"action": "accept", "cart": [], "message": "deal"}),
    )
    result = negotiator.negotiate(
        merchant_cart=_MERCHANT_CART, merchant_message="Here's my best offer.",
        intent=_INTENT, turn=1,
    )
    assert result["action"] == "accept"
    assert result["cart"] == _MERCHANT_CART
    assert isinstance(result["message"], str)


def test_accept_strips_extra_fields_from_merchant_cart(monkeypatch):
    # Defensive: even if the merchant_cart the caller hands in carries a
    # price-shaped field, an "accept" echo must never leak it back out.
    monkeypatch.setattr(
        "buyer.negotiator.llm.invoke",
        fake_invoke({"action": "accept", "cart": [], "message": "ok"}),
    )
    dirty_cart = [{"sku": "NW-SHOE-001", "qty": 1, "price_paise": 499900}]
    result = negotiator.negotiate(
        merchant_cart=dirty_cart, merchant_message="offer", intent=_INTENT, turn=1,
    )
    assert result["cart"] == [{"sku": "NW-SHOE-001", "qty": 1}]
    for item in result["cart"]:
        assert set(item.keys()) == {"sku", "qty"}


def test_counter_returns_validated_skus_only_cart(monkeypatch):
    monkeypatch.setattr(
        "buyer.negotiator.llm.invoke",
        fake_invoke({
            "action": "counter",
            "cart": [
                {"sku": "NW-SHOE-007", "qty": 1},
                {"sku": "NW-SHOE-002", "qty": 0},        # non-positive qty -> dropped
                {"sku": "NW-SHOE-003", "qty": True},     # stray bool qty -> dropped
                {"sku": "NW-SHOE-004", "qty": "two"},    # non-int qty -> dropped
                {"sku": 42, "qty": 1},                   # non-string sku -> dropped
                {"sku": "NW-SHOE-005", "qty": 2, "price_paise": 100},  # extra key stripped, item kept
            ],
            "message": "Can we do the cheaper model instead?",
        }),
    )
    result = negotiator.negotiate(
        merchant_cart=_MERCHANT_CART, merchant_message="Here's my offer.",
        intent=_INTENT, turn=2,
    )
    assert result["action"] == "counter"
    assert result["cart"] == [
        {"sku": "NW-SHOE-007", "qty": 1},
        {"sku": "NW-SHOE-005", "qty": 2},
    ]
    for item in result["cart"]:
        assert set(item.keys()) == {"sku", "qty"}
    assert result["message"]


def test_walk_away_returns_empty_cart(monkeypatch):
    monkeypatch.setattr(
        "buyer.negotiator.llm.invoke",
        fake_invoke({"action": "walk_away", "cart": [], "message": "too expensive"}),
    )
    result = negotiator.negotiate(
        merchant_cart=_MERCHANT_CART, merchant_message="final offer", intent=_INTENT, turn=4,
    )
    assert result == {"action": "walk_away", "cart": [], "message": "too expensive"}


def test_unrecognised_action_falls_back_to_accept(monkeypatch):
    monkeypatch.setattr(
        "buyer.negotiator.llm.invoke",
        fake_invoke({"action": "agree", "cart": [{"sku": "IGNORED", "qty": 9}], "message": "sure"}),
    )
    result = negotiator.negotiate(
        merchant_cart=_MERCHANT_CART, merchant_message="offer", intent=_INTENT, turn=1,
    )
    # Falls back to accept and echoes the MERCHANT's cart, not whatever the
    # model put under "cart" for its unrecognised verb.
    assert result["action"] == "accept"
    assert result["cart"] == _MERCHANT_CART


def test_malformed_response_raises_node_error(monkeypatch):
    monkeypatch.setattr("buyer.negotiator.llm.invoke", fake_invoke("not json at all"))
    with pytest.raises(NodeError):
        negotiator.negotiate(
            merchant_cart=_MERCHANT_CART, merchant_message="offer", intent=_INTENT, turn=1,
        )


def test_non_object_response_raises_node_error(monkeypatch):
    monkeypatch.setattr("buyer.negotiator.llm.invoke", fake_invoke(["accept"]))
    with pytest.raises(NodeError):
        negotiator.negotiate(
            merchant_cart=_MERCHANT_CART, merchant_message="offer", intent=_INTENT, turn=1,
        )


def test_missing_action_raises_node_error(monkeypatch):
    monkeypatch.setattr(
        "buyer.negotiator.llm.invoke",
        fake_invoke({"cart": [], "message": "no action field"}),
    )
    with pytest.raises(NodeError):
        negotiator.negotiate(
            merchant_cart=_MERCHANT_CART, merchant_message="offer", intent=_INTENT, turn=1,
        )


def test_return_value_never_contains_a_price_field(monkeypatch):
    monkeypatch.setattr(
        "buyer.negotiator.llm.invoke",
        fake_invoke({
            "action": "counter",
            "cart": [{"sku": "NW-SHOE-007", "qty": 1, "price": 1499}],
            "message": "cheaper please",
        }),
    )
    result = negotiator.negotiate(
        merchant_cart=_MERCHANT_CART, merchant_message="offer", intent=_INTENT, turn=1,
    )
    assert "price" not in result
    assert "price_paise" not in result
    assert "total_paise" not in result
    for item in result["cart"]:
        assert "price" not in item and "price_paise" not in item
