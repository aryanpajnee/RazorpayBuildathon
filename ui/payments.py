"""Capability-bound checkout and payment verification for the Vera UI.

The browser identifies only a run. Every money-bearing field is recovered
from the durable run and the merchant order created after the Gate passed.
Razorpay checkout signatures prove the returned payment belongs to that
order; the authenticated payment fetch then proves its amount, currency, and
capture state. Simulation is a separate operation restricted to orders that
were explicitly created by ``FakeGateway``.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from pathlib import Path
from typing import Callable

import config
from merchant import gateway
from ui.run_store import RunRecord


class PaymentError(RuntimeError):
    """Base for payment requests the server cannot safely satisfy."""


class RunNotPayableError(PaymentError):
    pass


class PurchaseRecordMismatchError(PaymentError):
    pass


class GatewayConfigurationError(PaymentError):
    pass


class InvalidCheckoutSignatureError(PaymentError):
    pass


class PaymentLookupError(PaymentError):
    pass


class PaymentVerificationError(PaymentError):
    pass


class SimulationNotAllowedError(PaymentError):
    pass


def _require_ordered_run(record: RunRecord) -> None:
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
    fully_captured = (
        order.status in {"captured", "paid"}
        and order.captured_amount_paise == order.amount_paise
    )
    if fully_captured:
        status = "paid"
    elif order.status == "partially_captured" or order.captured_amount_paise > 0:
        status = "partially_captured"
    elif order.status == "failed":
        status = "failed"
    else:
        status = "pending"
    return {
        "status": status,
        "gateway": order.gateway,
        "order_id": order.order_id,
        "payment_id": order.payment_id,
        "amount_paise": order.amount_paise,
        "captured_amount_paise": order.captured_amount_paise,
        "currency": order.currency,
        "reconciled": reconciled,
        "test_mode": True,
    }


class PaymentService:
    """Resolve and verify payments using merchant and run records only."""

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

    def _owned_order(self, record: RunRecord) -> gateway.Order:
        _require_ordered_run(record)
        order = gateway.find_by_order_id(
            record.order_id, db_path=self._orders_db_path
        )
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
        return order

    @staticmethod
    def _require_test_credentials(order: gateway.Order) -> None:
        if not config.RAZORPAY_KEY_ID or not config.RAZORPAY_KEY_SECRET:
            raise GatewayConfigurationError(
                "Razorpay key id and secret are required for payment verification"
            )
        if not config.RAZORPAY_KEY_ID.startswith("rzp_test_"):
            raise GatewayConfigurationError(
                "Vera checkout accepts Razorpay test-mode credentials only"
            )
        if not order.gateway_key_id:
            raise GatewayConfigurationError(
                "the order does not record which Razorpay key created it"
            )
        if not hmac.compare_digest(order.gateway_key_id, config.RAZORPAY_KEY_ID):
            raise GatewayConfigurationError(
                "the configured Razorpay key does not own this order"
            )

    def checkout(self, record: RunRecord) -> dict:
        order = self._owned_order(record)
        if order.gateway == "test-sim":
            return {
                "gateway": "test-sim",
                "order_id": order.order_id,
                "amount_paise": order.amount_paise,
                "currency": order.currency,
            }
        if order.gateway != "razorpay":
            raise GatewayConfigurationError(
                f"order has unsupported gateway provenance {order.gateway!r}"
            )
        self._require_test_credentials(order)
        return {
            "gateway": "razorpay",
            "order_id": order.order_id,
            "amount_paise": order.amount_paise,
            "currency": order.currency,
            "key_id": order.gateway_key_id,
        }

    def _fetch_verified_payment(
        self, order: gateway.Order, payment_id: str
    ) -> tuple[dict, object]:
        self._require_test_credentials(order)
        try:
            client = self._razorpay_gateway_factory()
            payment = client.fetch_payment(payment_id)
        except Exception as exc:
            raise PaymentLookupError("Razorpay payment lookup failed") from exc
        if not isinstance(payment, dict):
            raise PaymentVerificationError("Razorpay returned a malformed payment")

        expected = {
            "id": payment_id,
            "order_id": order.order_id,
            "amount": order.amount_paise,
            "currency": order.currency,
        }
        for field, value in expected.items():
            if payment.get(field) != value:
                raise PaymentVerificationError(
                    f"Razorpay payment {field} does not match the merchant order"
                )
        status = payment.get("status")
        if status not in {"created", "authorized", "captured", "failed"}:
            raise PaymentVerificationError(
                f"Razorpay returned unsupported payment status {status!r}"
            )
        return payment, client

    def _apply_payment(self, order: gateway.Order, payment: dict) -> gateway.Order:
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
        return order

    def confirm(
        self,
        record: RunRecord,
        *,
        payment_id: str,
        signature: str,
    ) -> dict:
        order = self._owned_order(record)
        if order.gateway != "razorpay":
            raise SimulationNotAllowedError(
                "simulated orders must use the explicit simulation endpoint"
            )
        self._require_test_credentials(order)
        signed = f"{order.order_id}|{payment_id}".encode("utf-8")
        expected = hmac.new(
            config.RAZORPAY_KEY_SECRET.encode("utf-8"), signed, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise InvalidCheckoutSignatureError(
                "Razorpay checkout signature does not match this order and payment"
            )
        payment, _client = self._fetch_verified_payment(order, payment_id)
        updated = self._apply_payment(order, payment)
        return _public_status(updated, reconciled=True)

    def simulate(self, record: RunRecord) -> dict:
        order = self._owned_order(record)
        if order.gateway != "test-sim":
            raise SimulationNotAllowedError(
                "simulation is allowed only for a recorded simulated order"
            )
        if (
            order.status in {"captured", "paid"}
            and order.captured_amount_paise == order.amount_paise
        ):
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
        order = self._owned_order(record)
        if order.gateway != "razorpay" or not order.payment_id:
            return _public_status(order, reconciled=True)
        try:
            payment, _client = self._fetch_verified_payment(order, order.payment_id)
            order = self._apply_payment(order, payment)
        except PaymentError:
            # A transient status check cannot undo a previously verified
            # settlement or make the whole owned-run record unreadable.
            return _public_status(order, reconciled=False)
        return _public_status(order, reconciled=True)
