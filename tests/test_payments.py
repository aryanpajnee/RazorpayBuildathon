"""What has to be true before a browser is told its money moved.

Every Razorpay call in this file goes through an injected double -- no network,
no real credentials, and the key secret used to sign fixtures is a literal
defined here, never anything read from the environment. Signatures are computed
by hand rather than by calling the code under test, so a broken HMAC cannot
validate itself.
"""

from __future__ import annotations

import hashlib
import hmac
import time

import pytest

import config
from merchant import gateway
from merchant.gateway import FakeGateway, create_order, find_by_order_id, update_order_status
from ui import payments
from ui.payments import (
    GatewayConfigurationError,
    InvalidCheckoutSignatureError,
    PaymentLookupError,
    PaymentService,
    PaymentVerificationError,
    PurchaseRecordMismatchError,
    RunNotPayableError,
    SimulationNotAllowedError,
)
from ui.run_store import RunRecord

KEY_ID = "rzp_test_paymenttrack"
KEY_SECRET = "unit_test_secret_not_a_real_key"
OTHER_KEY_ID = "rzp_test_someoneelse"


@pytest.fixture(autouse=True)
def test_credentials(monkeypatch):
    """Every test runs against fixed, obviously-fake test-mode credentials so
    nothing depends on what .env happens to hold."""
    monkeypatch.setattr(config, "RAZORPAY_KEY_ID", KEY_ID)
    monkeypatch.setattr(config, "RAZORPAY_KEY_SECRET", KEY_SECRET)


class _RazorpayLikeGateway:
    """Creates orders that carry real-gateway provenance without a network
    call, so the razorpay-only code paths can be exercised offline."""

    gateway_name = gateway.GATEWAY_RAZORPAY

    def __init__(self, key_id: str = KEY_ID) -> None:
        self.key_id = key_id
        self.calls = 0

    def create_order(self, amount_paise, currency, receipt, notes):
        self.calls += 1
        return {
            "id": f"order_LiveLike{self.calls:06d}",
            "amount": amount_paise,
            "currency": currency,
            "receipt": receipt,
            "status": "created",
        }


class _StubClient:
    """Stands in for RazorpayGateway on the fetch path. `.fetches` records
    every payment id asked about, so a test can prove the network was never
    reached at all."""

    def __init__(self, payment=None, error: Exception | None = None) -> None:
        self._payment = payment
        self._error = error
        self.fetches: list[str] = []

    def fetch_payment(self, payment_id: str) -> dict:
        self.fetches.append(payment_id)
        if self._error is not None:
            raise self._error
        return self._payment


def sign(order_id: str, payment_id: str, secret: str = KEY_SECRET) -> str:
    return hmac.new(
        secret.encode("utf-8"), f"{order_id}|{payment_id}".encode("utf-8"), hashlib.sha256
    ).hexdigest()


def make_record(order, *, status: str = "ordered", **overrides) -> RunRecord:
    now = time.time()
    fields = {
        "run_id": "run_test",
        "request": "a thing under budget",
        "budget_paise": 1000000,
        "mode": "live",
        "status": status,
        "reason": "ordered",
        "order_id": order.order_id if order else None,
        "quote_id": order.quote_id if order else None,
        "amount_paise": order.amount_paise if order else None,
        "steps": 3,
        "llm_calls": 2,
        "error": None,
        "created_at": now,
        "updated_at": now,
    }
    fields.update(overrides)
    return RunRecord(**fields)


def captured_payment(order, payment_id: str = "pay_captured01", **overrides) -> dict:
    payment = {
        "id": payment_id,
        "order_id": order.order_id,
        "amount": order.amount_paise,
        "currency": order.currency,
        "status": "captured",
        "captured": True,
    }
    payment.update(overrides)
    return payment


def live_order(db, quote_id: str = "quote_live", amount: int = 250000):
    return create_order(quote_id, amount, gateway=_RazorpayLikeGateway(), db_path=db)


def service(db, client=None) -> PaymentService:
    return PaymentService(
        orders_db_path=db,
        razorpay_gateway_factory=(lambda: client) if client is not None else None,
    )


# --- checkout: the existing order, never a new one --------------------------

