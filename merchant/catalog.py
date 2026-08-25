"""Deterministic access to the merchant's own product data.

This is not agent surface #2. #2 is the semantic search agent that arrives in
Phase 5 and decides *which* products match an intent; this module is the plain
lookup underneath it, and it never guesses.

The one rule worth stating out loud: a buyer names a sku and a quantity. The
price comes from here. Anything price-shaped arriving from a buyer is dropped
on the floor, not merged, not preferred, not even logged as a suggestion.
"""

from __future__ import annotations

import json
from functools import lru_cache

import config
from merchant.quote import LineItem

CATALOG_PATH = config.DATA_DIR / "catalog.json"


class CatalogError(Exception):
    """Base for anything the catalog refuses."""


class ProductNotFound(CatalogError):
    def __init__(self, sku: str) -> None:
        super().__init__(f"no such product: {sku}")
        self.sku = sku


class OutOfStock(CatalogError):
    """Raised when a requested quantity exceeds the catalog's recorded stock.

    This is an availability check against the static catalog file at quote
    time, not a reservation. Nothing here claims or decrements stock, so two
    concurrent quotes for the same last units both read the same number and
    both pass — see `resolve_lines` for where a real reservation would live.
    """

    def __init__(self, sku: str, requested: int, available: int) -> None:
        super().__init__(
            f"{sku}: requested {requested}, only {available} in stock"
        )
        self.sku = sku
        self.requested = requested
        self.available = available


@lru_cache(maxsize=1)
def load_catalog() -> dict:
    """Read and validate the catalog once per process.

    The int check runs at load rather than at quote time so a float price in
    the data file fails at startup, loudly, instead of halfway through a
    checkout.
    """
    with CATALOG_PATH.open(encoding="utf-8") as fh:
        data = json.load(fh)

    seen: set[str] = set()
    for product in data["products"]:
        sku = product["sku"]
        if sku in seen:
            raise CatalogError(f"duplicate sku in catalog: {sku}")
        seen.add(sku)
        if type(product["price_paise"]) is not int:
            raise CatalogError(
                f"{sku}: price_paise must be an int paise value, got "
                f"{type(product['price_paise']).__name__}"
            )
        if type(product["stock"]) is not int:
            raise CatalogError(f"{sku}: stock must be an int")

    return data


def all_products() -> list[dict]:
    return load_catalog()["products"]


def get_product(sku: str) -> dict:
    for product in all_products():
        if product["sku"] == sku:
            return product
    raise ProductNotFound(sku)


def resolve_lines(requests: list[dict]) -> list[LineItem]:
    """Turn a buyer's `[{sku, qty}, ...]` into priced, stock-checked lines.

    Duplicate skus are merged and the result is sorted by sku, so the same cart
    always produces the same lines — and therefore the same cart hash — no
    matter how the buyer chose to order or split its request.

    The stock check below is an availability check at quote time, not a
    reservation: `load_catalog` is `lru_cache`d over a read-only JSON file,
    so nothing here ever decrements or claims stock, and two callers quoting
    the last units of the same sku concurrently will both pass. This is a
    deliberate simplification for a demo whose subject is mandate
    enforcement, not inventory — see
    `test_known_limitation_concurrent_quotes_for_the_last_units_all_pass` in
    tests/test_catalog.py. A real implementation would reserve stock at
    checkout time, inside the Gate's transaction, where a second reservation
    against already-claimed stock would be rejected rather than merely
    checked against a stale number.
    """
    wanted: dict[str, int] = {}
    for request in requests:
        sku = request["sku"]
        qty = request.get("qty", 1)
        if type(qty) is not int:
            raise TypeError(f"{sku}: qty must be an int, got {type(qty).__name__}")
        if qty < 1:
            raise ValueError(f"{sku}: quantity must be at least 1, got {qty}")
        wanted[sku] = wanted.get(sku, 0) + qty

    lines = []
    for sku in sorted(wanted):
        qty = wanted[sku]
        product = get_product(sku)          # raises ProductNotFound
        if qty > product["stock"]:
            raise OutOfStock(sku, qty, product["stock"])
        lines.append(
            LineItem(
                sku=sku,
                name=product["name"],
                unit_paise=product["price_paise"],
                qty=qty,
            )
        )
    return lines
