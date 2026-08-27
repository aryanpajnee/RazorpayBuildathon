"""Order creation is where a bug becomes a duplicate charge, so idempotency is
the property under test more than any single call's return value.

Fully offline: every test injects a FakeGateway (or a test double built on
top of it) and a tmp_path database, never touches config.RAZORPAY_KEY_ID or
the network.
"""

from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from merchant.gateway import (
    AmountMismatchError,
    FakeGateway,
    GatewayError,
    Order,
    OrderCreationError,
    OrderNotFoundError,
    create_order,
    find_by_order_id,
    update_order_status,
)


class _AlwaysFailsGateway:
    """A gateway double that always declines, to exercise the no-phantom-row
    path without needing a real Razorpay rejection."""

    def create_order(self, amount_paise, currency, receipt, notes):
        raise RuntimeError("simulated decline: card_declined")


class _DivergentAmountGateway:
    """A gateway double whose response confirms a different amount than what
    was requested -- simulating a bug or drift on the gateway side. The
    stored order must reflect what the gateway actually confirmed, and the
    divergence itself must be surfaced loudly, not silently recorded as
    either number."""

    def create_order(self, amount_paise, currency, receipt, notes):
        return {"id": "order_divergent001", "amount": amount_paise + 1, "currency": currency, "status": "created"}


def test_a_gateway_response_confirming_a_different_amount_than_requested_raises(tmp_path):
    db = tmp_path / "orders.db"
    with pytest.raises(GatewayError):
        create_order("quote_divergent", 100000, gateway=_DivergentAmountGateway(), db_path=db)


class _SlowGateway:
    """A gateway double that sleeps briefly before responding, so a real
    `threading` race between two callers for the *same* quote_id has a
    window in which the second caller can observe the first's reservation
    while it is still in flight. Used to prove the reservation-first design
    actually excludes a second gateway call, rather than relying on timing
    getting lucky.
    """

    def __init__(self, delay_seconds: float = 0.08) -> None:
        self.calls = 0
        self._lock = threading.Lock()
        self._delay_seconds = delay_seconds

    def create_order(self, amount_paise, currency, receipt, notes):
        with self._lock:
            self.calls += 1
            call_number = self.calls
        time.sleep(self._delay_seconds)
        return {
            "id": f"order_slow{call_number:06d}",
            "amount": amount_paise,
            "currency": currency,
            "status": "created",
        }


# --- idempotency -------------------------------------------------------------

def test_create_order_persists_the_quote_id_to_order_id_mapping(tmp_path):
    db = tmp_path / "orders.db"
    order = create_order("quote_1", 589882, gateway=FakeGateway(), db_path=db)
    assert order.quote_id == "quote_1"
    assert order.order_id.startswith("order_fake")
    assert order.amount_paise == 589882
    assert order.currency == "INR"
    assert order.from_cache is False


def test_calling_create_order_twice_with_the_same_quote_id_returns_the_same_order_id(tmp_path):
    db = tmp_path / "orders.db"
    gw = FakeGateway()
    first = create_order("quote_2", 100000, gateway=gw, db_path=db)
    second = create_order("quote_2", 100000, gateway=gw, db_path=db)
    assert second.order_id == first.order_id
    assert second.from_cache is True


def test_a_second_call_for_the_same_quote_id_never_hits_the_gateway_again(tmp_path):
    db = tmp_path / "orders.db"
    gw = FakeGateway()
    create_order("quote_3", 100000, gateway=gw, db_path=db)
    assert gw.calls == 1
    create_order("quote_3", 100000, gateway=gw, db_path=db)
    assert gw.calls == 1, "second call must be served from the store, not the gateway"


def test_different_quote_ids_produce_different_orders(tmp_path):
    db = tmp_path / "orders.db"
    gw = FakeGateway()
    a = create_order("quote_a", 100000, gateway=gw, db_path=db)
    b = create_order("quote_b", 100000, gateway=gw, db_path=db)
    assert a.order_id != b.order_id
    assert gw.calls == 2


