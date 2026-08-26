"""The hash-chained, append-only audit ledger.

Every meaningful event in the canonical flow -- `AI Buyer -> Catalog -> Quote
-> Mandate -> GATE -> Razorpay -> Webhook -> Ledger` -- gets one row here.
Each row's hash covers the previous row's hash, so the rows form a chain:
editing any historical row changes what that row *should* hash to, which no
longer matches what the next row was told to expect. `verify_chain()` is what
catches that and says exactly where it started.

This file answers exactly one question: "given everything appended so far, is
it still exactly what was appended, in the order it was appended?" It does
not know what a `gate.refused` event *means* -- that is `merchant/gate.py`'s
job. It knows only whether the row that says `gate.refused` is the row that
was written, and whether it comes after the row it claims to come after.

Append-only on purpose: the only write operation exposed is `append()`. There
is no `update()` or `delete()` in the public API, deliberately -- not because
SQLite can't (the tamper demo in the spec's S10 uses raw SQL to prove exactly
that), but because nothing in this codebase should ever call it.

Spec: docs/specs/ledger-spec.md
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from core.mandate import canonical

import config

# --- constants ---------------------------------------------------------------

# The first row (seq=1) needs a prev_hash to point at, and there is no row 0.
# A fixed 64-char sentinel keeps `prev_hash` uniformly "64 lowercase hex
# chars, always" -- no `Optional[str]` or `if seq == 1` special case anywhere
# that touches this column.
GENESIS_HASH = "0" * 64

# Ten event types, derived directly from the seven arrows of the canonical
# flow. Not business logic about what an event *means* -- schema discipline
# on what may be written at all, same kind of guard as a NOT NULL column.
VALID_EVENT_TYPES = frozenset({
    "quote.issued",
    "mandate.verified",
    "mandate.rejected",
    "gate.passed",
    "gate.refused",
    "payment.attempted",
    "payment.succeeded",
    "payment.failed",
    "webhook.received",
    "order.created",
})

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ledger (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    event_type  TEXT    NOT NULL,
    payload     TEXT    NOT NULL,
    prev_hash   TEXT    NOT NULL,
    entry_hash  TEXT    NOT NULL UNIQUE
);
"""


# --- data types ----------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class LedgerEntry:
    seq: int
    ts: int
    event_type: str
    payload: dict
    prev_hash: str
    entry_hash: str


@dataclass(frozen=True, slots=True)
class ChainStatus:
    ok: bool
    entries_checked: int
    first_broken_seq: int | None   # None iff ok is True
    detail: str                    # human-readable, e.g. "entry_hash mismatch at seq=2"


class LedgerError(Exception):
    """Base for anything the ledger itself cannot guarantee."""


class UnknownEventType(LedgerError):
    def __init__(self, event_type: str) -> None:
        super().__init__(f"not a recognised event_type: {event_type!r}")
        self.event_type = event_type


class EntryNotFound(LedgerError):
    def __init__(self, seq: int) -> None:
        super().__init__(f"no ledger entry with seq={seq}")
        self.seq = seq


# --- internals -----------------------------------------------------------------

def _resolve_path(db_path: Path | None) -> Path:
    return db_path or config.LEDGER_DB


def _connect(path: Path) -> sqlite3.Connection:
    """Open a connection with the schema ensured and manual transaction control.

    isolation_level=None puts the connection in autocommit mode so that
    explicit "BEGIN IMMEDIATE" / "COMMIT" statements (needed by append() to
    make latest()+INSERT atomic) behave as real transaction boundaries rather
    than being swallowed by sqlite3's own implicit-transaction handling.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.execute(_SCHEMA_SQL)
    return conn


def _compute_entry_hash(seq: int, ts: int, event_type: str, payload: dict, prev_hash: str) -> str:
    """The one hashing formula. payload is embedded as a native nested dict,
    not pre-serialised -- a single canonical() call sorts the record's keys
    and the payload's keys together, in the same pass."""
    record = {
        "seq": seq,
        "ts": ts,
        "event_type": event_type,
        "payload": payload,
        "prev_hash": prev_hash,
    }
    return hashlib.sha256(canonical(record)).hexdigest()


def _row_to_entry(row: tuple) -> LedgerEntry:
    seq, ts, event_type, payload_text, prev_hash, entry_hash = row
    # The payload column is a human-legible mirror of what got hashed, not a
    # second source of truth -- parse it back to a dict before doing anything
    # with it, never string-compare the raw column.
    payload = json.loads(payload_text)
    return LedgerEntry(
        seq=seq,
        ts=ts,
        event_type=event_type,
        payload=payload,
        prev_hash=prev_hash,
        entry_hash=entry_hash,
    )


