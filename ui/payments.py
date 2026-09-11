"""Capability-bound checkout and payment verification for the Vera UI.

The browser identifies a run and nothing else. Every money-bearing field --
amount, currency, order id, and which gateway may be used at all -- is
recovered from the durable run record and from the merchant order that
``merchant.gateway.create_order`` wrote *after the Gate passed*. Nothing the
browser sends is authority; the request only says "act on the run I can prove
I own".

Three separate proofs stand between "the browser says it paid" and this module
reporting ``paid``:

1. The Razorpay checkout signature -- HMAC-SHA256 over ``order_id|payment_id``
   under the key secret -- proves the payment id was issued by Razorpay for
   *this* order rather than invented or lifted from another order.
2. An authenticated fetch of that payment, using server-held credentials,
   proves what the payment actually is: its order, its amount, its currency,
   its capture state. The signature alone proves origin, not settlement --
   an authorized-but-uncaptured payment carries a perfectly valid signature.
3. ``gateway.update_order_status`` decides what the order becomes, so a stale
   or replayed event can never walk a settled order backwards.

Simulation is a separate operation, allowed only for orders whose recorded
provenance is ``FakeGateway``. A real gateway failure is never a reason to
fall back to it: a failure stays a failure.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import uuid
from pathlib import Path
from typing import Callable

import config
from merchant import gateway
from ui.run_store import RunRecord

# The four public payment states, exactly as the HTTP contract names them.
STATUS_PAID = "paid"
STATUS_PARTIALLY_CAPTURED = "partially_captured"
STATUS_FAILED = "failed"
STATUS_PENDING = "pending"

# Razorpay's own id conventions, not settings: a test-mode key id always
# carries this prefix, and both id fields the browser hands back are opaque
# ASCII tokens. Validating their shape before they reach an HMAC or an f-string
# keeps a hostile value from turning a refusal into a 500.
_TEST_KEY_PREFIX = "rzp_test_"
_PAYMENT_ID_PATTERN = re.compile(r"\A[A-Za-z0-9_]{1,64}\Z")
# A Razorpay checkout signature is a SHA-256 HMAC hexdigest. Checking the shape
# first is not the security control -- compare_digest is -- but hmac's
# compare_digest raises TypeError on a non-ASCII str, so an unchecked value
# would surface as a 500 instead of a clean refusal.
_SIGNATURE_PATTERN = re.compile(r"\A[0-9a-f]{64}\Z")

# Payment states Razorpay can report that this module knows how to reason
# about. Anything else is refused rather than guessed at -- an unrecognised
# state must never be optimistically read as settlement.
_KNOWN_PAYMENT_STATES = frozenset({"created", "authorized", "captured", "failed"})


class PaymentError(RuntimeError):
    """Base for payment requests the server cannot safely satisfy."""


class RunNotPayableError(PaymentError):
    """The run carries no Gate-authorised order to pay."""


class PurchaseRecordMismatchError(PaymentError):
    """The run record and the merchant order disagree. Two stores that should
    describe the same purchase do not, so neither is trusted."""


class GatewayConfigurationError(PaymentError):
    """This order cannot be transacted with the credentials or provenance on
    file -- unknown gateway, missing key, or a key that did not create it."""


class InvalidCheckoutSignatureError(PaymentError):
    """The checkout signature does not authenticate this order and payment."""


class PaymentLookupError(PaymentError):
    """Razorpay could not be asked about the payment. Deliberately distinct
    from a verification failure: "we could not check" is not "we checked and
    it was bad", and it is never a reason to report settlement."""


class PaymentVerificationError(PaymentError):
    """The payment exists but is not what this order needs it to be."""


class SimulationNotAllowedError(PaymentError):
    """Simulation was asked for on a real order, or confirmation on a
    simulated one. The two paths never overlap."""


def _require_ordered_run(record: RunRecord) -> None:
    """A run may be paid only in the one state that means "the Gate authorised
    a purchase and the merchant created an order for it"."""
    if record.status != "ordered":
        raise RunNotPayableError(
            f"run status {record.status!r} has no Gate-authorised order to pay"
        )
    if (
        not record.order_id
        or not record.quote_id
        or type(record.amount_paise) is not int
        or record.amount_paise <= 0
    ):
        raise RunNotPayableError("ordered run has incomplete payment authority")


