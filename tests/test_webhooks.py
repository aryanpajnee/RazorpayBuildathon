"""Webhook verification and replay handling.

Every HMAC in this file is computed independently, by hand, against a fixed
secret and fixed raw bytes -- never by calling verify_signature and trusting
its own output. Fully offline: no network, no sleeping, no dependence on
whatever RAZORPAY_WEBHOOK_SECRET happens to hold in .env.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3

import pytest

import config
from merchant.gateway import FakeGateway, create_order, find_by_order_id
from merchant.webhooks import (
    AmountMismatchError,
    InvalidSignatureError,
    MalformedPayloadError,
    MissingWebhookSecretError,
    UnknownOrderError,
    UnsupportedEventError,
    WebhookError,
    handle_webhook,
    verify_signature,
)

SECRET = "test_webhook_secret_do_not_use_in_prod"


def sign(body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


@pytest.fixture(autouse=True)
def webhook_secret(monkeypatch):
    monkeypatch.setattr(config, "RAZORPAY_WEBHOOK_SECRET", SECRET)


def payment_captured_body(order_id: str, amount: int, payment_id: str = "pay_test001") -> bytes:
    payload = {
        "entity": "event",
        "event": "payment.captured",
        "contains": ["payment"],
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "order_id": order_id,
                    "amount": amount,
                    "currency": "INR",
                    "status": "captured",
                }
            }
        },
        "created_at": 1234567890,
    }
    return json.dumps(payload).encode("utf-8")


def order_paid_body(order_id: str, amount: int, receipt: str, payment_id: str = "pay_test002") -> bytes:
    payload = {
        "entity": "event",
        "event": "order.paid",
        "contains": ["order", "payment"],
        "payload": {
            "order": {
                "entity": {
                    "id": order_id,
                    "amount": amount,
                    "currency": "INR",
                    "receipt": receipt,
                    "status": "paid",
                }
            },
            "payment": {
                "entity": {
                    "id": payment_id,
                    "order_id": order_id,
                    "amount": amount,
                    "currency": "INR",
                    "status": "captured",
                }
            },
        },
        "created_at": 1234567891,
    }
    return json.dumps(payload).encode("utf-8")


# --- signature verification --------------------------------------------------

def test_verify_signature_accepts_a_correctly_computed_hmac():
    body = b'{"event": "payment.captured"}'
    verify_signature(body, sign(body))  # must not raise


def test_verify_signature_rejects_a_tampered_body():
    body = b'{"event": "payment.captured"}'
    signature = sign(body)
    tampered = b'{"event": "payment.failed"}'
    with pytest.raises(InvalidSignatureError):
        verify_signature(tampered, signature)


def test_verify_signature_rejects_a_signature_computed_with_the_wrong_secret():
    body = b'{"event": "payment.captured"}'
    wrong_signature = sign(body, secret="not_the_real_secret")
    with pytest.raises(InvalidSignatureError):
        verify_signature(body, wrong_signature)


def test_verify_signature_raises_when_the_webhook_secret_is_empty(monkeypatch):
    monkeypatch.setattr(config, "RAZORPAY_WEBHOOK_SECRET", "")
    body = b'{"event": "payment.captured"}'
    with pytest.raises(MissingWebhookSecretError):
        verify_signature(body, sign(body))


def test_reserialising_the_body_before_verifying_breaks_a_legitimate_signature():
    """The bug this module must not have: json.loads then json.dumps does not
    reproduce the original bytes (key order and spacing can drift), so
    verifying the reserialised form rejects a real webhook. This is proven
    directly rather than trusted."""
    original = json.dumps({"b": 2, "a": 1}).encode("utf-8")
    signature = sign(original)

    reserialised = json.dumps(json.loads(original), sort_keys=True).encode("utf-8")
    assert reserialised != original, "test is only meaningful if reserialising changes the bytes"

    verify_signature(original, signature)  # the real bytes verify fine
    with pytest.raises(InvalidSignatureError):
        verify_signature(reserialised, signature)  # the reserialised copy does not


# --- normalisation -------------------------------------------------------

def test_handle_webhook_normalises_a_payment_captured_event(tmp_path):
    orders_db = tmp_path / "orders.db"
    events_db = tmp_path / "events.db"
    order = create_order("quote_w1", 589882, gateway=FakeGateway(), db_path=orders_db)

    body = payment_captured_body(order.order_id, 589882)
    result = handle_webhook(body, sign(body), orders_db_path=orders_db, events_db_path=events_db)

    assert result["event"] == "payment.captured"
    assert result["order_id"] == order.order_id
    assert result["payment_id"] == "pay_test001"
    assert result["quote_id"] == "quote_w1"
    assert result["amount_paise"] == 589882
    assert result["state"] == "captured"
    assert result["replay"] is False


def test_handle_webhook_normalises_order_paid_using_the_order_entity(tmp_path):
    orders_db = tmp_path / "orders.db"
    events_db = tmp_path / "events.db"
    order = create_order("quote_w2", 100000, gateway=FakeGateway(), db_path=orders_db)

    body = order_paid_body(order.order_id, 100000, receipt="quote_w2")
    result = handle_webhook(body, sign(body), orders_db_path=orders_db, events_db_path=events_db)

    assert result["event"] == "order.paid"
    assert result["quote_id"] == "quote_w2"
    assert result["state"] == "paid"


# --- writing the status back to the orders table -----------------------

def test_handle_webhook_updates_the_orders_status_after_a_captured_event(tmp_path):
    """Downstream consumers (the ledger, the terminal UI, a recovery agent)
    read order status from gateway.py's orders table, not from
    webhook_events. If this never gets written, they will conclude payment
    never happened and could re-attempt a purchase already paid for."""
    orders_db = tmp_path / "orders.db"
    events_db = tmp_path / "events.db"
    order = create_order("quote_w8", 250000, gateway=FakeGateway(), db_path=orders_db)
    assert order.status == "created"

    body = payment_captured_body(order.order_id, 250000)
    handle_webhook(body, sign(body), orders_db_path=orders_db, events_db_path=events_db)

    reread = find_by_order_id(order.order_id, db_path=orders_db)
    assert reread.status == "captured"


def test_a_replayed_webhook_does_not_call_update_order_status_again(tmp_path, monkeypatch):
    orders_db = tmp_path / "orders.db"
    events_db = tmp_path / "events.db"
    order = create_order("quote_w9", 250000, gateway=FakeGateway(), db_path=orders_db)

    body = payment_captured_body(order.order_id, 250000)
    signature = sign(body)
    handle_webhook(body, signature, orders_db_path=orders_db, events_db_path=events_db)

    calls = []
    from merchant import gateway as gateway_module

    original = gateway_module.update_order_status

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(gateway_module, "update_order_status", spy)
    handle_webhook(body, signature, orders_db_path=orders_db, events_db_path=events_db)
    assert calls == [], "a replay must not re-apply the status update"


# --- replay safety ---------------------------------------------------------

def test_the_same_webhook_delivered_twice_is_recognised_as_a_replay(tmp_path):
    orders_db = tmp_path / "orders.db"
    events_db = tmp_path / "events.db"
    order = create_order("quote_w3", 100000, gateway=FakeGateway(), db_path=orders_db)

    body = payment_captured_body(order.order_id, 100000)
    signature = sign(body)

    first = handle_webhook(body, signature, orders_db_path=orders_db, events_db_path=events_db)
    second = handle_webhook(body, signature, orders_db_path=orders_db, events_db_path=events_db)

    assert first["replay"] is False
    assert second["replay"] is True
    assert second["quote_id"] == first["quote_id"]
    assert second["state"] == first["state"]

    conn = sqlite3.connect(events_db)
    count = conn.execute("SELECT COUNT(*) FROM webhook_events").fetchone()[0]
    conn.close()
    assert count == 1, "a replayed delivery must not create a second event row"


def test_a_non_collision_integrity_error_on_the_event_insert_raises_a_named_error(tmp_path, monkeypatch):
    """Mirrors the gateway.py finding: the code assumes an IntegrityError on
    the event INSERT can only mean 'a concurrent identical delivery already
    won', and re-selects by event_hash expecting to find that winner's row.
    If the IntegrityError came from something else (a NOT NULL violation, a
    corrupted schema), that re-select finds nothing and the naive code would
    crash trying to unpack None. Simulated here by forcing the INSERT itself
    to fail without any row ever being written."""
    orders_db = tmp_path / "orders.db"
    events_db = tmp_path / "events.db"
    order = create_order("quote_w10", 100000, gateway=FakeGateway(), db_path=orders_db)
    body = payment_captured_body(order.order_id, 100000)

    # sqlite3.Connection is a C type and cannot be monkeypatched directly, so
    # a subclass overriding execute() is swapped in via connect()'s factory
    # argument instead. This only ever intercepts the INSERT INTO
    # webhook_events statement -- every other statement (the CREATE TABLE,
    # the replay-check SELECT, and anything gateway.py runs against
    # orders.db) passes straight through to the real implementation.
    class _FlakyConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql.strip().startswith("INSERT INTO webhook_events"):
                raise sqlite3.IntegrityError("simulated non-collision constraint failure")
            return super().execute(sql, *args, **kwargs)

    original_connect = sqlite3.connect

    def flaky_connect(*args, **kwargs):
        kwargs.setdefault("factory", _FlakyConnection)
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", flaky_connect)

    with pytest.raises(WebhookError):
        handle_webhook(body, sign(body), orders_db_path=orders_db, events_db_path=events_db)


def test_two_different_events_for_the_same_order_are_both_recorded(tmp_path):
    """Replay dedup must key on the delivery, not the order -- a genuinely
    new event (payment.captured, then order.paid) for the same order is not
    a replay and must be processed both times."""
    orders_db = tmp_path / "orders.db"
    events_db = tmp_path / "events.db"
    order = create_order("quote_w4", 100000, gateway=FakeGateway(), db_path=orders_db)

    captured_body = payment_captured_body(order.order_id, 100000)
    paid_body = order_paid_body(order.order_id, 100000, receipt="quote_w4")

    first = handle_webhook(captured_body, sign(captured_body), orders_db_path=orders_db, events_db_path=events_db)
    second = handle_webhook(paid_body, sign(paid_body), orders_db_path=orders_db, events_db_path=events_db)

    assert first["replay"] is False
    assert second["replay"] is False
    assert first["state"] == "captured"
    assert second["state"] == "paid"


# --- not trusting the payload ------------------------------------------

def test_a_webhook_for_an_order_this_merchant_never_created_is_rejected(tmp_path):
    orders_db = tmp_path / "orders.db"
    events_db = tmp_path / "events.db"
    body = payment_captured_body("order_never_created", 100000)
    with pytest.raises(UnknownOrderError):
        handle_webhook(body, sign(body), orders_db_path=orders_db, events_db_path=events_db)


def test_a_webhook_amount_that_does_not_match_the_recorded_order_is_rejected(tmp_path):
    """A webhook claiming MORE than the order's recorded amount is the real
    tampering scenario -- a claim of less is a legitimate partial capture
    (see test_a_partial_capture_amount_less_than_the_order_total_is_accepted)
    and must not be confused with it."""
    orders_db = tmp_path / "orders.db"
    events_db = tmp_path / "events.db"
    order = create_order("quote_w5", 500000, gateway=FakeGateway(), db_path=orders_db)

    body = payment_captured_body(order.order_id, 999999)  # buyer/attacker claims more than quoted
    with pytest.raises(AmountMismatchError):
        handle_webhook(body, sign(body), orders_db_path=orders_db, events_db_path=events_db)


def test_a_partial_capture_amount_less_than_the_order_total_is_accepted(tmp_path):
    """Razorpay supports capturing less than the full order amount. A
    partial payment.captured legitimately carries an amount smaller than
    what create_order() recorded, and must not be rejected as tampering --
    only exact equality is required for order.paid, which represents the
    order's full settlement."""
    orders_db = tmp_path / "orders.db"
    events_db = tmp_path / "events.db"
    order = create_order("quote_w11", 500000, gateway=FakeGateway(), db_path=orders_db)

    body = payment_captured_body(order.order_id, 300000)  # partial capture
    result = handle_webhook(body, sign(body), orders_db_path=orders_db, events_db_path=events_db)
    assert result["amount_paise"] == 300000
    assert result["state"] == "captured"


