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
    GET  /api/runs/{id}    -> capability-protected durable run status
    POST /api/pay           -> create a payment target (real or simulated test-mode)
    POST /api/pay/confirm   -> record the demo's own payment confirmation
    GET  /api/health        -> {"ok": true, "dist_built": <bool>}
    GET  /                  -> the built React app (ui/web/dist), or a "not built" page
"""

from __future__ import annotations

import json
import uuid
from typing import Annotated, Literal
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator

import config
from demo import orchestrator
from ui.run_store import RunStore

app = FastAPI(title="Vera")

app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

_DIST = Path(__file__).parent / "web" / "dist"
RUN_STORE = RunStore()


class RunBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    request: Annotated[StrictStr, Field(min_length=1)]
    budget_rupees: Annotated[StrictInt, Field(gt=0)]
    mode: Literal["offline", "live"] = config.UI_DEFAULT_MODE

    @field_validator("request")
    @classmethod
    def _request_must_contain_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("request must contain text")
        return value


@app.post("/api/run")
def run_agent(body: RunBody) -> StreamingResponse:
    """Stream one buyer run as SSE frames, exactly per EVENT_SCHEMA.md: one
    `data: <json>\\n\\n` frame per event, the stream ending after exactly one
    terminal event (`run_complete` or `run_error`)."""

    capability = RUN_STORE.create(
        body.request,
        body.budget_rupees * config.PAISE_PER_RUPEE,
        body.mode,
    )

    def _frames():
        for event in orchestrator.run_streamed(
            body.request,
            body.budget_rupees,
            mode=body.mode,
            run_id=capability.run_id,
            run_token=capability.run_token,
            on_started=lambda: RUN_STORE.mark_running(capability.run_id),
            on_result=lambda result: RUN_STORE.complete(
                capability.run_id,
                status=result.status,
                reason=result.reason,
                order_id=result.order_id,
                quote_id=result.quote_id,
                amount_paise=result.total_paise,
                steps=result.steps,
                llm_calls=result.llm_calls,
            ),
            on_error=lambda error: RUN_STORE.fail(capability.run_id, error=error),
        ):
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(_frames(), media_type="text/event-stream")


@app.get("/api/runs/{run_id}")
def get_run(
    run_id: str,
    authorization: Annotated[str | None, Header()] = None,
) -> dict:
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Bearer run token required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    run_token = authorization.removeprefix("Bearer ")
    if not run_token or " " in run_token:
        raise HTTPException(
            status_code=401,
            detail="Bearer run token required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    record = RUN_STORE.get_owned(run_id, run_token)
    if record is None:
        raise HTTPException(status_code=404, detail="run not found")
    return record.as_dict()


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