class _PhantomIntegrityErrorGateway:
    """Forces the INSERT in create_order() to raise IntegrityError for a
    reason that is NOT a quote_id collision (no competing row exists). This
    reproduces a NOT NULL / CHECK constraint violation or similar -- the
    re-select after catching IntegrityError then finds nothing, and
    _row_to_order must not be handed None to unpack."""

    def create_order(self, amount_paise, currency, receipt, notes):
        return {"id": None, "amount": amount_paise, "currency": currency, "status": "created"}


def test_an_integrity_error_that_is_not_a_quote_id_collision_raises_a_named_error(tmp_path):
    db = tmp_path / "orders.db"
    with pytest.raises(GatewayError):
        create_order("quote_phantom", 100000, gateway=_PhantomIntegrityErrorGateway(), db_path=db)


def test_reservation_first_closes_the_cross_process_double_call_race(tmp_path):
    """Regression test for FAILURES.md's "Idempotency that stops a second
    row, not a second order": two callers race the *same* quote_id,
    released together via a Barrier, against a gateway that sleeps briefly
    before answering -- simulating the window in which a second process
    could, under the old check-then-act design, also pass the "not on file
    yet" check and also call Razorpay.

    Under reservation-first ordering only one of the two callers should ever
    reach the gateway at all: the other must find the reservation already
    claimed and wait for it, not call the gateway a second time.
    """
    db = tmp_path / "orders.db"
    gw = _SlowGateway(delay_seconds=0.08)
    barrier = threading.Barrier(2)
    results: list[Order] = []
    errors: list[BaseException] = []

    def worker():
        try:
            barrier.wait(timeout=5)
            order = create_order("quote_concurrent", 750000, gateway=gw, db_path=db)
            results.append(order)
        except BaseException as exc:  # noqa: BLE001 - captured for the main thread to re-raise
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, f"worker thread(s) raised: {errors}"
    assert len(results) == 2
    assert gw.calls == 1, "the gateway must be called exactly once for a raced quote_id"
    assert results[0].order_id == results[1].order_id
    assert {r.from_cache for r in results} == {True, False}, (
        "exactly one caller creates (from_cache=False), the other is served the cache"
    )

    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT order_id, status FROM orders WHERE quote_id = 'quote_concurrent'"
    ).fetchall()
    conn.close()
    assert rows == [(results[0].order_id, "created")], "exactly one row must survive the race"


# --- amount validation ---------------------------------------------------

def test_amount_paise_must_be_an_int(tmp_path):
    with pytest.raises(TypeError):
        create_order("quote_4", 100.0, gateway=FakeGateway(), db_path=tmp_path / "orders.db")


def test_a_bool_amount_is_rejected():
    """bool subclasses int in Python; True would silently mean 1 paise."""
    with pytest.raises(TypeError):
        create_order("quote_5", True, gateway=FakeGateway(), db_path=None)


def test_amount_paise_must_be_positive(tmp_path):
    with pytest.raises(ValueError):
        create_order("quote_6", 0, gateway=FakeGateway(), db_path=tmp_path / "orders.db")
    with pytest.raises(ValueError):
        create_order("quote_7", -100, gateway=FakeGateway(), db_path=tmp_path / "orders.db")


def test_a_second_call_with_a_different_amount_for_the_same_quote_id_raises(tmp_path):
    """A quote_id arriving twice with two different amounts means something is
    badly wrong upstream (a recovery agent replaying a stale quote, a caller
    bug). The cached-order path must never silently answer with the first
    amount as if the mismatch didn't happen."""
    db = tmp_path / "orders.db"
    gw = FakeGateway()
    create_order("quote_mismatch", 500000, gateway=gw, db_path=db)
    with pytest.raises(AmountMismatchError) as excinfo:
        create_order("quote_mismatch", 999999, gateway=gw, db_path=db)
    assert excinfo.value.requested_amount_paise == 999999
    assert excinfo.value.recorded_amount_paise == 500000


