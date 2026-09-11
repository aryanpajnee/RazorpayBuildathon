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


def test_separate_default_fake_gateways_create_distinct_order_ids(tmp_path):
    """The UI constructs a fresh fake gateway for each offline run. Its ids
    must therefore be unique across instances, not just within one object."""
    db = tmp_path / "orders.db"
    first = create_order("quote_first", 100000, gateway=FakeGateway(), db_path=db)
    second = create_order("quote_second", 100000, gateway=FakeGateway(), db_path=db)

    assert first.order_id != second.order_id
    assert find_by_order_id(first.order_id, db_path=db).quote_id == "quote_first"
    assert find_by_order_id(second.order_id, db_path=db).quote_id == "quote_second"


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


def test_an_unrecognised_status_is_refused(tmp_path):
    """The orders table is the only place that says whether money moved. A
    caller's typo must fail loudly, not write a status nothing can read."""
    db = tmp_path / "orders.db"
    created = create_order("quote_badstatus", 100000, gateway=FakeGateway(), db_path=db)
    for status in ("capture", "success", "PAID", "pending", "reclaiming", ""):
        with pytest.raises(ValueError):
            update_order_status(created.order_id, status, db_path=db)
    assert find_by_order_id(created.order_id, db_path=db).status == "created"


# --- monotonic settlement -------------------------------------------------

def test_a_full_capture_records_the_whole_amount(tmp_path):
    db = tmp_path / "orders.db"
    created = create_order("quote_cap1", 100000, gateway=FakeGateway(), db_path=db)
    updated = update_order_status(
        created.order_id, "captured", payment_id="pay_1", db_path=db
    )
    assert updated.status == "captured"
    assert updated.captured_amount_paise == 100000
    assert updated.payment_id == "pay_1"


def test_a_capture_short_of_the_order_total_is_stored_as_partial(tmp_path):
    """'Money arrived' and 'the order is settled' are different claims. Only
    the second may ever be shown to a buyer as paid."""
    db = tmp_path / "orders.db"
    created = create_order("quote_cap2", 100000, gateway=FakeGateway(), db_path=db)
    updated = update_order_status(
        created.order_id, "captured", captured_amount_paise=40000, db_path=db
    )
    assert updated.status == "partially_captured"
    assert updated.captured_amount_paise == 40000


def test_a_later_failure_cannot_unsettle_a_captured_order(tmp_path):
    db = tmp_path / "orders.db"
    created = create_order("quote_cap3", 100000, gateway=FakeGateway(), db_path=db)
    update_order_status(created.order_id, "captured", payment_id="pay_ok", db_path=db)
    updated = update_order_status(
        created.order_id, "failed", payment_id="pay_abandoned", db_path=db
    )
    assert updated.status == "captured"
    assert updated.captured_amount_paise == 100000
    assert updated.payment_id == "pay_ok", "the id that settled the order is evidence"


def test_a_later_authorized_event_cannot_unsettle_a_captured_order(tmp_path):
    db = tmp_path / "orders.db"
    created = create_order("quote_cap4", 100000, gateway=FakeGateway(), db_path=db)
    update_order_status(created.order_id, "captured", db_path=db)
    assert update_order_status(created.order_id, "authorized", db_path=db).status == "captured"


def test_a_partial_capture_cannot_demote_a_full_settlement(tmp_path):
    db = tmp_path / "orders.db"
    created = create_order("quote_cap5", 100000, gateway=FakeGateway(), db_path=db)
    update_order_status(created.order_id, "paid", db_path=db)
    updated = update_order_status(
        created.order_id, "captured", captured_amount_paise=10000, db_path=db
    )
    assert updated.status == "paid"
    assert updated.captured_amount_paise == 100000, "captured amounts only go up"


def test_a_capture_can_complete_an_earlier_partial_one(tmp_path):
    db = tmp_path / "orders.db"
    created = create_order("quote_cap6", 100000, gateway=FakeGateway(), db_path=db)
    update_order_status(created.order_id, "captured", captured_amount_paise=60000, db_path=db)
    updated = update_order_status(
        created.order_id, "captured", captured_amount_paise=100000, db_path=db
    )
    assert updated.status == "captured"
    assert updated.captured_amount_paise == 100000


def test_a_failed_order_can_still_be_recovered_by_a_later_success(tmp_path):
    """Nothing was taken on a failure, so a retry that captures is genuinely
    newer news -- monotonic protects settlement, not failure."""
    db = tmp_path / "orders.db"
    created = create_order("quote_cap7", 100000, gateway=FakeGateway(), db_path=db)
    update_order_status(created.order_id, "failed", payment_id="pay_first", db_path=db)
    updated = update_order_status(
        created.order_id, "captured", payment_id="pay_second", db_path=db
    )
    assert updated.status == "captured"
    assert updated.payment_id == "pay_second"


def test_a_capture_larger_than_the_order_is_refused(tmp_path):
    db = tmp_path / "orders.db"
    created = create_order("quote_cap8", 100000, gateway=FakeGateway(), db_path=db)
    with pytest.raises(AmountMismatchError):
        update_order_status(
            created.order_id, "captured", captured_amount_paise=100001, db_path=db
        )
    assert find_by_order_id(created.order_id, db_path=db).status == "created"


def test_a_bool_captured_amount_is_rejected(tmp_path):
    db = tmp_path / "orders.db"
    created = create_order("quote_cap9", 100000, gateway=FakeGateway(), db_path=db)
    for bad in (True, 100000.0, 0, -1):
        with pytest.raises(ValueError):
            update_order_status(
                created.order_id, "captured", captured_amount_paise=bad, db_path=db
            )


