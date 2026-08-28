"""Tests for Agent #3 — Sales (upsell / cross-sell / bundle),
`merchant/agents/sales.py`.

Every `llm.invoke` call is monkeypatched — none of these tests touch the
network or a real Gemini/NVIDIA key. Each fake returns a `FakeMsg` whose
`.content` is the JSON text a real model call would have produced, so the
agent's own parsing/validation logic is what's actually under test.
"""

from __future__ import annotations

import json

import pytest

from merchant.agents import sales
from buyer.nodes_common import NodeError


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


_CATALOG = [
    {"sku": "NW-SHOE-001", "name": "Trail Runner", "category": "footwear",
     "price_paise": 499900, "stock": 10, "tags": ["running"]},
    {"sku": "NW-SOCK-002", "name": "Wick Socks", "category": "socks",
     "price_paise": 59900, "stock": 20, "tags": ["socks"]},
    {"sku": "NW-SHOE-005", "name": "Premium Racer", "category": "footwear",
     "price_paise": 899900, "stock": 5, "tags": ["racing", "premium"]},
    {"sku": "NW-NUTR-001", "name": "Energy Gel", "category": "nutrition",
     "price_paise": 29900, "stock": 50, "tags": ["nutrition"]},
]

_INTENT = {
    "category": "footwear",
    "max_paise": 500000,
    "max_purchases": 2,
    "currency": "INR",
    "expires_at": 9999999999,
    "merchant_id": None,
}

_CART = [{"sku": "NW-SHOE-001", "qty": 1}]


def test_upsell_proposes_valid_addons(monkeypatch):
    monkeypatch.setattr(
        "merchant.agents.sales.llm.invoke",
        fake_invoke({
            "add": [{"sku": "NW-SOCK-002", "qty": 2}, {"sku": "NW-NUTR-001", "qty": 1}],
            "pitch": "Grab some socks and a gel to go with your new shoes.",
        }),
    )
    result = sales.upsell(cart=_CART, intent=_INTENT, catalog=_CATALOG)

    assert result["pitch"]
    skus_added = {item["sku"] for item in result["add"]}
    assert skus_added == {"NW-SOCK-002", "NW-NUTR-001"}
    # none already in the cart
    cart_skus = {item["sku"] for item in _CART}
    assert skus_added.isdisjoint(cart_skus)
    # subset of the catalog
    catalog_skus = {p["sku"] for p in _CATALOG}
    assert skus_added <= catalog_skus
    for item in result["add"]:
        assert type(item["qty"]) is int
        assert item["qty"] >= 1


def test_upsell_drops_invented_and_in_cart_and_duplicate_skus(monkeypatch):
    monkeypatch.setattr(
        "merchant.agents.sales.llm.invoke",
        fake_invoke({
            "add": [
                {"sku": "NW-SOCK-002", "qty": 1},
                {"sku": "NW-SOCK-002", "qty": 5},   # duplicate -> only first kept
                {"sku": "NW-SHOE-001", "qty": 1},   # already in cart -> dropped
                {"sku": "NW-FAKE-999", "qty": 1},   # invented sku -> dropped
            ],
            "pitch": "Bundle up.",
        }),
    )
    result = sales.upsell(cart=_CART, intent=_INTENT, catalog=_CATALOG)

    assert result["add"] == [{"sku": "NW-SOCK-002", "qty": 1}]


def test_upsell_empty_suggestion_is_a_valid_noop(monkeypatch):
    monkeypatch.setattr(
        "merchant.agents.sales.llm.invoke",
        fake_invoke({"add": [], "pitch": ""}),
    )
    result = sales.upsell(cart=_CART, intent=_INTENT, catalog=_CATALOG)
    assert result == {"add": [], "pitch": ""}


def test_upsell_malformed_output_raises_node_error(monkeypatch):
    monkeypatch.setattr(
        "merchant.agents.sales.llm.invoke",
        fake_invoke("not json at all, sorry"),
    )
    with pytest.raises(NodeError):
        sales.upsell(cart=_CART, intent=_INTENT, catalog=_CATALOG)


def test_upsell_non_object_output_raises_node_error(monkeypatch):
    # A bare JSON array (like discovery/evaluator return) is not the
    # {"add": ..., "pitch": ...} shape this agent contracts to return.
    monkeypatch.setattr(
        "merchant.agents.sales.llm.invoke",
        fake_invoke([{"sku": "NW-SOCK-002", "qty": 1}]),
    )
    with pytest.raises(NodeError):
        sales.upsell(cart=_CART, intent=_INTENT, catalog=_CATALOG)


def test_upsell_return_value_has_no_price_field(monkeypatch):
    monkeypatch.setattr(
        "merchant.agents.sales.llm.invoke",
        fake_invoke({
            "add": [{"sku": "NW-SHOE-005", "qty": 1}],
            "pitch": "Treat yourself to the Premium Racer too.",
        }),
    )
    result = sales.upsell(cart=_CART, intent=_INTENT, catalog=_CATALOG)

    dumped = json.dumps(result).lower()
    for forbidden in ("price", "paise", "total", "rupee", "amount"):
        assert forbidden not in dumped


def test_upsell_over_budget_suggestion_is_not_suppressed(monkeypatch):
    """The headline Phase 5 demo: the sales agent must NOT self-censor to
    stay under the buyer's budget. It pitches to maximise order value; the
    Gate (not this agent) is the only enforced bound.

    A tiny budget (₹100) plus a pricey add-on (NW-SHOE-005, ₹8,999) is fed
    in. If this agent suppressed anything the budget couldn't cover, the
    add-on would come back filtered out here. It must not be — this module
    contains no comparison between a candidate's price and
    intent["max_paise"] at all.
    """
    tiny_budget_intent = {**_INTENT, "max_paise": 10000}  # ₹100
    monkeypatch.setattr(
        "merchant.agents.sales.llm.invoke",
        fake_invoke({
            "add": [{"sku": "NW-SHOE-005", "qty": 1}],  # ₹8,999 — way over budget
            "pitch": "Upgrade to our Premium Racer for a serious performance boost.",
        }),
    )
    result = sales.upsell(cart=_CART, intent=tiny_budget_intent, catalog=_CATALOG)

    assert result["add"] == [{"sku": "NW-SHOE-005", "qty": 1}]
    assert result["pitch"]
