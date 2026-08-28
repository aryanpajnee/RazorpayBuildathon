"""Agent #12 — Recovery.

Called from `buyer/agent.py`'s `_recover` only on the branch where the cart's
COMPOSITION has to change to have any chance of succeeding — chiefly a Gate
refusal with `code == "OVER_LIMIT"`. Today that branch is a deterministic
stub, `_drop_most_expensive` (drops the priciest line, priced from the last
quote or discovery's candidates, never a model guess). This module is the LLM
upgrade for that ONE decision: which line(s) to drop or substitute so the next
attempt is more likely to clear the Gate. It does not decide the recover/fail/
abandon classification, does not touch `attempt_count`, and does not decide
which phase to loop back to - `_recover` in `agent.py` keeps all of that. This
module answers exactly one question: "given this failure and this cart, what
adjusted cart should we try next?"

Money discipline: like `evaluator.py`, this returns SKU and quantity only.
Prices (the cart's own lines, the failure's `detail`, and the candidates) are
shown to the model as whole rupees FOR REASONING ONLY - `agent.py` re-quotes
the adjusted cart from scratch (`state.quote = None` before looping to
COMMIT), and the Gate re-derives and re-enforces the total. A bad recovery
proposal can at worst pick a suboptimal substitute; it can never cause a wrong
charge, because nothing here is ever treated as an authoritative price.
"""

from __future__ import annotations

from buyer.nodes_common import NodeError, extract_json, message_text
from buyer import llm

_SYSTEM_PROMPT = """You are the recovery agent for an autonomous shopping buyer.
A checkout attempt just failed. You are given the structured failure, the
cart that failed, and the candidate products that are available as
substitutes. Propose an ADJUSTED cart more likely to succeed next time - for
example, drop the item that is pushing the cart over the budget ceiling, or
swap it for a cheaper candidate in the same category.

You may only use skus that appear in the cart or the candidates list below -
never invent a sku. You never see or return a price in paise or rupees, and
you never compute a total; pricing and totals are handled elsewhere by code
you do not control.

If the failure reason is OVER_LIMIT, bias toward dropping or substituting the
single most expensive line in the cart so the new cart is cheaper overall -
you do not need to hit an exact number, the merchant will re-price and
re-check the new cart itself.

Respond with exactly one JSON array of objects with ONLY "sku" and "qty"
keys, and nothing else - no markdown fence, no commentary before or after it,
no price field of any kind. Example: [{"sku": "NW-SHOE-007", "qty": 1}]
If no adjustment gives this cart a reasonable chance of succeeding, respond
with an empty array: [] to signal the attempt should be abandoned."""


def propose_recovery(*, failure: dict, cart: list[dict], candidates: list[dict], intent: dict) -> list[dict]:
    """Propose an adjusted cart in response to `failure`.

    `failure` is the same structured shape `agent.py` classifies elsewhere:
    `{"reason": str, "code": str | None, "recoverable": bool, "detail": dict}`.
    `cart` is the current `[{sku, qty}]` that failed. `candidates` is the
    discovery product list (`{sku, name, category, price_paise, stock, tags,
    ...}`) available as substitutes. Only skus present in `cart` or
    `candidates` are ever returned - anything the model invents is dropped.

    Returns the adjusted `[{"sku": str, "qty": int}]`, or `[]` if no sensible
    adjustment exists - the caller (`agent.py`) treats `[]` exactly like
    today's `_drop_most_expensive` returning `None`: give up, go to
    ABANDONED. Raises `NodeError` only when the model's output cannot be
    parsed into a list at all; a malformed individual item is dropped rather
    than failing the whole proposal, same policy as `evaluator.py`.

    Deliberately stricter than `evaluator.py` on one point: an item carrying
    any key beyond "sku"/"qty" is dropped outright rather than having the
    extra key silently stripped. Recovery's whole reason for existing is a
    refusal that already happened once: tolerating a shape the model wasn't
    asked for is the last place to start being lenient again.
    """
    if not cart:
        return []

    known_skus = {c["sku"] for c in candidates}
    known_skus.update(item["sku"] for item in cart if isinstance(item, dict) and isinstance(item.get("sku"), str))

    price_by_sku: dict[str, int] = {c["sku"]: c.get("price_paise", 0) for c in candidates}

    cart_lines = []
    for item in cart:
        sku = item.get("sku")
        qty = item.get("qty")
        price_paise = price_by_sku.get(sku)
        price_str = f"₹{price_paise // 100}" if price_paise is not None else "price unknown"
        cart_lines.append(f"- sku={sku} qty={qty} price={price_str}")

    candidate_lines = []
    for product in candidates:
        price_rupees = product.get("price_paise", 0) // 100
        candidate_lines.append(
            f"- sku={product['sku']} name={product.get('name', '')!r} "
            f"price=₹{price_rupees} stock={product.get('stock', 0)} "
            f"tags={product.get('tags', [])}"
        )

    detail_parts = []
    for key, value in (failure.get("detail") or {}).items():
        if key.endswith("_paise") and isinstance(value, (int, float)):
            detail_parts.append(f"{key.removesuffix('_paise')}=₹{int(value) // 100}")
        else:
            detail_parts.append(f"{key}={value!r}")
    detail_str = ", ".join(detail_parts) if detail_parts else "none"

    human_prompt = (
        f"Failure: reason={failure.get('reason')!r} code={failure.get('code')!r} "
        f"recoverable={failure.get('recoverable')!r}\n"
        f"Failure detail: {detail_str}\n\n"
        f"Intent: category={intent.get('category')!r}, "
        f"budget ceiling=₹{intent.get('max_paise', 0) // 100}, "
        f"max purchases={intent.get('max_purchases')}\n\n"
        "Current cart (failed):\n" + "\n".join(cart_lines) + "\n\n"
        "Available candidates:\n"
        + ("\n".join(candidate_lines) if candidate_lines else "(none)")
        + "\n\n"
        "Return the JSON array for the adjusted cart, or [] to give up."
    )

    response = llm.invoke(
        [("system", _SYSTEM_PROMPT), ("human", human_prompt)],
        purpose="recovery",
    )
    parsed = extract_json(message_text(response))

    if not isinstance(parsed, list):
        raise NodeError(f"recovery expected a JSON array, got {type(parsed).__name__}: {parsed!r}")

    adjusted: list[dict] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        if set(item.keys()) != {"sku", "qty"}:
            continue
        sku = item.get("sku")
        qty = item.get("qty")
        if not isinstance(sku, str) or sku not in known_skus:
            continue
        if type(qty) is not int or qty < 1:
            continue
        adjusted.append({"sku": sku, "qty": qty})
    return adjusted