def _public_status(order: gateway.Order, *, reconciled: bool) -> dict:
    """Project an order row into the one PaymentStatus shape the API speaks.

    ``paid`` is derived from two facts together -- a full-settlement status
    *and* a captured amount equal to the order total -- never from either
    alone. A row claiming captured while short of the total is a partial
    capture whatever its status column says, and is reported as one.
    """
    fully_captured = (
        order.status in {"captured", "paid"}
        and order.captured_amount_paise == order.amount_paise
    )
    if fully_captured:
        status = STATUS_PAID
    elif order.status == "partially_captured" or order.captured_amount_paise > 0:
        status = STATUS_PARTIALLY_CAPTURED
    elif order.status == "failed":
        status = STATUS_FAILED
    else:
        # created / authorized / anything else: money has not moved.
        status = STATUS_PENDING
    return {
        "status": status,
        "gateway": order.gateway,
        "order_id": order.order_id,
        "payment_id": order.payment_id,
        "amount_paise": order.amount_paise,
        "captured_amount_paise": order.captured_amount_paise,
        "currency": order.currency,
        "reconciled": reconciled,
        # Derived from the order's own provenance rather than asserted: a
        # simulated order is test-mode by construction, and a real one only
        # ever reaches here under a key id this module has already required to
        # be a test key.
        "test_mode": order.gateway == gateway.GATEWAY_TEST_SIM
        or (order.gateway_key_id or "").startswith(_TEST_KEY_PREFIX),
    }


