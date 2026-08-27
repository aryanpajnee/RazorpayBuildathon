"""Razorpay order creation, made idempotent by a local SQLite mapping of
quote_id -> order_id.

`quote_id` is the idempotency key everywhere in this system: retries,
recovery-agent decisions and webhook replays all resolve through it. This is
the module that owns the anchor row those resolutions read.

--- Design: reservation-first, not INSERT-after-the-fact -----------------

The previous version of this module called the gateway *before* trying to
record the result, and used a `UNIQUE(quote_id)` constraint plus an
in-process `threading.Lock` to catch collisions after the fact. That closes
"two rows for one quote_id" but not "two orders for one quote_id": the
in-process lock does nothing across processes (two uvicorn workers), so two
racing callers can both pass the "not on file yet" check, both call
Razorpay, and only then discover -- via the UNIQUE constraint -- that one of
their two *already-created* real orders has to be discarded. The discarded
order is not cancelled, just orphaned. See FAILURES.md, "Idempotency that
stops a second row, not a second order."

This version flips the order of operations so the *claim* happens before
the *call*, and makes the claim itself atomic:

1. A caller first tries to INSERT a `status='pending'`, `order_id=NULL` row
   for quote_id, inside a `BEGIN IMMEDIATE` transaction. SQLite's IMMEDIATE
   lock is what actually excludes a second writer -- across threads *and*
   across processes sharing the same database file, which the old
   `threading.Lock` never could. Only the caller whose INSERT lands may ever
   call the gateway for that quote_id.
2. A caller that loses the claim (finds an existing row) never calls the
   gateway. If the row is already `status='created'`, its order is returned
   (`from_cache=True`, with the original AmountMismatchError check). If the
   row is still `status='pending'`, the loser polls -- briefly, without
   holding any lock while it waits -- for the owner to finish.
3. If nobody finishes the reservation within `_PENDING_WAIT_TIMEOUT_SECONDS`,
   a waiting caller may *reclaim* it: an atomic `pending -> reclaiming`
   UPDATE that only one simultaneous reclaimer can win, so a reservation
   whose owner crashed between steps 1 and the final write-back is
   recoverable without letting two reclaimers both call the gateway.
4. Only the exclusive owner (fresh claimant or successful reclaimer) calls
   `gateway.create_order()`. Any failure -- gateway exception, an amount the
   gateway didn't actually confirm, or a garbage order id -- deletes the
   reservation so a retry claims a clean slate, per the promise in
   `OrderCreationError`'s docstring below.
5. The owner then `UPDATE`s the same row to `status='created'` with the real
   order_id. This is the only write that ever makes a row look "done", and
   it is the row every other caller (including `find_by_order_id`) reads.

**Honest residual window.** This closes the race for any reservation whose
owner is still alive and working -- which is the actual cross-process race
described in FAILURES.md. It does not (and structurally cannot, without a
second system of record) fully close the much narrower window where a
caller's process dies *after* the gateway confirms an order but *before*
the finalizing UPDATE commits: a subsequent reclaim of that abandoned
reservation will call the gateway again and create a second real order.
This window requires two independent failures (a crash, landing in that
exact few-millisecond gap) rather than ordinary concurrency, and a
single-process demo cannot hit it -- but it is not claimed to be closed.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import config

ORDERS_DB = config.ORDERS_DB

_ORDER_COLUMNS = "quote_id, order_id, amount_paise, currency, status, created_at"

_STATUS_PENDING = "pending"
_STATUS_RECLAIMING = "reclaiming"
_STATUS_CREATED = "created"

# How long a caller who finds someone else's still-open reservation will
# poll before concluding it was abandoned (a crash between reserve and
# gateway-return) and attempting to reclaim it. Generous relative to any
# realistic gateway call latency, so a live in-flight reservation is never
# mistaken for an abandoned one and double-called.
_PENDING_WAIT_TIMEOUT_SECONDS = 5.0
_PENDING_POLL_INTERVAL_SECONDS = 0.02


class GatewayError(Exception):
    """Base for anything this module refuses or the payment gateway rejects."""


class OrderCreationError(GatewayError):
    """The gateway declined or failed to create an order, or returned a
    response too broken to trust (e.g. no usable order id).

    Never retried silently: the caller decides whether to retry, and a retry
    with the same quote_id is always safe because of the idempotency
    contract above -- a failed attempt leaves no row behind to collide with.
    """


class GatewayAmountConfirmationError(GatewayError):
    """The gateway's response confirmed a different amount than the one this
    module asked it to charge. This should never happen against a correctly
    behaving gateway, which is exactly why it is not silently swallowed by
    recording either number as if nothing happened -- a divergence here means
    the order this module is about to treat as "created for amount_paise" may
    not be the order Razorpay actually created."""

    def __init__(self, quote_id: str, requested_amount_paise: int, confirmed_amount_paise) -> None:
        self.quote_id = quote_id
        self.requested_amount_paise = requested_amount_paise
        self.confirmed_amount_paise = confirmed_amount_paise
        super().__init__(
            f"quote_id={quote_id}: gateway confirmed amount={confirmed_amount_paise!r} "
            f"but amount_paise={requested_amount_paise} was requested"
        )


class OrderNotFoundError(GatewayError):
    """update_order_status was asked to update an order_id this merchant has
    no record of. Nothing to write back to -- raised rather than silently
    doing nothing, since a caller updating a status is relying on it having
    taken effect."""


class UnexpectedIntegrityError(GatewayError):
    """The reservation INSERT raised IntegrityError even though the SELECT
    earlier in the *same* BEGIN IMMEDIATE transaction found no row for this
    quote_id. Under IMMEDIATE locking that combination should be impossible
    for a plain quote_id collision -- nothing else can write between our own
    SELECT and INSERT. Kept as a named, loud failure rather than silently
    treated as "some other process won the race", because if this ever
    fires it means a NOT NULL/CHECK violation or a locking assumption this
    module makes turned out to be wrong -- not a race being handled."""


class AmountMismatchError(GatewayError):
    """The same quote_id arrived again with a different amount than the one
    already on file (whatever status that row is in). Idempotency means
    "same quote_id, same amount -> same order"; a differing amount is not a
    cache hit to serve quietly, it is a sign that something upstream (a
    stale retry, a recovery-agent bug, a forged replay) is trying to attach
    a new price to an old quote_id. This is raised, not swallowed, so a
    recovery agent can see exactly what the two amounts were and decide
    what to do."""

    def __init__(self, quote_id: str, requested_amount_paise: int, recorded_amount_paise: int) -> None:
        self.quote_id = quote_id
        self.requested_amount_paise = requested_amount_paise
        self.recorded_amount_paise = recorded_amount_paise
        super().__init__(
            f"quote_id={quote_id} was already recorded with amount_paise="
            f"{recorded_amount_paise}, but this call requested amount_paise="
            f"{requested_amount_paise}"
        )


@dataclass(frozen=True, slots=True)
class Order:
    """A merchant-side record of a Razorpay order. Frozen for the same reason
    quote.py's Totals is frozen: nothing downstream should be able to edit a
    total, or here, an order, after the fact."""

    order_id: str
    quote_id: str
    amount_paise: int
    currency: str
    status: str
    created_at: str
    from_cache: bool

    def as_dict(self) -> dict:
        return {
            "order_id": self.order_id,
            "quote_id": self.quote_id,
            "amount_paise": self.amount_paise,
            "currency": self.currency,
            "status": self.status,
            "created_at": self.created_at,
            "from_cache": self.from_cache,
        }


def _require_amount(amount_paise: object) -> int:
    """Same discipline as quote.py's _require_int: bool is an int subclass in
    Python, so `isinstance` would let `True` sail through as 1 paise."""
    if type(amount_paise) is not int:
        raise TypeError(
            f"amount_paise must be an int paise value, got {type(amount_paise).__name__}"
        )
    if amount_paise <= 0:
        raise ValueError(f"amount_paise must be positive, got {amount_paise}")
    return amount_paise


class FakeGateway:
    """In-process stand-in for Razorpay. Used automatically whenever
    config.USE_FAKE_GATEWAY is True, and can be injected explicitly so tests
    never depend on env state. Deterministic and sequential -- order ids are
    assigned by an internal counter, not randomness -- so assertions on
    exact ids are possible and `.calls` lets a test prove the gateway was
    (or was not) hit a second time.
    """

    def __init__(self) -> None:
        self.calls = 0

    def create_order(self, amount_paise: int, currency: str, receipt: str, notes: dict) -> dict:
        self.calls += 1
        return {
            "id": f"order_fake{self.calls:06d}",
            "amount": amount_paise,
            "currency": currency,
            "receipt": receipt,
            "status": "created",
            "notes": notes,
        }


class RazorpayGateway:
    """Thin wrapper over the real Razorpay SDK client. Constructed lazily
    (only when actually used) so importing this module never requires valid
    keys -- FakeGateway-only test runs and USE_FAKE_GATEWAY=True production
    runs both work without a razorpay.Client ever being built."""

    def __init__(self) -> None:
        import razorpay  # imported here, not at module scope, to keep the
        # fake-gateway path free of a hard dependency on network config.

        self._client = razorpay.Client(auth=(config.RAZORPAY_KEY_ID, config.RAZORPAY_KEY_SECRET))

    def create_order(self, amount_paise: int, currency: str, receipt: str, notes: dict) -> dict:
        return self._client.order.create(
            {
                "amount": amount_paise,
                "currency": currency,
                "receipt": receipt,
                "notes": notes or {},
                # payment_capture=1 makes a successful payment auto-capture, so
                # the outcome we react to is `payment.captured`. Without it,
                # capture depends on the account's default; a "manual" default
                # would leave the payment merely `authorized` and fire
                # `payment.authorized` instead -- money held, never taken.
                "payment_capture": 1,
            }
        )


def _default_gateway():
    return FakeGateway() if config.USE_FAKE_GATEWAY else RazorpayGateway()


def _connect(db_path: Path) -> sqlite3.Connection:
    """`isolation_level=None` puts the connection in autocommit mode, which
    is what lets this module issue `BEGIN IMMEDIATE` / `COMMIT` / `ROLLBACK`
    explicitly instead of relying on sqlite3's implicit transaction
    handling -- required to control exactly when the IMMEDIATE write lock is
    taken and released. `order_id` is nullable: a `pending` or `reclaiming`
    reservation has no order_id yet, only a `created` row does.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS orders (
            quote_id TEXT PRIMARY KEY,
            order_id TEXT,
            amount_paise INTEGER NOT NULL,
            currency TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    return conn


def _row_to_order(row: tuple, *, from_cache: bool) -> Order:
    quote_id, order_id, amount_paise, currency, status, created_at = row
    return Order(
        order_id=order_id,
        quote_id=quote_id,
        amount_paise=amount_paise,
        currency=currency,
        status=status,
        created_at=created_at,
        from_cache=from_cache,
    )


def _select(conn: sqlite3.Connection, quote_id: str) -> tuple | None:
    return conn.execute(
        f"SELECT {_ORDER_COLUMNS} FROM orders WHERE quote_id = ?", (quote_id,)
    ).fetchone()


def _select_by_order_id(conn: sqlite3.Connection, order_id: str) -> tuple | None:
    # No extra status filter needed: a pending/reclaiming row has
    # order_id IS NULL, and NULL never equals a bound non-null parameter in
    # SQL, so this already can't return a bare reservation.
    return conn.execute(
        f"SELECT {_ORDER_COLUMNS} FROM orders WHERE order_id = ?", (order_id,)
    ).fetchone()


def _try_claim_or_read(conn: sqlite3.Connection, quote_id: str, amount_paise: int) -> tuple[str, Order | None]:
    """One atomic look-then-act attempt, run inside its own BEGIN IMMEDIATE
    transaction so it is safe across processes, not just threads.

    Returns ("own", None) if this call just created a brand-new pending
    reservation -- it must now call the gateway. Returns ("cached", Order)
    if a finished order was already on file. Returns ("pending", None) if
    some other caller's reservation -- fresh or mid-reclaim -- is still in
    flight. Raises AmountMismatchError if quote_id is on file under a
    different amount, whatever status that row is in.
    """
    conn.execute("BEGIN IMMEDIATE")
    row = _select(conn, quote_id)
    if row is not None:
        existing = _row_to_order(row, from_cache=True)
        conn.execute("ROLLBACK")  # read-only so far; release the lock immediately
        if existing.amount_paise != amount_paise:
            raise AmountMismatchError(quote_id, amount_paise, existing.amount_paise)
        if existing.status == _STATUS_CREATED and existing.order_id:
            return "cached", existing
        return "pending", None

    created_at = datetime.now(timezone.utc).isoformat()
    try:
        conn.execute(
            "INSERT INTO orders (quote_id, order_id, amount_paise, currency, status, created_at) "
            "VALUES (?, NULL, ?, ?, ?, ?)",
            (quote_id, amount_paise, config.CURRENCY, _STATUS_PENDING, created_at),
        )
    except sqlite3.IntegrityError as exc:
        conn.execute("ROLLBACK")
        raise UnexpectedIntegrityError(
            f"quote_id={quote_id}: reservation INSERT raised IntegrityError even though "
            f"no row existed inside this same BEGIN IMMEDIATE transaction: {exc}"
        ) from exc
    conn.execute("COMMIT")
    return "own", None


def _try_reclaim(conn: sqlite3.Connection, quote_id: str, amount_paise: int) -> tuple[str, Order | None]:
    """Attempt to take over a reservation nobody has finished within the
    wait window -- most plausibly a process that reserved quote_id and
    crashed before calling the gateway or writing the result back.

    Atomically flips `pending -> reclaiming` so that if two waiting callers
    both time out on the same abandoned reservation at once, only one wins
    the flip and goes on to call the gateway; the other is told "pending"
    and loops back to wait again (see the module docstring's residual
    window for the one case this still cannot fully close).
    """
    conn.execute("BEGIN IMMEDIATE")
    row = _select(conn, quote_id)
    if row is None:
        conn.execute("ROLLBACK")
        return "pending", None
    existing = _row_to_order(row, from_cache=True)
    if existing.amount_paise != amount_paise:
        conn.execute("ROLLBACK")
        raise AmountMismatchError(quote_id, amount_paise, existing.amount_paise)
    if existing.status == _STATUS_CREATED and existing.order_id:
        conn.execute("ROLLBACK")
        return "cached", existing
    if existing.status != _STATUS_PENDING:
        conn.execute("ROLLBACK")
        return "pending", None
    conn.execute(
        "UPDATE orders SET status = ? WHERE quote_id = ? AND status = ?",
        (_STATUS_RECLAIMING, quote_id, _STATUS_PENDING),
    )
    conn.execute("COMMIT")
    return "own", None


def _acquire_ownership(db_path: Path, quote_id: str, amount_paise: int) -> Order | None:
    """Block until this call either (a) exclusively owns quote_id and must
    call the gateway, or (b) can serve a finished order from the store.
    Returns None for (a); returns the Order for (b)."""
    deadline = time.monotonic() + _PENDING_WAIT_TIMEOUT_SECONDS
    while True:
        conn = _connect(db_path)
        try:
            outcome, payload = _try_claim_or_read(conn, quote_id, amount_paise)
        finally:
            conn.close()

        if outcome == "cached":
            return payload
        if outcome == "own":
            return None

        # outcome == "pending": someone else's reservation is in flight (or
        # was, and its owner is gone). Poll without holding any lock while
        # waiting -- the owner needs to be able to take the write lock back
        # for its own finalize step.
        if time.monotonic() < deadline:
            time.sleep(_PENDING_POLL_INTERVAL_SECONDS)
            continue

        conn = _connect(db_path)
        try:
            outcome, payload = _try_reclaim(conn, quote_id, amount_paise)
        finally:
            conn.close()
        if outcome == "cached":
            return payload
        if outcome == "own":
            return None
        # Someone else reclaimed it first, or the race repeated -- give
        # ourselves one more wait window rather than spinning forever.
        deadline = time.monotonic() + _PENDING_WAIT_TIMEOUT_SECONDS


def _finalize(
    db_path: Path,
    quote_id: str,
    order_id: str,
    amount_paise: int,
    currency: str,
    status: str,
    created_at: str,
) -> None:
    """Write the gateway's confirmed result back to the row this call
    exclusively owns, marking it `created`. This is the only write that
    ever makes quote_id look "done" to every other reader."""
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE orders SET order_id = ?, currency = ?, status = ?, created_at = ? "
            "WHERE quote_id = ?",
            (order_id, currency, status, created_at, quote_id),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()


def _reset_after_failure(db_path: Path, quote_id: str) -> None:
    """Undo a reservation this call exclusively owned but could not finish
    -- the gateway declined, errored, or returned something too broken to
    trust. Deletes the row outright (rather than leaving a stale reservation
    behind) so a subsequent retry with the same quote_id claims a
    completely fresh reservation instead of entering the wait/reclaim dance
    against its own abandoned attempt."""
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "DELETE FROM orders WHERE quote_id = ? AND status IN (?, ?)",
            (quote_id, _STATUS_PENDING, _STATUS_RECLAIMING),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()


def create_order(
    quote_id: str,
    amount_paise: int,
    notes: dict | None = None,
    *,
    gateway=None,
    db_path: Path | None = None,
) -> Order:
    """Create a Razorpay order for `quote_id`, or return the one already on
    file. `quote_id` is used as the Razorpay receipt, and is the only key
    this function trusts for idempotency -- amount, notes and everything
    else describe the order but never identify it.

    Reservation-first: the quote_id is claimed in the database, atomically,
    before the gateway is ever called, so only one caller -- in this process
    or another -- can ever reach the gateway for a given quote_id. See the
    module docstring for the full design and its one honest residual gap.
    """
    _require_amount(amount_paise)

    db_path = db_path or ORDERS_DB
    gateway = gateway or _default_gateway()

    cached = _acquire_ownership(db_path, quote_id, amount_paise)
    if cached is not None:
        return cached

    # We now exclusively own quote_id -- either as a fresh reservation or by
    # reclaiming one whose original owner appears to have crashed. From here
    # on, only this call may reach the gateway for quote_id.
    try:
        raw = gateway.create_order(amount_paise, config.CURRENCY, quote_id, notes or {})
    except Exception as exc:
        _reset_after_failure(db_path, quote_id)
        raise OrderCreationError(
            f"order creation failed for quote_id={quote_id}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    # Record what the gateway actually confirmed, not what was asked for --
    # the two are expected to agree, but if they ever don't, that
    # disagreement is the important fact and must not be lost by quietly
    # storing the locally requested figure. A confirmed amount that isn't
    # even an int, or an order id that isn't a real string, is treated the
    # same way: too untrustworthy to record.
    confirmed_amount = raw.get("amount")
    order_id = raw.get("id")
    if type(confirmed_amount) is not int or confirmed_amount != amount_paise:
        _reset_after_failure(db_path, quote_id)
        raise GatewayAmountConfirmationError(quote_id, amount_paise, confirmed_amount)
    if not isinstance(order_id, str) or not order_id:
        _reset_after_failure(db_path, quote_id)
        raise OrderCreationError(
            f"gateway returned an invalid order id for quote_id={quote_id}: {order_id!r}"
        )

    created_at = datetime.now(timezone.utc).isoformat()
    currency = raw.get("currency", config.CURRENCY)
    status = raw.get("status", _STATUS_CREATED)
    _finalize(db_path, quote_id, order_id, confirmed_amount, currency, status, created_at)

    return Order(
        order_id=order_id,
        quote_id=quote_id,
        amount_paise=confirmed_amount,
        currency=currency,
        status=status,
        created_at=created_at,
        from_cache=False,
    )


def find_by_order_id(order_id: str, db_path: Path | None = None) -> Order | None:
    """Reverse lookup from a Razorpay order_id back to the quote_id that
    created it. Read-only: webhooks.py uses this to recover quote_id from a
    webhook payload without ever trusting the payload's own claim about it.
    Never returns a bare pending/reclaiming reservation -- those rows have
    no order_id to be looked up by.
    """
    db_path = db_path or ORDERS_DB
    conn = _connect(db_path)
    try:
        row = _select_by_order_id(conn, order_id)
        return None if row is None else _row_to_order(row, from_cache=True)
    finally:
        conn.close()


def update_order_status(order_id: str, status: str, *, db_path: Path | None = None) -> Order:
    """Write a new status back to the order this merchant created.

    This module owns the `orders` table -- webhooks.py must never write to
    it directly, only call this. A single UPDATE is already atomic in
    SQLite, so this does not need any extra locking beyond that.
    """
    db_path = db_path or ORDERS_DB
    conn = _connect(db_path)
    try:
        cursor = conn.execute(
            "UPDATE orders SET status = ? WHERE order_id = ?", (status, order_id)
        )
        if cursor.rowcount == 0:
            raise OrderNotFoundError(f"no order on file for order_id={order_id}")
        row = _select_by_order_id(conn, order_id)
        return _row_to_order(row, from_cache=True)
    finally:
        conn.close()
