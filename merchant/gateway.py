"""Razorpay order creation, made idempotent by a local SQLite mapping of
quote_id -> order_id.

`quote_id` is the idempotency key everywhere in this system: retries,
recovery-agent decisions and webhook replays all resolve through it. This is
the module that owns the anchor row those resolutions read.

Two layers enforce "one quote_id, one order":

1. An in-process `threading.Lock` per database path serialises the whole
   check-then-act sequence, so two concurrent calls in the same process never
   both reach the gateway.
2. A `UNIQUE` constraint on `quote_id` is the fallback for the case the lock
   cannot cover -- two separate processes (e.g. two uvicorn workers) racing
   the same quote_id. Whichever INSERT loses that race gets `IntegrityError`
   and returns the winner's row instead of its own. This module never treats
   its own gateway call as canonical just because it made it.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import config

ORDERS_DB = config.ORDERS_DB

_ORDER_COLUMNS = "quote_id, order_id, amount_paise, currency, status, created_at"


class GatewayError(Exception):
    """Base for anything this module refuses or the payment gateway rejects."""


class OrderCreationError(GatewayError):
    """The gateway declined or failed to create an order.

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
    """The INSERT raised IntegrityError, but a re-select by quote_id found no
    row. The only IntegrityError this module expects is a UNIQUE collision on
    quote_id, which always leaves a winning row behind to read back; anything
    else (a NOT NULL or CHECK violation, say) leaves nothing to recover and
    must not be treated as "some other process won the race"."""


class AmountMismatchError(GatewayError):
    """The same quote_id arrived again with a different amount than the one
    already on file. Idempotency means "same quote_id, same amount -> same
    order"; a differing amount is not a cache hit to serve quietly, it is a
    sign that something upstream (a stale retry, a recovery-agent bug, a
    forged replay) is trying to attach a new price to an old quote_id. This
    is raised, not swallowed, so a recovery agent can see exactly what the
    two amounts were and decide what to do."""

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
            }
        )


def _default_gateway():
    return FakeGateway() if config.USE_FAKE_GATEWAY else RazorpayGateway()


_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(db_path: Path) -> threading.Lock:
    key = str(db_path)
    with _locks_guard:
        if key not in _locks:
            _locks[key] = threading.Lock()
        return _locks[key]


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS orders (
            quote_id TEXT PRIMARY KEY,
            order_id TEXT NOT NULL,
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
    return conn.execute(
        f"SELECT {_ORDER_COLUMNS} FROM orders WHERE order_id = ?", (order_id,)
    ).fetchone()


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
    """
    _require_amount(amount_paise)

    db_path = db_path or ORDERS_DB
    gateway = gateway or _default_gateway()

    with _lock_for(db_path):
        conn = _connect(db_path)
        try:
            row = _select(conn, quote_id)
            if row is not None:
                cached = _row_to_order(row, from_cache=True)
                if cached.amount_paise != amount_paise:
                    raise AmountMismatchError(quote_id, amount_paise, cached.amount_paise)
                return cached

            # Not on file yet. Ask the gateway to create a real order. Any
            # exception the gateway raises -- SDK error, network failure, a
            # decline -- is wrapped so callers never see a raw SDK exception,
            # and specifically never see RAZORPAY_KEY_SECRET: it is used only
            # to authenticate the client above and is never interpolated into
            # any message this module constructs.
            try:
                raw = gateway.create_order(amount_paise, config.CURRENCY, quote_id, notes or {})
            except Exception as exc:
                raise OrderCreationError(
                    f"order creation failed for quote_id={quote_id}: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc

            # Record what the gateway actually confirmed, not what was asked
            # for -- the two are expected to agree, but if they ever don't,
            # that disagreement is the important fact and must not be lost by
            # quietly storing the locally requested figure. A confirmed
            # amount that isn't even an int is treated the same way: too
            # untrustworthy to record.
            confirmed_amount = raw.get("amount")
            if type(confirmed_amount) is not int or confirmed_amount != amount_paise:
                raise GatewayAmountConfirmationError(quote_id, amount_paise, confirmed_amount)

            created_at = datetime.now(timezone.utc).isoformat()
            try:
                conn.execute(
                    "INSERT INTO orders (quote_id, order_id, amount_paise, currency, status, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        quote_id,
                        raw["id"],
                        confirmed_amount,
                        raw.get("currency", config.CURRENCY),
                        raw.get("status", "created"),
                        created_at,
                    ),
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                # Lost a cross-process race: another worker's INSERT for this
                # quote_id committed first. Their row is the order of record.
                # (This module's own gateway call above already happened and
                # produced a real, orphaned order at Razorpay -- an accepted
                # cost of not serialising the gateway call itself across
                # processes; see FAILURES.md.)
                row = _select(conn, quote_id)
                if row is None:
                    # Not a quote_id collision after all -- some other
                    # constraint failed and there is no winning row to fall
                    # back to. Name the real problem instead of letting the
                    # caller hit an opaque "cannot unpack non-sequence
                    # NoneType" at the exact moment a payment is being made.
                    raise UnexpectedIntegrityError(
                        f"quote_id={quote_id}: INSERT raised IntegrityError but no row "
                        f"exists for this quote_id -- not a quote_id collision: {exc}"
                    ) from exc
                return _row_to_order(row, from_cache=True)

            return _row_to_order(
                (quote_id, raw["id"], confirmed_amount, raw.get("currency", config.CURRENCY),
                 raw.get("status", "created"), created_at),
                from_cache=False,
            )
        finally:
            conn.close()


def find_by_order_id(order_id: str, db_path: Path | None = None) -> Order | None:
    """Reverse lookup from a Razorpay order_id back to the quote_id that
    created it. Read-only: webhooks.py uses this to recover quote_id from a
    webhook payload without ever trusting the payload's own claim about it.
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
    SQLite, so this does not need the per-db-path lock create_order() uses
    for its check-then-act sequence.
    """
    db_path = db_path or ORDERS_DB
    conn = _connect(db_path)
    try:
        cursor = conn.execute(
            "UPDATE orders SET status = ? WHERE order_id = ?", (status, order_id)
        )
        conn.commit()
        if cursor.rowcount == 0:
            raise OrderNotFoundError(f"no order on file for order_id={order_id}")
        row = _select_by_order_id(conn, order_id)
        return _row_to_order(row, from_cache=True)
    finally:
        conn.close()