def test_a_zero_captured_amount_is_rejected(tmp_path):
    """<= would let a zero-amount capture through; it must still be > 0."""
    orders_db = tmp_path / "orders.db"
    events_db = tmp_path / "events.db"
    order = create_order("quote_w13", 500000, gateway=FakeGateway(), db_path=orders_db)

    body = payment_captured_body(order.order_id, 0)
    with pytest.raises(AmountMismatchError):
        handle_webhook(body, sign(body), orders_db_path=orders_db, events_db_path=events_db)


def test_order_paid_still_requires_exact_amount_equality(tmp_path):
    """order.paid represents full settlement of the order, so unlike
    payment.captured it must still demand an exact match -- a 'partial'
    order.paid would be a contradiction in terms."""
    orders_db = tmp_path / "orders.db"
    events_db = tmp_path / "events.db"
    order = create_order("quote_w14", 500000, gateway=FakeGateway(), db_path=orders_db)

    body = order_paid_body(order.order_id, 300000, receipt="quote_w14")
    with pytest.raises(AmountMismatchError):
        handle_webhook(body, sign(body), orders_db_path=orders_db, events_db_path=events_db)


def test_an_unsupported_event_type_is_rejected(tmp_path):
    orders_db = tmp_path / "orders.db"
    events_db = tmp_path / "events.db"
    payload = {"event": "refund.created", "payload": {}}
    body = json.dumps(payload).encode("utf-8")
    with pytest.raises(UnsupportedEventError):
        handle_webhook(body, sign(body), orders_db_path=orders_db, events_db_path=events_db)


