"""Agent #5 — Substitution.

Given a SKU the buyer can't actually get (out of stock, over their signed
budget, or just a poor fit for the intent), propose alternative products from
the SAME category that still satisfy the intent. Like every other agent in
this org, the model sees whole-rupee prices for reasoning only and returns
SKUs — never a price, never a total; the merchant re-derives every price from
its own catalog at quote time, and the Gate enforces the ceiling regardless of
what this agent suggests.

`purpose="substitution"` IS in `config.FAST_LLM_SURFACES` — this is
semantic matching over prose (name/category/tags/description) plus a ranking
call, with no arithmetic the model needs to get right, so it takes the fast
NVIDIA lane rather than Gemini.
"""

from __future__ import annotations

from buyer import llm
from buyer.nodes_common import NodeError, extract_json, message_text

_SYSTEM_PROMPT = """You are the substitution agent for an online merchant. A
buyer was unable to get one product - it might be out of stock, over their
budget, or a poor fit - and you are shown that product's reason for exclusion,
the buyer's shopping intent, and a list of other candidate products from the
SAME category. Propose alternative products that still satisfy the intent,
ranked best-first.

If the reason is "over_budget", prefer cheaper candidates over expensive ones.
If the reason is "out_of_stock", every candidate you are shown is already
confirmed in stock - just pick the ones that best fit the intent. For any
other reason, judge fit using name, tags, and description.

Only suggest products that actually appear in the candidate list you were
given - never invent a SKU, and never suggest the original excluded product.
You never see or return a price in paise, and you never compute a total;
pricing is handled elsewhere by code you do not control.

Respond with exactly one JSON array of SKU strings, ranked best-first, and
nothing else - no markdown fence, no commentary before or after it. Example:
["NW-SHOE-002", "NW-SHOE-007"]
If nothing in the candidates fits, respond with an empty array: []"""


def substitute(
    *, sku: str, reason: str, intent: dict, catalog: list[dict] | None = None
) -> list[dict]:
    """Propose alternatives to `sku` for `intent`, given `reason` it's excluded.

    Makes exactly one `llm.invoke` call. Returns a list of full catalog
    product dicts (never a model-fabricated price), ranked best-first,
    excluding `sku` itself. `catalog` defaults to
    `merchant.catalog.all_products()` when `None` — a keyword default rather
    than a hardcoded import-time call, so a caller (or a test) can pass a
    small fixed catalog without monkeypatching the catalog module.

    Candidate filtering happens in Python, not by asking the model to filter:
    - always excludes `sku` itself and anything outside `sku`'s category
      (the whole point is a same-category swap);
    - when `reason == "out_of_stock"`, also excludes every candidate with
      `stock <= 0` — the exact failure the buyer just hit shouldn't be handed
      back to them as the "fix". For any other reason (over_budget, no_fit,
      or anything else the caller passes as a free string) any stock level is
      offered; a buyer over budget on an in-stock item may still want a
      cheaper in-stock item, but "no_fit" alternatives can reasonably include
      one currently out of stock that the buyer might reorder later — this
      agent proposes, it doesn't gate.

    The model's ranked SKU list is then filtered again against that same
    candidate set: a SKU the model invents or one outside the candidates
    (including the excluded original) is silently dropped rather than
    "fixed" — fixing a bad item risks fabricating a suggestion the model
    didn't actually make. `NodeError` is reserved for output that couldn't be
    parsed or shaped as a JSON array at all.
    """
    if catalog is None:
        from merchant.catalog import all_products

        catalog = all_products()

    original = next((p for p in catalog if p["sku"] == sku), None)
    category = original["category"] if original else None

    candidates = [p for p in catalog if p["sku"] != sku]
    if category is not None:
        candidates = [p for p in candidates if p.get("category") == category]
    if reason == "out_of_stock":
        candidates = [p for p in candidates if p.get("stock", 0) > 0]

    if not candidates:
        return []

    by_sku = {p["sku"]: p for p in candidates}

    candidate_lines = []
    for product in candidates:
        price_rupees = product.get("price_paise", 0) // 100
        candidate_lines.append(
            f"- sku={product['sku']} name={product.get('name', '')!r} "
            f"price=₹{price_rupees} stock={product.get('stock', 0)} "
            f"tags={product.get('tags', [])} "
            f"description={product.get('description', '')!r}"
        )

    original_name = original.get("name", "") if original else ""
    original_rupees = (original.get("price_paise", 0) // 100) if original else None
    original_price_str = f"₹{original_rupees}" if original_rupees is not None else "unknown"
    human_prompt = (
        f"Excluded product: sku={sku!r} name={original_name!r} "
        f"price={original_price_str}\n"
        f"Reason excluded: {reason!r}\n\n"
        f"Intent: category={intent.get('category')!r}, "
        f"budget ceiling=₹{intent.get('max_paise', 0) // 100}, "
        f"max purchases={intent.get('max_purchases')}\n\n"
        "Candidate alternatives (same category):\n" + "\n".join(candidate_lines) + "\n\n"
        "Return the ranked JSON array of alternative SKUs only."
    )

    response = llm.invoke(
        [("system", _SYSTEM_PROMPT), ("human", human_prompt)],
        purpose="substitution",
    )
    parsed = extract_json(message_text(response))

    if not isinstance(parsed, list):
        raise NodeError(
            f"substitution expected a JSON array, got {type(parsed).__name__}: {parsed!r}"
        )

    alternatives: list[dict] = []
    seen_skus: set[str] = set()
    for item in parsed:
        if isinstance(item, str) and item in by_sku and item not in seen_skus:
            seen_skus.add(item)
            alternatives.append(by_sku[item])
    return alternatives
