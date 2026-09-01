"""The merchant's HTTP surface: catalog search, quoting, checkout, ledger.

This file is wiring, not logic. Every endpoint is a thin adapter over a
vault-code module that already enforces its own rules:

    GET  /catalog/search  -> merchant.catalog.all_products (plain filter)
    POST /quote            -> merchant.catalog.resolve_lines + merchant.quote.create_quote
                               + merchant.quote_store.save_quote + core.ledger.append
    POST /offer             -> merchant.offers.create_offer (relist a web find),
                               then the identical /quote recipe above
    POST /checkout          -> merchant.gate.check  (the one chokepoint before money),
                               then, only on a pass, merchant.gateway.create_order
    GET  /pay/{order_id}    -> merchant.checkout_page.render_checkout_page (the one
                               human step: a real test-mode payment against the order)
    POST /webhook           -> merchant.webhooks.handle_webhook (Razorpay's callback)
    GET  /ledger            -> core.ledger.all_entries

The Gate never talks to Razorpay, so the payment-execution ledger events
(`order.created`, `payment.attempted`, and — from the webhook — `payment.succeeded`
/ `payment.failed`) are appended here, not inside `gate.check()`. A Razorpay order
is created *before* any payment exists (order-first), so `order.created` precedes
`payment.attempted`.

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

import json

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, StrictInt

import config
from core.ledger import all_entries, append
from merchant import catalog, gateway, offers, webhooks
from merchant.agents import refusal_explainer, sales, storefront, substitution
from merchant.agents import catalog as catalog_agent
from merchant.agents import negotiator as merchant_negotiator
from merchant.catalog import all_products, resolve_lines
from merchant.checkout_page import render_checkout_page
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


class OfferRequest(BaseModel):
    """A web find, on its way to becoming a merchant offer + quote.

    `price_paise` and `qty` are `StrictInt`: pydantic v2's strict-mode int
    coercion rejects bool and float at the request boundary (422) rather than
    silently truncating `1999.0` -> `1999` or accepting `True` as `1` -- the
    same "no floats touch a monetary value" discipline `offers.create_offer`
    and `merchant/quote.py` enforce with `type(x) is int`, just applied one
    layer earlier so a malformed request never even reaches that code.
    """

    title: str
    url: str | None = None
    price_paise: StrictInt
    category: str | None = None
    source: str = "external"
    qty: StrictInt = 1


# --- merchant agent-org request bodies (surfaces #1-#6) ----------------------


class StorefrontRequest(BaseModel):
    message: str
    context: str = ""


class SemanticSearchRequest(BaseModel):
    query: str
    intent: dict | None = None
    limit: int = 10


class UpsellRequest(BaseModel):
    cart: list[dict]
    intent: dict


class NegotiateRequest(BaseModel):
    buyer_cart: list[dict]
    buyer_message: str = ""
    intent: dict
    turn: int = 1


class SubstituteRequest(BaseModel):
    sku: str
    reason: str = "no_fit"
    intent: dict


class RefusalExplainRequest(BaseModel):
    reason_code: str
    message: str = ""
    detail: dict = {}


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


# --- offer (relist a web find, then quote it) --------------------------------


@app.post("/offer")
def post_offer(body: OfferRequest) -> dict:
    """Relist a web find as a real Northwind product and quote it in one call.

    This is the seam the canonical flow names as "Merchant Offer/Catalog":
    everything upstream (a buyer's web search) is untrusted reasoning data;
    `offers.create_offer` is where the merchant takes ownership of the price
    and the category, and from there this route reuses the exact same
    resolve_lines -> create_quote -> save_quote -> ledger recipe `/quote`
    uses, so a relisted find is quoted through the identical, already-tested
    path -- no second quoting code path to keep in sync.
    """
    category = body.category or offers.map_to_category(body.title)
    if not category:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "uncategorised_offer",
                "message": "could not map this find to a merchant category",
                "title": body.title,
            },
        )

    try:
        offer = offers.create_offer(
            title=body.title,
            url=body.url,
            price_paise=body.price_paise,
            category=category,
            source=body.source,
        )
    except offers.OfferError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": "offer_rejected", "message": str(exc)},
        ) from exc

    try:
        lines = resolve_lines([{"sku": offer.sku, "qty": body.qty}])
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
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_request", "message": str(exc)},
        ) from exc

    save_quote(quote)
    # NOTE: the spec for this route also asked for an `offer.listed` ledger
    # entry here, before `quote.issued`, for observability. core/ledger.py is
    # FROZEN and its VALID_EVENT_TYPES is a closed frozenset that does not
    # include "offer.listed" -- append() raises UnknownEventType for any
    # event_type outside it. Registering a new event type would mean editing
    # core/ledger.py, which is off-limits for this change. Rather than work
    # around that (e.g. swallowing the exception, or misusing an existing
    # event type), this route only appends the one event the ledger already
    # recognises: quote.issued, identical to what /quote appends. See the
    # PR/task notes for this gap -- a future ledger-schema change is the
    # correct place to add "offer.listed".
    append(
        "quote.issued",
        {
            "quote_id": quote.quote_id,
            "cart_hash": quote.cart_hash,
            "total_paise": quote.total_paise,
            "expires_at": quote.expires_at,
        },
    )
    return {
        **quote.as_dict(),
        "offer": {
            "sku": offer.sku,
            "name": offer.name,
            "category": offer.category,
            "source": offer.source,
            "url": offer.url,
            "unit_paise": offer.unit_paise,
        },
    }


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
    response = {
        "passed": result.passed,
        "reason_code": result.reason_code,
        "message": result.message,
        "detail": result.detail,
        "total_paise": result.total_paise,
        "quote_id": result.quote_id,
        "cart_mandate_id": result.cart_mandate_id,
        "checked_at": result.checked_at,
    }
    if not result.passed:
        return response

    # Gate passed. gate.check() already appended `gate.passed`; the money now
    # moves, so this is where the order is actually created — the Gate never
    # calls Razorpay. `total_paise` is the Gate's OWN re-derived total, never a
    # number the buyer supplied. quote_id is the idempotency key, so a second
    # checkout for the same quote returns the same order rather than a new one.
    try:
        order = gateway.create_order(
            result.quote_id,
            result.total_paise,
            notes={"quote_id": result.quote_id, "cart_mandate_id": result.cart_mandate_id},
        )
    except gateway.GatewayError as exc:
        # The gate approved, but order creation failed (a decline, a network
        # error, an unconfirmed amount). Still HTTP 200 — the caller branches
        # on the body, same contract as a refusal — with the failure surfaced.
        response["order_error"] = str(exc)
        return response

    # Order-first, then payment.attempted — and only when the order was
    # genuinely just created (not an idempotent cache hit for a re-quoted same
    # quote_id), so the ledger stays one order.created per real order.
    if not order.from_cache:
        append(
            "order.created",
            {"order_id": order.order_id, "quote_id": order.quote_id, "total_paise": order.amount_paise},
        )
        append(
            "payment.attempted",
            {"quote_id": order.quote_id, "razorpay_order_id": order.order_id},
        )
    response["order_id"] = order.order_id
    response["pay_url"] = f"/pay/{order.order_id}"
    return response


# --- pay (the one human step) ------------------------------------------------


@app.get("/pay/{order_id}", response_class=HTMLResponse)
def pay_page(order_id: str) -> str:
    """Serve the Razorpay Standard Checkout page for an order this merchant
    created. This is the single human step in the flow: someone opens it once
    and completes a real test-mode card payment, which fires the webhook.

    `find_by_order_id` returns None for a pending reservation or an unknown id,
    so a checkout page is only ever served for a real, created order.
    """
    order = gateway.find_by_order_id(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail={"error": "unknown_order", "order_id": order_id})
    return render_checkout_page(
        key_id=config.RAZORPAY_KEY_ID,
        order_id=order.order_id,
        amount_paise=order.amount_paise,
        currency=order.currency,
        merchant_name="Northwind",
    )


# --- webhook -----------------------------------------------------------------


def _success_already_logged(razorpay_payment_id: str | None) -> bool:
    """True if the ledger already holds a payment.succeeded for this payment.

    Razorpay announces one successful payment with *two* distinct events --
    `payment.captured` and `order.paid`. They carry different bytes, so neither
    is a byte-identical redelivery of the other, and webhooks.py (which dedupes
    on the SHA-256 of the raw body) correctly lets both through as first
    deliveries. Both map to state captured/paid, so without this guard each one
    appends its own payment.succeeded -- two success rows for one payment.

    The money-truth identity of a payment is its razorpay_payment_id, so that
    is the dedupe key here: one captured payment -> at most one
    payment.succeeded, however many events announce it. A None payment_id can't
    be deduped and is left to append (a success event always carries a payment
    entity, so this is defensive, not expected). payment.failed needs no such
    guard: only one event type maps to "failed", and each failed attempt has
    its own payment_id, so distinct failures are genuinely distinct rows.

    This is a full ledger scan -- fine at demo scale (see all_entries' own
    note) and honest about correctness over speed. It relies on the /webhook
    handler being single-writer: the scan and the append below run with no
    `await` between them, so under a single uvicorn worker two concurrent
    deliveries serialise rather than interleave. A multi-process deployment
    would need this dedupe enforced in the store, not by a read-then-write.
    """
    if razorpay_payment_id is None:
        return False
    for entry in all_entries():
        if (
            entry.event_type == "payment.succeeded"
            and entry.payload.get("razorpay_payment_id") == razorpay_payment_id
        ):
            return True
    return False


@app.post("/webhook")
async def post_webhook(request: Request) -> dict:
    """Razorpay's server-to-server callback. Verifies the HMAC over the exact
    raw bytes (never a re-serialised copy), is idempotent against redelivery,
    and appends the payment outcome to the ledger.

    `webhook.received` is logged BEFORE verification — a forged or malformed
    delivery that fails the signature check is exactly what the audit log
    should prove arrived. The event type logged there is untrusted best-effort
    metadata; nothing about money is trusted from the payload (see webhooks.py).
    """
    raw_body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")
    event_id = request.headers.get("X-Razorpay-Event-Id")

    claimed_type = None
    try:
        parsed = json.loads(raw_body)
        claimed_type = parsed.get("event") if isinstance(parsed, dict) else None
    except ValueError:
        pass
    append("webhook.received", {"event_id": event_id, "razorpay_event_type": claimed_type})

    try:
        result = webhooks.handle_webhook(raw_body, signature)
    except webhooks.MissingWebhookSecretError as exc:
        raise HTTPException(
            status_code=500, detail={"error": "webhook_secret_unset", "message": str(exc)}
        ) from exc
    except webhooks.InvalidSignatureError as exc:
        raise HTTPException(status_code=400, detail={"error": "invalid_signature"}) from exc
    except webhooks.UnsupportedEventError:
        # An event type we don't act on. Acknowledge with 200 so Razorpay
        # stops redelivering it; nothing to record beyond webhook.received.
        return {"status": "ignored"}
    except webhooks.WebhookError as exc:
        raise HTTPException(
            status_code=400, detail={"error": "webhook_rejected", "message": str(exc)}
        ) from exc

    # Only log a money outcome on FIRST delivery. Razorpay redelivers a webhook
    # it didn't get a 200 for, so `result["replay"]` guards against a second
    # payment.succeeded/failed for the same delivery.
    if not result["replay"]:
        if result["state"] in ("captured", "paid"):
            # replay=False only means "not a byte-identical redelivery". The two
            # distinct events for one payment (payment.captured + order.paid)
            # both reach here as first deliveries, so dedupe on the payment id
            # to keep it one payment.succeeded per real payment.
            if not _success_already_logged(result["payment_id"]):
                append(
                    "payment.succeeded",
                    {
                        "quote_id": result["quote_id"],
                        "razorpay_payment_id": result["payment_id"],
                        "amount_paise": result["amount_paise"],
                    },
                )
        elif result["state"] == "failed":
            append(
                "payment.failed",
                {
                    "quote_id": result["quote_id"],
                    "razorpay_payment_id": result["payment_id"],
                    "reason": result["event"],
                },
            )

    return {
        "status": "ok",
        "state": result["state"],
        "replay": result["replay"],
        "quote_id": result["quote_id"],
    }


# --- merchant agent org (surfaces #1-#6) -------------------------------------
# Thin adapters over the LLM agent modules in merchant/agents/. These are
# advisory surfaces OFF the money path: none of them signs, quotes, computes an
# authoritative total, or reaches Razorpay — the buyer still has to go through
# POST /quote and POST /checkout (the Gate) to actually transact. So, like the
# Gate itself, they never raise on a bad/agent-failed result; each returns HTTP
# 200 with a safe fallback the caller can branch on, so a live negotiation loop
# or a storefront chat never dead-ends on a transient model hiccup. This is also
# the surface a Phase 7 MCP server will expose to any external MCP client.


@app.post("/storefront")
def post_storefront(body: StorefrontRequest) -> dict:
    """#1 Storefront — conversational front door. `reply` never raises (it has
    its own deterministic fallback), so this is a straight pass-through."""
    return {"reply": storefront.reply(buyer_message=body.message, context=body.context)}


@app.post("/catalog/semantic_search")
def post_semantic_search(body: SemanticSearchRequest) -> dict:
    """#2 Catalog — semantic search over the merchant's own inventory."""
    try:
        products = catalog_agent.search(query=body.query, intent=body.intent, limit=body.limit)
    except Exception:  # noqa: BLE001 - advisory surface: never 500 on a model hiccup
        products = []
    return {"products": products}