def test_checkout_returns_the_recorded_order_and_its_key(tmp_path):
    db = tmp_path / "orders.db"
    order = live_order(db)
    result = service(db).checkout(make_record(order))
    assert result == {
        "gateway": "razorpay",
        "order_id": order.order_id,
        "amount_paise": 250000,
        "currency": "INR",
        "key_id": KEY_ID,
    }


def test_checkout_of_a_simulated_order_carries_no_key(tmp_path):
    db = tmp_path / "orders.db"
    order = create_order("quote_sim", 120000, gateway=FakeGateway(), db_path=db)
    result = service(db).checkout(make_record(order))
    assert result["gateway"] == "test-sim"
    assert "key_id" not in result


def test_a_run_without_an_authorised_order_cannot_be_paid(tmp_path):
    db = tmp_path / "orders.db"
    record = make_record(None, status="refused")
    with pytest.raises(RunNotPayableError):
        service(db).checkout(record)


def test_a_run_amount_that_disagrees_with_the_order_is_refused(tmp_path):
    """Two stores describing one purchase must agree. If they don't, neither
    is trusted -- the browser does not get to break the tie."""
    db = tmp_path / "orders.db"
    order = live_order(db)
    record = make_record(order, amount_paise=order.amount_paise - 1)
    with pytest.raises(PurchaseRecordMismatchError):
        service(db).checkout(record)


def test_an_order_missing_from_the_merchant_store_is_refused(tmp_path):
    db = tmp_path / "orders.db"
    order = live_order(db)
    record = make_record(order, order_id="order_nosuchorder")
    with pytest.raises(PurchaseRecordMismatchError):
        service(db).checkout(record)


def test_a_key_that_did_not_create_the_order_is_refused(tmp_path):
    """A signature checked under a different account's secret proves nothing
    about this order, so the mismatch is caught before any signature work."""
    db = tmp_path / "orders.db"
    order = create_order(
        "quote_otherkey", 250000, gateway=_RazorpayLikeGateway(OTHER_KEY_ID), db_path=db
    )
    with pytest.raises(GatewayConfigurationError):
        service(db).checkout(make_record(order))


def test_live_mode_requires_test_credentials(tmp_path, monkeypatch):
    db = tmp_path / "orders.db"
    order = live_order(db)
    monkeypatch.setattr(config, "RAZORPAY_KEY_ID", "rzp_live_realmoney")
    with pytest.raises(GatewayConfigurationError):
        service(db).checkout(make_record(order))


# --- legacy rows fail closed ------------------------------------------------

def test_a_legacy_order_with_unknown_provenance_fails_closed(tmp_path):
    """A row written before provenance was recorded cannot be classified. It
    is refused on every path rather than guessed either way: guessing 'real'
    would check a signature against a key that never created it, and guessing
    'simulated' would allow it to be marked paid with no money moved."""
    db = tmp_path / "orders.db"
    order = create_order("quote_legacy", 250000, gateway=FakeGateway(), db_path=db)
    conn = gateway._connect(db)
    conn.execute("UPDATE orders SET gateway = 'unknown' WHERE order_id = ?", (order.order_id,))
    conn.close()

    legacy = find_by_order_id(order.order_id, db_path=db)
    assert legacy.gateway == "unknown"
    record = make_record(legacy)
    svc = service(db)
    with pytest.raises(GatewayConfigurationError):
        svc.checkout(record)
    with pytest.raises(GatewayConfigurationError):
        svc.simulate(record)
    with pytest.raises(GatewayConfigurationError):
        svc.confirm(record, payment_id="pay_x", signature="0" * 64)


# --- confirm: signature first ----------------------------------------------

def test_a_forged_signature_is_refused_without_asking_razorpay(tmp_path):
    db = tmp_path / "orders.db"
    order = live_order(db)
    client = _StubClient(captured_payment(order))
    svc = service(db, client)

    with pytest.raises(InvalidCheckoutSignatureError):
        svc.confirm(make_record(order), payment_id="pay_captured01", signature="a" * 64)

    assert client.fetches == [], "an unauthenticated payment id must not buy a fetch"
    assert find_by_order_id(order.order_id, db_path=db).status == "created"


