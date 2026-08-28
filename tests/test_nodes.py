"""Tests for the buyer's Phase 4 LLM node modules: planner, discovery,
evaluator, intent_compiler.

Every `llm.invoke` call is monkeypatched — none of these tests touch the
network or a real Gemini/NVIDIA key. Each fake returns a `FakeMsg` whose
`.content` is the JSON text a real model call would have produced, so the
node's own parsing/validation logic is what's actually under test.
"""

from __future__ import annotations

import json

import pytest

import config
from buyer import discovery, evaluator, intent_compiler, planner
from buyer.nodes_common import NodeError, extract_json, message_text


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


# --- planner -----------------------------------------------------------------

_INTENT = {
    "category": "footwear",
    "max_paise": 500000,
    "max_purchases": 2,
    "currency": "INR",
    "expires_at": 9999999999,
    "merchant_id": None,
}


def test_planner_feasible(monkeypatch):
    monkeypatch.setattr(
        "buyer.planner.llm.invoke",
        fake_invoke({
            "feasible": True,
            "target_category": "footwear",
            "strategy": "search for running shoes under budget",
            "reason": "category and budget both fit the intent",
        }),
    )
    result = planner.plan(goal="get me running shoes", intent=_INTENT)
    assert result["feasible"] is True
    assert result["target_category"] == "footwear"
    assert result["reason"]
    assert "max_paise" not in result
    assert "price" not in result


def test_planner_infeasible(monkeypatch):
    monkeypatch.setattr(
        "buyer.planner.llm.invoke",
        fake_invoke({
            "feasible": False,
            "target_category": "footwear",
            "strategy": "",
            "reason": "goal asks for electronics, intent only covers footwear",
        }),
    )
    result = planner.plan(goal="buy me a laptop", intent=_INTENT)
    assert result["feasible"] is False
    assert result["reason"]


def test_planner_raises_on_malformed_response(monkeypatch):
    monkeypatch.setattr("buyer.planner.llm.invoke", fake_invoke("not json at all"))
    with pytest.raises(NodeError):
        planner.plan(goal="get me shoes", intent=_INTENT)


def test_planner_parses_a_fenced_json_response(monkeypatch):
    # Models routinely wrap JSON in a ```json fence despite instructions not to;
    # extract_json's fence-stripping is the whole reason it exists, so exercise
    # it through a node rather than only on unfenced payloads.
    fenced = (
        "Here is the plan:\n```json\n"
        '{"feasible": true, "target_category": "footwear", '
        '"strategy": "search running shoes", "reason": "fits the intent"}\n'
        "```\nHope that helps!"
    )
    monkeypatch.setattr("buyer.planner.llm.invoke", fake_invoke(fenced))
    result = planner.plan(goal="get me running shoes", intent=_INTENT)
    assert result["feasible"] is True
    assert result["target_category"] == "footwear"


class FakeBlockMsg:
    """A message whose `.content` is a LIST of content blocks — the shape a
    real gemini-3.6-flash response actually returns, which the string-only
    fakes never exercised (the bug the first live run surfaced)."""

    def __init__(self, blocks) -> None:
        self.content = blocks


def test_message_text_coalesces_content_shapes():
    # bare string
    assert message_text(FakeMsg('{"a": 1}')) == '{"a": 1}'
    # list of {"type":"text","text":...} blocks (real Gemini shape)
    assert message_text(FakeBlockMsg([{"type": "text", "text": '{"a"'}, {"type": "text", "text": ": 1}"}])) == '{"a": 1}'
    # list of plain strings
    assert message_text(FakeBlockMsg(["foo", "bar"])) == "foobar"
    # non-text blocks (e.g. a tool call) are ignored, text is kept
    assert message_text(FakeBlockMsg([{"type": "tool_use", "id": "x"}, {"type": "text", "text": "ok"}])) == "ok"


