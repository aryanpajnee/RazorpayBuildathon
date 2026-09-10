"""SQLite persistence and atomic accounting for verified Intent Mandates.

The stored payload is the result of the one cryptographic verification at
grant time. Gate checks reserve both one purchase and its amount here in a
single ``BEGIN IMMEDIATE`` transaction, so concurrent workers cannot each
observe the same remaining authority and both spend it.

Reservations have an explicit lifecycle. A successful order commits its
reservation; a definitely side-effect-free/idempotent result may release it.
An uncertain gateway outcome deliberately remains reserved until reconciled.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import config
from core.mandate import canonical

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS intents (
    mandate_id TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    purchases_used INTEGER NOT NULL DEFAULT 0 CHECK (purchases_used >= 0),
    spent_paise INTEGER NOT NULL DEFAULT 0 CHECK (spent_paise >= 0)
)
"""

_CREATE_RESERVATIONS_SQL = """
CREATE TABLE IF NOT EXISTS intent_authority_reservations (
    reservation_id TEXT PRIMARY KEY,
    mandate_id TEXT NOT NULL,
    amount_paise INTEGER NOT NULL CHECK (amount_paise > 0),
    status TEXT NOT NULL CHECK (status IN ('reserved', 'committed', 'released'))
)
"""


@dataclass(frozen=True, slots=True)
class AuthorityReservation:
    reserved: bool
    reason: str | None
    purchases_used: int
    spent_paise: int
    max_purchases: int
    max_paise: int

    @property
    def remaining_paise(self) -> int:
        return max(0, self.max_paise - self.spent_paise)


def _connect(db_path: Path | None) -> sqlite3.Connection:
    path = db_path or config.INTENTS_DB
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.execute("PRAGMA busy_timeout = 10000")

    # Legacy purchases have unknowable amounts. Consume their full signed
    # ceiling rather than silently refilling monetary authority. This backfill
    # runs only when the column is first added, preserving later live values.
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(_CREATE_TABLE_SQL)
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(intents)").fetchall()
        }
        if "spent_paise" not in columns:
            conn.execute(
                "ALTER TABLE intents ADD COLUMN spent_paise INTEGER NOT NULL DEFAULT 0"
            )
            for mandate_id, payload_json in conn.execute(
                "SELECT mandate_id, payload_json FROM intents WHERE purchases_used > 0"
            ).fetchall():
                max_paise = json.loads(payload_json)["max_paise"]
                if type(max_paise) is not int or max_paise <= 0:
                    raise ValueError(f"legacy intent {mandate_id!r} has invalid max_paise")
                conn.execute(
                    "UPDATE intents SET spent_paise = ? WHERE mandate_id = ?",
                    (max_paise, mandate_id),
                )
        conn.execute(_CREATE_RESERVATIONS_SQL)
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise
    return conn