def test_a_signature_for_another_order_is_refused(tmp_path):
    """The signature binds a payment to one order id. Replaying a genuine
    signature from a different order must not authenticate this one."""
    db = tmp_path / "orders.db"
    gw = _RazorpayLikeGateway()
    mine = create_order("quote_mine", 250000, gateway=gw, db_path=db)
    theirs = create_order("quote_theirs", 250000, gateway=gw, db_path=db)
    svc = service(db, _StubClient(captured_payment(mine)))

    with pytest.raises(InvalidCheckoutSignatureError):
        svc.confirm(
            make_record(mine),
            payment_id="pay_captured01",
            signature=sign(theirs.order_id, "pay_captured01"),
        )


def test_a_signature_made_with_the_wrong_secret_is_refused(tmp_path):
    db = tmp_path / "orders.db"
    order = live_order(db)
    svc = service(db, _StubClient(captured_payment(order)))
    with pytest.raises(InvalidCheckoutSignatureError):
        svc.confirm(
            make_record(order),
            payment_id="pay_captured01",
            signature=sign(order.order_id, "pay_captured01", secret="not-the-secret"),
        )


@pytest.mark.parametrize(
    "signature",
    ["", "not-hex", "A" * 64, "0" * 63, "0" * 65, "ç" * 64],
    ids=["empty", "non-hex", "uppercase", "too-short", "too-long", "non-ascii"],
)
def test_a_malformed_signature_is_a_refusal_not_a_crash(tmp_path, signature):
    """hmac.compare_digest raises TypeError on a non-ASCII str; an unshaped
    value would surface as a 500 instead of a refusal."""
    db = tmp_path / "orders.db"
    order = live_order(db)
    svc = service(db, _StubClient(captured_payment(order)))
    with pytest.raises(InvalidCheckoutSignatureError):
        svc.confirm(make_record(order), payment_id="pay_captured01", signature=signature)


@pytest.mark.parametrize("payment_id", ["", "pay with spaces", "pay/../etc", "p" * 65])
def test_a_malformed_payment_id_is_refused(tmp_path, payment_id):
    db = tmp_path / "orders.db"
    order = live_order(db)
    svc = service(db, _StubClient(captured_payment(order)))
    with pytest.raises(InvalidCheckoutSignatureError):
        svc.confirm(
            make_record(order),
            payment_id=payment_id,
            signature=sign(order.order_id, payment_id),
        )


def test_an_invented_payment_id_is_refused(tmp_path):
    """A payment id Razorpay has never heard of. Even with a signature this
    server would accept, the fetch fails and the order stays unpaid -- 'we
    could not check' is never reported as settlement."""
    db = tmp_path / "orders.db"
    order = live_order(db)
    client = _StubClient(error=RuntimeError("The id provided does not exist"))
    with pytest.raises(PaymentLookupError):
        service(db, client).confirm(
            make_record(order),
            payment_id="pay_invented99",
            signature=sign(order.order_id, "pay_invented99"),
        )
    assert client.fetches == ["pay_invented99"]
    assert find_by_order_id(order.order_id, db_path=db).status == "created"


def test_a_lookup_failure_never_leaks_the_key_secret(tmp_path):
    db = tmp_path / "orders.db"
    order = live_order(db)
    client = _StubClient(error=RuntimeError(f"auth failed for {KEY_SECRET}"))
    try:
        service(db, client).confirm(
            make_record(order),
            payment_id="pay_leaky01",
            signature=sign(order.order_id, "pay_leaky01"),
        )
    except PaymentLookupError as exc:
        assert KEY_SECRET not in str(exc)
    else:  # pragma: no cover - the stub always raises
        pytest.fail("expected a lookup failure")


# --- confirm: the fetched payment must be this order's payment --------------

@pytest.mark.parametrize(
    "overrides, ids",
    [
        ({"amount": 250001}, "amount-above-order"),
        ({"currency": "USD"}, "currency-mismatch"),
        ({"order_id": "order_SomeoneElse01"}, "order-mismatch"),
        ({"id": "pay_different99"}, "identity-mismatch"),
        ({"amount": 250000.0}, "float-amount"),
        ({"amount": 0}, "zero-amount"),
        ({"status": "refunded"}, "unknown-status"),
        ({"captured": False}, "self-contradicting-capture"),
    ],
)
def test_a_payment_that_is_not_this_orders_payment_is_refused(tmp_path, overrides, ids):
    db = tmp_path / "orders.db"
    order = live_order(db)
    payment = captured_payment(order, **overrides)
    svc = service(db, _StubClient(payment))
    with pytest.raises(PaymentVerificationError):
        svc.confirm(
            make_record(order),
            payment_id="pay_captured01",
            signature=sign(order.order_id, "pay_captured01"),
        )
    assert find_by_order_id(order.order_id, db_path=db).status == "created"


