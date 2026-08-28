"""Tests for Agent #5 — Substitution (`merchant/agents/substitution.py`).

Every `llm.invoke` call is monkeypatched — none of these tests touch the
network or a real NVIDIA/Gemini key. Each fake returns a `FakeMsg` whose
`.content` is the JSON text a real model call would have produced, so the
node's own parsing/validation/filtering logic is what's actually under test.
"""

from __future__ import annotations

import json

import pytest

from buyer.nodes_common import NodeError
from merchant.agents import substitution


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
    "max_paise": 400000,
    "max_purchases": 1,
    "currency": "INR",
    "expires_at": 9999999999,
    "merchant_id": None,
}

_CATALOG = [
    {
        "sku": "NW-SHOE-004",
        "name": "Trail Runner Pro",
        "category": "footwear",
        "price_paise": 799900,
        "stock": 0,
        "tags": ["trail", "running"],
        "description": "Out of stock trail shoe.",
    },
    {
        "sku": "NW-SHOE-001",
        "name": "Road Runner Basic",
        "category": "footwear",
        "price_paise": 499900,
        "stock": 14,
        "tags": ["road", "running"],
        "description": "In-stock road running shoe.",
    },
    {
        "sku": "NW-SHOE-007",
        "name": "Budget Sprinter",
        "category": "footwear",
        "price_paise": 149900,
        "stock": 30,
        "tags": ["running", "budget"],
        "description": "Cheapest running shoe in the catalog.",
    },
    {
        "sku": "NW-SHOE-005",
        "name": "Elite Marathon",
        "category": "footwear",
        "price_paise": 899900,
        "stock": 4,
        "tags": ["running", "premium"],
        "description": "Most expensive running shoe.",
    },
    {
        "sku": "NW-SOCK-001",
        "name": "Ankle Socks",
        "category": "socks",
        "price_paise": 29900,
        "stock": 20,
        "tags": ["socks"],
        "description": "Not footwear - must never be offered.",
    },
]


def test_over_budget_prefers_cheaper_alternatives(monkeypatch):
    monkeypatch.setattr(
        "merchant.agents.substitution.llm.invoke",
        fake_invoke(["NW-SHOE-007", "NW-SHOE-001"]),
    )
    result = substitution.substitute(
        sku="NW-SHOE-005",
        reason="over_budget",
        intent=_INTENT,
        catalog=_CATALOG,
    )
    skus = [p["sku"] for p in result]
    assert skus == ["NW-SHOE-007", "NW-SHOE-001"]
    # returned dicts are untouched catalog dicts, not model-fabricated
    for product in result:
        assert product in _CATALOG


def test_out_of_stock_excludes_zero_stock_items(monkeypatch):
    """NW-SHOE-004 (stock=0) is the excluded original. Even if the model
    tries to recommend another out-of-stock sku, the candidate set handed to
    it never contained one, so it structurally can't come back."""
    captured_prompt = {}

    def _invoke(messages, *, purpose):
        captured_prompt["human"] = messages[-1][1]
        return FakeMsg(json.dumps(["NW-SHOE-001", "NW-SHOE-007"]))

    monkeypatch.setattr("merchant.agents.substitution.llm.invoke", _invoke)

    result = substitution.substitute(
        sku="NW-SHOE-004",
        reason="out_of_stock",
        intent=_INTENT,
        catalog=_CATALOG,
    )
    skus = [p["sku"] for p in result]
    assert "NW-SHOE-004" not in skus
    for product in result:
        assert product["stock"] > 0
    # the out-of-stock original was never listed among the candidates shown
    # to the model (it legitimately appears once, earlier, as "Excluded product")
    candidates_section = captured_prompt["human"].split("Candidate alternatives")[1]
    assert "NW-SHOE-004" not in candidates_section


def test_original_sku_always_excluded(monkeypatch):
    monkeypatch.setattr(
        "merchant.agents.substitution.llm.invoke",
        fake_invoke(["NW-SHOE-005", "NW-SHOE-001"]),
    )
    result = substitution.substitute(
        sku="NW-SHOE-005",
        reason="no_fit",
        intent=_INTENT,
        catalog=_CATALOG,
    )
    skus = [p["sku"] for p in result]
    assert "NW-SHOE-005" not in skus
    assert skus == ["NW-SHOE-001"]