def register_intent(intent_payload: dict, *, db_path: Path | None = None) -> None:
    """Persist a verified grant once; re-registration never refills it."""
    conn = _connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO intents (mandate_id, payload_json)
            VALUES (?, ?)
            ON CONFLICT(mandate_id) DO NOTHING
            """,
            (
                intent_payload["mandate_id"],
                canonical(intent_payload).decode("utf-8"),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_intent(mandate_id: str, *, db_path: Path | None = None) -> dict | None:
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT payload_json FROM intents WHERE mandate_id = ?", (mandate_id,)
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else json.loads(row[0])


def authority_usage(
    mandate_id: str, *, db_path: Path | None = None
) -> tuple[int, int]:
    """Return ``(purchases_used, spent_paise)``; unknown intents read as zero."""
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT purchases_used, spent_paise FROM intents WHERE mandate_id = ?",
            (mandate_id,),
        ).fetchone()
    finally:
        conn.close()
    return (0, 0) if row is None else (row[0], row[1])


def purchases_used(mandate_id: str, *, db_path: Path | None = None) -> int:
    return authority_usage(mandate_id, db_path=db_path)[0]


def spent_paise(mandate_id: str, *, db_path: Path | None = None) -> int:
    return authority_usage(mandate_id, db_path=db_path)[1]


def reserve_authority(
    mandate_id: str,
    reservation_id: str,
    amount_paise: int,
    *,
    db_path: Path | None = None,
) -> AuthorityReservation:
    """Atomically reserve one purchase and cumulative spend.

    The limits come from the stored, verified payload inside the same write
    transaction as the counters. ``reason`` is ``purchase_count`` or
    ``total_spend`` when authority is exhausted.
    """
    if type(amount_paise) is not int:
        raise TypeError("amount_paise must be an int")
    if amount_paise <= 0:
        raise ValueError("amount_paise must be positive")

    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT payload_json, purchases_used, spent_paise FROM intents WHERE mandate_id = ?",
            (mandate_id,),
        ).fetchone()
        if row is None:
            conn.rollback()
            raise KeyError(f"unknown intent mandate {mandate_id!r}")

        payload = json.loads(row[0])
        used, spent = row[1], row[2]
        max_purchases = payload["max_purchases"]
        max_paise = payload["max_paise"]

        existing = conn.execute(
            "SELECT mandate_id, amount_paise, status FROM intent_authority_reservations "
            "WHERE reservation_id = ?",
            (reservation_id,),
        ).fetchone()
        if existing is not None:
            conn.rollback()
            if existing[0] != mandate_id or existing[1] != amount_paise:
                raise ValueError("reservation_id is already bound to different authority")
            return AuthorityReservation(
                reserved=existing[2] in ("reserved", "committed"),
                reason=None if existing[2] in ("reserved", "committed") else "released",
                purchases_used=used,
                spent_paise=spent,
                max_purchases=max_purchases,
                max_paise=max_paise,
            )

        if used >= max_purchases:
            conn.rollback()
            return AuthorityReservation(
                False, "purchase_count", used, spent, max_purchases, max_paise
            )
        if amount_paise > max_paise - spent:
            conn.rollback()
            return AuthorityReservation(
                False, "total_spend", used, spent, max_purchases, max_paise
            )

        updated = conn.execute(
            """
            UPDATE intents
            SET purchases_used = purchases_used + 1,
                spent_paise = spent_paise + ?
            WHERE mandate_id = ?
              AND purchases_used < ?
              AND spent_paise <= ?
            """,
            (amount_paise, mandate_id, max_purchases, max_paise - amount_paise),
        )
        if updated.rowcount != 1:
            conn.rollback()
            raise RuntimeError("intent authority changed during atomic reservation")
        conn.execute(
            "INSERT INTO intent_authority_reservations "
            "(reservation_id, mandate_id, amount_paise, status) VALUES (?, ?, ?, 'reserved')",
            (reservation_id, mandate_id, amount_paise),
        )
        conn.commit()
        return AuthorityReservation(
            True, None, used + 1, spent + amount_paise, max_purchases, max_paise
        )
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def commit_authority(
    reservation_id: str, *, db_path: Path | None = None
) -> bool:
    """Mark a reservation committed. Idempotent; false if absent/released."""
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status FROM intent_authority_reservations WHERE reservation_id = ?",
            (reservation_id,),
        ).fetchone()
        if row is None or row[0] == "released":
            conn.rollback()
            return False
        if row[0] == "reserved":
            conn.execute(
                "UPDATE intent_authority_reservations SET status = 'committed' "
                "WHERE reservation_id = ? AND status = 'reserved'",
                (reservation_id,),
            )
        conn.commit()
        return True
    finally:
        conn.close()


def release_authority(
    reservation_id: str, *, db_path: Path | None = None
) -> bool:
    """Release an uncommitted reservation exactly once.

    A committed reservation cannot be released. Releasing an already released
    reservation succeeds without changing counters again.
    """
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT mandate_id, amount_paise, status "
            "FROM intent_authority_reservations WHERE reservation_id = ?",
            (reservation_id,),
        ).fetchone()
        if row is None or row[2] == "committed":
            conn.rollback()
            return False
        if row[2] == "released":
            conn.rollback()
            return True
        updated = conn.execute(
            "UPDATE intents SET purchases_used = purchases_used - 1, "
            "spent_paise = spent_paise - ? WHERE mandate_id = ?",
            (row[1], row[0]),
        )
        if updated.rowcount != 1:
            conn.rollback()
            raise RuntimeError("reserved authority no longer matches its intent")
        conn.execute(
            "UPDATE intent_authority_reservations SET status = 'released' "
            "WHERE reservation_id = ? AND status = 'reserved'",
            (reservation_id,),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def reservation_status(
    reservation_id: str, *, db_path: Path | None = None
) -> str | None:
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT status FROM intent_authority_reservations WHERE reservation_id = ?",
            (reservation_id,),
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else row[0]


def record_purchase(mandate_id: str, *, db_path: Path | None = None) -> None:
    """Record an amount-unknown legacy purchase conservatively."""
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT payload_json FROM intents WHERE mandate_id = ?", (mandate_id,)
        ).fetchone()
        if row is not None:
            conn.execute(
                "UPDATE intents SET purchases_used = purchases_used + 1, spent_paise = ? "
                "WHERE mandate_id = ?",
                (json.loads(row[0])["max_paise"], mandate_id),
            )
        conn.commit()
    finally:
        conn.close()
