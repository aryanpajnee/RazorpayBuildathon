"""Agent #4 — Merchant Negotiator.

Responds to a buyer's negotiation turn (a proposed cart plus a plain-English
message asking for a better deal) with `hold`, `concede`, or `walk_away`. The
one rule this module exists to enforce: **a negotiator never invents a
discount.** It has no lever to lower a price - `merchant/quote.py` computes
every total from `merchant/catalog.py`'s real `price_paise`, and the Gate
re-derives it again independently. So "conceding" here can only ever mean one
thing: proposing a genuinely cheaper REAL-catalog cart - a cheaper alternative
SKU in the same category, a smaller quantity, or a real bundle SKU - which the
merchant then quotes at its true catalog price. This mirrors AP2's model
(agents negotiate carts; the merchant authors the cart's price) and Razorpay's
own cart-recovery pattern (offer a better deal via a different cart, not a
different price on the same cart).

Its "walk-away point" is exactly that constraint made explicit: it will not,
and structurally cannot, propose anything below its cheapest genuinely-fitting
catalog offer - because every offer it can make IS a catalog offer, priced by
someone else.
"""

from __future__ import annotations

from buyer import llm
from buyer.nodes_common import NodeError, extract_json, message_text
from merchant.catalog import all_products

_VALID_ACTIONS = {"hold", "concede", "walk_away"}

_SYSTEM_PROMPT = """You are the merchant's negotiation agent for Northwind, an
online athletic-gear store. An AI buyer has proposed a cart and sent you a
message, usually asking for a better price or a discount.

You have NO authority to lower a price on any product - prices are fixed by
the catalog and you never state, invent, or apply a discount amount. The only
way you can offer the buyer a better deal is by proposing a DIFFERENT, cheaper
cart built entirely from the real catalog products shown to you below: a
cheaper alternative product in the same category, a smaller quantity, or a
genuine bundle SKU. You never invent a SKU that isn't listed below.

Decide one of three actions:
- "hold": you are not offering anything cheaper than the buyer's own cart.
  Return the buyer's cart unchanged and explain politely that this is your
  standing offer.
- "concede": you found a genuinely cheaper cart from the real catalog below
  that still plausibly satisfies the buyer's need. Return that cart's skus
  and quantities, and a message presenting it as your better offer.
- "walk_away": further negotiation will not produce anything cheaper (you are
  already at your cheapest genuinely-fitting catalog offer, or nothing in the
  catalog fits at all). Return an empty cart and a message declining further
  negotiation.

You never state a price, a discount percentage, or a total of any kind in
your message or your cart - prices are computed elsewhere, not by you.

Respond with exactly one JSON object with keys "action" ("hold", "concede",
or "walk_away"), "cart" (an array of {"sku": str, "qty": int} objects - use
the buyer's own cart for "hold", the cheaper cart for "concede", or an empty
array for "walk_away"), and "message" (a short string with no price or number
in it). No markdown fence, no commentary before or after it."""


def counter(*, buyer_cart: list[dict], buyer_message: str, intent: dict, turn: int) -> dict:
    """One negotiation turn. Makes exactly one `llm.invoke` call.

    Returns `{"action": ..., "cart": [{"sku", "qty"}, ...], "message": ...}`.
    `cart` is always validated against `merchant.catalog.all_products()` -
    any sku the model invents (for "concede" or a mis-shaped "hold") is
    dropped, never repaired or substituted, matching `discovery.py` and
    `evaluator.py`'s convention. `turn` is informational only: this function
    does not compare it against `config.NEGOTIATION_TURN_CAP` - the caller
    (the buyer/merchant orchestration loop) owns the turn cap and decides when
    to stop calling `counter` at all. Raises `NodeError` if the model's output
    cannot be parsed as the expected JSON shape; an unrecognised `action`
    string is treated as "hold" rather than raised, since "hold" is the
    always-safe no-op response.
    """
    catalog = all_products()
    known_skus = {product["sku"] for product in catalog}
    by_sku = {product["sku"]: product for product in catalog}

    buyer_cart_lines = []
    for item in buyer_cart:
        sku = item.get("sku") if isinstance(item, dict) else None
        qty = item.get("qty") if isinstance(item, dict) else None
        product = by_sku.get(sku)
        if product is None:
            continue
        price_rupees = product.get("price_paise", 0) // 100
        buyer_cart_lines.append(
            f"- sku={sku} qty={qty} name={product.get('name', '')!r} "
            f"price=₹{price_rupees}"
        )

    catalog_lines = []
    for product in catalog:
        price_rupees = product.get("price_paise", 0) // 100
        catalog_lines.append(
            f"- sku={product['sku']} name={product.get('name', '')!r} "
            f"category={product.get('category', '')!r} price=₹{price_rupees} "
            f"stock={product.get('stock', 0)} tags={product.get('tags', [])}"
        )

    human_prompt = (
        f"Negotiation turn {turn}.\n"
        f"Buyer's message: {buyer_message!r}\n\n"
        f"Buyer's proposed cart:\n" + ("\n".join(buyer_cart_lines) or "(empty)") + "\n\n"
        f"Intent: category={intent.get('category')!r}, "
        f"budget ceiling=₹{intent.get('max_paise', 0) // 100}, "
        f"max purchases={intent.get('max_purchases')}\n\n"
        "Real catalog (only these skus may appear in your response cart):\n"
        + "\n".join(catalog_lines) + "\n\n"
        "Return the JSON object for your decision."
    )

    response = llm.invoke(
        [("system", _SYSTEM_PROMPT), ("human", human_prompt)],
        purpose="merchant_negotiator",
    )
    parsed = extract_json(message_text(response))

    if not isinstance(parsed, dict):
        raise NodeError(
            f"merchant_negotiator expected a JSON object, got {type(parsed).__name__}: {parsed!r}"
        )

    action = parsed.get("action")
    if action not in _VALID_ACTIONS:
        action = "hold"

    message = parsed.get("message")
    if not isinstance(message, str):
        message = ""

    if action == "walk_away":
        return {"action": "walk_away", "cart": [], "message": message}

    raw_cart = parsed.get("cart")
    cart: list[dict] = []
    if isinstance(raw_cart, list):
        for item in raw_cart:
            if not isinstance(item, dict):
                continue
            sku = item.get("sku")
            qty = item.get("qty")
            if not isinstance(sku, str) or sku not in known_skus:
                continue
            if type(qty) is not int or qty < 1:
                continue
            cart.append({"sku": sku, "qty": qty})

    if action == "hold" and not cart:
        # No usable cart came back (e.g. the model echoed something
        # unparseable) - the always-safe fallback for "hold" is the buyer's
        # own cart, re-validated against the real catalog the same way.
        for item in buyer_cart:
            if not isinstance(item, dict):
                continue
            sku = item.get("sku")
            qty = item.get("qty")
            if not isinstance(sku, str) or sku not in known_skus:
                continue
            if type(qty) is not int or qty < 1:
                continue
            cart.append({"sku": sku, "qty": qty})

    return {"action": action, "cart": cart, "message": message}