def test_planner_parses_a_list_content_response(monkeypatch):
    # End-to-end through a node: a list-content model response must parse, not
    # raise "expected a string ... got list" (the exact live-run failure).
    blocks = [{"type": "text", "text": '{"feasible": true, "target_category": "footwear", '},
              {"type": "text", "text": '"strategy": "s", "reason": "fits"}'}]

    def _invoke(messages, *, purpose):
        return FakeBlockMsg(blocks)

    monkeypatch.setattr("buyer.planner.llm.invoke", _invoke)
    result = planner.plan(goal="shoes", intent=_INTENT)
    assert result["feasible"] is True and result["target_category"] == "footwear"


def test_extract_json_strips_fence_directly():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('prose before [1, 2, 3] prose after') == [1, 2, 3]
    with pytest.raises(NodeError):
        extract_json("no json here at all")


# --- discovery -----------------------------------------------------------------

class FakeResponse:
    def __init__(self, data: dict) -> None:
        self._data = data

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._data


class FakeHttp:
    """Stand-in for the httpx.Client discovery.discover() reads from — no
    network, just a canned catalog page."""

    def __init__(self, products: list[dict]) -> None:
        self._products = products
        self.calls: list[dict] = []

    def get(self, path, params=None):
        self.calls.append({"path": path, "params": params})
        return FakeResponse({"products": self._products})


_PRODUCTS = [
    {"sku": "NW-SHOE-001", "name": "Tempo 3", "category": "footwear",
     "price_paise": 499900, "stock": 14, "tags": ["running"], "description": "a shoe"},
    {"sku": "NW-SHOE-002", "name": "Tempo 3 Wide", "category": "footwear",
     "price_paise": 519900, "stock": 6, "tags": ["running", "wide"], "description": "a wider shoe"},
]

_STRATEGY = {"feasible": True, "target_category": "footwear", "strategy": "search running shoes", "reason": "fits"}


def test_discovery_maps_skus_back_to_full_products(monkeypatch):
    monkeypatch.setattr("buyer.discovery.llm.invoke", fake_invoke(["NW-SHOE-001"]))
    http = FakeHttp(_PRODUCTS)
    result = discovery.discover(strategy=_STRATEGY, intent=_INTENT, http=http)
    assert len(result) == 1
    assert result[0]["sku"] == "NW-SHOE-001"
    assert result[0]["name"] == "Tempo 3"


def test_discovery_drops_invented_skus(monkeypatch):
    monkeypatch.setattr(
        "buyer.discovery.llm.invoke",
        fake_invoke(["NW-SHOE-001", "NW-DOES-NOT-EXIST"]),
    )
    http = FakeHttp(_PRODUCTS)
    result = discovery.discover(strategy=_STRATEGY, intent=_INTENT, http=http)
    assert [p["sku"] for p in result] == ["NW-SHOE-001"]


def test_discovery_returns_empty_list_when_model_selects_nothing(monkeypatch):
    monkeypatch.setattr("buyer.discovery.llm.invoke", fake_invoke([]))
    http = FakeHttp(_PRODUCTS)
    result = discovery.discover(strategy=_STRATEGY, intent=_INTENT, http=http)
    assert result == []


def test_discovery_returns_empty_list_when_search_is_empty(monkeypatch):
    def unexpected_invoke(messages, *, purpose):
        raise AssertionError("llm.invoke should not be called on an empty search")

    monkeypatch.setattr("buyer.discovery.llm.invoke", unexpected_invoke)
    http = FakeHttp([])
    result = discovery.discover(strategy=_STRATEGY, intent=_INTENT, http=http)
    assert result == []


def test_discovery_relaxed_widens_query(monkeypatch):
    monkeypatch.setattr("buyer.discovery.llm.invoke", fake_invoke(["NW-SHOE-001"]))
    http = FakeHttp(_PRODUCTS)
    discovery.discover(strategy=_STRATEGY, intent=_INTENT, http=http, relaxed=True)
    assert http.calls[0]["params"]["q"] == ""