class PaymentService:
    """Resolve and verify payments from merchant and run records only.

    ``razorpay_gateway_factory`` exists so tests can supply a double: nothing
    in this module may make a real network call under test, and constructing
    the real client lazily (per call, only on the razorpay path) also keeps a
    simulated run from ever needing credentials.
    """

    def __init__(
        self,
        *,
        orders_db_path: Path | None = None,
        razorpay_gateway_factory: Callable[[], object] | None = None,
    ) -> None:
        self._orders_db_path = orders_db_path
        self._razorpay_gateway_factory = (
            razorpay_gateway_factory or gateway.RazorpayGateway
        )

    # --- resolving the one order this run may pay --------------------------

    def _owned_order(self, record: RunRecord) -> gateway.Order:
        """The order this run authorises, cross-checked against the run.

        Looked up by the order id the *server* recorded, then required to
        agree with the run on quote, amount and currency. The cross-check is
        not redundant with the lookup: it is what catches a run record and an
        orders row that have drifted apart (a restored database, a half-
        migrated file), where each store alone still looks self-consistent.
        """
        _require_ordered_run(record)
        order = gateway.find_by_order_id(record.order_id, db_path=self._orders_db_path)
        if order is None:
            raise PurchaseRecordMismatchError(
                "run order is absent from the merchant order store"
            )
        if (
            order.quote_id != record.quote_id
            or order.amount_paise != record.amount_paise
            or order.currency != config.CURRENCY
        ):
            raise PurchaseRecordMismatchError(
                "run and merchant order disagree on quote, amount, or currency"
            )
        if order.gateway not in {gateway.GATEWAY_RAZORPAY, gateway.GATEWAY_TEST_SIM}:
            # A row from before provenance was recorded. There is no safe
            # guess here -- treating it as real would let a signature be
            # checked against a key that never created it, and treating it as
            # simulated would let it be marked paid with no money moved. So it
            # is refused outright, and a new run creates a new order.
            raise GatewayConfigurationError(
                f"order has unrecorded gateway provenance {order.gateway!r}"
            )
        return order

    @staticmethod
    def _require_test_credentials(order: gateway.Order) -> None:
        """Refuse to act unless test-mode credentials that own this order are
        configured. The key-id equality check is the one that matters: keys
        can be rotated or swapped between environments, and a signature
        verified under a *different* account's secret proves nothing about
        this order."""
        if not config.RAZORPAY_KEY_ID or not config.RAZORPAY_KEY_SECRET:
            raise GatewayConfigurationError(
                "Razorpay key id and secret are required for payment verification"
            )
        if not config.RAZORPAY_KEY_ID.startswith(_TEST_KEY_PREFIX):
            raise GatewayConfigurationError(
                "Vera checkout accepts Razorpay test-mode credentials only"
            )
        if not order.gateway_key_id:
            raise GatewayConfigurationError(
                "the order does not record which Razorpay key created it"
            )
        if order.gateway_key_id != config.RAZORPAY_KEY_ID:
            raise GatewayConfigurationError(
                "the configured Razorpay key does not own this order"
            )

    # --- the three operations ---------------------------------------------

    def checkout(self, record: RunRecord) -> dict:
        """Hand the browser what it needs to open checkout for the order that
        already exists. Creates nothing: a second Payment Link or order here
        would be a second way to charge for one Gate decision."""
        order = self._owned_order(record)
        if order.gateway == gateway.GATEWAY_TEST_SIM:
            return {
                "gateway": order.gateway,
                "order_id": order.order_id,
                "amount_paise": order.amount_paise,
                "currency": order.currency,
            }
        self._require_test_credentials(order)
        return {
            "gateway": order.gateway,
            "order_id": order.order_id,
            "amount_paise": order.amount_paise,
            "currency": order.currency,
            # The key that created the order, not whatever config holds now --
            # they are already required to be equal, and sending the recorded
            # one keeps the order and the checkout key provably the same key.
            "key_id": order.gateway_key_id,
        }

    def confirm(self, record: RunRecord, *, payment_id: str, signature: str) -> dict:
        """Verify a Standard Checkout callback and settle the order from it.

        Signature first, fetch second. The order is deliberate: the fetch
        costs a network round trip and spends a real API call, so an
        unauthenticated payment id should never buy one -- and the signature
        is also what binds the payment id to *this* order before anything
        else is asked about it.
        """
        order = self._owned_order(record)
        if order.gateway != gateway.GATEWAY_RAZORPAY:
            raise SimulationNotAllowedError(
                "a simulated order cannot be confirmed with a Razorpay signature"
            )
        self._require_test_credentials(order)

        if not isinstance(payment_id, str) or not _PAYMENT_ID_PATTERN.match(payment_id):
            raise InvalidCheckoutSignatureError("malformed razorpay_payment_id")
        if not isinstance(signature, str) or not _SIGNATURE_PATTERN.match(signature):
            raise InvalidCheckoutSignatureError("malformed razorpay_signature")

        signed = f"{order.order_id}|{payment_id}".encode("utf-8")
        expected = hmac.new(
            config.RAZORPAY_KEY_SECRET.encode("utf-8"), signed, hashlib.sha256
        ).hexdigest()
        # compare_digest, not ==: == short-circuits at the first differing
        # character, and how long that takes leaks how many leading hex digits
        # a forger already has right.
        if not hmac.compare_digest(expected, signature):
            raise InvalidCheckoutSignatureError(
                "Razorpay checkout signature does not match this order and payment"
            )

        payment = self._fetch_verified_payment(order, payment_id)
        return _public_status(self._apply_payment(order, payment), reconciled=True)

    def simulate(self, record: RunRecord) -> dict:
        """Settle an order that no real gateway ever created.

        Restricted to recorded ``test-sim`` provenance, and reached only by
        its own endpoint. It is never a fallback for a failed real payment:
        if this could be used to rescue a razorpay order, "paid" would stop
        meaning "money moved".
        """
        order = self._owned_order(record)
        if order.gateway != gateway.GATEWAY_TEST_SIM:
            raise SimulationNotAllowedError(
                "simulation is allowed only for an order created by the test gateway"
            )
        if (
            order.status in {"captured", "paid"}
            and order.captured_amount_paise == order.amount_paise
        ):
            # Already settled: return the existing state rather than minting a
            # second payment id for the same order.
            return _public_status(order, reconciled=True)
        payment_id = order.payment_id or f"pay_sim_{uuid.uuid4().hex}"
        updated = gateway.update_order_status(
            order.order_id,
            "captured",
            captured_amount_paise=order.amount_paise,
            payment_id=payment_id,
            db_path=self._orders_db_path,
        )
        return _public_status(updated, reconciled=True)

    def status(self, record: RunRecord) -> dict:
        """Current payment state, re-verified against Razorpay when there is
        a payment to re-verify.

        A failed re-check returns the recorded state with ``reconciled:
        false`` instead of raising: a poll that cannot reach Razorpay must not
        erase a settlement this server already verified, nor invent one.
        """
        order = self._owned_order(record)
        if order.gateway != gateway.GATEWAY_RAZORPAY or not order.payment_id:
            return _public_status(order, reconciled=True)
        try:
            payment = self._fetch_verified_payment(order, order.payment_id)
            order = self._apply_payment(order, payment)
        except PaymentError:
            return _public_status(order, reconciled=False)
        return _public_status(order, reconciled=True)

    # --- talking to Razorpay ----------------------------------------------

    def _fetch_verified_payment(self, order: gateway.Order, payment_id: str) -> dict:
        """Fetch one payment with server-held credentials and require it to be
        this order's payment, for this order's money.

        Every field is checked against the merchant's own record, never
        against anything the browser sent. The amount comparison is typed
        (`type(...) is int`): `50000.0 == 50000` is True in Python, so an
        untyped check would accept a float amount as an exact match and let a
        non-integer paise value into the money path.
        """
        self._require_test_credentials(order)
        try:
            client = self._razorpay_gateway_factory()
            payment = client.fetch_payment(payment_id)
        except Exception as exc:
            # The underlying error is not repeated into the message: a
            # gateway client's exception text can carry request context, and
            # this string is destined for an HTTP response body.
            raise PaymentLookupError("Razorpay payment lookup failed") from exc

        if not isinstance(payment, dict):
            raise PaymentVerificationError("Razorpay returned a malformed payment")
        if payment.get("id") != payment_id:
            raise PaymentVerificationError(
                "Razorpay returned a different payment than the one requested"
            )
        if payment.get("order_id") != order.order_id:
            raise PaymentVerificationError(
                "the payment belongs to a different order than this run's"
            )
        if payment.get("currency") != order.currency:
            raise PaymentVerificationError(
                "the payment currency does not match the merchant order"
            )

        amount = payment.get("amount")
        if type(amount) is not int or amount <= 0:
            raise PaymentVerificationError(
                "the payment amount is not a positive int paise value"
            )
        if amount > order.amount_paise:
            # More than this merchant quoted. Refused rather than accepted as
            # a generous overpayment: it means the payment is not for this
            # order's price, and the Gate only ever authorised that price.
            raise PaymentVerificationError(
                "the payment amount exceeds the merchant order amount"
            )

        status = payment.get("status")
        if status not in _KNOWN_PAYMENT_STATES:
            raise PaymentVerificationError(
                f"Razorpay returned unsupported payment status {status!r}"
            )
        if status == "captured" and payment.get("captured") is False:
            # The entity contradicts itself. Nothing here is worth guessing at
            # when the guess would be "the money is ours".
            raise PaymentVerificationError(
                "Razorpay payment claims captured status but reports captured=false"
            )
        return payment

    def _apply_payment(self, order: gateway.Order, payment: dict) -> gateway.Order:
        """Record a verified payment against the order.

        The captured amount is passed through explicitly rather than defaulted
        so that a capture short of the order total is stored as a partial one.
        gateway.update_order_status makes the final call about what the order
        becomes -- this function never decides that a settled order is now
        unsettled.
        """
        status = payment["status"]
        if status == "captured":
            return gateway.update_order_status(
                order.order_id,
                "captured",
                captured_amount_paise=payment["amount"],
                payment_id=payment["id"],
                db_path=self._orders_db_path,
            )
        if status in {"authorized", "failed"}:
            return gateway.update_order_status(
                order.order_id,
                status,
                payment_id=payment["id"],
                db_path=self._orders_db_path,
            )
        # status == "created": Razorpay has the payment on file but nothing
        # has happened to it. Nothing to record.
        return order
