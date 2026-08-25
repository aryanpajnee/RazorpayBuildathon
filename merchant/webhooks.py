"""Receiving and verifying Razorpay webhooks.

Two rules make this safe against a hostile or merely unreliable network:

1. The signature is verified over the exact bytes Razorpay sent, never a
   re-serialised copy of the parsed JSON. `json.loads` then `json.dumps` does
   not round-trip byte-for-byte -- key order, whitespace and unicode escaping
   can all drift -- so verifying the reserialised form rejects legitimate
   webhooks. `verify_signature` therefore takes `bytes` and only `bytes`;
   there is no code path here that ever reconstructs a body to check.

2. Razorpay redelivers a webhook it didn't get a 200 for, so the same event
   arrives more than once in practice. The dedupe key is the SHA-256 of the
   raw body: a byte-identical redelivery hashes identically and is recognised
   as a replay without re-applying any state change. Two distinct events
   cannot collide with each other this way -- only a byte-identical replay of
   the same delivery can.

Nothing in a webhook payload is trusted for money. order_id is used only to
look up the order this merchant itself created (via gateway.find_by_order_id)
and quote_id always comes from that lookup, never from the payload. The
payload's claimed amount is cross-checked against that same record and
rejected on any mismatch -- the same "nothing external is authoritative about
money" rule gateway.py applies to amount_paise.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from pathlib import Path

import config
from merchant import gateway

WEBHOOK_EVENTS_DB = config.WEBHOOK_EVENTS_DB

_EVENT_STATES = {
    "payment.captured": "captured",
    "payment.failed": "failed",
    "order.paid": "paid",
}

_EVENT_COLUMNS = "event, order_id, payment_id, quote_id, amount_paise, state"


class WebhookError(Exception):
    """Base for anything this module refuses."""


class MissingWebhookSecretError(WebhookError):
    """RAZORPAY_WEBHOOK_SECRET is empty. Verifying against an empty secret
    would make every signature "valid" (an HMAC with an empty key is trivial
    to reproduce), which is worse than refusing outright -- so this fails
    loudly instead of silently accepting forged webhooks."""


class InvalidSignatureError(WebhookError):
    pass


class UnsupportedEventError(WebhookError):
    pass


class MalformedPayloadError(WebhookError):
    pass


class UnknownOrderError(WebhookError):
    """The webhook references an order_id this merchant never created."""


class AmountMismatchError(WebhookError):
    """The webhook's amount does not match the order this merchant created.
    Trusting it would let a tampered or malformed delivery record a state
    change for an amount nobody actually quoted or was charged."""


def verify_signature(raw_body: bytes, signature_header: str) -> None:
    if not config.RAZORPAY_WEBHOOK_SECRET:
        raise MissingWebhookSecretError(
            "RAZORPAY_WEBHOOK_SECRET is empty -- refusing to verify against an "
            "empty secret, which would accept any signature as valid"
        )
    expected = hmac.new(
        config.RAZORPAY_WEBHOOK_SECRET.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    # compare_digest, not ==: a plain == on strings short-circuits at the
    # first mismatched character, and how long that takes leaks how many
    # leading hex digits an attacker already got right. compare_digest runs
    # in time independent of where the strings first differ.
    if not hmac.compare_digest(expected, signature_header or ""):
        raise InvalidSignatureError("webhook signature does not match the computed HMAC")


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS webhook_events (
            event_hash TEXT PRIMARY KEY,
            event TEXT NOT NULL,
            order_id TEXT NOT NULL,
            payment_id TEXT,
            quote_id TEXT NOT NULL,
            amount_paise INTEGER NOT NULL,
            state TEXT NOT NULL
        )
        """
    )
    return conn


def _row_to_result(row: tuple, *, replay: bool) -> dict:
    event, order_id, payment_id, quote_id, amount_paise, state = row
    return {
        "event": event,
        "order_id": order_id,
        "payment_id": payment_id,
        "quote_id": quote_id,
        "amount_paise": amount_paise,
        "state": state,
        "replay": replay,
    }


def _extract(payload: dict, event: str) -> tuple[str, str | None, int]:
    """Pull (order_id, payment_id, amount_paise) out of a parsed payload.

    `order.paid` carries both an order entity and a payment entity;
    `payment.*` events carry only the payment entity, which itself names its
    order_id. The order entity is preferred when present because its amount
    and id are exactly what create_order() recorded.
    """
    try:
        body = payload["payload"]
    except KeyError as exc:
        raise MalformedPayloadError("webhook payload has no 'payload' key") from exc

    order_entity = body.get("order", {}).get("entity")
    payment_entity = body.get("payment", {}).get("entity")

    if order_entity is not None:
        order_id = order_entity.get("id")
        amount = order_entity.get("amount")
    elif payment_entity is not None:
        order_id = payment_entity.get("order_id")
        amount = payment_entity.get("amount")
    else:
        raise MalformedPayloadError(f"{event}: payload has neither an order nor a payment entity")

    payment_id = payment_entity.get("id") if payment_entity else None

    if order_id is None:
        raise MalformedPayloadError(f"{event}: payload has no order id")
    if type(amount) is not int:
        raise MalformedPayloadError(
            f"{event}: amount must be an int paise value, got {type(amount).__name__}"
        )
    return order_id, payment_id, amount


