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
"""

from __future__ import annotations

from dataclasses import dataclass

import config


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