def test_concurrent_status_updates_cannot_race_settlement_backwards(tmp_path):
    """Two webhook workers, one delivering a capture and many delivering a
    stale failure. A read-then-write outside a transaction would let a
    loser's stale conclusion land last; the decision runs under the same
    BEGIN IMMEDIATE as the read, so it cannot."""
    db = tmp_path / "orders.db"
    created = create_order("quote_race", 100000, gateway=FakeGateway(), db_path=db)
    barrier = threading.Barrier(8)
    errors = []

    def apply(status, payment_id):
        try:
            barrier.wait()
            update_order_status(created.order_id, status, payment_id=payment_id, db_path=db)
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=apply, args=("captured", "pay_ok"))]
    threads += [
        threading.Thread(target=apply, args=("failed", f"pay_stale{i}")) for i in range(7)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    final = find_by_order_id(created.order_id, db_path=db)
    assert final.status == "captured"
    assert final.captured_amount_paise == 100000


# --- provenance and migration ---------------------------------------------

def test_a_created_order_records_which_gateway_made_it(tmp_path):
    db = tmp_path / "orders.db"
    created = create_order("quote_prov", 100000, gateway=FakeGateway(), db_path=db)
    assert created.gateway == "test-sim"
    assert created.gateway_key_id is None
    assert created.captured_amount_paise == 0
    assert created.payment_id is None
    assert find_by_order_id(created.order_id, db_path=db).gateway == "test-sim"


def _legacy_database(path, rows):
    """An orders.db in the pre-provenance schema, written by hand."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE orders ("
        "quote_id TEXT PRIMARY KEY, order_id TEXT, "
        "amount_paise INTEGER NOT NULL, currency TEXT NOT NULL, "
        "status TEXT NOT NULL, created_at TEXT NOT NULL)"
    )
    conn.executemany(
        "INSERT INTO orders (quote_id, order_id, amount_paise, currency, status, created_at) "
        "VALUES (?, ?, ?, 'INR', ?, '2026-01-01T00:00:00+00:00')",
        rows,
    )
    conn.commit()
    conn.close()


def test_opening_a_legacy_database_is_safe_and_repeatable(tmp_path):
    db = tmp_path / "orders.db"
    _legacy_database(
        db,
        [
            ("quote_old1", "order_fake000001", 100000, "created"),
            ("quote_old2", "order_LiveLike0001x", 250000, "captured"),
        ],
    )

    for _ in range(3):  # repeatable: opening again must change nothing
        first = find_by_order_id("order_fake000001", db_path=db)
        second = find_by_order_id("order_LiveLike0001x", db_path=db)

    # An id in FakeGateway's exact shape is provably simulated.
    assert first.gateway == "test-sim"
    assert first.captured_amount_paise == 0
    # Anything else stays unknown, and ui/payments.py refuses it at checkout.
    assert second.gateway == "unknown"
    # A legacy row that already said captured was fully settled by the
    # semantics of its day; 0 would read as "not a paisa taken".
    assert second.captured_amount_paise == 250000


def test_a_legacy_database_still_accepts_new_orders(tmp_path):
    db = tmp_path / "orders.db"
    _legacy_database(db, [("quote_old3", "order_fake000001", 100000, "created")])
    created = create_order("quote_new", 500000, gateway=FakeGateway(), db_path=db)
    assert created.gateway == "test-sim"
    assert created.captured_amount_paise == 0


def test_a_real_order_id_can_never_be_backfilled_as_simulated(tmp_path):
    """The backfill matches FakeGateway's exact id shape, not the prefix. A
    real Razorpay id is a different length, so however its random suffix
    falls out it cannot be relabelled simulatable -- and a simulatable order
    can be marked paid with no money moved."""
    db = tmp_path / "orders.db"
    _legacy_database(
        db,
        [
            ("quote_r1", "order_fakeAAAAAA", 100000, "created"),
            ("quote_r2", "order_fake0000012", 100000, "created"),
            ("quote_r3", "order_fake00001", 100000, "created"),
        ],
    )
    for order_id in ("order_fakeAAAAAA", "order_fake0000012", "order_fake00001"):
        assert find_by_order_id(order_id, db_path=db).gateway == "unknown"


def test_a_finished_order_is_served_from_cache_whatever_its_status(tmp_path):
    """Once a webhook has moved a row past 'created', a retry with the same
    quote_id must still recognise its own order. Requiring status == 'created'
    to serve the cache meant a paid quote_id looked like an unfinished
    reservation: the caller waited out the window, could not reclaim it
    either, and looped -- and a reclaim that did succeed would have created a
    second real order for an order already paid."""
    db = tmp_path / "orders.db"
    gw = FakeGateway()
    first = create_order("quote_settled", 100000, gateway=gw, db_path=db)
    update_order_status(first.order_id, "captured", payment_id="pay_done", db_path=db)

    again = create_order("quote_settled", 100000, gateway=gw, db_path=db)
    assert again.order_id == first.order_id
    assert again.from_cache is True
    assert again.status == "captured"
    assert gw.calls == 1, "a settled quote_id must never reach the gateway again"


def test_a_failed_order_is_also_served_from_cache(tmp_path):
    db = tmp_path / "orders.db"
    gw = FakeGateway()
    first = create_order("quote_failedorder", 100000, gateway=gw, db_path=db)
    update_order_status(first.order_id, "failed", payment_id="pay_no", db_path=db)

    again = create_order("quote_failedorder", 100000, gateway=gw, db_path=db)
    assert again.order_id == first.order_id
    assert gw.calls == 1, "a retry pays the existing order, it does not make a new one"
