"""Day-1 DEVELOPER PROOF HARNESS — NOT a demo, NOT a scripted fallback.

This is a developer proof script, written to sanity-check that the new
"offers" lane (a web find becoming a real, quotable, Gate-passable Northwind
product) is wired up correctly, fully IN-PROCESS: no HTTP server, no
Razorpay, no real money. The actual demo runs the real live agents; this
script exists only so a human (or CI) can prove the plumbing works before
wiring the buyer agent's live tool-calling loop on top of it.

It proves the new path end to end:

    web find -> offers.create_offer -> resolve_lines -> create_quote ->
    save_quote -> sign Cart Mandate -> gate.check() -> PASS

Day 1 stops at Gate PASS. Order creation and the actual payment against
Razorpay test mode is the Day 2 / human step, deliberately not exercised
here — see the closing banner.

Run:
    uv run python scripts/day1_offer_proof.py                      # offline, baked find
    uv run python scripts/day1_offer_proof.py --live                # real web search (spends credits)
    uv run python scripts/day1_offer_proof.py --query "..." --max-rupees 3000
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import pathlib
from pathlib import Path

# Allow `uv run python scripts/day1_offer_proof.py` to run from anywhere:
# Python only puts this file's own scripts/ dir on sys.path, so add the repo
# root (its parent) so `config`, `core`, and `merchant` resolve.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402

# --- DB isolation ------------------------------------------------------------
# Repoint the ledger/quote/nonce/intent databases at a fresh temp directory
# BEFORE any store call, so this proof never pollutes real data/*.db and
# never collides with a concurrently running server or another proof run.
# The stores read config.*_DB at call time (not at import time), so setting
# these here, before any of core.mandate/merchant.* is used to touch a
# store, is enough.
_tmp = pathlib.Path(tempfile.mkdtemp(prefix="day1_proof_"))
config.LEDGER_DB = _tmp / "ledger.db"
config.QUOTES_DB = _tmp / "quotes.db"
config.GATE_NONCES_DB = _tmp / "gate_nonces.db"
config.INTENTS_DB = _tmp / "intents.db"

from core.mandate import (  # noqa: E402
    generate_keypair,
    make_cart_mandate,
    make_intent_mandate,
    sign,
)
from merchant import intent_store, offers, quote_store  # noqa: E402
from merchant.catalog import resolve_lines  # noqa: E402
from merchant.gate import check as gate_check  # noqa: E402
from merchant.quote import create_quote  # noqa: E402

# A representative, offline web find — used unless --live is passed. Kept
# baked-in so a developer (or CI) can run this proof with zero web-search
# credits spent and zero network dependency.
_OFFLINE_FIND = {
    "title": "Nivia Marathon Running Shoes For Men",
    "url": "https://www.amazon.in/dp/DAY1PROOF",
    "price_paise": 105900,
    "source": "serper",
}


def _rupees(paise: int) -> str:
    """Integer-only paise -> '₹4,767.20'. No float touches a money value."""
    return f"₹{paise // 100:,}.{paise % 100:02d}"


def _section(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m")


def _fail(message: str) -> None:
    print(f"\n\033[31m✗ {message}\033[0m")
    sys.exit(1)


def _pick_live_find(query: str, max_paise: int) -> dict:
    """Search the real web and pick the first result with a usable price at
    or under the ceiling whose title maps to a real catalog category."""
    from demo.search import web_search  # imported lazily — only touches the
    # network when --live is actually passed

    results = web_search(query)
    for r in results:
        if r.price_paise is None or r.price_paise > max_paise:
            continue
        category = offers.map_to_category(r.title)
        if category is None:
            continue
        return {
            "title": r.title,
            "url": r.url,
            "price_paise": r.price_paise,
            "source": r.source,
            "category": category,
        }
    _fail(
        f"no usable web find for query {query!r} at/under {_rupees(max_paise)} "
        f"— every candidate was priceless, over-ceiling, or uncategorisable"
    )
    raise AssertionError("unreachable")  # for type-checkers; _fail exits


def run(*, live: bool, query: str, max_paise: int) -> None:
    print(f"Mode: {'LIVE web search' if live else 'OFFLINE (baked find, zero web credits)'}")

    offers.clear_offers()

    # --- 1. Get a web find ---------------------------------------------
    _section("1. Web find")
    if live:
        find = _pick_live_find(query, max_paise)
    else:
        find = dict(_OFFLINE_FIND)
        find["category"] = offers.map_to_category(find["title"])
        if find["category"] is None:
            _fail(f"offline find title {find['title']!r} did not map to any known category")
    print(f"  title      {find['title']}")
    print(f"  url        {find['url']}")
    print(f"  price      {_rupees(find['price_paise'])}   source {find['source']}")
    print(f"  category   {find['category']}")

    # --- 2. Grant the intent (stands in for the Phase 4 user-consent step,
    #        scoped to the SAME category the find mapped to, or the Gate
    #        will refuse CATEGORY_MISMATCH) ---------------------------------
    _section("2. Grant intent")
    sk, vk = generate_keypair()
    agent_id = f"agent_day1_{vk.encode().hex()[:8]}"
    intent_payload = make_intent_mandate(
        user_id="user_day1_proof",
        agent_id=agent_id,
        agent_pubkey=vk.encode().hex(),
        category=find["category"],
        max_paise=max_paise,
        max_purchases=5,
        ttl_seconds=3600,
    )
    intent_store.register_intent(intent_payload)
    print(f"  agent      {agent_id}")
    print(f"  category   {find['category']}   ceiling {_rupees(max_paise)}   mandate {intent_payload['mandate_id']}")

    # --- 3. Relist the find as a real Northwind offer -----------------------
    _section("3. Create offer (merchant relists the web find)")
    offer = offers.create_offer(
        title=find["title"],
        url=find["url"],
        price_paise=find["price_paise"],
        category=find["category"],
        source=find["source"],
    )
    print(f"  sku        {offer.sku}")
    print(f"  unit_paise {_rupees(offer.unit_paise)}   (merchant's own price, not the web's verbatim)")

    # --- 4. Quote via the real path -----------------------------------------
    _section("4. Quote")
    lines = resolve_lines([{"sku": offer.sku, "qty": 1}])
    quote = create_quote(lines)
    quote_store.save_quote(quote)
    print(f"  quote_id   {quote.quote_id}")
    print(f"  total      {_rupees(quote.total_paise)}   (cart_hash {quote.cart_hash[:12]}…, TTL to {quote.expires_at})")
    if quote.total_paise > max_paise:
        offers.clear_offers()
        _fail(
            f"quoted total {_rupees(quote.total_paise)} exceeds the intent ceiling "
            f"{_rupees(max_paise)} — the find was too expensive; in a real run the "
            f"recovery agent would search again for something cheaper"
        )

    # --- 5. Sign the Cart Mandate (the agent's key, never the model) -------
    _section("5. Sign Cart Mandate")
    cart_payload = make_cart_mandate(
        intent_mandate_id=intent_payload["mandate_id"],
        agent_id=agent_id,
        merchant_id=config.MERCHANT_ID,
        quote_id=quote.quote_id,
        cart_hash=quote.cart_hash,
        total_paise=quote.total_paise,
    )
    envelope = sign(cart_payload, sk)
    print(f"  signed     cart_mandate {cart_payload['mandate_id']}   nonce {cart_payload['nonce']}")

    # --- 6. The real Gate ----------------------------------------------------
    _section("6. Gate.check()")
    result = gate_check(envelope)
    print(f"  passed        {result.passed}")
    print(f"  reason_code   {result.reason_code}")
    if result.total_paise is not None:
        print(f"  total_paise   {_rupees(result.total_paise)}   (Gate's own re-derivation, not the mandate's claim)")

    offers.clear_offers()

    if not result.passed:
        _fail(f"Gate REFUSED: {result.reason_code} — {result.message}")

    _section("Result")
    print("\033[32m✓ Day 1 proof complete — external web find PASSED the real Gate\033[0m")
    print(
        "  Order creation and the actual payment against Razorpay test mode is "
        "the Day 2 / human step — deliberately NOT exercised by this proof."
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Day-1 developer proof: web find -> offer -> quote -> Cart Mandate -> Gate PASS, fully in-process."
    )
    parser.add_argument(
        "--live", action="store_true",
        help="use a real web search (demo.search.web_search) instead of the baked offline find — spends web-search credits",
    )
    parser.add_argument(
        "--query", default="running shoes under 3000",
        help="search query for --live mode (default: 'running shoes under 3000')",
    )
    parser.add_argument(
        "--max-rupees", type=int, default=5000,
        help="intent spending ceiling in whole rupees (default 5000)",
    )
    args = parser.parse_args()

    run(live=args.live, query=args.query, max_paise=args.max_rupees * 100)


if __name__ == "__main__":
    main()
