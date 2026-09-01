"""Hermetic tests for demo/fixtures.py — the offline, zero-API test data.

No network call, no API key, no LLM. These tests only need
`merchant.offers.map_to_category` (pure, deterministic) and
`langchain_core.messages.AIMessage` (already a project dependency).
"""

from __future__ import annotations

from langchain_core.messages import AIMessage

from demo.fixtures import (
    CHEAP_HEADPHONES,
    CHEAP_SHOE,
    ScriptedModel,
    fake_search,
    happy_path_script,
    headphones_script,
    recovery_script,
)
from demo.search import SearchResult
from merchant import offers


def test_fake_search_returns_multiple_results() -> None:
    results = fake_search("running shoes")
    assert len(results) >= 2
    assert all(isinstance(r, SearchResult) for r in results)


def test_fake_search_cheap_result_maps_to_footwear() -> None:
    results = fake_search("running shoes")
    cheap = [r for r in results if r.price_paise is not None and r.price_paise < 200_000]
    assert cheap, "expected at least one cheap, priced candidate"
    for r in cheap:
        assert offers.map_to_category(r.title) == "footwear"


def test_fake_search_has_an_over_budget_candidate() -> None:
    results = fake_search("running shoes")
    over_10k = [r for r in results if r.price_paise is not None and r.price_paise > 1_000_000]
    assert over_10k, "expected at least one candidate priced over Rs 10,000"


def test_fake_search_is_deterministic() -> None:
    first = fake_search("running shoes")
    second = fake_search("running shoes")
    assert first == second


def test_fake_search_is_keyword_responsive_to_socks() -> None:
    results = fake_search("comfortable running socks")
    assert results
    for r in results:
        assert offers.map_to_category(r.title) == "socks"


def test_fake_search_respects_max_results() -> None:
    results = fake_search("running shoes", max_results=1)
    assert len(results) == 1


def test_seed_category_fixtures_still_map() -> None:
    """The shoe/sock fixtures happen to sit in the merchant's SEED taxonomy, so
    they still resolve via the (now-optional) keyword map. This is no longer
    load-bearing — the loop lists a find under the run's signed category, not the
    title's keyword — but it keeps the sport fixtures realistic."""
    seen_titles: set[str] = set()
    for query in ("running shoes", "running socks"):
        for r in fake_search(query, max_results=10):
            seen_titles.add(r.title)
    assert seen_titles, "expected at least one fixture title"
    for title in seen_titles:
        assert offers.map_to_category(title) in offers.config.CATALOG_CATEGORIES


def test_headphones_fixture_is_open_vocab_and_priced() -> None:
    """Headphones are the open-vocabulary proof: a NON-sport product the seed
    keyword map does NOT recognise, yet fake_search returns it and the loop can
    still buy it (the offer is listed under the run's understood category)."""
    results = fake_search("wireless headphones")
    assert results and results[0].title == CHEAP_HEADPHONES.title
    # It deliberately does NOT map to any seed category — that's the whole point.
    assert offers.map_to_category(CHEAP_HEADPHONES.title) is None
    assert CHEAP_HEADPHONES.price_paise and CHEAP_HEADPHONES.price_paise > 0


def test_headphones_script_starts_with_web_search() -> None:
    model = headphones_script()
    model.bind_tools([])
    first = model.invoke([])
    assert first.tool_calls and first.tool_calls[0]["name"] == "web_search"


def test_scripted_model_bind_tools_returns_invocable() -> None:
    model = ScriptedModel(turns=[[{"name": "web_search", "args": {"query": "x"}, "id": "1"}]])
    bound = model.bind_tools(["tool_a", "tool_b"])
    assert bound is model
    assert model.bound_tools == ["tool_a", "tool_b"]
    reply = bound.invoke([])
    assert isinstance(reply, AIMessage)
    assert reply.tool_calls[0]["name"] == "web_search"


def test_scripted_model_walks_turns_and_ends_with_no_tool_calls() -> None:
    model = ScriptedModel(
        turns=[
            [{"name": "web_search", "args": {"query": "running shoes"}, "id": "1"}],
            [{"name": "sign_and_submit", "args": {}, "id": "2"}],
            "all done",
        ]
    )
    model.bind_tools([])

    first = model.invoke([])
    assert first.tool_calls and first.tool_calls[0]["name"] == "web_search"

    second = model.invoke([])
    assert second.tool_calls and second.tool_calls[0]["name"] == "sign_and_submit"

    third = model.invoke([])
    assert third.tool_calls == []
    assert third.content == "all done"

    # Script exhausted: further invokes are a clean "no tool calls" finish,
    # never an IndexError.
    fourth = model.invoke([])
    assert fourth.tool_calls == []


def test_happy_path_script_builds_and_starts_with_web_search() -> None:
    model = happy_path_script()
    model.bind_tools([])
    first = model.invoke([])
    assert first.tool_calls
    assert first.tool_calls[0]["name"] == "web_search"


def test_recovery_script_builds_and_starts_with_web_search() -> None:
    model = recovery_script()
    model.bind_tools([])
    first = model.invoke([])
    assert first.tool_calls
    assert first.tool_calls[0]["name"] == "web_search"


def test_recovery_script_contains_an_over_limit_then_recovery_flow() -> None:
    model = recovery_script()
    model.bind_tools([])
    names = []
    for _ in range(len(model.turns)):
        reply = model.invoke([])
        if reply.tool_calls:
            names.append(reply.tool_calls[0]["name"])
    assert names == [
        "web_search",
        "list_with_merchant",
        "sign_and_submit",
        "explain_refusal",
        "web_search",
        "list_with_merchant",
        "sign_and_submit",
    ]


def test_happy_path_uses_the_named_cheap_shoe_fixture() -> None:
    """The scripts reuse the same named fixture rows fake_search returns, so
    the two never drift out of sync."""
    model = happy_path_script()
    model.bind_tools([])
    model.invoke([])  # web_search
    listing = model.invoke([])
    args = listing.tool_calls[0]["args"]
    assert args["title"] == CHEAP_SHOE.title
    assert args["price_paise"] == CHEAP_SHOE.price_paise