# --- failure leaves no phantom row -------------------------------------------

def test_a_declined_order_creation_raises_a_typed_error(tmp_path):
    db = tmp_path / "orders.db"
    with pytest.raises(OrderCreationError):
        create_order("quote_8", 100000, gateway=_AlwaysFailsGateway(), db_path=db)


def test_a_failed_creation_leaves_the_store_clean_so_a_retry_can_still_succeed(tmp_path):
    db = tmp_path / "orders.db"
    with pytest.raises(OrderCreationError):
        create_order("quote_9", 100000, gateway=_AlwaysFailsGateway(), db_path=db)

    assert find_by_order_id("order_fake000001", db_path=db) is None

    # A later retry with a working gateway must succeed as if nothing happened.
    order = create_order("quote_9", 100000, gateway=FakeGateway(), db_path=db)
    assert order.from_cache is False
    assert order.quote_id == "quote_9"


def test_order_creation_error_never_contains_the_key_secret(tmp_path):
    """A gateway exception message could in principle carry request context;
    the wrapper must never let RAZORPAY_KEY_SECRET leak into it."""
    db = tmp_path / "orders.db"
    try:
        create_order("quote_10", 100000, gateway=_AlwaysFailsGateway(), db_path=db)
    except OrderCreationError as exc:
        assert "RAZORPAY_KEY_SECRET" not in str(exc)


# --- typed dataclass ----------------------------------------------------

def test_order_is_a_frozen_dataclass(tmp_path):
    order = create_order("quote_11", 100000, gateway=FakeGateway(), db_path=tmp_path / "orders.db")
    assert isinstance(order, Order)
    with pytest.raises(Exception):
        order.amount_paise = 1


def test_find_by_order_id_returns_none_for_an_unknown_order(tmp_path):
    db = tmp_path / "orders.db"
    assert find_by_order_id("order_does_not_exist", db_path=db) is None


def test_find_by_order_id_never_returns_a_bare_pending_reservation(tmp_path):
    """A pending (or reclaiming) reservation has order_id IS NULL -- it must
    never be surfaced by find_by_order_id, which webhooks.py relies on to
    resolve only real, gateway-confirmed orders."""
    db = tmp_path / "orders.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS orders ("
        "quote_id TEXT PRIMARY KEY, order_id TEXT, "
        "amount_paise INTEGER NOT NULL, currency TEXT NOT NULL, "
        "status TEXT NOT NULL, created_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO orders (quote_id, order_id, amount_paise, currency, status, created_at) "
        "VALUES ('quote_pending_only', NULL, 100000, 'INR', 'pending', '2026-01-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    assert find_by_order_id("order_never_assigned", db_path=db) is None


def test_find_by_order_id_recovers_the_quote_id(tmp_path):
    db = tmp_path / "orders.db"
    created = create_order("quote_12", 100000, gateway=FakeGateway(), db_path=db)
    found = find_by_order_id(created.order_id, db_path=db)
    assert found is not None
    assert found.quote_id == "quote_12"


# --- update_order_status --------------------------------------------------

def test_update_order_status_writes_the_new_status_back(tmp_path):
    db = tmp_path / "orders.db"
    created = create_order("quote_13", 100000, gateway=FakeGateway(), db_path=db)
    assert created.status == "created"

    updated = update_order_status(created.order_id, "captured", db_path=db)
    assert updated.status == "captured"

    reread = find_by_order_id(created.order_id, db_path=db)
    assert reread.status == "captured"


def test_update_order_status_on_an_unknown_order_id_raises(tmp_path):
    db = tmp_path / "orders.db"
    with pytest.raises(OrderNotFoundError):
        update_order_status("order_does_not_exist", "captured", db_path=db)