def test_a_float_amount_is_not_accepted_as_an_exact_match(tmp_path):
    """250000.0 == 250000 is True in Python. Only a typed check keeps a float
    out of the money path."""
    db = tmp_path / "orders.db"
    order = live_order(db)
    svc = service(db, _StubClient(captured_payment(order, amount=float(order.amount_paise))))
    with pytest.raises(PaymentVerificationError):
        svc.confirm(
            make_record(order),
            payment_id="pay_captured01",
            signature=sign(order.order_id, "pay_captured01"),
        )


# --- confirm: outcomes ------------------------------------------------------

def test_a_verified_full_capture_is_paid(tmp_path):
    db = tmp_path / "orders.db"
    order = live_order(db)
    result = service(db, _StubClient(captured_payment(order))).confirm(
        make_record(order),
        payment_id="pay_captured01",
        signature=sign(order.order_id, "pay_captured01"),
    )
    assert result["status"] == "paid"
    assert result["payment_id"] == "pay_captured01"
    assert result["captured_amount_paise"] == 250000
    assert result["amount_paise"] == 250000
    assert result["currency"] == "INR"
    assert result["gateway"] == "razorpay"
    assert result["reconciled"] is True
    assert result["test_mode"] is True


def test_an_authorized_but_uncaptured_payment_is_never_paid(tmp_path):
    """The signature on an authorized payment is perfectly valid. Money held
    is not money taken, so the signature alone can never mean paid."""
    db = tmp_path / "orders.db"
    order = live_order(db)
    payment = captured_payment(order, status="authorized", captured=False)
    result = service(db, _StubClient(payment)).confirm(
        make_record(order),
        payment_id="pay_captured01",
        signature=sign(order.order_id, "pay_captured01"),
    )
    assert result["status"] == "pending"
    assert result["captured_amount_paise"] == 0
    assert find_by_order_id(order.order_id, db_path=db).status == "authorized"


def test_a_partial_capture_is_never_paid(tmp_path):
    db = tmp_path / "orders.db"
    order = live_order(db)
    payment = captured_payment(order, amount=100000)
    result = service(db, _StubClient(payment)).confirm(
        make_record(order),
        payment_id="pay_captured01",
        signature=sign(order.order_id, "pay_captured01"),
    )
    assert result["status"] == "partially_captured"
    assert result["captured_amount_paise"] == 100000
    assert result["amount_paise"] == 250000
    assert find_by_order_id(order.order_id, db_path=db).status == "partially_captured"


def test_a_failed_payment_is_reported_failed(tmp_path):
    db = tmp_path / "orders.db"
    order = live_order(db)
    payment = captured_payment(order, status="failed", captured=False)
    result = service(db, _StubClient(payment)).confirm(
        make_record(order),
        payment_id="pay_captured01",
        signature=sign(order.order_id, "pay_captured01"),
    )
    assert result["status"] == "failed"


def test_confirming_twice_does_not_double_settle(tmp_path):
    db = tmp_path / "orders.db"
    order = live_order(db)
    svc = service(db, _StubClient(captured_payment(order)))
    args = dict(
        payment_id="pay_captured01", signature=sign(order.order_id, "pay_captured01")
    )
    first = svc.confirm(make_record(order), **args)
    second = svc.confirm(make_record(order), **args)
    assert first["status"] == second["status"] == "paid"
    assert second["captured_amount_paise"] == 250000


# --- simulation and real payment never overlap ------------------------------

def test_simulation_is_refused_for_a_real_order(tmp_path):
    """The one rule that keeps 'paid' meaning money moved: a real gateway
    failure can never be rescued by simulating success."""
    db = tmp_path / "orders.db"
    order = live_order(db)
    with pytest.raises(SimulationNotAllowedError):
        service(db).simulate(make_record(order))
    assert find_by_order_id(order.order_id, db_path=db).status == "created"


def test_confirmation_is_refused_for_a_simulated_order(tmp_path):
    db = tmp_path / "orders.db"
    order = create_order("quote_sim2", 120000, gateway=FakeGateway(), db_path=db)
    with pytest.raises(SimulationNotAllowedError):
        service(db).confirm(
            make_record(order),
            payment_id="pay_captured01",
            signature=sign(order.order_id, "pay_captured01"),
        )


