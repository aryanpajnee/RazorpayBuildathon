"""HTTP contract for capability-bound payment.

The app is wired to temporary run and order databases and an injected Razorpay
double. No test reads credentials from the environment or reaches the network.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

import config
from merchant import gateway
from ui import server
from ui.payments import PaymentService
from ui.run_store import RunStore


KEY_ID = "rzp_test_httpcontract"
KEY_SECRET = "http_test_secret_not_a_real_key"


class _RazorpayOrderGateway:
    gateway_name = gateway.GATEWAY_RAZORPAY
    key_id = KEY_ID

    def __init__(self) -> None:
        self.calls = 0

    def create_order(self, amount_paise, currency, receipt, notes):
        self.calls += 1
        return {
            "id": f"order_HttpTest{self.calls:06d}",
            "amount": amount_paise,
            "currency": currency,
            "receipt": receipt,
            "status": "created",
        }


@dataclass
class _PaymentClient:
    payments: dict[str, dict] = field(default_factory=dict)
    error: Exception | None = None
    fetches: list[str] = field(default_factory=list)

    def fetch_payment(self, payment_id: str) -> dict:
        self.fetches.append(payment_id)
        if self.error is not None:
            raise self.error
        if payment_id not in self.payments:
            raise RuntimeError("unknown payment")
        return self.payments[payment_id]


@pytest.fixture
def payment_api(tmp_path, monkeypatch):
    runs = RunStore(tmp_path / "runs.db")
    orders_db = tmp_path / "orders.db"
    payment_client = _PaymentClient()

    monkeypatch.setattr(config, "RAZORPAY_KEY_ID", KEY_ID)
    monkeypatch.setattr(config, "RAZORPAY_KEY_SECRET", KEY_SECRET)
    monkeypatch.setattr(server, "RUN_STORE", runs)
    monkeypatch.setattr(
        server,
        "PAYMENT_SERVICE",
        PaymentService(
            orders_db_path=orders_db,
            razorpay_gateway_factory=lambda: payment_client,
        ),
    )

    @dataclass
    class API:
        client: TestClient
        runs: RunStore
        orders_db: object
        payment_client: _PaymentClient

        def ordered_run(self, order):
            capability = self.runs.create("buy the selected item", 1_000_000, "live")
            self.runs.mark_running(capability.run_id)
            self.runs.complete(
                capability.run_id,
                status="ordered",
                reason="Gate passed",
                order_id=order.order_id,
                quote_id=order.quote_id,
                amount_paise=order.amount_paise,
                steps=3,
                llm_calls=2,
            )
            return capability

        def refused_run(self):
            capability = self.runs.create("buy an unauthorised item", 1_000_000, "live")
            self.runs.mark_running(capability.run_id)
            self.runs.complete(
                capability.run_id,
                status="refused",
                reason="Gate refused",
                order_id=None,
                quote_id=None,
                amount_paise=None,
                steps=2,
                llm_calls=1,
            )
            return capability

        @staticmethod
        def auth(capability) -> dict[str, str]:
            return {"Authorization": f"Bearer {capability.run_token}"}

    return API(TestClient(server.app), runs, orders_db, payment_client)


def _live_order(api, *, quote_id="quote_http", amount=250_000):
    return gateway.create_order(
        quote_id,
        amount,
        gateway=_RazorpayOrderGateway(),
        db_path=api.orders_db,
    )


def _sign(order_id: str, payment_id: str) -> str:
    return hmac.new(
        KEY_SECRET.encode(),
        f"{order_id}|{payment_id}".encode(),
        hashlib.sha256,
    ).hexdigest()


def _payment(order, payment_id="pay_httpcaptured", **overrides):
    value = {
        "id": payment_id,
        "order_id": order.order_id,
        "amount": order.amount_paise,
        "currency": order.currency,
        "status": "captured",
        "captured": True,
    }
    value.update(overrides)
    return value


def test_checkout_returns_only_the_existing_gate_created_order(payment_api):
    order_gateway = _RazorpayOrderGateway()
    order = gateway.create_order(
        "quote_existing",
        432_100,
        gateway=order_gateway,
        db_path=payment_api.orders_db,
    )
    capability = payment_api.ordered_run(order)

    response = payment_api.client.post(
        "/api/pay",
        json={"run_id": capability.run_id},
        headers=payment_api.auth(capability),
    )

    assert response.status_code == 200
    assert response.json() == {
        "gateway": "razorpay",
        "order_id": order.order_id,
        "amount_paise": 432_100,
        "currency": "INR",
        "key_id": KEY_ID,
    }
    assert order_gateway.calls == 1, "checkout must not create a second order"


@pytest.mark.parametrize("field,value", [("amount_paise", 1), ("order_id", "order_forged"), ("mode", "offline"), ("product", {"title": "forged"}), ("origin", "https://attacker.test")])
def test_checkout_rejects_all_client_money_and_purchase_authority(payment_api, field, value):
    order = _live_order(payment_api)
    capability = payment_api.ordered_run(order)
    response = payment_api.client.post(
        "/api/pay",
        json={"run_id": capability.run_id, field: value},
        headers=payment_api.auth(capability),
    )
    assert response.status_code == 422


def test_payment_routes_require_the_matching_run_capability(payment_api):
    first = payment_api.ordered_run(_live_order(payment_api, quote_id="quote_first"))
    second = payment_api.ordered_run(_live_order(payment_api, quote_id="quote_second"))

    assert payment_api.client.post("/api/pay", json={"run_id": first.run_id}).status_code == 401
    wrong = payment_api.client.post(
        "/api/pay",
        json={"run_id": second.run_id},
        headers=payment_api.auth(first),
    )
    assert wrong.status_code == 404
    assert wrong.json() == {"detail": "run not found"}


def test_a_run_the_gate_refused_has_no_payment_endpoint_authority(payment_api):
    capability = payment_api.refused_run()
    response = payment_api.client.post(
        "/api/pay",
        json={"run_id": capability.run_id},
        headers=payment_api.auth(capability),
    )
    assert response.status_code == 409


def test_forged_signature_is_rejected_before_any_payment_fetch(payment_api):
    order = _live_order(payment_api)
    capability = payment_api.ordered_run(order)
    response = payment_api.client.post(
        "/api/pay/confirm",
        json={
            "run_id": capability.run_id,
            "razorpay_payment_id": "pay_httpcaptured",
            "razorpay_signature": "0" * 64,
        },
        headers=payment_api.auth(capability),
    )
    assert response.status_code == 400
    assert payment_api.payment_client.fetches == []


def test_verified_full_capture_is_paid_end_to_end(payment_api):
    order = _live_order(payment_api)
    capability = payment_api.ordered_run(order)
    payment = _payment(order)
    payment_api.payment_client.payments[payment["id"]] = payment

    response = payment_api.client.post(
        "/api/pay/confirm",
        json={
            "run_id": capability.run_id,
            "razorpay_payment_id": payment["id"],
            "razorpay_signature": _sign(order.order_id, payment["id"]),
        },
        headers=payment_api.auth(capability),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "paid"
    assert response.json()["captured_amount_paise"] == order.amount_paise
    stored = gateway.find_by_order_id(order.order_id, db_path=payment_api.orders_db)
    assert stored.status == "captured"
    assert stored.payment_id == payment["id"]


@pytest.mark.parametrize(
    "overrides,expected_status,expected_capture",
    [
        ({"status": "authorized", "captured": False}, "pending", 0),
        ({"amount": 100_000}, "partially_captured", 100_000),
    ],
)
def test_uncaptured_and_partial_payments_are_not_paid(
    payment_api, overrides, expected_status, expected_capture
):
    order = _live_order(payment_api)
    capability = payment_api.ordered_run(order)
    payment = _payment(order, **overrides)
    payment_api.payment_client.payments[payment["id"]] = payment
    response = payment_api.client.post(
        "/api/pay/confirm",
        json={
            "run_id": capability.run_id,
            "razorpay_payment_id": payment["id"],
            "razorpay_signature": _sign(order.order_id, payment["id"]),
        },
        headers=payment_api.auth(capability),
    )
    assert response.status_code == 200
    assert response.json()["status"] == expected_status
    assert response.json()["captured_amount_paise"] == expected_capture


def test_real_lookup_failure_never_falls_back_to_simulated_success(payment_api, monkeypatch):
    monkeypatch.setattr(config, "USE_FAKE_GATEWAY", True)
    order = _live_order(payment_api)
    capability = payment_api.ordered_run(order)
    payment_api.payment_client.error = RuntimeError("gateway unavailable")
    payment_id = "pay_gatewaydown"

    response = payment_api.client.post(
        "/api/pay/confirm",
        json={
            "run_id": capability.run_id,
            "razorpay_payment_id": payment_id,
            "razorpay_signature": _sign(order.order_id, payment_id),
        },
        headers=payment_api.auth(capability),
    )

    assert response.status_code == 502
    stored = gateway.find_by_order_id(order.order_id, db_path=payment_api.orders_db)
    assert stored.status == "created"
    assert stored.captured_amount_paise == 0


def test_simulation_is_explicit_and_limited_to_recorded_simulated_orders(payment_api):
    simulated = gateway.create_order(
        "quote_simulated",
        120_000,
        gateway=gateway.FakeGateway(),
        db_path=payment_api.orders_db,
    )
    sim_capability = payment_api.ordered_run(simulated)
    settled = payment_api.client.post(
        "/api/pay/simulate",
        json={"run_id": sim_capability.run_id},
        headers=payment_api.auth(sim_capability),
    )
    assert settled.status_code == 200
    assert settled.json()["status"] == "paid"
    assert settled.json()["gateway"] == "test-sim"

    real = _live_order(payment_api, quote_id="quote_real")
    real_capability = payment_api.ordered_run(real)
    refused = payment_api.client.post(
        "/api/pay/simulate",
        json={"run_id": real_capability.run_id},
        headers=payment_api.auth(real_capability),
    )
    assert refused.status_code == 409
    assert gateway.find_by_order_id(real.order_id, db_path=payment_api.orders_db).status == "created"


def test_status_is_owner_protected_and_reports_recorded_settlement(payment_api):
    order = gateway.create_order(
        "quote_status",
        120_000,
        gateway=gateway.FakeGateway(),
        db_path=payment_api.orders_db,
    )
    capability = payment_api.ordered_run(order)
    payment_api.client.post(
        "/api/pay/simulate",
        json={"run_id": capability.run_id},
        headers=payment_api.auth(capability),
    )

    assert payment_api.client.get(f"/api/pay/{capability.run_id}").status_code == 401
    response = payment_api.client.get(
        f"/api/pay/{capability.run_id}",
        headers=payment_api.auth(capability),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "paid"
    assert response.json()["reconciled"] is True
