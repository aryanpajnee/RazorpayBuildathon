"""The quote engine: a cart of line items in, one exact total out.

This is vault code, not agent code. No LLM, no network, no clock-dependent
branching inside the arithmetic. Given the same lines it returns the same
numbers forever, because the total it produces is the number a mandate gets
signed over and the Gate later re-derives. If quoting were not reproducible,
the Gate could not tell tampering from arithmetic drift.

Money rules, in one place:

    subtotal = sum(unit_paise * qty)
    shipping = 0 if subtotal >= FREE_SHIPPING_ABOVE_PAISE else SHIPPING_FLAT_PAISE
    taxable  = subtotal + shipping          # GST applies to shipping too
    gst      = round_half_up(taxable * 18%)
    total    = taxable + gst

Every value is an int paise. Nothing here converts to or from rupees.

`create_quote` takes lines that have ALREADY been resolved against the catalog.
The dependency runs one way on purpose: `merchant/catalog.py` imports LineItem
from here, so importing `resolve_lines` back would be circular. Catalog turns a
buyer's request into priced lines; this module turns priced lines into a total
and a signed-over hash.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

import config
from core.mandate import cart_hash as _cart_hash


@dataclass(frozen=True, slots=True)
class LineItem:
    """One resolved cart line.

    `unit_paise` is the merchant's own catalog price, never a price the buyer
    sent. A buyer supplies a sku and a quantity; the price is looked up here.
    """

    sku: str
    name: str
    unit_paise: int
    qty: int

    @property
    def line_paise(self) -> int:
        return self.unit_paise * self.qty

    def as_dict(self) -> dict:
        return {
            "sku": self.sku,
            "name": self.name,
            "unit_paise": self.unit_paise,
            "qty": self.qty,
            "line_paise": self.line_paise,
        }

    def as_cart_item(self) -> dict:
        """The minimal projection the cart hash is computed over.

        Deliberately excludes `name`. A marketing edit to a product title would
        otherwise change the hash and invalidate a Cart Mandate the buyer had
        already signed, refusing an honest cart for a reason that has nothing
        to do with the transaction. Only sku, quantity and unit price bind the
        economics — and unit price must be in here, because the Gate's
        price-drift check depends on a price change being visible in the hash.

        `line_paise` is left out as derived: qty x unit_paise already covers it,
        and hashing a value twice adds nothing.
        """
        return {"sku": self.sku, "qty": self.qty, "unit_paise": self.unit_paise}


@dataclass(frozen=True, slots=True)
class Totals:
    """The full breakdown. Frozen because the Gate re-derives it and compares;
    a mutable total is a total someone can edit between check and charge."""

    subtotal_paise: int
    shipping_paise: int
    taxable_paise: int
    gst_paise: int
    total_paise: int

    def as_dict(self) -> dict:
        return {
            "subtotal_paise": self.subtotal_paise,
            "shipping_paise": self.shipping_paise,
            "taxable_paise": self.taxable_paise,
            "gst_paise": self.gst_paise,
            "total_paise": self.total_paise,
        }


def _require_int(value: object, field: str) -> int:
    """Reject anything that is not exactly an int.

    `isinstance(True, int)` is True in Python, so a stray bool would sail
    through as qty=1 and quietly bill for an item nobody ordered. Floats are
    rejected rather than coerced: int(4768.0) hides the rounding bug it came
    from instead of surfacing it.
    """
    if type(value) is not int:
        raise TypeError(f"{field} must be an int paise value, got {type(value).__name__}")
    return value


def compute_total(items: list[LineItem]) -> Totals:
    """Turn resolved line items into an exact total. Pure integer arithmetic."""
    if not items:
        raise ValueError("cannot quote an empty cart")

    subtotal = 0
    for item in items:
        _require_int(item.unit_paise, f"{item.sku}.unit_paise")
        _require_int(item.qty, f"{item.sku}.qty")
        if item.qty < 1:
            raise ValueError(f"{item.sku}: quantity must be at least 1, got {item.qty}")
        if item.unit_paise < 0:
            raise ValueError(
                f"{item.sku}: price cannot be negative — a negative line would let a "
                f"cart subtract its way under a mandate limit"
            )
        subtotal += item.unit_paise * item.qty

    shipping = (
        0
        if subtotal >= config.FREE_SHIPPING_ABOVE_PAISE
        else config.SHIPPING_FLAT_PAISE
    )
    taxable = subtotal + shipping

    # Round half up in integer space: adding half the divisor before floor
    # division is exactly round-half-up, with no float anywhere near it.
    gst = (taxable * config.GST_RATE_BPS + config.BPS_DIVISOR // 2) // config.BPS_DIVISOR

    return Totals(
        subtotal_paise=subtotal,
        shipping_paise=shipping,
        taxable_paise=taxable,
        gst_paise=gst,
        total_paise=taxable + gst,
    )


@dataclass(frozen=True, slots=True)
class Quote:
    """One priced, time-boxed offer from this merchant.

    The Gate re-derives every one of these fields from its own records when a
    Cart Mandate arrives, and refuses if anything disagrees. A quote is
    therefore a promise the merchant must be able to reproduce exactly, not a
    convenience object.
    """

    quote_id: str
    merchant_id: str
    currency: str
    lines: tuple[LineItem, ...]
    totals: Totals
    cart_hash: str
    issued_at: int
    expires_at: int

    @property
    def total_paise(self) -> int:
        """What a Cart Mandate must claim, exactly. The Gate compares the
        signed mandate's total_paise against this."""
        return self.totals.total_paise

    def is_expired(self, now: int | None = None) -> bool:
        """True once the TTL has elapsed. The boundary second is still valid.

        The clock is injectable so expiry is testable without waiting 90
        seconds. This reports a fact about the quote; the decision to REFUSE
        an expired quote belongs to the Gate, not here.
        """
        return (int(time.time()) if now is None else now) > self.expires_at

    def as_dict(self) -> dict:
        return {
            "quote_id": self.quote_id,
            "merchant_id": self.merchant_id,
            "currency": self.currency,
            "lines": [line.as_dict() for line in self.lines],
            "cart_hash": self.cart_hash,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            **self.totals.as_dict(),
        }


def create_quote(items: list[LineItem]) -> Quote:
    """Turn resolved line items into a time-boxed, hash-bound quote.

    The cart hash comes from `core.mandate.cart_hash`, not from a local
    implementation. There must be exactly one canonical serialiser in this
    codebase: if the merchant and the buyer hash the same cart through two
    different code paths, they will eventually disagree by a space character
    and the Gate will start refusing honest carts.
    """
    totals = compute_total(items)   # validates ints, rejects an empty cart

    issued_at = int(time.time())
    return Quote(
        quote_id=f"qt_{uuid.uuid4().hex[:12]}",
        merchant_id=config.MERCHANT_ID,
        currency=config.CURRENCY,
        lines=tuple(items),
        totals=totals,
        cart_hash=_cart_hash([item.as_cart_item() for item in items]),
        issued_at=issued_at,
        expires_at=issued_at + config.QUOTE_TTL_SECONDS,
    )