def test_simulating_a_test_order_settles_it_once(tmp_path):
    db = tmp_path / "orders.db"
    order = create_order("quote_sim3", 120000, gateway=FakeGateway(), db_path=db)
    svc = service(db)
    first = svc.simulate(make_record(order))
    second = svc.simulate(make_record(order))
    assert first["status"] == "paid"
    assert first["gateway"] == "test-sim"
    assert first["captured_amount_paise"] == 120000
    assert second == first, "a second simulate must not mint a second payment"


def test_a_simulated_order_needs_no_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RAZORPAY_KEY_ID", "")
    monkeypatch.setattr(config, "RAZORPAY_KEY_SECRET", "")
    db = tmp_path / "orders.db"
    order = create_order("quote_sim4", 120000, gateway=FakeGateway(), db_path=db)
    svc = service(db)
    assert svc.checkout(make_record(order))["gateway"] == "test-sim"
    assert svc.simulate(make_record(order))["status"] == "paid"


# --- status: a poll can never undo a settlement -----------------------------

def test_a_status_poll_that_cannot_reach_razorpay_keeps_the_settled_state(tmp_path):
    db = tmp_path / "orders.db"
    order = live_order(db)
    svc = service(db, _StubClient(captured_payment(order)))
    args = dict(
        payment_id="pay_captured01", signature=sign(order.order_id, "pay_captured01")
    )
    assert svc.confirm(make_record(order), **args)["status"] == "paid"

    offline = service(db, _StubClient(error=RuntimeError("connection reset")))
    result = offline.status(make_record(order))
    assert result["status"] == "paid"
    assert result["reconciled"] is False


def test_a_status_poll_before_any_payment_is_pending(tmp_path):
    db = tmp_path / "orders.db"
    order = live_order(db)
    client = _StubClient(captured_payment(order))
    result = service(db, client).status(make_record(order))
    assert result["status"] == "pending"
    assert result["payment_id"] is None
    assert client.fetches == [], "with no payment on file there is nothing to fetch"


def test_a_delayed_failure_cannot_unsettle_a_paid_order(tmp_path):
    """Razorpay reports a first, abandoned attempt as failed after the second
    attempt captured. The later-arriving event is older news."""
    db = tmp_path / "orders.db"
    order = live_order(db)
    svc = service(db, _StubClient(captured_payment(order)))
    svc.confirm(
        make_record(order),
        payment_id="pay_captured01",
        signature=sign(order.order_id, "pay_captured01"),
    )
    update_order_status(order.order_id, "failed", payment_id="pay_abandoned", db_path=db)
    assert svc.status(make_record(order))["status"] == "paid"
    assert find_by_order_id(order.order_id, db_path=db).payment_id == "pay_captured01"


def test_public_status_shape_is_the_contract_shape(tmp_path):
    db = tmp_path / "orders.db"
    order = create_order("quote_shape", 120000, gateway=FakeGateway(), db_path=db)
    result = service(db).status(make_record(order))
    assert set(result) == {
        "status",
        "gateway",
        "order_id",
        "payment_id",
        "amount_paise",
        "captured_amount_paise",
        "currency",
        "reconciled",
        "test_mode",
    }
    assert result["status"] in {"paid", "partially_captured", "failed", "pending"}
    assert type(result["amount_paise"]) is int
    assert type(result["captured_amount_paise"]) is int


def test_the_module_never_builds_a_real_client_on_the_simulated_path(tmp_path):
    """Constructing RazorpayGateway imports the SDK and reads credentials. A
    simulated run must never touch it."""
    db = tmp_path / "orders.db"
    order = create_order("quote_nolive", 120000, gateway=FakeGateway(), db_path=db)

    def explode():  # pragma: no cover - called only on failure
        raise AssertionError("the simulated path built a real gateway client")

    svc = PaymentService(orders_db_path=db, razorpay_gateway_factory=explode)
    assert svc.simulate(make_record(order))["status"] == "paid"
    assert svc.status(make_record(order))["status"] == "paid"


def test_the_default_factory_is_the_real_gateway():
    """The injected double is a test affordance, not the production default."""
    assert PaymentService()._razorpay_gateway_factory is gateway.RazorpayGateway
    assert payments.STATUS_PAID == "paid"
