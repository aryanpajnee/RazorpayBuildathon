"""Tests for `merchant/agents/catalog.py` — Agent #2, Catalog (semantic search).

Mirrors `tests/test_nodes.py`: `llm.invoke` is monkeypatched with a fake that
returns a `FakeMsg` whose `.content` is the JSON text a real model call would
have produced. No network, no real API key. Uses the real merchant catalog
(`merchant.catalog.all_products()`) so the returned dicts are provably the
catalog's own, not model output.
"""

from __future__ import annotations

import json

import pytest

from merchant import catalog as merchant_catalog
from merchant.agents import catalog as catalog_agent
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


def _real_skus(n: int) -> list[str]:
    return [p["sku"] for p in merchant_catalog.all_products()[:n]]


def test_model_picks_a_subset_returned_in_order(monkeypatch):
    skus = _real_skus(3)
    # Ask the model to return them reversed - the function must preserve
    # exactly the model's ranking order, not catalog order.
    ordered = list(reversed(skus))
    monkeypatch.setattr("merchant.agents.catalog.llm.invoke", fake_invoke(ordered))

    result = catalog_agent.search(query="running shoes")

    assert [p["sku"] for p in result] == ordered
    # Every dict returned must be the catalog's own object/content.
    by_sku = {p["sku"]: p for p in merchant_catalog.all_products()}
    for product in result:
        assert product == by_sku[product["sku"]]


def test_invented_sku_is_dropped(monkeypatch):
    real_sku = _real_skus(1)[0]
    monkeypatch.setattr(
        "merchant.agents.catalog.llm.invoke",
        fake_invoke([real_sku, "NW-DOES-NOT-EXIST"]),
    )

    result = catalog_agent.search(query="shoes")

    assert [p["sku"] for p in result] == [real_sku]


def test_empty_query_short_circuits_without_llm_call(monkeypatch):
    def _boom(messages, *, purpose):
        raise AssertionError("llm.invoke must not be called for an empty query")

    monkeypatch.setattr("merchant.agents.catalog.llm.invoke", _boom)

    result = catalog_agent.search(query="", limit=5)

    assert result == merchant_catalog.all_products()[:5]


def test_malformed_model_output_raises_node_error(monkeypatch):
    monkeypatch.setattr(
        "merchant.agents.catalog.llm.invoke", fake_invoke("not json at all")
    )
    with pytest.raises(NodeError):
        catalog_agent.search(query="shoes")


def test_model_output_not_a_list_raises_node_error(monkeypatch):
    monkeypatch.setattr(
        "merchant.agents.catalog.llm.invoke", fake_invoke({"sku": "NW-SHOE-001"})
    )
    with pytest.raises(NodeError):
        catalog_agent.search(query="shoes")


def test_returned_dicts_carry_no_model_fabricated_price(monkeypatch):
    # The model is never given a way to emit a price (SKU-only output
    # contract), and the function only ever echoes catalog dicts back - this
    # asserts that invariant holds by checking every price_paise in the
    # result matches the catalog's own authoritative value exactly.
    skus = _real_skus(2)
    monkeypatch.setattr("merchant.agents.catalog.llm.invoke", fake_invoke(skus))
    by_sku = {p["sku"]: p for p in merchant_catalog.all_products()}

    result = catalog_agent.search(query="footwear")

    for product in result:
        assert product["price_paise"] == by_sku[product["sku"]]["price_paise"]
        assert product is by_sku[product["sku"]]


def test_limit_is_respected(monkeypatch):
    skus = _real_skus(5)
    monkeypatch.setattr("merchant.agents.catalog.llm.invoke", fake_invoke(skus))

    result = catalog_agent.search(query="anything", limit=2)

    assert len(result) == 2
    assert [p["sku"] for p in result] == skus[:2]


def test_intent_category_is_included_in_prompt_without_error(monkeypatch):
    skus = _real_skus(1)
    monkeypatch.setattr("merchant.agents.catalog.llm.invoke", fake_invoke(skus))

    result = catalog_agent.search(
        query="running shoes", intent={"category": "footwear", "max_paise": 500000}
    )

    assert [p["sku"] for p in result] == skus
