"""SQLite-backed persistence for issued Quotes.

`merchant/quote.py:create_quote` returns a `Quote` but nothing stores it. The
Gate looks a quote up by `quote_id` and re-derives its price over the STORED
line items — that's the whole reason this store persists full line items
(sku, name, unit_paise, qty), not just the summary totals. If the Gate only
had the totals to check against, a buyer could sign a mandate over a total
that happens to match without the underlying cart ever having been quoted
that way.

This is vault code: no LLM, no network, no clock-dependent branching. Every
function opens its own connection, commits, and closes — no long-lived
connection is held across calls, so the store is safe to hit from multiple
processes (Gate, quote endpoint, tests) without coordinating a connection
pool.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import config
from core.mandate import canonical
from merchant.quote import LineItem, Quote, Totals

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS quotes (
    quote_id TEXT PRIMARY KEY,
    merchant_id TEXT,
    currency TEXT,
    cart_hash TEXT,
    issued_at INTEGER,
    expires_at INTEGER,
    subtotal_paise INTEGER,
    shipping_paise INTEGER,
    taxable_paise INTEGER,
    gst_paise INTEGER,
    total_paise INTEGER,
    lines_json TEXT
)
"""


def _connect(db_path: Path | None) -> sqlite3.Connection:
    path = db_path or config.QUOTES_DB
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(_CREATE_TABLE_SQL)
    return conn


def _lines_to_json(lines: tuple[LineItem, ...]) -> str:
    """Serialise line items through the project's one canonical serialiser.

    `line_paise` is deliberately excluded: it's derived (unit_paise * qty)
    and `LineItem.__init__` has no such field, so storing it would just have
    to be stripped again on the way back in.
    """
    payload = [
        {"sku": item.sku, "name": item.name, "unit_paise": item.unit_paise, "qty": item.qty}
        for item in lines
    ]
    return canonical(payload).decode("utf-8")


def _json_to_lines(lines_json: str) -> tuple[LineItem, ...]:
    return tuple(LineItem(**d) for d in json.loads(lines_json))


def save_quote(quote: Quote, *, db_path: Path | None = None) -> None:
    """Persist a quote. Idempotent: re-saving the same quote_id overwrites it."""
    conn = _connect(db_path)
    try:
        conn.execute(
            """
            INSERT OR REPLACE INTO quotes (
                quote_id, merchant_id, currency, cart_hash, issued_at, expires_at,
                subtotal_paise, shipping_paise, taxable_paise, gst_paise, total_paise,
                lines_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                quote.quote_id,
                quote.merchant_id,
                quote.currency,
                quote.cart_hash,
                quote.issued_at,
                quote.expires_at,
                quote.totals.subtotal_paise,
                quote.totals.shipping_paise,
                quote.totals.taxable_paise,
                quote.totals.gst_paise,
                quote.totals.total_paise,
                _lines_to_json(quote.lines),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_quote(quote_id: str, *, db_path: Path | None = None) -> Quote | None:
    """Look up a quote by id. Returns None if no such quote was ever saved."""
    conn = _connect(db_path)
    try:
        row = conn.execute(
            """
            SELECT merchant_id, currency, cart_hash, issued_at, expires_at,
                   subtotal_paise, shipping_paise, taxable_paise, gst_paise, total_paise,
                   lines_json
            FROM quotes WHERE quote_id = ?
            """,
            (quote_id,),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return None

    (
        merchant_id,
        currency,
        cart_hash,
        issued_at,
        expires_at,
        subtotal_paise,
        shipping_paise,
        taxable_paise,
        gst_paise,
        total_paise,
        lines_json,
    ) = row

    return Quote(
        quote_id=quote_id,
        merchant_id=merchant_id,
        currency=currency,
        lines=_json_to_lines(lines_json),
        totals=Totals(
            subtotal_paise=subtotal_paise,
            shipping_paise=shipping_paise,
            taxable_paise=taxable_paise,
            gst_paise=gst_paise,
            total_paise=total_paise,
        ),
        cart_hash=cart_hash,
        issued_at=issued_at,
        expires_at=expires_at,
    )
