"""Agent #3 — Sales (upsell / cross-sell / bundle).

The merchant's growth agent: given the buyer's current cart and its shopping
intent, it proposes complementary ADD-ON products drawn from the real catalog
to raise order value. Like Discovery/Evaluator on the buyer side, it returns
SKUs and quantities only — `list[{"sku": str, "qty": int}]` — never a price,
never a total. The merchant re-derives every price from its own catalog at
quote time and the Gate enforces the user's authorised ceiling regardless of
what this agent pitches.

`purpose="sales"` is deliberately NOT in `config.FAST_LLM_SURFACES`: picking a
complementary product against a cart and a budget figure is a judgment call
that benefits from Gemini's stronger reasoning, not a pure-prose task, so it
stays on the default (numeric-safe) provider.

The headline design decision — read before touching this file — is in
`upsell`'s docstring: this agent does NOT self-censor to stay under the
buyer's budget ceiling. It pitches to maximise order value; the Gate is the
only enforced bound. Suppressing an over-ceiling suggestion here would hide
the exact scenario Phase 5 exists to demonstrate: the merchant's own sales
agent proposes something outside the user's signed authority, and the
merchant's own Gate — not this agent's good behaviour — is what refuses it.
"""

from __future__ import annotations

from buyer import llm
from buyer.nodes_common import NodeError, extract_json, message_text

_SYSTEM_PROMPT = """You are the sales agent for an online merchant. You are
shown a buyer's current cart, their shopping intent, and the merchant's full
catalog. Your job is to pitch ADD-ON products that complement what's already
in the cart - cross-sell, upsell, or bundle items - to raise the order's
value. You are the merchant's growth agent: your goal is to maximise what the
buyer ends up purchasing, not to stay under any budget. Do not hold back a
good add-on because it might be expensive - a separate system enforces
spending limits, not you.

Never suggest a product already in the cart. Only suggest products that
actually appear in the catalog list you were given - never invent a SKU. You
never see or return a price in paise or rupees, and you never compute a
total; pricing and budget enforcement are handled elsewhere by code you do
not control.

Respond with exactly one JSON object with two keys: "add", a JSON array of
objects with ONLY "sku" and "qty" keys, and "pitch", a short (1-3 sentence)
persuasive prose blurb for the buyer. Nothing else - no markdown fence, no
commentary before or after it, no price field of any kind. Example:
{"add": [{"sku": "NW-SOCK-002", "qty": 2}], "pitch": "Pair these with a set of our moisture-wicking socks."}
If nothing in the catalog complements the cart, respond with
{"add": [], "pitch": ""}"""


def upsell(*, cart: list[dict], intent: dict, catalog: list[dict] | None = None) -> dict:
    """Propose complementary add-ons for `cart` given `intent`.

    Makes exactly one `llm.invoke` call. Returns
    `{"add": list[{"sku": str, "qty": int}], "pitch": str}`. `catalog`
    defaults to `merchant.catalog.all_products()` when `None` — a keyword
    default rather than a hardcoded import-time call, so a caller (or a test)
    can pass a small fixed catalog without monkeypatching the catalog module.

    `add` is filtered, not repaired: a SKU the model invents, a SKU already
    in `cart`, a duplicate within the model's own response, or a qty that
    isn't a positive `int` (via `type(qty) is int`, so a stray `True`/`False`
    can't pass as a quantity) is silently dropped rather than "fixed" -
    fixing a bad item risks fabricating a suggestion the model didn't
    actually make. An empty `add` is a valid, expected no-op (nothing fits),
    not an error; `NodeError` is reserved for output that couldn't be parsed
    or shaped as a JSON object at all.

    Deliberately absent: any check that compares a suggested add-on's price
    against `intent["max_paise"]` or the cart's running total. This agent is
    the merchant's *sales* agent - its job is to maximise order value, and
    the only bound on what a buyer can actually be charged is the Gate,
    downstream, checking the signed mandate ceiling. A self-imposed budget
    check here would quietly suppress the exact case Phase 5 demonstrates:
    the merchant's own growth agent proposing something outside the buyer's
    authorised spend, and the merchant's own enforcement - not this agent's
    restraint - being what stops it from becoming a charge.
    """
    if catalog is None:
        from merchant.catalog import all_products

        catalog = all_products()

    cart_skus = {item["sku"] for item in cart if isinstance(item, dict) and "sku" in item}
    candidates = [p for p in catalog if p["sku"] not in cart_skus]
    if not candidates:
        return {"add": [], "pitch": ""}

    known_skus = {p["sku"] for p in candidates}

    cart_lines = []
    for item in cart:
        cart_lines.append(f"- sku={item.get('sku')} qty={item.get('qty')}")

    catalog_lines = []
    for product in candidates:
        price_rupees = product.get("price_paise", 0) // 100
        catalog_lines.append(
            f"- sku={product['sku']} name={product.get('name', '')!r} "
            f"category={product.get('category', '')!r} price=₹{price_rupees} "
            f"stock={product.get('stock', 0)} tags={product.get('tags', [])}"
        )

    human_prompt = (
        f"Intent: category={intent.get('category')!r}, "
        f"budget ceiling=₹{intent.get('max_paise', 0) // 100}\n\n"
        "Current cart:\n" + ("\n".join(cart_lines) if cart_lines else "(empty)") + "\n\n"
        "Catalog (add-on candidates, cart items excluded):\n"
        + "\n".join(catalog_lines) + "\n\n"
        "Return the JSON object with your \"add\" suggestions and \"pitch\"."
    )

    response = llm.invoke(
        [("system", _SYSTEM_PROMPT), ("human", human_prompt)],
        purpose="sales",
    )
    parsed = extract_json(message_text(response))

    if not isinstance(parsed, dict):
        raise NodeError(f"sales expected a JSON object, got {type(parsed).__name__}: {parsed!r}")

    raw_add = parsed.get("add")
    if not isinstance(raw_add, list):
        raise NodeError(f"sales expected 'add' to be a JSON array, got {type(raw_add).__name__}: {raw_add!r}")

    pitch = parsed.get("pitch")
    if not isinstance(pitch, str):
        pitch = ""

    add: list[dict] = []
    seen_skus: set[str] = set()
    for item in raw_add:
        if not isinstance(item, dict):
            continue
        sku = item.get("sku")
        qty = item.get("qty")
        if not isinstance(sku, str) or sku not in known_skus:
            continue
        if sku in seen_skus:
            continue
        if type(qty) is not int or qty < 1:
            continue
        seen_skus.add(sku)
        add.append({"sku": sku, "qty": qty})

    return {"add": add, "pitch": pitch}
