"""Tests for Agent #12 Recovery (`buyer/recovery.py`).

Every `llm.invoke` call is monkeypatched - none of these tests touch the
network or a real Gemini key. Mirrors `tests/test_nodes.py`'s pattern
(`FakeMsg` + `fake_invoke`).
"""

from __future__ import annotations

import json

import pytest

from buyer import recovery
from buyer.nodes_common import NodeError


class FakeMsg:
    def __init__(self, content: str) -> None:
        self.content = content


def fake_invoke(payload):
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

_CANDIDATES = [
    {"sku": "NW-SHOE-005", "name": "Peak Trail", "category": "footwear",
     "price_paise": 899900, "stock": 4, "tags": ["trail"], "description": "premium trail shoe"},
    {"sku": "NW-SHOE-007", "name": "Budget Runner", "category": "footwear",
     "price_paise": 149900, "stock": 20, "tags": ["running", "budget"], "description": "cheap runner"},
]

_CART = [{"sku": "NW-SHOE-005", "qty": 1}]

_OVER_LIMIT_FAILURE = {
    "reason": "GATE_REFUSAL",
    "code": "OVER_LIMIT",
    "recoverable": True,
    "detail": {"limit_paise": 500000, "over_by_paise": 399900},
}


def test_over_limit_returns_cheaper_cart(monkeypatch):
    # Model drops the expensive line and substitutes the cheap candidate.
    monkeypatch.setattr(
        "buyer.recovery.llm.invoke",
        fake_invoke([{"sku": "NW-SHOE-007", "qty": 1}]),
    )
    result = recovery.propose_recovery(
        failure=_OVER_LIMIT_FAILURE, cart=_CART, candidates=_CANDIDATES, intent=_INTENT
    )
    assert result == [{"sku": "NW-SHOE-007", "qty": 1}]
    for item in result:
        assert set(item.keys()) == {"sku", "qty"}


def test_unknown_sku_is_dropped(monkeypatch):
    monkeypatch.setattr(
        "buyer.recovery.llm.invoke",
        fake_invoke([
            {"sku": "NW-SHOE-007", "qty": 1},
            {"sku": "NOT-A-REAL-SKU", "qty": 1},
        ]),
    )
    result = recovery.propose_recovery(
        failure=_OVER_LIMIT_FAILURE, cart=_CART, candidates=_CANDIDATES, intent=_INTENT
    )
    assert result == [{"sku": "NW-SHOE-007", "qty": 1}]


def test_sku_from_cart_itself_is_allowed_even_if_not_in_candidates(monkeypatch):
    # A sku the model wants to keep from the ORIGINAL cart must be accepted
    # even when discovery's candidate list doesn't happen to include it -
    # "cart or candidates", not "candidates only".
    monkeypatch.setattr(
        "buyer.recovery.llm.invoke",
        fake_invoke([{"sku": "NW-SHOE-005", "qty": 1}]),
    )
    result = recovery.propose_recovery(
        failure=_OVER_LIMIT_FAILURE, cart=_CART, candidates=[], intent=_INTENT
    )
    assert result == [{"sku": "NW-SHOE-005", "qty": 1}]


def test_empty_array_signals_give_up(monkeypatch):
    monkeypatch.setattr("buyer.recovery.llm.invoke", fake_invoke([]))
    result = recovery.propose_recovery(
        failure=_OVER_LIMIT_FAILURE, cart=_CART, candidates=_CANDIDATES, intent=_INTENT
    )
    assert result == []


def test_empty_cart_short_circuits_without_calling_model():
    def unexpected_invoke(messages, *, purpose):
        raise AssertionError("llm.invoke should not be called when there is no cart to adjust")

    result = recovery.propose_recovery(
        failure=_OVER_LIMIT_FAILURE, cart=[], candidates=_CANDIDATES, intent=_INTENT
    )
    assert result == []


def test_malformed_output_raises_node_error(monkeypatch):
    monkeypatch.setattr("buyer.recovery.llm.invoke", fake_invoke("not json at all"))
    with pytest.raises(NodeError):
        recovery.propose_recovery(
            failure=_OVER_LIMIT_FAILURE, cart=_CART, candidates=_CANDIDATES, intent=_INTENT
        )


def test_non_list_output_raises_node_error(monkeypatch):
    monkeypatch.setattr(
        "buyer.recovery.llm.invoke",
        fake_invoke({"sku": "NW-SHOE-007", "qty": 1}),  # object, not an array
    )
    with pytest.raises(NodeError):
        recovery.propose_recovery(
            failure=_OVER_LIMIT_FAILURE, cart=_CART, candidates=_CANDIDATES, intent=_INTENT
        )


def test_stray_extra_key_is_rejected(monkeypatch):
    # Recovery is deliberately stricter than evaluator.py here: an item with
    # any key beyond sku/qty is dropped outright, not silently stripped.
    monkeypatch.setattr(
        "buyer.recovery.llm.invoke",
        fake_invoke([
            {"sku": "NW-SHOE-007", "qty": 1, "price_paise": 149900},
            {"sku": "NW-SHOE-005", "qty": 1},
        ]),
    )
    result = recovery.propose_recovery(
        failure=_OVER_LIMIT_FAILURE, cart=_CART, candidates=_CANDIDATES, intent=_INTENT
    )
    assert result == [{"sku": "NW-SHOE-005", "qty": 1}]


def test_bad_qty_types_are_dropped(monkeypatch):
    monkeypatch.setattr(
        "buyer.recovery.llm.invoke",
        fake_invoke([
            {"sku": "NW-SHOE-007", "qty": 0},        # non-positive -> dropped
            {"sku": "NW-SHOE-007", "qty": "one"},     # non-int -> dropped
            {"sku": "NW-SHOE-007", "qty": True},      # bool must not pass as int
        ]),
    )
    result = recovery.propose_recovery(
        failure=_OVER_LIMIT_FAILURE, cart=_CART, candidates=_CANDIDATES, intent=_INTENT
    )
    assert result == []


def test_result_never_contains_a_price_field(monkeypatch):
    monkeypatch.setattr(
        "buyer.recovery.llm.invoke",
        fake_invoke([{"sku": "NW-SHOE-007", "qty": 1}]),
    )
    result = recovery.propose_recovery(
        failure=_OVER_LIMIT_FAILURE, cart=_CART, candidates=_CANDIDATES, intent=_INTENT
    )
    for item in result:
        assert "price" not in item and "price_paise" not in item and "total" not in item