def test_discovery_searches_on_single_token_category_not_prose(monkeypatch):
    # /catalog/search is a substring filter. A phrase target_category
    # ("running shoes") must NOT be used as q verbatim (it matches nothing);
    # the clean single-token intent category ("footwear") is used instead.
    monkeypatch.setattr("buyer.discovery.llm.invoke", fake_invoke(["NW-SHOE-001"]))
    http = FakeHttp(_PRODUCTS)
    prose_strategy = {**_STRATEGY, "target_category": "running shoes"}
    discovery.discover(strategy=prose_strategy, intent=_INTENT, http=http)
    assert http.calls[0]["params"]["q"] == "footwear"


def test_discovery_searches_whole_catalog_when_no_single_token_available(monkeypatch):
    # Both category sources are phrases -> fall back to the whole catalog ("")
    # and let the model narrow, rather than filter to zero on a phrase.
    monkeypatch.setattr("buyer.discovery.llm.invoke", fake_invoke(["NW-SHOE-001"]))
    http = FakeHttp(_PRODUCTS)
    prose_intent = {**_INTENT, "category": "trail running shoes"}
    prose_strategy = {**_STRATEGY, "target_category": "waterproof gear"}
    discovery.discover(strategy=prose_strategy, intent=prose_intent, http=http)
    assert http.calls[0]["params"]["q"] == ""


def test_discovery_raises_on_non_list_model_output(monkeypatch):
    monkeypatch.setattr("buyer.discovery.llm.invoke", fake_invoke({"sku": "NW-SHOE-001"}))
    http = FakeHttp(_PRODUCTS)
    with pytest.raises(NodeError):
        discovery.discover(strategy=_STRATEGY, intent=_INTENT, http=http)


# --- evaluator -----------------------------------------------------------------

def test_evaluator_returns_sku_qty_pairs(monkeypatch):
    monkeypatch.setattr(
        "buyer.evaluator.llm.invoke",
        fake_invoke([{"sku": "NW-SHOE-001", "qty": 1}]),
    )
    result = evaluator.evaluate(candidates=_PRODUCTS, intent=_INTENT)
    assert result == [{"sku": "NW-SHOE-001", "qty": 1}]


def test_evaluator_drops_bad_items(monkeypatch):
    monkeypatch.setattr(
        "buyer.evaluator.llm.invoke",
        fake_invoke([
            {"sku": "NW-SHOE-001", "qty": 1},
            {"sku": "NOT-A-REAL-SKU", "qty": 1},   # unknown sku -> dropped
            {"sku": "NW-SHOE-002", "qty": 0},       # non-positive qty -> dropped
            {"sku": "NW-SHOE-002", "qty": "two"},   # non-int qty -> dropped
            {"sku": "NW-SHOE-002", "price_paise": 100, "qty": 2},  # extra key tolerated, item kept
        ]),
    )
    result = evaluator.evaluate(candidates=_PRODUCTS, intent=_INTENT)
    assert result == [
        {"sku": "NW-SHOE-001", "qty": 1},
        {"sku": "NW-SHOE-002", "qty": 2},
    ]
    for item in result:
        assert set(item.keys()) == {"sku", "qty"}


def test_evaluator_returns_empty_list_on_no_fit(monkeypatch):
    monkeypatch.setattr("buyer.evaluator.llm.invoke", fake_invoke([]))
    result = evaluator.evaluate(candidates=_PRODUCTS, intent=_INTENT)
    assert result == []


def test_evaluator_returns_empty_list_for_no_candidates():
    # No llm.invoke patch needed: discovery found nothing, so evaluate must
    # short-circuit without making a call at all.
    result = evaluator.evaluate(candidates=[], intent=_INTENT)
    assert result == []


def test_evaluator_raises_on_non_list_model_output(monkeypatch):
    monkeypatch.setattr(
        "buyer.evaluator.llm.invoke",
        fake_invoke({"sku": "NW-SHOE-001", "qty": 1}),  # object, not an array
    )
    with pytest.raises(NodeError):
        evaluator.evaluate(candidates=_PRODUCTS, intent=_INTENT)


# --- intent_compiler -----------------------------------------------------------------

