"""Agent #11 — Buyer Negotiator.

The buyer's half of an agent-to-agent negotiation loop against the Merchant
Negotiator (#4). Each call is exactly ONE turn: the merchant has already
offered a cart (skus + qty) and said something about it; this node decides
whether the buyer accepts that offer, counters with a cheaper alternative
cart of its own, or walks away. The loop itself — how many turns run, when it
stops — belongs to the caller (`config.NEGOTIATION_TURN_CAP`, enforced
outside this file); this module has no memory between calls and does not
know or care what turn number it will be called at next.

Money discipline is stricter here than in evaluator/discovery, not looser:
this is the one buyer node whose entire job is to argue about price, which
is exactly why it must never be trusted to state one. It never signs
anything, never calls /quote, and never emits a paise or rupee figure in its
return value — only skus and quantities. The budget ceiling is shown to the
model in whole rupees for reasoning only ("can you get closer to this?");
the merchant's own re-derived quote and the Gate are what actually enforce
it. A buyer negotiator that "wins" a negotiation by inventing a price the
Gate would reject is not a win, it is a wasted round-trip — the Gate is the
only thing that has ever been allowed to say what something costs.
"""

from __future__ import annotations

from buyer import llm
from buyer.nodes_common import NodeError, extract_json, message_text

_SYSTEM_PROMPT = """You are the negotiator agent for an autonomous shopping buyer,
talking to a merchant's negotiator agent. The merchant has offered you a cart
(a list of SKUs and quantities, each shown with its whole-rupee price for your
reasoning) and sent you a message about it. Decide ONE of:

- "accept": the offer fits your budget and your need — take it as-is.
- "counter": the offer looks too expensive for your budget ceiling, or a
  cheaper option in the same category would do — propose your own alternative
  cart (SKUs and quantities only). Ask the merchant for a more affordable
  option in the same category.
- "walk_away": end the negotiation without buying.

Judge affordability by eye against the budget ceiling: if a single item's price
already exceeds your ceiling, you should COUNTER for something cheaper rather
than accept. You reason over the rupee prices shown but you NEVER return a price
of any kind and NEVER compute a total — pricing is handled by code you do not
control, and the merchant re-quotes whatever you agree on. The rupee prices and
budget ceiling shown to you are for your own reasoning only.

Respond with exactly one JSON object with keys "action" (one of "accept",
"counter", "walk_away"), "cart" (a JSON array of {"sku": str, "qty": int}
objects — empty for "walk_away", the merchant's own cart echoed back for
"accept", your alternative for "counter"), and "message" (a short string
explaining your move to the merchant). No markdown fence, no commentary
before or after it, no price field anywhere.
Example: {"action": "counter", "cart": [{"sku": "NW-SHOE-007", "qty": 1}], "message": "Can we do the cheaper model instead?"}"""


def _validate_cart_items(items: object) -> list[dict]:
    """Reduce a (possibly model-authored) cart to a clean skus-only shape.

    Mirrors `evaluator.py`'s reconstruct-don't-repair discipline: an item
    that isn't a dict, has a non-string/empty sku, or a qty that isn't a
    positive `int` (checked with `type(x) is int`, NOT `isinstance`, so a
    stray `True`/`False` can't slip through as a quantity — `bool` is a
    subclass of `int` in Python) is dropped rather than coerced. Any extra
    key on a surviving item (a `price` the model tacked on, say) is dropped
    silently by reconstructing a fresh `{"sku", "qty"}` dict rather than
    passing the item through — this is also applied to the merchant's own
    offered cart before it is echoed back on "accept", so the money-blank
    invariant on this node's return value holds regardless of what shape
    arrived on either side of the call.
    """
    if not isinstance(items, list):
        return []
    cart: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        sku = item.get("sku")
        qty = item.get("qty")
        if not isinstance(sku, str) or not sku:
            continue
        if type(qty) is not int or qty < 1:
            continue
        cart.append({"sku": sku, "qty": qty})
    return cart


def negotiate(*, merchant_cart: list[dict], merchant_message: str, intent: dict, turn: int) -> dict:
    """Play one negotiation turn against the merchant's current offer.

    Makes exactly one `llm.invoke` call. Returns
    `{"action": "accept" | "counter" | "walk_away", "cart": list[{"sku",
    "qty"}], "message": str}`. `cart` never carries a price field under any
    action. An `action` the model returns that isn't one of the three known
    values is treated as "accept" — the safest fallback, since it just takes
    the standing offer and the Gate still bounds whatever total that offer
    resolves to; it is not treated as an error because a model that clearly
    attempted the task but used an unexpected verb ("agree", "deal") should
    not blow the caller's retry budget. Only genuinely unparseable output
    (bad JSON, wrong top-level shape, missing/non-string `action`) raises
    `NodeError` — the caller (the negotiation loop, capped at
    `config.NEGOTIATION_TURN_CAP`) owns retries, same as every other node.
    """
    offer_lines = []
    for item in merchant_cart if isinstance(merchant_cart, list) else []:
        if isinstance(item, dict):
            # price_rupees is an OPTIONAL reasoning hint the caller may attach
            # (whole rupees, never paise); shown to the model, never returned.
            price = item.get("price_rupees")
            price_str = f" price=₹{price}" if price is not None else ""
            offer_lines.append(f"- sku={item.get('sku')} qty={item.get('qty')}{price_str}")
    offer_text = "\n".join(offer_lines) if offer_lines else "(empty cart)"

    human_prompt = (
        f"Turn {turn}.\n"
        f"Your budget ceiling: ₹{intent.get('max_paise', 0) // 100}\n"
        f"Your category: {intent.get('category')!r}, max purchases: {intent.get('max_purchases')}\n\n"
        f"Merchant's offered cart:\n{offer_text}\n\n"
        f"Merchant says: {merchant_message!r}\n\n"
        "Return the JSON object for your move."
    )

    response = llm.invoke(
        [("system", _SYSTEM_PROMPT), ("human", human_prompt)],
        purpose="buyer_negotiator",
    )
    parsed = extract_json(message_text(response))

    if not isinstance(parsed, dict):
        raise NodeError(f"negotiator expected a JSON object, got {type(parsed).__name__}: {parsed!r}")

    action = parsed.get("action")
    if not isinstance(action, str):
        raise NodeError(f"negotiator response missing a string 'action': {parsed!r}")
    action = action.strip().lower()

    message = parsed.get("message")
    if not isinstance(message, str):
        message = ""

    if action == "walk_away":
        return {"action": "walk_away", "cart": [], "message": message}

    if action == "counter":
        return {"action": "counter", "cart": _validate_cart_items(parsed.get("cart")), "message": message}

    # "accept", or any unrecognised verb -> accept the merchant's own offer.
    return {"action": "accept", "cart": _validate_cart_items(merchant_cart), "message": message}
