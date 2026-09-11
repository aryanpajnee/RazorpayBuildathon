"""Offline, zero-API test data for the Day-2 agent loop.

Two pieces, both pure and deterministic — no network call, no API key, safe to
import anywhere (including CI):

1. `fake_search` — a drop-in replacement for `demo.search.web_search` with the
   identical signature and return shape (`list[SearchResult]`). It lets the
   whole agent loop + the day2 proof script run end to end against fixed,
   realistic candidates before anyone spends a live Tavily/Serper/Gemini call.
   It is ALSO what the UI's "Simulated" mode shops against, where a real model
   reads these rows and picks for itself — so the shelf has to be wide enough to
   answer an ordinary request, and honest enough to return nothing when it
   cannot (see `fake_search`).

2. `ScriptedModel` — a stand-in for a LangChain chat model bound with tools
   (`model.bind_tools(tools)` then `model.invoke(messages)`), so `demo/agent.py`
   can be exercised without a real Gemini call. It walks a pre-written list of
   turns and returns real `langchain_core.messages.AIMessage` objects, so the
   agent loop cannot tell it apart from a live model reply. This is a TEST
   instrument only: the scripts below pin one exact tool sequence so a test can
   assert on the loop's plumbing. No user-facing run may be driven by one — a
   script that replays a purchase is a fake agent judgement, which the project's
   rules forbid, and which is precisely the bug that made a coffee-machine
   request buy running shoes.

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

import re
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import AIMessage

import config
from demo.search import SearchResult

# --------------------------------------------------------------------------- #
# 1. Fake web search
# --------------------------------------------------------------------------- #
# The fixture shelf. Every row is a named constant so the ScriptedModel scripts
# below can reference the exact same rows `fake_search` returns — the two stay in
# sync by construction, rather than by two people remembering to keep numbers
# matching.
#
# WHY THE SHELF IS WIDE. Simulated mode runs the REAL model against these rows
# (see `demo/orchestrator.py::_offline_kwargs`), so the shelf is the only thing
# standing between "Vera reasons over a fixed candidate set" and a user asking
# for a coffee machine. A narrow shelf that defaulted every unmatched query to
# one product set is how a coffee-machine request came back with sneakers; the
# honest shape of "we have no fixture for that" is an EMPTY result list, which
# `demo/tools.py::web_search_tool` already renders as readable "no candidates"
# text for the model to react to.


def _fixture(
    *,
    title: str,
    slug: str,
    price_paise: int,
    snippet: str,
    seller: str = "ExampleMart",
) -> SearchResult:
    """One fixture row, with its display price DERIVED from its paise value.

    Typing the display string by hand next to the paise integer is exactly where
    a rupee/paise scaling typo hides, and a real model reads these rows to reason
    about what fits a budget — so there is one source of truth per row and the
    ₹ string is computed from it in integer space.
    """
    rupees, paise = divmod(price_paise, 100)
    display = f"₹{rupees:,}" if paise == 0 else f"₹{rupees:,}.{paise:02d}"
    return SearchResult(
        title=title,
        url=f"https://example-shop.test/products/{slug}",
        price_paise=price_paise,
        price_display=display,
        seller=seller,
        source="fixture",
        snippet=snippet,
    )


@dataclass(frozen=True, slots=True)
class FixtureSet:
    """One product category's shelf: what query words reach it, what it holds,
    and the budget the demo would sign for it.

    `demo_budget_paise` is documentation with teeth, not a limit anybody
    enforces — no fixture may ever enforce a budget, that is the Gate's job
    alone. It states the budget at which this set demonstrates BOTH outcomes:
    at least one row lands under it (a clean buy) and at least one row lands
    over it (the OVER_LIMIT refusal, which is the project's headline demo).
    `tests/test_demo_fixtures.py` asserts that property for every shipped set,
    so a row added later at a careless price cannot quietly destroy the
    refusal demo.
    """

    name: str
    keywords: tuple[str, ...]
    results: tuple[SearchResult, ...]
    demo_budget_paise: int


# --- footwear --------------------------------------------------------------- #
CHEAP_SHOE = _fixture(
    title="StreetFlex Running Sneakers",
    slug="streetflex-running-sneakers",
    price_paise=105_900,  # ₹1,059 — comfortably in-budget
    snippet="Lightweight running sneakers with breathable mesh upper.",
)

OVER_BUDGET_SHOE = _fixture(
    title="Trailblazer Pro Running Shoes",
    slug="trailblazer-pro-running-shoes",
    price_paise=1_899_900,  # ₹18,999 — drives an OVER_LIMIT refusal
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

# --- socks ------------------------------------------------------------------ #
CHEAP_SOCKS = _fixture(
    title="ComfortFit Running Socks (3-Pack)",
    slug="comfortfit-running-socks",
    price_paise=39_900,  # ₹399 — in-budget
    snippet="Cushioned, moisture-wicking running socks, pack of three.",
)

OVER_BUDGET_SOCKS = _fixture(
    title="AlpinePeak Merino Wool Socks",
    slug="alpinepeak-merino-socks",
    price_paise=249_900,  # ₹2,499 — over a tight sock budget
    snippet="Premium merino wool hiking socks, thermal insulation.",
)

# --- audio ------------------------------------------------------------------ #
# Headphones — a deliberately NON-sport product, to prove the open-vocabulary
# path: the merchant has no "headphones" in its seed catalog, yet the buyer can
# still search for, list, and buy them because the category is now understood
# from the request, not looked up in a fixed table.
CHEAP_HEADPHONES = _fixture(
    title="SoundWave BT-200 Wireless Headphones",
    slug="soundwave-bt200",
    price_paise=199_900,  # ₹1,999 — in-budget
    snippet="Over-ear Bluetooth headphones with 30-hour battery.",
)

OVER_BUDGET_HEADPHONES = _fixture(
    title="AudioPro Studio ANC Headphones",
    slug="audiopro-studio-anc",
    price_paise=2_499_900,  # ₹24,999 — drives an OVER_LIMIT refusal
    snippet="Reference studio headphones with active noise cancellation.",
)

# --- coffee ----------------------------------------------------------------- #
# The request that exposed the old default-to-shoes bug. Three rows, so a real
# model has a genuine choice to make under a ₹20,000 cap rather than one obvious
# pick: a budget drip maker, a mid-range espresso machine, and a bean-to-cup
# machine that is plainly out of reach.
CHEAP_COFFEE_MAKER = _fixture(
    title="BrewRight 600ml Drip Coffee Maker",
    slug="brewright-drip-coffee-maker",
    price_paise=249_900,  # ₹2,499
    snippet="4-cup drip coffee maker with reusable filter and warming plate.",
    seller="KitchenKart",
)

ESPRESSO_MACHINE = _fixture(
    title="Brewline 20 Bar Espresso Coffee Machine",
    slug="brewline-20-bar-espresso-machine",
    price_paise=1_249_900,  # ₹12,499
    snippet="20 bar espresso coffee machine with steam wand for cappuccino.",
    seller="KitchenKart",
)

OVER_BUDGET_ESPRESSO_MACHINE = _fixture(
    title="Barista Pro Bean-to-Cup Espresso Coffee Machine",
    slug="barista-pro-bean-to-cup-espresso-machine",
    price_paise=5_499_900,  # ₹54,999 — drives an OVER_LIMIT refusal
    snippet="Fully automatic bean-to-cup espresso machine with milk carafe.",
    seller="KitchenKart",
)

# --- kettle ----------------------------------------------------------------- #
CHEAP_KETTLE = _fixture(
    title="SwiftBoil 1.5L Stainless Steel Electric Kettle",
    slug="swiftboil-electric-kettle",
    price_paise=119_900,  # ₹1,199
    snippet="1500W electric kettle with auto shut-off and boil-dry protection.",
    seller="KitchenKart",
)

OVER_BUDGET_KETTLE = _fixture(
    title="ThermoPrecise Gooseneck Temperature Control Kettle",
    slug="thermoprecise-gooseneck-kettle",
    price_paise=649_900,  # ₹6,499
    snippet="Variable temperature gooseneck kettle for pour-over brewing.",
    seller="KitchenKart",
)

# --- microwave -------------------------------------------------------------- #
CHEAP_MICROWAVE = _fixture(
    title="HeatWave 20L Solo Microwave Oven",
    slug="heatwave-20l-solo-microwave",
    price_paise=629_000,  # ₹6,290
    snippet="20 litre solo microwave oven with five power levels.",
    seller="KitchenKart",
)

OVER_BUDGET_MICROWAVE = _fixture(
    title="HeatWave 32L Convection Microwave Oven",
    slug="heatwave-32l-convection-microwave",
    price_paise=2_199_900,  # ₹21,999
    snippet="32 litre convection microwave oven with grill and auto-cook menus.",
    seller="KitchenKart",
)

# --- blender ---------------------------------------------------------------- #
CHEAP_BLENDER = _fixture(
    title="VortexMix 500W Mixer Grinder (3 Jars)",
    slug="vortexmix-500w-mixer-grinder",
    price_paise=279_900,  # ₹2,799
    snippet="500W mixer grinder blender with three stainless steel jars.",
    seller="KitchenKart",
)

OVER_BUDGET_BLENDER = _fixture(
    title="VortexMix Pro 1200W High-Speed Blender",
    slug="vortexmix-pro-1200w-blender",
    price_paise=1_149_900,  # ₹11,499
    snippet="1200W high-speed blender with tritan jar and smoothie presets.",
    seller="KitchenKart",
)

# --- backpack --------------------------------------------------------------- #
CHEAP_BACKPACK = _fixture(
    title="TrailPack 30L Laptop Backpack",
    slug="trailpack-30l-laptop-backpack",
    price_paise=149_900,  # ₹1,499
    snippet="30 litre water-resistant backpack with padded 15-inch laptop sleeve.",
)

OVER_BUDGET_BACKPACK = _fixture(
    title="Summit Carry 45L Travel Backpack",
    slug="summit-carry-45l-travel-backpack",
    price_paise=799_900,  # ₹7,999
    snippet="45 litre cabin-size travel backpack with rain cover and hip harness.",
)

# --- keyboard --------------------------------------------------------------- #
CHEAP_KEYBOARD = _fixture(
    title="KeyCraft TKL Wireless Mechanical Keyboard",
    slug="keycraft-tkl-wireless-keyboard",
    price_paise=329_900,  # ₹3,299
    snippet="Tenkeyless wireless mechanical keyboard with red switches.",
    seller="PixelDepot",
)

OVER_BUDGET_KEYBOARD = _fixture(
    title="KeyCraft Pro 75% Hot-Swap Mechanical Keyboard",
    slug="keycraft-pro-75-hotswap-keyboard",
    price_paise=999_900,  # ₹9,999
    snippet="Aluminium 75% hot-swappable mechanical keyboard with volume knob.",
    seller="PixelDepot",
)

# --- mouse ------------------------------------------------------------------ #
CHEAP_MOUSE = _fixture(
    title="GlideOne Wireless Optical Mouse",
    slug="glideone-wireless-optical-mouse",
    price_paise=89_900,  # ₹899
    snippet="2.4GHz wireless optical mouse with silent clicks.",
    seller="PixelDepot",
)

OVER_BUDGET_MOUSE = _fixture(
    title="GlideOne Pro Ergonomic Wireless Mouse",
    slug="glideone-pro-ergonomic-mouse",
    price_paise=549_900,  # ₹5,499
    snippet="Ergonomic vertical wireless mouse with an 8000 DPI sensor.",
    seller="PixelDepot",
)

# --- monitor ---------------------------------------------------------------- #
CHEAP_MONITOR = _fixture(
    title="ClearView 24-inch Full HD IPS Monitor",
    slug="clearview-24-inch-fhd-monitor",
    price_paise=949_900,  # ₹9,499
    snippet="24-inch 1080p IPS monitor, 75Hz, with HDMI and VGA inputs.",
    seller="PixelDepot",
)

OVER_BUDGET_MONITOR = _fixture(
    title="ClearView 32-inch 4K UHD Monitor",
    slug="clearview-32-inch-4k-monitor",
    price_paise=3_499_900,  # ₹34,999
    snippet="32-inch 4K UHD monitor with USB-C power delivery.",
    seller="PixelDepot",
)

# --- water bottle ----------------------------------------------------------- #
CHEAP_WATER_BOTTLE = _fixture(
    title="HydroSteel 1L Insulated Water Bottle",
    slug="hydrosteel-1l-insulated-water-bottle",
    price_paise=74_900,  # ₹749
    snippet="1 litre vacuum insulated stainless steel water bottle.",
)

OVER_BUDGET_WATER_BOTTLE = _fixture(
    title="HydroSteel Pro 1.5L Vacuum Water Bottle",
    slug="hydrosteel-pro-1500ml-water-bottle",
    price_paise=249_900,  # ₹2,499
    snippet="1.5 litre double-walled vacuum water bottle with a carry loop.",
)

# --- yoga mat --------------------------------------------------------------- #
CHEAP_YOGA_MAT = _fixture(
    title="FlexFlow 6mm Non-Slip Yoga Mat",
    slug="flexflow-6mm-yoga-mat",
    price_paise=99_900,  # ₹999
    snippet="6mm anti-skid TPE yoga mat with a carrying strap.",
)

OVER_BUDGET_YOGA_MAT = _fixture(
    title="FlexFlow Pro 8mm Natural Rubber Yoga Mat",
    slug="flexflow-pro-8mm-rubber-yoga-mat",
    price_paise=499_900,  # ₹4,999
    snippet="8mm natural rubber yoga mat with alignment markings.",
)


# The shelf, in match order. `fake_search` takes the FIRST set with a keyword in
# the query, so the order is load-bearing wherever two sets could both match:
# socks sit above footwear because "running socks" is a sock query, not a shoe
# one. Rejected alternative: scoring every set by how many keywords hit and
# taking the best. It reads cleverer and behaves worse — a near-tie would flip on
# an unrelated wording change, and a fixture shelf should be boringly
# predictable.
FIXTURE_SETS: tuple[FixtureSet, ...] = (
    FixtureSet(
        name="coffee machine",
        keywords=("coffee", "espresso", "cappuccino", "barista"),
        results=(CHEAP_COFFEE_MAKER, ESPRESSO_MACHINE, OVER_BUDGET_ESPRESSO_MACHINE),
        demo_budget_paise=2_000_000,   # ₹20,000
    ),
    FixtureSet(
        name="kettle",
        keywords=("kettle",),
        results=(CHEAP_KETTLE, OVER_BUDGET_KETTLE),
        demo_budget_paise=300_000,     # ₹3,000
    ),
    FixtureSet(
        name="microwave",
        keywords=("microwave",),
        results=(CHEAP_MICROWAVE, OVER_BUDGET_MICROWAVE),
        demo_budget_paise=1_200_000,   # ₹12,000
    ),
    FixtureSet(
        name="blender",
        keywords=("blender", "mixer grinder", "mixie", "smoothie maker"),
        results=(CHEAP_BLENDER, OVER_BUDGET_BLENDER),
        demo_budget_paise=600_000,     # ₹6,000
    ),
    FixtureSet(
        name="backpack",
        keywords=("backpack", "rucksack", "laptop bag"),
        results=(CHEAP_BACKPACK, OVER_BUDGET_BACKPACK),
        demo_budget_paise=400_000,     # ₹4,000
    ),
    FixtureSet(
        name="keyboard",
        keywords=("keyboard",),
        results=(CHEAP_KEYBOARD, OVER_BUDGET_KEYBOARD),
        demo_budget_paise=500_000,     # ₹5,000
    ),
    FixtureSet(
        name="mouse",
        keywords=("mouse", "trackpad"),
        results=(CHEAP_MOUSE, OVER_BUDGET_MOUSE),
        demo_budget_paise=300_000,     # ₹3,000
    ),
    FixtureSet(
        name="monitor",
        keywords=("monitor",),
        results=(CHEAP_MONITOR, OVER_BUDGET_MONITOR),
        demo_budget_paise=2_000_000,   # ₹20,000
    ),
    FixtureSet(
        name="water bottle",
        keywords=("water bottle", "bottle", "flask"),
        results=(CHEAP_WATER_BOTTLE, OVER_BUDGET_WATER_BOTTLE),
        demo_budget_paise=150_000,     # ₹1,500
    ),
    FixtureSet(
        name="yoga mat",
        keywords=("yoga", "exercise mat"),
        results=(CHEAP_YOGA_MAT, OVER_BUDGET_YOGA_MAT),
        demo_budget_paise=300_000,     # ₹3,000
    ),
    FixtureSet(
        name="socks",
        keywords=("sock",),
        results=(CHEAP_SOCKS, OVER_BUDGET_SOCKS),
        demo_budget_paise=100_000,     # ₹1,000
    ),
    FixtureSet(
        name="headphones",
        keywords=("headphone", "earbud", "earphone", "headset", "audio"),
        results=(CHEAP_HEADPHONES, OVER_BUDGET_HEADPHONES),
        demo_budget_paise=500_000,     # ₹5,000
    ),
    FixtureSet(
        name="footwear",
        keywords=("shoe", "sneaker", "trainer", "footwear", "cleat"),
        results=(CHEAP_SHOE, OVER_BUDGET_SHOE, PRICELESS_SHOE),
        demo_budget_paise=900_000,     # ₹9,000
    ),
)


def fake_search(query: str, *, max_results: int | None = None) -> list[SearchResult]:
    """Deterministic drop-in for `demo.search.web_search`.

    Same signature, same return shape (`list[SearchResult]`), zero network
    calls: no randomness, no clock, no I/O, so a test or a proof run can assert
    on it byte-for-byte.

    A query that matches no shelf returns an EMPTY list. That is the point of
    this function, not an oversight: the previous version defaulted every
    unmatched query to the shoe set, so "a coffee machine for my kitchen" bought
    running sneakers and reported success. An honest "no candidates found" is a
    result the model can act on (search again with different words, or finish and
    say nothing was found); a plausible-looking unrelated product is a lie the
    rest of the money path has no way to catch, because relisting a candidate the
    buyer chose is legitimate by design.
    """
    q = (query or "").lower()
    match = next(
        (fixture_set for fixture_set in FIXTURE_SETS
         if any(keyword in q for keyword in fixture_set.keywords)),
        None,
    )
    if match is None:
        return []

    limit = max_results if max_results is not None else config.SEARCH_MAX_RESULTS
    return list(match.results[:limit])


# The provenance boundary recognizes this exact server-side marker.  It makes
# fixture prices eligible only for simulated checkout; ordinary injected or
# live search functions are advisory by default.
fake_search.__vera_candidate_authority__ = "trusted_demo"


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

        candidate_ids: list[str] = []
        for message in messages:
            content = getattr(message, "content", "")
            if isinstance(content, str):
                candidate_ids.extend(re.findall(r"candidate_id:\s*(cand_[a-f0-9]+)", content))
        calls = []
        for original in turn:
            call = {**original, "args": dict(original.get("args") or {})}
            for key, value in call["args"].items():
                if isinstance(value, str) and value.startswith("$candidate_"):
                    index = int(value.rsplit("_", 1)[1]) - 1
                    if 0 <= index < len(candidate_ids):
                        call["args"][key] = candidate_ids[index]
            calls.append(call)
        return AIMessage(content="", tool_calls=calls)

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
                    {"candidate_id": "$candidate_1"},
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
                    {"candidate_id": "$candidate_2"},
                    "call_2",
                )
            ],
            [_tool_call("sign_and_submit", {}, "call_3")],
            [_tool_call("explain_refusal", {"reason_code": "OVER_LIMIT"}, "call_4")],
            [_tool_call("web_search", {"query": "running shoes under budget"}, "call_5")],
            [
                _tool_call(
                    "list_with_merchant",
                    {"candidate_id": "$candidate_1"},
                    "call_6",
                )
            ],
            [_tool_call("sign_and_submit", {}, "call_7")],
            "Recovered from the refusal — bought the StreetFlex Running Sneakers instead.",
        ]
    )


def headphones_script() -> ScriptedModel:
    """A NON-sport buy, proving the open-vocabulary path: search -> list the cheap
    in-budget headphones -> sign & submit (passes) -> finish. The merchant has no
    headphones in its seed catalog; this works because the run is scoped to the
    category understood from the request, not to a fixed list.
    """
    return ScriptedModel(
        turns=[
            [_tool_call("web_search", {"query": "wireless headphones"}, "call_1")],
            [
                _tool_call(
                    "list_with_merchant",
                    {"candidate_id": "$candidate_1"},
                    "call_2",
                )
            ],
            [_tool_call("sign_and_submit", {}, "call_3")],
            "Order placed — the SoundWave wireless headphones are on the way.",
        ]
    )