def test_draft_intent_converts_rupees_to_paise_in_python(monkeypatch):
    monkeypatch.setattr(
        "buyer.intent_compiler.llm.invoke",
        fake_invoke({
            "category": "footwear",
            "max_rupees": 5000,
            "max_purchases": 1,
            "ttl_hours": 24,
        }),
    )
    payload = intent_compiler.draft_intent("get me running shoes under ₹5000", agent_pubkey="ab" * 32)
    assert payload["max_paise"] == 500000
    assert payload["category"] == "footwear"
    assert payload["max_purchases"] == 1
    assert payload["merchant_id"] is None
    assert payload["expires_at"] - payload["issued_at"] == 24 * 3600


def test_draft_intent_maps_phrase_category_to_merchant_vocabulary(monkeypatch):
    # The model is told to return a controlled category; even if it returns a
    # capitalised valid one, draft maps it to the canonical config spelling
    # the Gate compares against (exact-match), so no CATEGORY_MISMATCH downstream.
    monkeypatch.setattr(
        "buyer.intent_compiler.llm.invoke",
        fake_invoke({"category": "Footwear", "max_rupees": 5000, "max_purchases": 1, "ttl_hours": 24}),
    )
    payload = intent_compiler.draft_intent("get me running shoes under 5000", agent_pubkey="ab" * 32)
    assert payload["category"] == "footwear"
    assert payload["category"] in config.CATALOG_CATEGORIES


def test_draft_intent_rejects_category_outside_merchant_vocabulary(monkeypatch):
    monkeypatch.setattr(
        "buyer.intent_compiler.llm.invoke",
        fake_invoke({"category": "running shoes", "max_rupees": 5000, "max_purchases": 1, "ttl_hours": 24}),
    )
    with pytest.raises(NodeError):
        intent_compiler.draft_intent("get me running shoes under 5000", agent_pubkey="ab" * 32)


def test_draft_intent_raises_on_malformed_response(monkeypatch):
    monkeypatch.setattr("buyer.intent_compiler.llm.invoke", fake_invoke("nonsense, not json"))
    with pytest.raises(NodeError):
        intent_compiler.draft_intent("get me shoes", agent_pubkey="ab" * 32)


@pytest.mark.parametrize(
    "bad_rupees",
    [
        5000.0,   # a float paise/rupees hallucination must never reach max_paise
        -100,     # negative
        0,        # zero
        True,     # bool is not an int here, same discipline as core.mandate._require_int
        "5000",   # string
    ],
)
def test_draft_intent_rejects_non_positive_int_rupees(monkeypatch, bad_rupees):
    # THE money-boundary guard: the model's spend figure is the only model
    # output that flows into a signed money field, so anything but a positive
    # int rupee value must be rejected, never coerced, before *100 -> max_paise.
    monkeypatch.setattr(
        "buyer.intent_compiler.llm.invoke",
        fake_invoke({
            "category": "footwear",
            "max_rupees": bad_rupees,
            "max_purchases": 1,
            "ttl_hours": 24,
        }),
    )
    with pytest.raises(NodeError):
        intent_compiler.draft_intent("get me running shoes", agent_pubkey="ab" * 32)


def test_readback_contains_exact_rupee_ceiling(monkeypatch):
    monkeypatch.setattr(
        "buyer.intent_compiler.llm.invoke",
        fake_invoke({
            "category": "footwear",
            "max_rupees": 5000,
            "max_purchases": 1,
            "ttl_hours": 24,
        }),
    )
    payload = intent_compiler.draft_intent("get me running shoes under ₹5000", agent_pubkey="ab" * 32)
    text = intent_compiler.readback(payload)
    assert "₹5,000.00" in text
    assert "footwear" in text
    assert "any merchant" in text


@pytest.mark.parametrize(
    "user_input,expected",
    [
        ("confirm", True),
        ("Yes", True),
        ("y", True),
        (" sign ", True),
        ("I Confirm", True),
        ("CONFIRMED", True),
        ("no", False),
        ("cancel", False),
        ("maybe", False),
        ("", False),
        ("yes please", False),
    ],
)
def test_is_confirmation_truth_table(user_input, expected):
    assert intent_compiler.is_confirmation(user_input) is expected
