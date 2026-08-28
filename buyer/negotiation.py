"""The bounded buyer<->merchant negotiation loop — deterministic glue.

NOT an agent surface. The two LLM surfaces are #11 (`buyer/negotiator.py`,
the buyer's side) and #4 (`merchant/agents/negotiator.py`, reached over the
merchant's `POST /negotiate` endpoint). This module is the deterministic
orchestration between them, with a HARD turn cap (`config.NEGOTIATION_TURN_CAP`)
so an agent-to-agent haggle can never run the demo — or the shared 15-RPM
Gemini budget — into the ground. It is the same kind of code as
`buyer/agent.py` itself: a state machine that calls LLM nodes as opaque
functions and never itself reasons in natural language.

Money discipline: this file computes no total and trusts no price. Every cart
exchanged is `list[{"sku", "qty"}]` only; the agreed cart is handed back to
`agent.py`, which quotes it at the merchant's real catalog prices and lets the
Gate enforce the user's ceiling. Negotiation changes WHAT is in the cart, never
what a thing costs — this is the whole reason it can be an LLM loop without
ever putting an LLM on the money path (mirrors AP2: agents negotiate carts, the
merchant authors the cart's price).

Outcomes (all bounded, none can loop forever):
- ACCEPTED       — the buyer accepted an offer; `cart` is that offer.
- MERCHANT_WALKED — the merchant ended negotiation; `cart` is its last offer.
- WALKED_AWAY    — the buyer declined further negotiation; `cart` is the last
                   standing merchant offer (the buyer may still proceed with it).
- STALEMATE      — the turn cap was hit without agreement; `cart` is the last
                   standing merchant offer.

In every outcome `cart` is a real, catalog-shaped cart the caller can quote.
Negotiation here can only IMPROVE the cart or leave it unchanged; it never
returns an empty cart or blocks a purchase the buyer already decided to make.
That is a deliberate scope choice (see `agent.py`'s COMMIT integration): it
keeps negotiation a pure optimisation pass and avoids a second recovery loop.
"""

from __future__ import annotations

import config
from buyer import negotiator as _buyer_negotiator

ACCEPTED = "accepted"
MERCHANT_WALKED = "merchant_walked"
WALKED_AWAY = "walked_away"
STALEMATE = "stalemate"


def _intent_projection(intent: dict) -> dict:
    """The minimal, non-authoritative intent context the negotiators reason
    over. Deliberately NOT the signed envelope: negotiation is advisory, and
    the only fields either side needs are the category and the budget/purchase
    context. The signed authority is enforced later, at the Gate, over the
    quote — never here."""
    return {
        "category": intent.get("category"),
        "max_paise": intent.get("max_paise", 0),
        "max_purchases": intent.get("max_purchases"),
    }