def handle_webhook(
    raw_body: bytes,
    signature_header: str,
    *,
    orders_db_path: Path | None = None,
    events_db_path: Path | None = None,
) -> dict:
    """Verify, parse, and normalise one webhook delivery into a dict describing
    what happened. Safe to call twice with an identical delivery -- the second
    call returns the first call's result with `replay: True` and makes no
    further state change.
    """
    verify_signature(raw_body, signature_header)

    event_hash = hashlib.sha256(raw_body).hexdigest()
    conn = _connect(events_db_path or WEBHOOK_EVENTS_DB)
    try:
        row = conn.execute(
            f"SELECT {_EVENT_COLUMNS} FROM webhook_events WHERE event_hash = ?",
            (event_hash,),
        ).fetchone()
        if row is not None:
            return _row_to_result(row, replay=True)

        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError as exc:
            raise MalformedPayloadError(f"webhook body is not valid JSON: {exc}") from exc

        event = payload.get("event")
        if event not in _EVENT_STATES:
            raise UnsupportedEventError(f"unhandled event type: {event!r}")

        order_id, payment_id, amount_paise = _extract(payload, event)

        order = gateway.find_by_order_id(order_id, db_path=orders_db_path)
        if order is None:
            raise UnknownOrderError(f"webhook references unknown order_id={order_id}")
        # order.paid represents full settlement of the order, so it must
        # match the recorded amount exactly. payment.* events (in practice,
        # payment.captured) can legitimately carry less than the full order
        # amount -- Razorpay supports capturing a partial amount -- so those
        # only need to be no more than what was quoted, and strictly
        # positive (a zero or negative "capture" is never legitimate).
        if event == "order.paid":
            valid = amount_paise == order.amount_paise
        else:
            valid = 0 < amount_paise <= order.amount_paise
        if not valid:
            raise AmountMismatchError(
                f"order {order_id}: webhook amount {amount_paise} is not a valid amount for "
                f"{event} against recorded amount {order.amount_paise}"
            )

        state = _EVENT_STATES[event]

        # Write the order's status before recording the event, not after.
        # gateway.py owns the orders table, so webhooks.py never writes to it
        # directly -- but the *ordering* of these two writes still matters:
        # they live in separate databases, so nothing here can make them
        # atomic together. If this process dies between the two writes, a
        # redelivery of the same event needs to still get the status applied.
        # Doing the status write first means that on a crash-and-retry, the
        # worst case is calling update_order_status twice with the same
        # state (idempotent, harmless). Doing it after the event INSERT
        # would mean the opposite failure mode: the event row commits, the
        # redelivery is then recognised as a replay and -- correctly, per
        # the no-double-apply rule -- never retries the status write, so the
        # order would be stuck at the wrong status forever.
        gateway.update_order_status(order_id, state, db_path=orders_db_path)

        try:
            conn.execute(
                "INSERT INTO webhook_events "
                "(event_hash, event, order_id, payment_id, quote_id, amount_paise, state) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (event_hash, event, order_id, payment_id, order.quote_id, amount_paise, state),
            )
            conn.commit()
        except sqlite3.IntegrityError as exc:
            # Lost a race against a concurrent identical delivery. The row
            # that won is byte-for-byte the event we would have recorded, so
            # returning it as a replay is correct, not just a fallback.
            row = conn.execute(
                f"SELECT {_EVENT_COLUMNS} FROM webhook_events WHERE event_hash = ?",
                (event_hash,),
            ).fetchone()
            if row is None:
                # Not actually a collision on this event_hash -- some other
                # constraint failed and there is no winning row to recover.
                # Name the real problem rather than handing None to
                # _row_to_result and crashing on an opaque unpack error.
                raise WebhookError(
                    f"event_hash={event_hash}: INSERT raised IntegrityError but no row "
                    f"exists for this event_hash -- not a duplicate-delivery collision: {exc}"
                ) from exc
            return _row_to_result(row, replay=True)

        return _row_to_result(
            (event, order_id, payment_id, order.quote_id, amount_paise, state), replay=False
        )
    finally:
        conn.close()
