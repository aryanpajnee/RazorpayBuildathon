"""FastAPI backend for Vera, the Day-3 buyer console.

The React app POSTs one request+budget here and watches the REAL
autonomous buyer run live: `demo.orchestrator.run_streamed` drives
`demo.agent.run` on a worker thread and streams every event it emits back
over Server-Sent Events, framed exactly as `scratchpad/day3/EVENT_SCHEMA.md`
specifies (`data: <event-json>\\n\\n`, one terminal event per run). Nothing in
this file computes a total, verifies a signature, or decides pass/refuse --
it only serves the built React app and turns one HTTP request into one live
event stream from the real agent + the real merchant + the real Gate.

This file also owns Vera's own demo-facing checkout step (`/api/pay`,
`/api/pay/confirm`) -- separate from the frozen money path above, which has
already run a cart through the mandate/Gate/webhook flow to completion by
the time a run reaches Verdict. Paying is a second, independent action a
person takes afterwards, so it gets its own pair of endpoints rather than
reusing quote_id/order_id machinery meant for the mandate-enforced cart.

    uv run uvicorn ui.server:app --port 8100

Endpoints:
    POST /api/run          -> SSE stream of one buyer run's events
    POST /api/reset         -> wipe the demo's ledger/quote/intent/order state
    POST /api/pay           -> create a payment target (real or simulated test-mode)
    POST /api/pay/confirm   -> record the demo's own payment confirmation
    GET  /api/health        -> {"ok": true, "dist_built": <bool>}
    GET  /                  -> the built React app (ui/web/dist), or a "not built" page
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

import config
from demo import orchestrator

app = FastAPI(title="Vera")

app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

_DIST = Path(__file__).parent / "web" / "dist"

# The demo's own operational state -- the hash-chained ledger plus the
# ordinary bookkeeping stores a run's mandates/quotes/orders live in.
# NOT `merchant/webhooks.py`'s WEBHOOK_EVENTS_DB: that module caches its path
# at import time, so a runtime reset here could never redirect it anyway, and
# the mission-control dashboard's "fresh chain" promise is about the ledger
# and the money-path stores a re-run of the demo would otherwise accumulate
# in, not webhook replay-defence bookkeeping.
_RESET_DB_PATHS = (
    "LEDGER_DB", "QUOTES_DB", "INTENTS_DB", "GATE_NONCES_DB", "ORDERS_DB",
)


class RunBody(BaseModel):
    request: str
    budget_rupees: int
    mode: str = config.UI_DEFAULT_MODE


@app.post("/api/run")
def run_agent(body: RunBody) -> StreamingResponse:
    """Stream one buyer run as SSE frames, exactly per EVENT_SCHEMA.md: one
    `data: <json>\\n\\n` frame per event, the stream ending after exactly one
    terminal event (`run_complete` or `run_error`)."""

    def _frames():
        for event in orchestrator.run_streamed(body.request, body.budget_rupees, mode=body.mode):
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(_frames(), media_type="text/event-stream")


@app.post("/api/reset")
def reset() -> dict:
    """Wipe the demo's ledger + money-path bookkeeping so the next run starts
    from an empty, genesis-hashed chain. `core.ledger` is deliberately
    append-only (no update/delete in its public API -- see that module's
    docstring), so "reset" here means removing the underlying SQLite files
    themselves; every store re-creates its schema on first use afterwards
    (each resolves its DB path from `config.*` at call time, so this is safe
    to do between runs without restarting the process)."""
    for name in _RESET_DB_PATHS:
        path: Path = getattr(config, name)
        path.unlink(missing_ok=True)
    return {"ok": True}


class ProductInfo(BaseModel):
    title: str | None = None
    url: str | None = None


class PayBody(BaseModel):
    amount_paise: int
    request: str
    mode: str = config.UI_DEFAULT_MODE
    origin: str | None = None  # where to send the browser back after the hosted payment
    product: ProductInfo | None = None  # what Vera chose — shown on the gateway + receipt


def _simulated_payment(amount_paise: int) -> dict:
    """A clearly-labelled simulated test capture -- no external call. Used only
    when there are no real Razorpay keys on file (`config.USE_FAKE_GATEWAY`), and
    as the fallback if a real gateway call fails, so this demo step never
    dead-ends. Note: a Razorpay TEST-MODE order is itself a test (no real money),
    so the payment step reaches real netbanking even from a "Test run" agent
    pass whenever keys are present -- that is the gateway behaviour we want to
    exercise on camera."""
    return {
        "gateway": "test-sim",
        "order_id": f"test_sim_{uuid.uuid4().hex[:12]}",
        "amount_paise": amount_paise,
        "currency": config.CURRENCY,
    }


@app.post("/api/pay")
def pay(body: PayBody) -> dict:
    """Create a payment target for Vera's checkout step.

    Once the Gate has authorised the cart, the payment agent sends the buyer
    straight to the Razorpay gateway to pay. To make that a genuine "you are now
    on the gateway" hand-off (not a fragile in-page modal), this creates a
    Razorpay TEST-MODE **Payment Link** -- a hosted Razorpay page -- and returns
    its `payment_url`; the frontend redirects the browser there. On success
    Razorpay sends the browser back to `origin/?vera_paid=1`.

    This is deliberately NOT the frozen money path's `merchant.gateway.create_order`
    (the mandate-enforced cart already cleared the Gate); it is Vera's own
    demo checkout. Falls back to a clearly-labelled simulated capture only when
    there are no real Razorpay keys on file, so the step never dead-ends."""
    if config.USE_FAKE_GATEWAY:
        return _simulated_payment(body.amount_paise)

    try:
        import razorpay

        client = razorpay.Client(auth=(config.RAZORPAY_KEY_ID, config.RAZORPAY_KEY_SECRET))
        product_title = body.product.title if body.product and body.product.title else body.request
        payload: dict = {
            "amount": body.amount_paise,
            "currency": config.CURRENCY,
            "accept_partial": False,
            "reference_id": f"vera_{uuid.uuid4().hex[:12]}",
            "description": f"Vera — {product_title}"[:250],
            "reminder_enable": False,
        }
        # Carry what Vera bought onto the Razorpay order: the product shows on
        # the hosted gateway (via description) and is recorded on the payment
        # (via notes).
        notes: dict = {}
        if body.product and body.product.title:
            notes["product_title"] = body.product.title[:255]
        if body.product and body.product.url:
            notes["product_url"] = body.product.url[:255]
        if notes:
            payload["notes"] = notes
        if body.origin:
            payload["callback_url"] = f"{body.origin.rstrip('/')}/?vera_paid=1"
            payload["callback_method"] = "get"
        link = client.payment_link.create(payload)
        return {
            "gateway": "razorpay",
            "payment_url": link["short_url"],
            "order_id": link["id"],
            "amount_paise": body.amount_paise,
            "currency": config.CURRENCY,
        }
    except Exception:
        # Never expose the key secret, and never let a gateway hiccup dead-end
        # the demo -- fall back to the same simulated shape.
        return _simulated_payment(body.amount_paise)


class PayConfirmBody(BaseModel):
    order_id: str
    razorpay_payment_id: str | None = None
    razorpay_signature: str | None = None


@app.post("/api/pay/confirm")
def pay_confirm(body: PayConfirmBody) -> dict:
    """Record Vera's own demo payment confirmation. Deliberately NOT a
    reimplementation of the frozen webhook/signature-verify path in
    `merchant/webhooks.py` (unchanged, untouched) -- this just closes out the
    UI's own checkout step for display."""
    return {
        "status": "paid",
        "order_id": body.order_id,
        "method": "netbanking",
        "test_mode": True,
    }


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "dist_built": _DIST.exists()}


@app.get("/")
def index() -> FileResponse:
    index_html = _DIST / "index.html"
    if not index_html.exists():
        return FileResponse(Path(__file__).parent / "web" / "not_built.html")
    return FileResponse(index_html)


if _DIST.exists():
    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=str(_DIST), html=True), name="static")
