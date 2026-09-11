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
    FIXTURE_SETS,
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
    """The script selects a server-issued row instead of echoing product fields."""
    model = happy_path_script()
    model.bind_tools([])
    model.invoke([])  # web_search
    listing = model.invoke([])
    args = listing.tool_calls[0]["args"]
    assert args == {"candidate_id": "$candidate_1"}


# --------------------------------------------------------------------------- #
# The shelf itself — properties every shipped fixture set must hold. Simulated
# mode runs a REAL model over these rows, so a careless row here is a bad demo,
# and a missing row is a request Vera cannot answer.
# --------------------------------------------------------------------------- #
def _priced_rows() -> list:
    return [
        row
        for fixture_set in FIXTURE_SETS
        for row in fixture_set.results
        if row.price_paise is not None
    ]


def test_a_coffee_machine_query_returns_coffee_machines_not_shoes() -> None:
    """The regression. `fake_search` used to default every unmatched query to
    the shoe set, so this exact request came back with running sneakers."""
    results = fake_search("coffee machine")
    assert results, "a coffee-machine query must find candidates"
    titles = [r.title.lower() for r in results]
    assert all("shoe" not in t and "sneaker" not in t for t in titles), titles
    assert any("coffee" in t for t in titles), titles


def test_espresso_phrasing_reaches_the_same_shelf() -> None:
    """A user says 'espresso maker', not 'coffee machine'. The model picks its
    own query words, so the shelf has to answer the obvious synonyms."""
    assert fake_search("espresso maker") == fake_search("coffee machine")


def test_an_unmatched_query_returns_nothing_rather_than_a_default_product() -> None:
    """An honest empty result is correct; substituting an unrelated product is
    the bug. `demo/tools.py::web_search_tool` renders [] as readable
    "no candidates found" text the model can act on."""
    assert fake_search("flux capacitor") == []
    assert fake_search("") == []


def test_every_fixture_set_can_demonstrate_both_a_buy_and_a_refusal() -> None:
    """Each shelf must hold something under its demo budget AND something over
    it — the OVER_LIMIT refusal is the headline demo and must stay reachable for
    every category, not just shoes."""
    for fixture_set in FIXTURE_SETS:
        priced = [r.price_paise for r in fixture_set.results if r.price_paise is not None]
        budget = fixture_set.demo_budget_paise
        assert any(p < budget for p in priced), f"{fixture_set.name}: nothing in budget"
        assert any(p > budget for p in priced), f"{fixture_set.name}: nothing over budget"


def test_every_fixture_price_is_genuine_positive_int_paise() -> None:
    """`type(x) is int`, not isinstance: `isinstance(True, int)` is True, and a
    bool or a float price would be caught downstream by
    `merchant/offers.py::create_offer` — but a fixture should never be the thing
    that tests that check."""
    for row in _priced_rows():
        assert type(row.price_paise) is int, row.title
        assert row.price_paise > 0, row.title


def test_every_fixture_display_price_matches_its_paise_value() -> None:
    """The model reads the display string when it reasons about affordability,
    so a display that disagrees with the paise integer would be teaching it a
    lie — and hiding a rupee/paise scaling error."""
    for row in _priced_rows():
        assert row.price_display == f"₹{row.price_paise // 100:,}", row.title


def test_every_fixture_row_is_identifiable_and_fixture_sourced() -> None:
    for fixture_set in FIXTURE_SETS:
        for row in fixture_set.results:
            assert row.title.strip(), fixture_set.name
            assert row.url.startswith("https://"), row.title
            assert row.source == "fixture", row.title
            assert row.snippet.strip(), row.title


def test_fixture_urls_are_unique_per_row() -> None:
    """Two rows sharing a url would collapse into one offer sku downstream."""
    urls = [row.url for fixture_set in FIXTURE_SETS for row in fixture_set.results]
    assert len(urls) == len(set(urls))


def test_every_shelf_answers_its_own_name() -> None:
    """The set's name is what a user would type; it must reach its own shelf."""
    for fixture_set in FIXTURE_SETS:
        assert fake_search(fixture_set.name) == list(fixture_set.results), fixture_set.name


def test_fake_search_is_deterministic_for_a_newly_added_category() -> None:
    assert fake_search("coffee machine") == fake_search("coffee machine")
    assert fake_search("yoga mat") == fake_search("yoga mat")


def test_fake_search_keeps_the_trusted_demo_authority_marker() -> None:
    """The provenance boundary keys off this exact attribute to make fixture
    prices eligible for SIMULATED checkout only. Losing it silently blocks every
    simulated purchase; renaming it silently widens what counts as trusted."""
    assert fake_search.__vera_candidate_authority__ == "trusted_demo"


def test_a_coffee_machine_candidate_matches_a_coffee_machine_scope() -> None:
    """The fixture rows have to survive the merchant's own scope check, or a
    simulated run dies at `create_offer_from_candidate` instead of buying."""
    scope = "coffee machine"
    matched = [
        r for r in fake_search("coffee machine")
        if offers.candidate_matches_scope(r.title, r.snippet, scope)
    ]
    assert len(matched) == len(fake_search("coffee machine"))
    # And the shoe rows must NOT pass that same scope — the check is doing work.
    assert not any(
        offers.candidate_matches_scope(r.title, r.snippet, scope)
        for r in fake_search("running shoes")
    )