_SELECT_COLUMNS = "seq, ts, event_type, payload, prev_hash, entry_hash"


# --- writing -------------------------------------------------------------------

def append(event_type: str, payload: dict, *, db_path: Path | None = None) -> LedgerEntry:
    """Append one event. Stamps ts itself; assigns seq itself.

    Raises UnknownEventType if event_type is not in VALID_EVENT_TYPES.
    Raises LedgerError if payload cannot be canonicalised (propagated from
    canonical(), e.g. a float in a money-shaped field, a non-JSON type).
    A call that returns has been durably committed and chained -- there is
    no separate 'flush' step.
    """
    if event_type not in VALID_EVENT_TYPES:
        raise UnknownEventType(event_type)

    try:
        payload_text = canonical(payload).decode("utf-8")
    except TypeError as exc:
        raise LedgerError(f"payload cannot be canonicalised: {exc}") from exc

    ts = int(time.time())
    path = _resolve_path(db_path)
    conn = _connect(path)
    try:
        # BEGIN IMMEDIATE grabs the write lock up front, so no other
        # connection can read the same latest() row and race us to append a
        # conflicting entry before we COMMIT.
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM ledger ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        if row is None:
            seq = 1
            prev_hash = GENESIS_HASH
        else:
            prev_hash = row[5]  # previous row's entry_hash
            seq = row[0] + 1

        entry_hash = _compute_entry_hash(seq, ts, event_type, payload, prev_hash)

        conn.execute(
            "INSERT INTO ledger (seq, ts, event_type, payload, prev_hash, entry_hash) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (seq, ts, event_type, payload_text, prev_hash, entry_hash),
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()

    return LedgerEntry(
        seq=seq,
        ts=ts,
        event_type=event_type,
        payload=payload,
        prev_hash=prev_hash,
        entry_hash=entry_hash,
    )


# --- reading -------------------------------------------------------------------

def get_entry(seq: int, *, db_path: Path | None = None) -> LedgerEntry:
    """Raises EntryNotFound if seq does not exist."""
    path = _resolve_path(db_path)
    conn = _connect(path)
    try:
        row = conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM ledger WHERE seq = ?", (seq,)
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        raise EntryNotFound(seq)
    return _row_to_entry(row)


def all_entries(*, db_path: Path | None = None) -> list[LedgerEntry]:
    """Every row, seq ascending. Fine at demo scale; do not reach for this
    in a hot path -- it's a full table scan."""
    path = _resolve_path(db_path)
    conn = _connect(path)
    try:
        rows = conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM ledger ORDER BY seq ASC"
        ).fetchall()
    finally:
        conn.close()

    return [_row_to_entry(row) for row in rows]


def latest(*, db_path: Path | None = None) -> LedgerEntry | None:
    """The last row, or None if the ledger is empty. append() uses this
    internally to find the prev_hash for the next entry."""
    path = _resolve_path(db_path)
    conn = _connect(path)
    try:
        row = conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM ledger ORDER BY seq DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return None
    return _row_to_entry(row)


# --- verification ----------------------------------------------------------

def verify_chain(*, db_path: Path | None = None) -> ChainStatus:
    """Walk the chain from seq=1, recomputing each entry_hash and checking
    each prev_hash against the previous entry's freshly recomputed hash (not
    its stored entry_hash column -- a stored hash is a cache, not a source of
    truth). Stops at the first row that fails either check and reports its
    seq. Does not raise -- a broken chain is a successful detection, not a
    malfunction of this function.

    Re-queries SQLite itself (via all_entries()) on every call rather than
    accepting a cached list, so a live tamper made between calls is always
    caught.
    """
    entries = all_entries(db_path=db_path)

    expected_prev = GENESIS_HASH
    checked = 0
    for entry in entries:
        checked += 1

        if entry.prev_hash != expected_prev:
            return ChainStatus(
                ok=False,
                entries_checked=checked,
                first_broken_seq=entry.seq,
                detail=f"prev_hash mismatch at seq={entry.seq}",
            )

        recomputed = _compute_entry_hash(
            entry.seq, entry.ts, entry.event_type, entry.payload, entry.prev_hash
        )
        if recomputed != entry.entry_hash:
            return ChainStatus(
                ok=False,
                entries_checked=checked,
                first_broken_seq=entry.seq,
                detail=f"entry_hash mismatch at seq={entry.seq}",
            )

        # Carry the freshly recomputed hash forward as what the next row's
        # prev_hash must equal -- not the stored entry_hash column. This is
        # what makes editing a row's content (without also updating its own
        # entry_hash column) still get caught here, not just via the row's
        # own self-check above.
        expected_prev = recomputed

    return ChainStatus(ok=True, entries_checked=checked, first_broken_seq=None, detail="chain intact")
