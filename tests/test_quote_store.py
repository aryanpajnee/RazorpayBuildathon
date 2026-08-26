"""quote_store is on the money path: the Gate looks a quote up by quote_id
and re-derives its price over the STORED line items. Every test here checks
that what comes back out is field-for-field identical to what went in —
especially the full line items, not just the summary totals.

All DB access is isolated to pytest's tmp_path; nothing here ever touches
data/quotes.db.
"""

from __future__ import annotations

from pathlib import Path

from merchant.catalog import resolve_lines
from merchant.quote import create_quote
from merchant.quote_store import get_quote, save_quote


def db(tmp_path: Path) -> Path:
    return tmp_path / "quotes.db"


def test_save_then_get_round_trips_field_for_field(tmp_path):
    quote = create_quote(resolve_lines([{"sku": "NW-SHOE-001", "qty": 1}]))
    path = db(tmp_path)

    save_quote(quote, db_path=path)
    fetched = get_quote(quote.quote_id, db_path=path)

    assert fetched is not None
    assert fetched.quote_id == quote.quote_id
    assert fetched.merchant_id == quote.merchant_id
    assert fetched.currency == quote.currency
    assert fetched.cart_hash == quote.cart_hash
    assert fetched.issued_at == quote.issued_at
    assert fetched.expires_at == quote.expires_at
    assert fetched.totals.subtotal_paise == quote.totals.subtotal_paise
    assert fetched.totals.shipping_paise == quote.totals.shipping_paise
    assert fetched.totals.taxable_paise == quote.totals.taxable_paise
    assert fetched.totals.gst_paise == quote.totals.gst_paise
    assert fetched.totals.total_paise == quote.totals.total_paise
    assert fetched.lines == quote.lines


def test_get_unknown_quote_id_returns_none(tmp_path):
    path = db(tmp_path)
    # Must create the table (via a save) before probing for absence, since
    # save_quote and get_quote each open their own connection independently.
    quote = create_quote(resolve_lines([{"sku": "NW-SOCK-001", "qty": 1}]))
    save_quote(quote, db_path=path)

    assert get_quote("qt_does_not_exist", db_path=path) is None


def test_get_on_a_fresh_db_with_no_saves_returns_none(tmp_path):
    path = db(tmp_path)
    assert get_quote("qt_nope", db_path=path) is None


def test_line_items_preserved_exactly(tmp_path):
    quote = create_quote(
        resolve_lines([{"sku": "NW-SHOE-001", "qty": 2}, {"sku": "NW-SHOE-002", "qty": 1}])
    )
    path = db(tmp_path)
    save_quote(quote, db_path=path)
    fetched = get_quote(quote.quote_id, db_path=path)

    assert len(fetched.lines) == len(quote.lines)
    for original, restored in zip(quote.lines, fetched.lines):
        assert restored.sku == original.sku
        assert restored.name == original.name
        assert restored.unit_paise == original.unit_paise
        assert restored.qty == original.qty
        assert restored.line_paise == original.unit_paise * original.qty


def test_total_paise_and_cart_hash_preserved_exactly(tmp_path):
    quote = create_quote(resolve_lines([{"sku": "NW-SHOE-003", "qty": 3}]))
    path = db(tmp_path)
    save_quote(quote, db_path=path)
    fetched = get_quote(quote.quote_id, db_path=path)

    assert fetched.total_paise == quote.total_paise
    assert fetched.cart_hash == quote.cart_hash


def test_two_quotes_in_same_db_retrieved_independently(tmp_path):
    quote_a = create_quote(resolve_lines([{"sku": "NW-SHOE-001", "qty": 1}]))
    quote_b = create_quote(resolve_lines([{"sku": "NW-SHOE-006", "qty": 2}]))
    path = db(tmp_path)

    save_quote(quote_a, db_path=path)
    save_quote(quote_b, db_path=path)

    fetched_a = get_quote(quote_a.quote_id, db_path=path)
    fetched_b = get_quote(quote_b.quote_id, db_path=path)

    assert fetched_a.quote_id == quote_a.quote_id
    assert fetched_a.lines == quote_a.lines
    assert fetched_b.quote_id == quote_b.quote_id
    assert fetched_b.lines == quote_b.lines
    assert fetched_a.quote_id != fetched_b.quote_id


def test_save_quote_twice_same_id_is_idempotent_and_returns_latest(tmp_path):
    quote = create_quote(resolve_lines([{"sku": "NW-SHOE-001", "qty": 1}]))
    path = db(tmp_path)

    save_quote(quote, db_path=path)
    # Re-save the identical quote under the same quote_id — must not error,
    # and the row must still resolve to one consistent record.
    save_quote(quote, db_path=path)

    fetched = get_quote(quote.quote_id, db_path=path)
    assert fetched.quote_id == quote.quote_id
    assert fetched.total_paise == quote.total_paise
    assert fetched.lines == quote.lines