@app.post("/sales/upsell")
def post_upsell(body: UpsellRequest) -> dict:
    """#3 Sales — upsell/cross-sell. Deliberately NOT budget-limited here; the
    Gate is what refuses an over-ceiling cart, even the merchant's own."""
    try:
        result = sales.upsell(cart=body.cart, intent=body.intent)
    except Exception:  # noqa: BLE001
        result = {"add": [], "pitch": ""}
    return result


@app.post("/negotiate")
def post_negotiate(body: NegotiateRequest) -> dict:
    """#4 Merchant Negotiator — one counter-turn. On any failure returns a
    safe `hold` (the buyer's own cart, unchanged) so the bounded negotiation
    loop on the buyer side always makes progress rather than erroring out."""
    try:
        return merchant_negotiator.counter(
            buyer_cart=body.buyer_cart,
            buyer_message=body.buyer_message,
            intent=body.intent,
            turn=body.turn,
        )
    except Exception:  # noqa: BLE001
        return {"action": "hold", "cart": body.buyer_cart, "message": "Holding at the current offer."}


@app.post("/substitute")
def post_substitute(body: SubstituteRequest) -> dict:
    """#5 Substitution — equivalent alternatives for an unavailable/over-budget SKU."""
    try:
        alternatives = substitution.substitute(sku=body.sku, reason=body.reason, intent=body.intent)
    except Exception:  # noqa: BLE001
        alternatives = []
    return {"alternatives": alternatives}


@app.post("/refusal/explain")
def post_refusal_explain(body: RefusalExplainRequest) -> dict:
    """#6 Refusal Explainer — a Gate refusal -> plain English + a fix. `explain`
    already never raises (deterministic template fallback), so this passes through."""
    return refusal_explainer.explain(
        reason_code=body.reason_code, message=body.message, detail=body.detail
    )


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
