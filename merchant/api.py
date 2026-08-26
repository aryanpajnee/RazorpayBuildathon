"""The merchant's HTTP surface: catalog search, quoting, checkout, ledger.

This file is wiring, not logic. Every endpoint is a thin adapter over a
vault-code module that already enforces its own rules:

    GET  /catalog/search  -> merchant.catalog.all_products (plain filter)
    POST /quote            -> merchant.catalog.resolve_lines + merchant.quote.create_quote
                               + merchant.quote_store.save_quote + core.ledger.append
    POST /checkout          -> merchant.gate.check  (the one chokepoint before money)
    GET  /ledger            -> core.ledger.all_entries

No business logic lives here. In particular, `/checkout` never raises on a
buyer's cart mandate, however malformed or adversarial — `gate.check()`
already turns every such case into a `GateResult`, and this route always
returns HTTP 200 with `passed`/`reason_code` in the body. A downstream
recovery agent branches on those fields, not on the HTTP status.

Catalog search here is a plain, deterministic substring filter — case
insensitive, over name/tags/category/description. No LLM anywhere in this
file; semantic catalog search is a later-phase agent surface layered on top
of `all_products()`, not a replacement for it.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from core.ledger import all_entries, append
from merchant import catalog
from merchant.catalog import all_products, resolve_lines
from merchant.gate import check
from merchant.quote import create_quote
from merchant.quote_store import save_quote

app = FastAPI(title="Northwind Merchant API")


# --- request bodies ----------------------------------------------------------


class QuoteItem(BaseModel):
    sku: str
    qty: int = 1


class QuoteRequest(BaseModel):
    items: list[QuoteItem]


class CheckoutRequest(BaseModel):
    cart_envelope: dict


# --- catalog -------------------------------------------------------------


def _matches(product: dict, needle: str) -> bool:
    """Case-insensitive substring match against name/tags/category/description.

    A plain filter, deliberately — see the module docstring. An empty or
    omitted `q` matches everything.
    """
    if not needle:
        return True
    haystacks = [
        str(product.get("name", "")),
        str(product.get("category", "")),
        str(product.get("description", "")),
        *[str(tag) for tag in product.get("tags") or []],
    ]
    return any(needle in haystack.lower() for haystack in haystacks)


@app.get("/catalog/search")
def catalog_search(q: str = "") -> dict:
    needle = q.lower()
    products = [product for product in all_products() if _matches(product, needle)]
    return {"products": products}


# --- quote -----------------------------------------------------------------


@app.post("/quote")
def post_quote(body: QuoteRequest) -> dict:
    requests = [item.model_dump() for item in body.items]

    try:
        lines = resolve_lines(requests)
        quote = create_quote(lines)
    except catalog.ProductNotFound as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": "product_not_found", "message": str(exc)},
        ) from exc
    except catalog.OutOfStock as exc:
        raise HTTPException(
            status_code=409,
            detail={"error": "out_of_stock", "message": str(exc)},
        ) from exc
    except (TypeError, ValueError) as exc:
        # Bad quantity, empty cart, etc. — buyer's fault, never a 500.
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_request", "message": str(exc)},
        ) from exc

    save_quote(quote)
    append(
        "quote.issued",
        {
            "quote_id": quote.quote_id,
            "cart_hash": quote.cart_hash,
            "total_paise": quote.total_paise,
            "expires_at": quote.expires_at,
        },
    )
    return quote.as_dict()


# --- checkout ----------------------------------------------------------------


@app.post("/checkout")
def post_checkout(body: CheckoutRequest) -> dict:
    """Run the cart envelope through the Gate. Always HTTP 200.

    `gate.check()` never raises on buyer input — pass and refuse are both
    ordinary outcomes a downstream recovery agent branches on via the body,
    not the status code. Raising here for a refusal would defeat that
    contract.
    """
    result = check(body.cart_envelope)
    return {
        "passed": result.passed,
        "reason_code": result.reason_code,
        "message": result.message,
        "detail": result.detail,
        "total_paise": result.total_paise,
        "quote_id": result.quote_id,
        "cart_mandate_id": result.cart_mandate_id,
        "checked_at": result.checked_at,
    }


# --- ledger ------------------------------------------------------------------


@app.get("/ledger")
def get_ledger() -> dict:
    entries = all_entries()
    return {
        "entries": [
            {
                "seq": entry.seq,
                "ts": entry.ts,
                "event_type": entry.event_type,
                "payload": entry.payload,
                "prev_hash": entry.prev_hash,
                "entry_hash": entry.entry_hash,
            }
            for entry in entries
        ]
    }