def test_a_payload_missing_the_payload_key_is_rejected(tmp_path):
    orders_db = tmp_path / "orders.db"
    events_db = tmp_path / "events.db"
    payload = {"event": "payment.captured"}
    body = json.dumps(payload).encode("utf-8")
    with pytest.raises(MalformedPayloadError):
        handle_webhook(body, sign(body), orders_db_path=orders_db, events_db_path=events_db)


def test_a_float_amount_in_the_payload_is_rejected(tmp_path):
    orders_db = tmp_path / "orders.db"
    events_db = tmp_path / "events.db"
    order = create_order("quote_w6", 500000, gateway=FakeGateway(), db_path=orders_db)

    payload = {
        "event": "payment.captured",
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_test003",
                    "order_id": order.order_id,
                    "amount": 500000.0,
                    "status": "captured",
                }
            }
        },
    }
    body = json.dumps(payload).encode("utf-8")
    with pytest.raises(MalformedPayloadError):
        handle_webhook(body, sign(body), orders_db_path=orders_db, events_db_path=events_db)


def test_signature_is_verified_before_any_lookup_happens(tmp_path):
    """An invalid signature must be rejected even for a payload that
    references a real order -- verification is not skippable just because
    the payload looks legitimate."""
    orders_db = tmp_path / "orders.db"
    events_db = tmp_path / "events.db"
    order = create_order("quote_w7", 500000, gateway=FakeGateway(), db_path=orders_db)

    body = payment_captured_body(order.order_id, 500000)
    with pytest.raises(InvalidSignatureError):
        handle_webhook(body, "0" * 64, orders_db_path=orders_db, events_db_path=events_db)
