"""Agent #10 — Evaluator.

Picks the best cart (sku + qty pairs) from discovery's candidates. This is
the last stop before `buyer/agent.py` builds and signs a Cart Mandate, so the
boundary here is deliberately narrow: the model returns SKU and quantity only
- never a price, never a total. `buyer/agent.py` calls `/quote` afterwards and
the merchant computes the authoritative total from its own catalog; nothing
this module returns is ever trusted as a price.
"""

from __future__ import annotations

from buyer.nodes_common import NodeError, extract_json, message_text
from buyer import llm

_SYSTEM_PROMPT = """You are the evaluator agent for an autonomous shopping buyer.
You are given a list of candidate products and a shopping intent. Choose the
best cart that satisfies the intent - considering fit, quantity, and staying
within the max_purchases count. You never see or return a price in paise or
rupees, and you never compute a total; pricing and totals are handled
elsewhere by code you do not control.

Respond with exactly one JSON array of objects with ONLY "sku" and "qty"
keys, and nothing else - no markdown fence, no commentary before or after it,
no price field of any kind. Example: [{"sku": "NW-SHOE-001", "qty": 1}]
If nothing in the candidates fits, respond with an empty array: []"""


def evaluate(*, candidates: list[dict], intent: dict, relaxed: bool = False) -> list[dict]:
    """Choose a cart from `candidates` for `intent`.

    Makes exactly one `llm.invoke` call. Returns a list of
    `{"sku": str, "qty": int}` dicts - never a price field. Items with an
    unrecognised sku (not in `candidates`) or a non-positive/non-int qty are
    dropped rather than repaired: this function must not crash on a bad
    shape, but "fixing" a bad item risks fabricating a decision the model
    didn't actually make. The authoritative strict validation
    (`extra="forbid"`) happens downstream in `agent.py`.
    """
    if not candidates:
        return []

    known_skus = {c["sku"] for c in candidates}

    candidate_lines = []
    for product in candidates:
        price_rupees = product.get("price_paise", 0) // 100
        candidate_lines.append(
            f"- sku={product['sku']} name={product.get('name', '')!r} "
            f"price=₹{price_rupees} stock={product.get('stock', 0)} "
            f"tags={product.get('tags', [])}"
        )

    human_prompt = (
        f"Intent: category={intent.get('category')!r}, "
        f"budget ceiling=₹{intent.get('max_paise', 0) // 100}, "
        f"max purchases={intent.get('max_purchases')}\n\n"
        "Candidates:\n" + "\n".join(candidate_lines) + "\n\n"
        "Return the JSON array of {sku, qty} for the best cart only."
    )

    response = llm.invoke(
        [("system", _SYSTEM_PROMPT), ("human", human_prompt)],
        purpose="evaluator",
    )
    parsed = extract_json(message_text(response))

    if not isinstance(parsed, list):
        raise NodeError(f"evaluator expected a JSON array, got {type(parsed).__name__}: {parsed!r}")

    cart: list[dict] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        sku = item.get("sku")
        qty = item.get("qty")
        if not isinstance(sku, str) or sku not in known_skus:
            continue
        if type(qty) is not int or qty < 1:
            continue
        cart.append({"sku": sku, "qty": qty})
    return cart
