"""Offline, zero-API test data for the Day-2 agent loop.

Two pieces, both pure and deterministic — no network call, no API key, safe to
import anywhere (including CI):

1. `fake_search` — a drop-in replacement for `demo.search.web_search` with the
   identical signature and return shape (`list[SearchResult]`). It lets the
   whole agent loop + the day2 proof script run end to end against fixed,
   realistic candidates before anyone spends a live Tavily/Serper/Gemini call.

2. `ScriptedModel` — a stand-in for a LangChain chat model bound with tools
   (`model.bind_tools(tools)` then `model.invoke(messages)`), so `demo/agent.py`
   can be exercised without a real Gemini call. It walks a pre-written list of
   turns and returns real `langchain_core.messages.AIMessage` objects, so the
   agent loop cannot tell it apart from a live model reply.

WHERE THIS SITS RELATIVE TO THE MONEY BOUNDARY: everything in this file is
reasoning-side fixture data — fake search results and a fake model's tool
choices. None of it computes a total, signs anything, or decides whether a
payment is allowed; the real tools (called by whatever loop plugs a script in)
still run against the real, deterministic `merchant/offers.py` +
`merchant/quote.py` + `merchant/gate.py`, so `create_offer`'s price/category
validation and the Gate's re-derivation still apply exactly as they would to a
live web find. A fixture title that failed `offers.map_to_category` would be
useless here for the same reason a bad title is useless live — see the module
test, `test_demo_fixtures.py`, which asserts every shipped title actually
resolves to a real `config.CATALOG_CATEGORIES` entry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import AIMessage

import config
from demo.search import SearchResult

# --------------------------------------------------------------------------- #
# 1. Fake web search
# --------------------------------------------------------------------------- #
# Named constants so the ScriptedModel scripts below can reference the exact
# same rows `fake_search` returns — the two stay in sync by construction,
# rather than by two people remembering to keep numbers matching.

CHEAP_SHOE = SearchResult(
    title="StreetFlex Running Sneakers",
    url="https://example-shop.test/products/streetflex-running-sneakers",
    price_paise=105_900,  # ~₹1,059 — comfortably in-budget
    price_display="₹1,059",
    seller="ExampleMart",
    source="fixture",
    snippet="Lightweight running sneakers with breathable mesh upper.",
)

OVER_BUDGET_SHOE = SearchResult(
    title="Trailblazer Pro Running Shoes",
    url="https://example-shop.test/products/trailblazer-pro-running-shoes",
    price_paise=1_899_900,  # ~₹18,999 — drives an OVER_LIMIT refusal
    price_display="₹18,999",
    seller="ExampleMart",
    source="fixture",
    snippet="Premium trail running shoes with a carbon plate.",
)

PRICELESS_SHOE = SearchResult(
    title="Horizon Trainer Shoes",
    url="https://example-shop.test/products/horizon-trainer-shoes",
    price_paise=None,  # scrape found no price — open_product / skip candidate
    price_display=None,
    seller=None,
    source="fixture",
    snippet="Trainer shoes — price not listed on the search snippet.",
)

CHEAP_SOCKS = SearchResult(
    title="ComfortFit Running Socks (3-Pack)",
    url="https://example-shop.test/products/comfortfit-running-socks",
    price_paise=39_900,  # ~₹399 — in-budget
    price_display="₹399",
    seller="ExampleMart",
    source="fixture",
    snippet="Cushioned, moisture-wicking running socks, pack of three.",
)

OVER_BUDGET_SOCKS = SearchResult(
    title="AlpinePeak Merino Wool Socks",
    url="https://example-shop.test/products/alpinepeak-merino-socks",
    price_paise=249_900,  # ~₹2,499 — over a tight sock budget
    price_display="₹2,499",
    seller="ExampleMart",
    source="fixture",
    snippet="Premium merino wool hiking socks, thermal insulation.",
)

# Keyword-routed result sets. `fake_search` picks a set by sniffing the query,
# same spirit as `merchant.offers.map_to_category` — first match wins, checked
# in a fixed order so the function stays deterministic.
_SHOE_RESULTS: tuple[SearchResult, ...] = (CHEAP_SHOE, OVER_BUDGET_SHOE, PRICELESS_SHOE)
_SOCKS_RESULTS: tuple[SearchResult, ...] = (CHEAP_SOCKS, OVER_BUDGET_SOCKS)

_KEYWORD_ROUTES: tuple[tuple[str, tuple[SearchResult, ...]], ...] = (
    ("sock", _SOCKS_RESULTS),
    # "shoe"/"sneaker"/"trainer"/"running" all fall through to the shoe set,
    # which is also the default below — "running shoes" is the flagship demo
    # query and should always resolve to it even with no keyword match at all.
)


def fake_search(query: str, *, max_results: int | None = None) -> list[SearchResult]:
    """Deterministic drop-in for `demo.search.web_search`.

    Same signature, same return shape (`list[SearchResult]`), zero network
    calls. Keyword-responsive (e.g. a query containing "sock" returns the
    socks fixture set) but otherwise always returns the same rows for the
    same query — no randomness, no clock, no I/O — so a test or a proof run
    can assert on it byte-for-byte.
    """
    q = (query or "").lower()
    results = _SHOE_RESULTS
    for keyword, rows in _KEYWORD_ROUTES:
        if keyword in q:
            results = rows
            break

    limit = max_results if max_results is not None else config.SEARCH_MAX_RESULTS
    return list(results[:limit])


# --------------------------------------------------------------------------- #
# 2. A scripted tool-calling model
# --------------------------------------------------------------------------- #
@dataclass
class ScriptedModel:
    """A fake LangChain chat model bound with tools, for testing `demo/agent.py`
    without a live Gemini call.

    Construct with a list of `turns`; each turn is either:
      * a list of tool-call dicts (`{"name": ..., "args": {...}, "id": ...}`),
        turned into an `AIMessage` whose `.tool_calls` the loop should act on, or
      * a plain string, turned into a final `AIMessage(content=<string>)` with
        no tool calls.

    `.bind_tools(tools)` mimics the real `Runnable.bind_tools` interface: it
    records the tools (so a test can assert the loop bound the right set) and
    returns `self`, so `model.bind_tools(tools).invoke(messages)` — the exact
    call shape `demo/agent.py` uses per the shared contract — works unchanged.

    `.invoke(messages)` ignores `messages` (a script is pre-written, not
    reactive) and advances one turn per call. Once the script is exhausted,
    every further `.invoke` returns a final, empty-tool-call `AIMessage` — the
    loop's natural "the model is done" signal — rather than raising, so a loop
    bug that calls one turn too many fails as a normal "no tool calls" finish
    instead of an opaque IndexError.
    """

    turns: list[list[dict[str, Any]] | str] = field(default_factory=list)
    bound_tools: list[Any] | None = field(default=None, init=False, repr=False)
    _step: int = field(default=0, init=False, repr=False)

    def bind_tools(self, tools: list[Any]) -> "ScriptedModel":
        self.bound_tools = list(tools)
        return self

    def invoke(self, messages: list[Any]) -> AIMessage:  # noqa: ARG002 — script is pre-written
        if self._step >= len(self.turns):
            return AIMessage(content="", tool_calls=[])

        turn = self.turns[self._step]
        self._step += 1

        if isinstance(turn, str):
            return AIMessage(content=turn, tool_calls=[])

        return AIMessage(content="", tool_calls=list(turn))

    @property
    def calls_made(self) -> int:
        """How many `.invoke` calls this instance has served — useful for a
        test asserting the loop stayed within `config.AGENT_MAX_LLM_CALLS`."""
        return self._step

    @property
    def exhausted(self) -> bool:
        return self._step >= len(self.turns)


def _tool_call(name: str, args: dict[str, Any], call_id: str) -> dict[str, Any]:
    """Small helper so every scripted turn below has an identical shape."""
    return {"name": name, "args": args, "id": call_id}


def happy_path_script() -> ScriptedModel:
    """The clean buy: search -> list the cheap in-budget find -> sign & submit
    (passes the Gate first try) -> finish.
    """
    return ScriptedModel(
        turns=[
            [_tool_call("web_search", {"query": "running shoes"}, "call_1")],
            [
                _tool_call(
                    "list_with_merchant",
                    {
                        "title": CHEAP_SHOE.title,
                        "url": CHEAP_SHOE.url,
                        "price_paise": CHEAP_SHOE.price_paise,
                        "source": CHEAP_SHOE.source,
                    },
                    "call_2",
                )
            ],
            [_tool_call("sign_and_submit", {}, "call_3")],
            "Order placed — the StreetFlex Running Sneakers are on the way.",
        ]
    )


def recovery_script() -> ScriptedModel:
    """The refusal-then-recover path: search -> list an over-budget find ->
    sign & submit (Gate refuses OVER_LIMIT) -> explain the refusal -> search
    again -> list a cheaper find -> sign & submit (passes) -> finish.
    """
    return ScriptedModel(
        turns=[
            [_tool_call("web_search", {"query": "running shoes"}, "call_1")],
            [
                _tool_call(
                    "list_with_merchant",
                    {
                        "title": OVER_BUDGET_SHOE.title,
                        "url": OVER_BUDGET_SHOE.url,
                        "price_paise": OVER_BUDGET_SHOE.price_paise,
                        "source": OVER_BUDGET_SHOE.source,
                    },
                    "call_2",
                )
            ],
            [_tool_call("sign_and_submit", {}, "call_3")],
            [_tool_call("explain_refusal", {"reason_code": "OVER_LIMIT"}, "call_4")],
            [_tool_call("web_search", {"query": "running shoes under budget"}, "call_5")],
            [
                _tool_call(
                    "list_with_merchant",
                    {
                        "title": CHEAP_SHOE.title,
                        "url": CHEAP_SHOE.url,
                        "price_paise": CHEAP_SHOE.price_paise,
                        "source": CHEAP_SHOE.source,
                    },
                    "call_6",
                )
            ],
            [_tool_call("sign_and_submit", {}, "call_7")],
            "Recovered from the refusal — bought the StreetFlex Running Sneakers instead.",
        ]
    )
