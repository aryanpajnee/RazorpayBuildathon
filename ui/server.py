"""FastAPI backend for Vera, the Day-3 buyer console.

The React app POSTs one request+budget here and watches the REAL
autonomous buyer run live: `demo.orchestrator.run_streamed` drives
`demo.agent.run` on a worker thread and streams every event it emits back
over Server-Sent Events, framed exactly as `scratchpad/day3/EVENT_SCHEMA.md`
specifies (`data: <event-json>\\n\\n`, one terminal event per run). Nothing in
this file computes a total, verifies a signature, or decides pass/refuse --
it only serves the built React app and turns one HTTP request into one live
event stream from the real agent + the real merchant + the real Gate.

This file also exposes the human checkout step for the *same* order the Gate
authorised. The browser receives only the existing order's public checkout
fields. It cannot name an amount, product, quote, order, or gateway; those are
recovered from the capability-protected run and merchant order stores.

    uv run uvicorn ui.server:app --port 8100

Endpoints:
    POST /api/device/register -> pin a fresh anonymous browser-device key
    POST /api/consent/prepare -> return exact canonical consent bytes to sign
    POST /api/run          -> consume signed consent, then stream one buyer run
    GET  /api/runs/{id}    -> capability-protected durable run status
    POST /api/pay           -> return the run's existing Gate-created order
    POST /api/pay/confirm   -> verify signature + fetched Razorpay payment
    POST /api/pay/simulate  -> settle an explicitly simulated order only
    GET  /api/pay/{run_id}  -> owner-protected verified payment status
    GET  /api/health        -> {"ok": true, "dist_built": <bool>}
    GET  /                  -> the built React app (ui/web/dist), or a "not built" page
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator

import config
from demo import orchestrator
from demo.intent import consent_category
from merchant.user_registry import DeviceCredential, UserRegistry
from ui import payments
from ui.consent import ConsentError, ConsentStore
from ui.payments import PaymentService
from ui.run_store import RunRecord, RunStore

app = FastAPI(title="Vera")

app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

_DIST = Path(__file__).parent / "web" / "dist"
RUN_STORE = RunStore()
PAYMENT_SERVICE = PaymentService()
USER_REGISTRY = UserRegistry()
CONSENT_STORE = ConsentStore()


class DeviceRegistrationBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    public_key: Annotated[
        StrictStr, Field(min_length=64, max_length=64, pattern=r"^[0-9a-fA-F]{64}$")
    ]


class DeviceRegistrationResponse(BaseModel):
    user_id: str
    device_token: str
    public_key: str
    expires_at: int


def _device_credential(
    authorization: Annotated[str | None, Header()] = None,
) -> DeviceCredential:
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Bearer device token required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization.removeprefix("Bearer ")
    if not token or " " in token:
        raise HTTPException(
            status_code=401,
            detail="Bearer device token required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    credential = USER_REGISTRY.authenticate(token)
    if credential is None:
        raise HTTPException(
            status_code=401,
            detail="device token is invalid or expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return credential


DeviceToken = Annotated[DeviceCredential, Depends(_device_credential)]


@app.post("/api/device/register", response_model=DeviceRegistrationResponse)
def register_device(body: DeviceRegistrationBody) -> DeviceRegistrationResponse:
    credential = USER_REGISTRY.register_anonymous(body.public_key)
    return DeviceRegistrationResponse(
        user_id=credential.user_id,
        device_token=credential.device_token,
        public_key=credential.public_key,
        expires_at=credential.expires_at,
    )


class ConsentPrepareBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    request: Annotated[
        StrictStr, Field(min_length=1, max_length=config.CONSENT_MAX_REQUEST_CHARS)
    ]
    budget_rupees: Annotated[
        StrictInt, Field(gt=0, le=config.UI_MAX_BUDGET_RUPEES)
    ]
    mode: Literal["offline", "live"] = config.UI_DEFAULT_MODE

    @field_validator("request")
    @classmethod
    def _request_must_contain_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("request must contain text")
        return value


class ConsentPrepareResponse(BaseModel):
    consent_id: str
    payload: dict
    canonical_payload: str
    expires_at: int


@app.post("/api/consent/prepare", response_model=ConsentPrepareResponse)
def prepare_consent(
    body: ConsentPrepareBody,
    credential: DeviceToken,
) -> ConsentPrepareResponse:
    category = (
        config.CONSENT_OFFLINE_CATEGORY
        if body.mode == "offline"
        else consent_category(body.request)
    )
    prepared = CONSENT_STORE.prepare(
        credential,
        request=body.request,
        budget_paise=body.budget_rupees * config.PAISE_PER_RUPEE,
        category=category,
        mode=body.mode,
    )
    return ConsentPrepareResponse(
        consent_id=prepared.consent_id,
        payload=prepared.payload,
        canonical_payload=prepared.canonical_payload,
        expires_at=prepared.expires_at,
    )


class RunBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    request: Annotated[
        StrictStr, Field(min_length=1, max_length=config.CONSENT_MAX_REQUEST_CHARS)
    ]
    budget_rupees: Annotated[
        StrictInt, Field(gt=0, le=config.UI_MAX_BUDGET_RUPEES)
    ]
    mode: Literal["offline", "live"] = config.UI_DEFAULT_MODE
    consent_id: Annotated[StrictStr, Field(pattern=r"^consent_[0-9a-f]{32}$")]
    consent: "SignedConsent"

    @field_validator("request")
    @classmethod
    def _request_must_contain_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("request must contain text")
        return value


class SignedConsent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    payload: dict
    signature: Annotated[
        StrictStr, Field(min_length=128, max_length=128, pattern=r"^[0-9a-fA-F]{128}$")
    ]
    public_key: Annotated[
        StrictStr, Field(min_length=64, max_length=64, pattern=r"^[0-9a-fA-F]{64}$")
    ]
    alg: Literal["Ed25519"]


@app.post("/api/run")
def run_agent(body: RunBody, credential: DeviceToken) -> StreamingResponse:
    """Stream one buyer run as SSE frames, exactly per EVENT_SCHEMA.md: one
    `data: <json>\\n\\n` frame per event, the stream ending after exactly one
    terminal event (`run_complete` or `run_error`)."""

    try:
        context = CONSENT_STORE.consume(
            credential,
            consent_id=body.consent_id,
            envelope=body.consent.model_dump(),
            request=body.request,
            budget_paise=body.budget_rupees * config.PAISE_PER_RUPEE,
            mode=body.mode,
        )
    except ConsentError as exc:
        status_code = {
            "consent_not_found": 404,
            "consent_replayed": 409,
            "consent_expired": 410,
        }.get(exc.code, 400)
        raise HTTPException(
            status_code=status_code,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc

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
            context=context,
        ):
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(_frames(), media_type="text/event-stream")


def _bearer_token(
    authorization: Annotated[str | None, Header()] = None,
) -> str:
    if authorization is None:
        raise HTTPException(
            status_code=401,
            detail="Bearer run token required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    scheme, separator, run_token = authorization.partition(" ")
    malformed = (
        separator != " "
        or scheme.lower() != "bearer"
        or not run_token
        or run_token.strip() != run_token
        or any(character.isspace() for character in run_token)
    )
    if malformed:
        raise HTTPException(
            status_code=401,
            detail="Bearer run token required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return run_token


RunToken = Annotated[str, Depends(_bearer_token)]


def _owned_run(run_id: str, run_token: str) -> RunRecord:
    record = RUN_STORE.get_owned(run_id, run_token)
    if record is None:
        raise HTTPException(status_code=404, detail="run not found")
    return record


@app.get("/api/runs/{run_id}")
def get_run(run_id: str, run_token: RunToken) -> dict:
    return _owned_run(run_id, run_token).as_dict()


class PayBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    run_id: Annotated[StrictStr, Field(min_length=1, max_length=128)]


class PayConfirmBody(PayBody):
    razorpay_payment_id: Annotated[StrictStr, Field(min_length=1, max_length=128)]
    razorpay_signature: Annotated[StrictStr, Field(min_length=1, max_length=128)]


class CheckoutResponse(BaseModel):
    gateway: Literal["test-sim", "razorpay"]
    order_id: str
    amount_paise: StrictInt
    currency: str
    key_id: str | None = None


class PaymentStatusResponse(BaseModel):
    status: Literal["pending", "partially_captured", "paid", "failed"]
    gateway: Literal["test-sim", "razorpay"]
    order_id: str
    payment_id: str | None
    amount_paise: StrictInt
    captured_amount_paise: StrictInt
    currency: str
    reconciled: bool
    test_mode: bool


def _payment_http_error(exc: payments.PaymentError) -> HTTPException:
    """Map named, sanitised payment failures to stable HTTP semantics."""
    if isinstance(exc, payments.InvalidCheckoutSignatureError):
        return HTTPException(status_code=400, detail="invalid Razorpay checkout signature")
    if isinstance(exc, payments.PaymentVerificationError):
        return HTTPException(status_code=400, detail="Razorpay payment did not match the order")
    if isinstance(exc, payments.PaymentLookupError):
        return HTTPException(status_code=502, detail="Razorpay payment verification unavailable")
    if isinstance(exc, payments.GatewayConfigurationError):
        return HTTPException(status_code=503, detail="payment gateway is not safely configured")
    if isinstance(
        exc,
        (
            payments.RunNotPayableError,
            payments.PurchaseRecordMismatchError,
            payments.SimulationNotAllowedError,
        ),
    ):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=400, detail="payment request refused")


@app.post("/api/pay", response_model_exclude_none=True)
def pay(body: PayBody, run_token: RunToken) -> CheckoutResponse:
    """Return the existing Gate-created order; this operation creates nothing."""
    record = _owned_run(body.run_id, run_token)
    try:
        return CheckoutResponse(**PAYMENT_SERVICE.checkout(record))
    except payments.PaymentError as exc:
        raise _payment_http_error(exc) from exc


@app.post("/api/pay/confirm")
def pay_confirm(body: PayConfirmBody, run_token: RunToken) -> PaymentStatusResponse:
    record = _owned_run(body.run_id, run_token)
    try:
        return PaymentStatusResponse(
            **PAYMENT_SERVICE.confirm(
                record,
                payment_id=body.razorpay_payment_id,
                signature=body.razorpay_signature,
            )
        )
    except payments.PaymentError as exc:
        raise _payment_http_error(exc) from exc


@app.post("/api/pay/simulate")
def pay_simulate(body: PayBody, run_token: RunToken) -> PaymentStatusResponse:
    record = _owned_run(body.run_id, run_token)
    try:
        return PaymentStatusResponse(**PAYMENT_SERVICE.simulate(record))
    except payments.PaymentError as exc:
        raise _payment_http_error(exc) from exc


@app.get("/api/pay/{run_id}")
def pay_status(run_id: str, run_token: RunToken) -> PaymentStatusResponse:
    record = _owned_run(run_id, run_token)
    try:
        return PaymentStatusResponse(**PAYMENT_SERVICE.status(record))
    except payments.PaymentError as exc:
        raise _payment_http_error(exc) from exc


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
