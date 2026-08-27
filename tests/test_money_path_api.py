"""Tests for the Phase 3 money-path wiring in merchant/api.py.

Covers /checkout's order-creation + ledger sequencing, /pay/{order_id}, and
/webhook (signature verification, replay defence, captured/failed outcomes).

Isolation follows tests/test_api.py exactly: every sqlite store the app
touches is monkeypatched to a per-test tmp_path, and config.USE_FAKE_GATEWAY
is forced True so /checkout only ever talks to gateway.FakeGateway, never
real Razorpay. Two extra module-level attributes need patching beyond what
test_api.py does, because gateway.py and webhooks.py each bind their db path
constant at import time (`ORDERS_DB = config.ORDERS_DB`,
`WEBHOOK_EVENTS_DB = config.WEBHOOK_EVENTS_DB`) rather than reading
config.*_DB fresh on every call the way quote_store/intent_store/gate/ledger
do -- so patching config.ORDERS_DB alone would not be picked up.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

import config
from merchant import gateway, webhooks
from merchant.api import app

from tests.test_api import _grant_intent_and_sign_cart, _issue_quote

WEBHOOK_SECRET = "test_webhook_secret"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "QUOTES_DB", tmp_path / "quotes.db")
    monkeypatch.setattr(config, "INTENTS_DB", tmp_path / "intents.db")
    monkeypatch.setattr(config, "GATE_NONCES_DB", tmp_path / "gate_nonces.db")
    monkeypatch.setattr(config, "LEDGER_DB", tmp_path / "ledger.db")
    monkeypatch.setattr(config, "USE_FAKE_GATEWAY", True)
    monkeypatch.setattr(config, "RAZORPAY_WEBHOOK_SECRET", WEBHOOK_SECRET)
    # gateway.py and webhooks.py bind these at import time, so config.* alone
    # does not reach them -- patch the module-level attributes directly.
    monkeypatch.setattr(gateway, "ORDERS_DB", tmp_path / "orders.db")
    monkeypatch.setattr(webhooks, "WEBHOOK_EVENTS_DB", tmp_path / "webhook_events.db")
    return TestClient(app)


def _sign_webhook(body: dict) -> tuple[bytes, str]:
    raw = json.dumps(body).encode("utf-8")
    sig = hmac.new(WEBHOOK_SECRET.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    return raw, sig


def _captured_body(order_id: str, amount_paise: int) -> dict:
    return {
        "event": "payment.captured",
        "payload": {
            "payment": {
                "entity": {"id": "pay_TEST", "order_id": order_id, "amount": amount_paise}
            }
        },
    }


def _order_paid_body(order_id: str, amount_paise: int) -> dict:
    """Razorpay's `order.paid`, which announces the SAME successful payment as
    `payment.captured` -- same payment id (pay_TEST) -- but as a different event
    with an order entity alongside the payment entity. Distinct bytes from
    _captured_body, so it is not a redelivery; the ledger must still record it
    as the same one payment.succeeded, not a second."""
    return {
        "event": "order.paid",
        "payload": {
            "order": {"entity": {"id": order_id, "amount": amount_paise}},
            "payment": {
                "entity": {"id": "pay_TEST", "order_id": order_id, "amount": amount_paise}
            },
        },
    }


def _failed_body(order_id: str, amount_paise: int) -> dict:
    return {
        "event": "payment.failed",
        "payload": {
            "payment": {
                "entity": {"id": "pay_FAIL", "order_id": order_id, "amount": amount_paise}
            }
        },
    }


def _checkout(client) -> dict:
    """Issue a quote, grant a matching intent, sign a cart, and check out.

    Returns the checkout response body (a passing checkout, well above the
    quoted total). Caller has quote_data available via the return value's
    quote_id if needed, but most tests only need the checkout body itself.
    """
    quote_data = _issue_quote(client)
    envelope, _intent = _grant_intent_and_sign_cart(
        client, max_paise=10_000_00, quote_data=quote_data
    )
    resp = client.post("/checkout", json={"cart_envelope": envelope})
    assert resp.status_code == 200
    body = resp.json()
    assert body["passed"] is True
    return body


def _ledger_entries(client) -> list[dict]:
    return client.get("/ledger").json()["entries"]


# --- checkout: happy path creates order + logs events in order --------------


def test_checkout_pass_creates_order_and_logs_in_order(client):
    quote_data = _issue_quote(client)
    envelope, _intent = _grant_intent_and_sign_cart(
        client, max_paise=10_000_00, quote_data=quote_data
    )

    resp = client.post("/checkout", json={"cart_envelope": envelope})
    assert resp.status_code == 200
    body = resp.json()
    assert body["passed"] is True
    assert "order_id" in body
    order_id = body["order_id"]
    assert body["pay_url"] == f"/pay/{order_id}"

    entries = _ledger_entries(client)
    by_type = {}
    for e in entries:
        by_type.setdefault(e["event_type"], []).append(e)

    assert "gate.passed" in by_type
    assert "order.created" in by_type
    assert "payment.attempted" in by_type
    assert len(by_type["order.created"]) == 1
    assert len(by_type["payment.attempted"]) == 1

    gate_passed_seq = by_type["gate.passed"][0]["seq"]
    order_created_seq = by_type["order.created"][0]["seq"]
    payment_attempted_seq = by_type["payment.attempted"][0]["seq"]

    assert gate_passed_seq < order_created_seq < payment_attempted_seq

    order_created_payload = by_type["order.created"][0]["payload"]
    assert order_created_payload["order_id"] == order_id
    assert order_created_payload["quote_id"] == quote_data["quote_id"]
    assert order_created_payload["total_paise"] == quote_data["total_paise"]

    payment_attempted_payload = by_type["payment.attempted"][0]["payload"]
    assert payment_attempted_payload["razorpay_order_id"] == order_id
    assert payment_attempted_payload["quote_id"] == quote_data["quote_id"]


# --- checkout: refusal creates no order --------------------------------------


def test_checkout_refusal_creates_no_order(client):
    quote_data = _issue_quote(client)
    # max_paise well below the quoted total -> OVER_LIMIT refusal.
    envelope, _intent = _grant_intent_and_sign_cart(client, max_paise=1000, quote_data=quote_data)

    resp = client.post("/checkout", json={"cart_envelope": envelope})
    assert resp.status_code == 200
    body = resp.json()
    assert body["passed"] is False
    assert "order_id" not in body

    entries = _ledger_entries(client)
    event_types = [e["event_type"] for e in entries]
    assert "gate.refused" in event_types
    assert "order.created" not in event_types
    assert "payment.attempted" not in event_types


# --- checkout: idempotent on quote_id ----------------------------------------


def test_checkout_idempotent_on_quote_id(client):
    quote_data = _issue_quote(client)
    envelope, _intent_payload = _grant_intent_and_sign_cart(
        client, max_paise=10_000_00, quote_data=quote_data
    )

    resp1 = client.post("/checkout", json={"cart_envelope": envelope})
    assert resp1.status_code == 200
    body1 = resp1.json()
    assert body1["passed"] is True
    order_id_1 = body1["order_id"]

    # Build a second, independently-authorized cart mandate for the SAME
    # quote_id (a fresh intent + fresh agent keypair + fresh nonce, exactly
    # what the helper mints each call). quote_id idempotency is meant to
    # handle exactly this: two distinct, independently-valid carts converging
    # on the same quote_id must still resolve to one order.
    envelope2, _intent2 = _grant_intent_and_sign_cart(
        client, max_paise=10_000_00, quote_data=quote_data
    )

    resp2 = client.post("/checkout", json={"cart_envelope": envelope2})
    assert resp2.status_code == 200
    body2 = resp2.json()
    assert body2["passed"] is True
    assert body2["order_id"] == order_id_1

    entries = _ledger_entries(client)
    order_created_entries = [e for e in entries if e["event_type"] == "order.created"]
    assert len(order_created_entries) == 1


# --- pay page -----------------------------------------------------------------


def test_pay_page_renders_for_known_order(client):
    body = _checkout(client)
    order_id = body["order_id"]

    resp = client.get(f"/pay/{order_id}")
    assert resp.status_code == 200
    assert order_id in resp.text
    assert "checkout.razorpay.com/v1/checkout.js" in resp.text


def test_pay_page_404_for_unknown_order(client):
    resp = client.get("/pay/order_does_not_exist")
    assert resp.status_code == 404


# --- webhook: valid captured --------------------------------------------------


def test_webhook_valid_captured_logs_payment_succeeded(client):
    checkout_body = _checkout(client)
    order_id = checkout_body["order_id"]
    amount_paise = checkout_body["total_paise"]

    raw, sig = _sign_webhook(_captured_body(order_id, amount_paise))
    resp = client.post("/webhook", content=raw, headers={"X-Razorpay-Signature": sig})
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "captured"
    assert body["replay"] is False

    entries = _ledger_entries(client)
    event_types = [e["event_type"] for e in entries]
    assert "webhook.received" in event_types
    assert "payment.succeeded" in event_types
    received_seq = next(e["seq"] for e in entries if e["event_type"] == "webhook.received")
    succeeded_entry = next(e for e in entries if e["event_type"] == "payment.succeeded")
    assert received_seq < succeeded_entry["seq"]
    assert succeeded_entry["payload"]["amount_paise"] == amount_paise


# --- webhook: invalid signature -----------------------------------------------


def test_webhook_invalid_signature_logs_arrival_but_not_success(client):
    checkout_body = _checkout(client)
    order_id = checkout_body["order_id"]
    amount_paise = checkout_body["total_paise"]

    raw = json.dumps(_captured_body(order_id, amount_paise)).encode("utf-8")
    resp = client.post(
        "/webhook", content=raw, headers={"X-Razorpay-Signature": "deadbeef" * 8}
    )
    assert resp.status_code == 400

    entries = _ledger_entries(client)
    event_types = [e["event_type"] for e in entries]
    assert "webhook.received" in event_types
    assert "payment.succeeded" not in event_types


# --- webhook: replay -----------------------------------------------------------


def test_webhook_replay_does_not_double_log(client):
    checkout_body = _checkout(client)
    order_id = checkout_body["order_id"]
    amount_paise = checkout_body["total_paise"]

    raw, sig = _sign_webhook(_captured_body(order_id, amount_paise))

    resp1 = client.post("/webhook", content=raw, headers={"X-Razorpay-Signature": sig})
    assert resp1.status_code == 200
    assert resp1.json()["replay"] is False

    resp2 = client.post("/webhook", content=raw, headers={"X-Razorpay-Signature": sig})
    assert resp2.status_code == 200
    assert resp2.json()["replay"] is True

    entries = _ledger_entries(client)
    succeeded_entries = [e for e in entries if e["event_type"] == "payment.succeeded"]
    assert len(succeeded_entries) == 1
    # But the raw arrival is still logged both times.
    received_entries = [e for e in entries if e["event_type"] == "webhook.received"]
    assert len(received_entries) == 2


# --- webhook: two events, one payment -----------------------------------------


def test_captured_then_order_paid_logs_one_success(client):
    """payment.captured and order.paid describe the SAME payment. They are
    different events (not byte-identical), so both reach the money-outcome
    branch as replay=False -- but the ledger must hold exactly one
    payment.succeeded for the single payment, deduped on razorpay_payment_id."""
    checkout_body = _checkout(client)
    order_id = checkout_body["order_id"]
    amount_paise = checkout_body["total_paise"]

    raw_cap, sig_cap = _sign_webhook(_captured_body(order_id, amount_paise))
    r1 = client.post("/webhook", content=raw_cap, headers={"X-Razorpay-Signature": sig_cap})
    assert r1.status_code == 200
    assert r1.json()["replay"] is False

    raw_paid, sig_paid = _sign_webhook(_order_paid_body(order_id, amount_paise))
    r2 = client.post("/webhook", content=raw_paid, headers={"X-Razorpay-Signature": sig_paid})
    assert r2.status_code == 200
    # Not a byte-identical replay -- it is a genuinely distinct event...
    assert r2.json()["replay"] is False
    assert r2.json()["state"] == "paid"

    # ...but it must not produce a second success row for the same payment.
    entries = _ledger_entries(client)
    succeeded = [e for e in entries if e["event_type"] == "payment.succeeded"]
    assert len(succeeded) == 1
    assert succeeded[0]["payload"]["razorpay_payment_id"] == "pay_TEST"


# --- webhook: payment.failed ---------------------------------------------------


def test_webhook_payment_failed_logs_payment_failed(client):
    checkout_body = _checkout(client)
    order_id = checkout_body["order_id"]
    amount_paise = checkout_body["total_paise"]

    raw, sig = _sign_webhook(_failed_body(order_id, amount_paise))
    resp = client.post("/webhook", content=raw, headers={"X-Razorpay-Signature": sig})
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "failed"

    entries = _ledger_entries(client)
    failed_entries = [e for e in entries if e["event_type"] == "payment.failed"]
    assert len(failed_entries) == 1
    payload = failed_entries[0]["payload"]
    assert payload["quote_id"] == checkout_body["quote_id"]
    assert payload["razorpay_payment_id"] == "pay_FAIL"