def _price_map(http) -> dict[str, int]:
    """Whole-rupee price per sku, read once from the merchant's own catalog, so
    the buyer negotiator can reason about whether an offer fits the budget. Best
    effort: on any failure (or no http) the buyer simply negotiates without price
    hints, exactly as before. This is a reasoning aid only — the buyer never
    returns a price, and the real total is always the merchant's re-derived quote."""
    if http is None:
        return {}
    try:
        products = http.get("/catalog/search", params={"q": ""}).json().get("products", [])
        return {p["sku"]: p.get("price_paise", 0) // 100 for p in products}
    except Exception:  # noqa: BLE001 - price hints are optional; never break the loop
        return {}


def _priced(cart: list[dict], prices: dict[str, int]) -> list[dict]:
    """A copy of `cart` with a `price_rupees` hint attached where known. The
    buyer negotiator shows it for reasoning and drops it from anything it returns
    (its validator keeps only sku/qty), so this never leaks a price downstream."""
    view = []
    for item in cart:
        entry = dict(item)
        sku = item.get("sku")
        if sku in prices:
            entry["price_rupees"] = prices[sku]
        view.append(entry)
    return view


def _ask_merchant(http, *, buyer_cart, buyer_message, intent, turn, merchant_negotiate):
    """One call to the merchant's negotiator (#4).

    `merchant_negotiate` is injectable so a test can drive the loop without a
    live merchant; in the real flow it is None and the merchant is reached over
    HTTP at `POST /negotiate`, keeping the buyer a genuinely external actor that
    only ever talks to the merchant through its published surface.
    """
    if merchant_negotiate is not None:
        return merchant_negotiate(
            buyer_cart=buyer_cart, buyer_message=buyer_message, intent=intent, turn=turn
        )
    response = http.post(
        "/negotiate",
        json={
            "buyer_cart": buyer_cart,
            "buyer_message": buyer_message,
            "intent": _intent_projection(intent),
            "turn": turn,
        },
    )
    response.raise_for_status()
    return response.json()


def negotiate_cart(
    *,
    selected: list[dict],
    intent: dict,
    http,
    buyer_negotiate=_buyer_negotiator.negotiate,
    merchant_negotiate=None,
    turn_cap: int | None = None,
) -> dict:
    """Run the bounded buyer<->merchant negotiation over `selected`.

    Returns `{"cart": list[{"sku","qty"}], "turns": int, "outcome": str,
    "transcript": list[dict]}`. `cart` is always a real catalog-shaped cart the
    caller can quote (the agreed offer, or the last standing merchant offer).
    `turns` counts completed buyer->merchant round trips and never exceeds
    `turn_cap` (`config.NEGOTIATION_TURN_CAP` by default). `transcript` is the
    ordered list of moves for the terminal UI / audit.

    The merchant's OPENING offer is `selected` itself — the cart the buyer's
    Evaluator already chose. Each turn the buyer either accepts it, walks away,
    or counters; a counter is sent to the merchant, whose reply becomes the new
    standing offer. The loop is hard-bounded by `turn_cap`.
    """
    turn_cap = turn_cap if turn_cap is not None else config.NEGOTIATION_TURN_CAP
    proj = _intent_projection(intent)
    prices = _price_map(http)  # read once; a reasoning hint for the buyer only

    merchant_cart = list(selected)
    merchant_message = "Here is the cart you selected. Let me know if you'd like to proceed."
    transcript: list[dict] = []
    turns = 0

    while turns < turn_cap:
        buyer_move = buyer_negotiate(
            merchant_cart=_priced(merchant_cart, prices),
            merchant_message=merchant_message,
            intent=proj,
            turn=turns + 1,
        )
        transcript.append({"side": "buyer", **buyer_move})
        action = buyer_move.get("action")

        if action == "accept":
            agreed = buyer_move.get("cart") or merchant_cart
            return {"cart": agreed, "turns": turns, "outcome": ACCEPTED, "transcript": transcript}
        if action == "walk_away":
            return {"cart": merchant_cart, "turns": turns, "outcome": WALKED_AWAY, "transcript": transcript}

        # action == "counter" (or, defensively, anything else): send the buyer's
        # proposed cart to the merchant and let it respond. This counts as one
        # completed round trip against the cap.
        turns += 1
        merchant_move = _ask_merchant(
            http,
            buyer_cart=buyer_move.get("cart") or [],
            buyer_message=buyer_move.get("message", ""),
            intent=intent,
            turn=turns,
            merchant_negotiate=merchant_negotiate,
        )
        transcript.append({"side": "merchant", **merchant_move})

        if merchant_move.get("action") == "walk_away":
            return {"cart": merchant_cart, "turns": turns, "outcome": MERCHANT_WALKED, "transcript": transcript}

        # hold or concede: the merchant's cart (validated merchant-side) becomes
        # the new standing offer. A merchant that returned an empty cart for a
        # hold keeps the prior standing offer rather than emptying the cart.
        merchant_cart = merchant_move.get("cart") or merchant_cart
        merchant_message = merchant_move.get("message", merchant_message)

    return {"cart": merchant_cart, "turns": turns, "outcome": STALEMATE, "transcript": transcript}