def test_invented_sku_is_dropped(monkeypatch):
    monkeypatch.setattr(
        "merchant.agents.substitution.llm.invoke",
        fake_invoke(["NW-SHOE-001", "NW-FAKE-999", "NW-SOCK-001"]),
    )
    result = substitution.substitute(
        sku="NW-SHOE-005",
        reason="over_budget",
        intent=_INTENT,
        catalog=_CATALOG,
    )
    skus = [p["sku"] for p in result]
    # NW-FAKE-999 doesn't exist; NW-SOCK-001 exists but is a different category
    assert skus == ["NW-SHOE-001"]


def test_empty_when_nothing_fits(monkeypatch):
    monkeypatch.setattr(
        "merchant.agents.substitution.llm.invoke",
        fake_invoke([]),
    )
    result = substitution.substitute(
        sku="NW-SHOE-005",
        reason="no_fit",
        intent=_INTENT,
        catalog=_CATALOG,
    )
    assert result == []


def test_no_candidates_short_circuits_without_llm_call(monkeypatch):
    """When filtering leaves zero candidates (e.g. every same-category item is
    the original or out of stock), substitute() must return [] without ever
    calling the model."""

    def _invoke(messages, *, purpose):
        raise AssertionError("llm.invoke must not be called with no candidates")

    monkeypatch.setattr("merchant.agents.substitution.llm.invoke", _invoke)

    lone_catalog = [
        {
            "sku": "NW-SHOE-004",
            "name": "Trail Runner Pro",
            "category": "footwear",
            "price_paise": 799900,
            "stock": 0,
            "tags": [],
            "description": "",
        }
    ]
    result = substitution.substitute(
        sku="NW-SHOE-004",
        reason="out_of_stock",
        intent=_INTENT,
        catalog=lone_catalog,
    )
    assert result == []


def test_malformed_output_raises_node_error(monkeypatch):
    monkeypatch.setattr(
        "merchant.agents.substitution.llm.invoke",
        fake_invoke({"not": "a list"}),
    )
    with pytest.raises(NodeError):
        substitution.substitute(
            sku="NW-SHOE-005",
            reason="over_budget",
            intent=_INTENT,
            catalog=_CATALOG,
        )


def test_unparseable_output_raises_node_error(monkeypatch):
    monkeypatch.setattr(
        "merchant.agents.substitution.llm.invoke",
        fake_invoke("this is not json at all"),
    )
    with pytest.raises(NodeError):
        substitution.substitute(
            sku="NW-SHOE-005",
            reason="over_budget",
            intent=_INTENT,
            catalog=_CATALOG,
        )


def test_no_price_field_in_return_shape(monkeypatch):
    """Money discipline: the returned dicts are untouched catalog rows, but
    confirm the function itself never fabricates or injects a new price-like
    field beyond what the catalog already carries."""
    monkeypatch.setattr(
        "merchant.agents.substitution.llm.invoke",
        fake_invoke(["NW-SHOE-001"]),
    )
    result = substitution.substitute(
        sku="NW-SHOE-005",
        reason="over_budget",
        intent=_INTENT,
        catalog=_CATALOG,
    )
    assert result[0] == _CATALOG[1]  # NW-SHOE-001 entry, byte-for-byte identical


def test_real_catalog_out_of_stock_sku(monkeypatch):
    """Sanity check against the real catalog file, using the exact
    stock=0 sku the brief calls out (NW-SHOE-004)."""
    monkeypatch.setattr(
        "merchant.agents.substitution.llm.invoke",
        fake_invoke(["NW-SHOE-001"]),
    )
    result = substitution.substitute(
        sku="NW-SHOE-004",
        reason="out_of_stock",
        intent=_INTENT,
        catalog=None,
    )
    skus = [p["sku"] for p in result]
    assert "NW-SHOE-004" not in skus
    for product in result:
        assert product["category"] == "footwear"
        assert product["stock"] > 0
