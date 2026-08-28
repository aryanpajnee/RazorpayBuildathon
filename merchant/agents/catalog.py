"""Agent #2 — Catalog (semantic search).

The merchant's own semantic inventory search, offered as a service: given a
natural-language query (and an optional intent), rank/filter the merchant's
real catalog by meaning rather than by substring match. `merchant/catalog.py`
already has a plain deterministic lookup (`all_products`, `get_product`) — this
module sits on top of it and adds judgment: "cross-training shoes for a bad
knee" has to become a set of SKUs, and that's not a job for `str.find`.

Same money-path boundary as every other agent surface in this project: the
model is shown whole-rupee prices for reasoning only, and this function never
returns anything the model invented. Every dict in the result is the
catalog's own — untouched, straight from `merchant.catalog.all_products()` —
so a model that "picks" a product can at worst return the wrong SKU; it can
never smuggle in a wrong price, because there is no price field left for it
to write. A SKU the model invents (not present in the catalog it was shown)
is dropped, exactly like `buyer/discovery.py` drops an invented SKU from its
own model.
"""

from __future__ import annotations

from buyer import llm
from buyer.nodes_common import NodeError, extract_json, message_text
from merchant import catalog as merchant_catalog

_SYSTEM_PROMPT = """You are the merchant's own catalog search agent. You are
given the merchant's full product catalog and a shopper's natural-language
query (and optionally a shopping intent). Rank the SKUs of products that are
semantically relevant to the query - ignore products that clearly don't fit
the query's meaning, and ignore products outside the intent's category when an
intent is given. You never see or return a price in paise; the rupee prices
shown are for your own reasoning only, and you never invent a SKU that isn't
in the list you were given.

Respond with exactly one JSON array of SKU strings, ordered from most to
least relevant, and nothing else - no markdown fence, no commentary before or
after it. Example: ["NW-SHOE-001", "NW-SHOE-003"]
If nothing is relevant, respond with an empty array: []"""


def search(*, query: str, intent: dict | None = None, limit: int = 10) -> list[dict]:
    """Semantically rank the merchant's catalog against `query`.

    Returns up to `limit` product dicts straight from
    `merchant.catalog.all_products()`, in the order the model judged most
    relevant to `query` (and to `intent["category"]` when `intent` is given).
    An empty `query` short-circuits to the first `limit` catalog products
    without calling the model at all - there is nothing for the model to rank
    against, and a query-less "search" is really just "list the catalog", so
    paying for an LLM call to shuffle SKUs the model has no basis to order
    would be a false economy of judgment: no information, no inference.

    Raises `NodeError` if the model's output isn't a parseable JSON array of
    strings. This is a service surface other agents call, not something a
    shopper sees directly, so the caller owns the retry/fallback decision -
    this function does not swallow a bad response and guess instead.
    """
    products = merchant_catalog.all_products()
    if not query or not query.strip():
        return products[:limit]

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

    intent_line = ""
    if intent:
        intent_line = (
            f"Intent category (must match if given): {intent.get('category')!r}\n"
        )

    human_prompt = (
        f"Query: {query!r}\n"
        f"{intent_line}\n"
        "Full catalog:\n" + "\n".join(catalog_lines) + "\n\n"
        "Return the JSON array of relevant SKUs, ranked most to least relevant."
    )

    response = llm.invoke(
        [("system", _SYSTEM_PROMPT), ("human", human_prompt)],
        purpose="catalog_agent",
    )
    parsed = extract_json(message_text(response))

    if not isinstance(parsed, list):
        raise NodeError(
            f"catalog agent expected a JSON array, got {type(parsed).__name__}: {parsed!r}"
        )

    ranked: list[dict] = []
    for sku in parsed:
        if isinstance(sku, str) and sku in by_sku:
            ranked.append(by_sku[sku])
        if len(ranked) >= limit:
            break
    return ranked
