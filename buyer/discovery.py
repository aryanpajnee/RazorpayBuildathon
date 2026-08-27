"""Agent #9 — Discovery.

Reads the merchant's catalog search (`GET /catalog/search`) and asks the model
to pick which returned products plausibly satisfy the intent/strategy. This is
semantic matching, not pricing: the model sees whole-rupee prices for
readability only, never computes a total, and returns skus — nothing else.

`merchant/catalog.py`'s own docstring draws the same line this module respects:
"a buyer names a sku and a quantity... [a]nything price-shaped arriving from a
buyer is dropped on the floor." Discovery is on the buyer side of that line —
it only ever reads the catalog, and the merchant re-derives every price itself
at quote time regardless of what discovery or evaluator say.
"""

from __future__ import annotations

from buyer import llm
from buyer.nodes_common import NodeError, extract_json, message_text

_SYSTEM_PROMPT = """You are the discovery agent for an autonomous shopping buyer.
You are given a list of catalog products and a shopping intent. Pick the SKUs
of products that plausibly satisfy the intent - ignore products that clearly
don't fit the category, tags, or description. You never see or return a price
in paise; the rupee prices shown are for your own reasoning only.

Respond with exactly one JSON array of SKU strings, and nothing else - no
markdown fence, no commentary before or after it. Example: ["NW-SHOE-001", "NW-SHOE-003"]
If nothing fits, respond with an empty array: []"""


def _search_query(strategy: dict, intent: dict, *, relaxed: bool) -> str:
    """Deterministic query string for `GET /catalog/search`.

    `/catalog/search` is a plain case-insensitive SUBSTRING filter (see
    `merchant/api.py`), so `q` must be a single clean token like "footwear".
    A phrase such as "running shoes" is a substring no product name, tag,
    category or description contains verbatim, so it would filter the catalog
    down to nothing and silently kill the discovery pass. So: use the first
    single-word category token available (intent category first, then the
    planner's `target_category`); if neither is a single token, search the
    WHOLE catalog ("") and let the model narrow — never filter to zero on a
    phrase. The planner's prose `strategy`/`target_category` still informs the
    model via the prompt below; it just isn't used as the raw substring.

    Relaxed mode (used by recovery, e.g. after a first pass returns nothing)
    widens deliberately to the whole catalog for the same reason.
    """
    if relaxed:
        return ""
    for source in (intent.get("category"), strategy.get("target_category")):
        if isinstance(source, str):
            token = source.strip()
            if token and " " not in token:
                return token
    return ""


def discover(*, strategy: dict, intent: dict, http, relaxed: bool = False) -> list[dict]:
    """Search the catalog and return the full product dicts the model deems
    plausible candidates for `intent`.

    `http` is an `httpx.Client` with `base_url` already pointed at the
    merchant; this function performs exactly one catalog read and at most one
    `llm.invoke` call. Unknown SKUs the model invents (not present in the
    search results) are silently dropped. Returns `[]` if the search itself
    is empty or the model selects nothing.
    """
    query = _search_query(strategy, intent, relaxed=relaxed)
    response = http.get("/catalog/search", params={"q": query})
    response.raise_for_status()
    products = response.json().get("products", [])
    if not products:
        return []

    by_sku = {product["sku"]: product for product in products}

    catalog_lines = []
    for product in products:
        price_rupees = product.get("price_paise", 0) // 100
        catalog_lines.append(
            f"- sku={product['sku']} name={product.get('name', '')!r} "
            f"category={product.get('category', '')!r} price=₹{price_rupees} "
            f"stock={product.get('stock', 0)} tags={product.get('tags', [])} "
            f"description={product.get('description', '')!r}"
        )

    human_prompt = (
        f"Intent: category={intent.get('category')!r}, "
        f"budget ceiling=₹{intent.get('max_paise', 0) // 100}, "
        f"max purchases={intent.get('max_purchases')}\n"
        f"Strategy: {strategy.get('strategy', '')!r}\n\n"
        "Candidate products:\n" + "\n".join(catalog_lines) + "\n\n"
        "Return the JSON array of plausible SKUs only."
    )

    response = llm.invoke(
        [("system", _SYSTEM_PROMPT), ("human", human_prompt)],
        purpose="discovery",
    )
    parsed = extract_json(message_text(response))

    if not isinstance(parsed, list):
        raise NodeError(f"discovery expected a JSON array, got {type(parsed).__name__}: {parsed!r}")

    selected: list[dict] = []
    for sku in parsed:
        if isinstance(sku, str) and sku in by_sku:
            selected.append(by_sku[sku])
    return selected
