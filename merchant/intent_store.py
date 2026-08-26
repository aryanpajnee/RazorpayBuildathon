"""SQLite-backed persistence for already-verified Intent Mandates.

An Intent Mandate is verified cryptographically exactly once, at the moment
the user grants it (`core.mandate.verify`) — never re-verified against
Ed25519 on every cart that cites it. What this store holds is the *result*
of that one verification: the trusted payload, plus how many purchases it
has already produced. `merchant/gate.py` reads both on every cart, but the
lookup here is a local read against a trusted record, not a second
signature check.

This is vault code: no LLM, no clock-dependent branching. Every function
opens its own connection, commits, and closes — no long-lived connection is
held across calls, so this is safe to hit from multiple processes (the
mandate-grant endpoint, the Gate, tests) without coordinating a connection
pool. Same pattern as `merchant/quote_store.py`.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import config
from core.mandate import canonical

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS intents (
    mandate_id TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    purchases_used INTEGER NOT NULL DEFAULT 0
)
"""


def _connect(db_path: Path | None) -> sqlite3.Connection:
    path = db_path or config.INTENTS_DB
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(_CREATE_TABLE_SQL)
    return conn


def register_intent(intent_payload: dict, *, db_path: Path | None = None) -> None:
    """Store an already-verified intent payload, keyed by its mandate_id.

    Routes through `core.mandate.canonical` rather than `json.dumps` directly
    — this is the project's one serialiser, and re-implementing a second one
    here is exactly the kind of drift that eventually produces two different
    byte strings for what a human reads as the same payload.

    Granted once, immutable: a mandate_id is registered on first sight and
    every later registration of the same mandate_id is a silent no-op — the
    original row, payload AND `purchases_used` counter alike, is left
    untouched. An Intent Mandate is a one-time signed grant; a genuine new
    grant always carries a fresh uuid `mandate_id`, so a repeat id is not a
    legitimate re-grant to honour. Two things depend on that being ignored
    rather than applied:

    - `purchases_used` backs the Gate's `max_purchases` cap. If a repeat
      registration reset it to its DEFAULT 0, replaying the same mandate_id
      through the registration endpoint would silently refill the buyer's
      purchase allowance — a `max_purchases` bypass.
    - The stored payload backs every other Gate limit (`max_paise`, expiry,
      allowed items, ...). If a repeat registration overwrote it, a second
      submission under the same mandate_id could swap in a payload claiming
      a higher `max_paise` than the one actually signed and granted.
    """
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
    """Look up a registered intent payload by mandate_id. None if absent."""
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT payload_json FROM intents WHERE mandate_id = ?",
            (mandate_id,),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return None
    return json.loads(row[0])


def purchases_used(mandate_id: str, *, db_path: Path | None = None) -> int:
    """How many purchases this intent has already produced. 0 if unknown."""
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT purchases_used FROM intents WHERE mandate_id = ?",
            (mandate_id,),
        ).fetchone()
    finally:
        conn.close()

    return row[0] if row is not None else 0


def record_purchase(mandate_id: str, *, db_path: Path | None = None) -> None:
    """Increment purchases_used by 1 for an already-registered intent.

    Silently a no-op if the mandate_id isn't registered — callers (the Gate)
    only ever call this after `get_intent` has already confirmed the row
    exists, so this is not expected to fire on an unknown id in practice.
    """
    conn = _connect(db_path)
    try:
        conn.execute(
            "UPDATE intents SET purchases_used = purchases_used + 1 WHERE mandate_id = ?",
            (mandate_id,),
        )
        conn.commit()
    finally:
        conn.close()
