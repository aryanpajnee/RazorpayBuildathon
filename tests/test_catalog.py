"""The catalog is the merchant's own record of what things cost.

The security property under test is narrow but load-bearing: a buyer asks for
a sku and a quantity, and the price attached to that line comes from here and
nowhere else. A buyer that sends a price is ignored, not trusted.
"""

import pytest

from merchant.catalog import (
    OutOfStock,
    ProductNotFound,
    all_products,
    get_product,
    load_catalog,
    resolve_lines,
)


def test_a_known_sku_resolves_to_its_catalog_price():
    p = get_product("NW-SHOE-001")
    assert p["price_paise"] == 499900
    assert p["name"].startswith("Northwind Tempo 3")


def test_an_unknown_sku_is_refused():
    with pytest.raises(ProductNotFound):
        get_product("NW-DOES-NOT-EXIST")


def test_every_catalog_price_is_an_int():
    for p in all_products():
        assert type(p["price_paise"]) is int, f"{p['sku']} price is not an int"


def test_the_catalog_has_thirty_products_with_unique_skus():
    skus = [p["sku"] for p in all_products()]
    assert len(skus) == 30
    assert len(set(skus)) == 30


# --- the part that matters --------------------------------------------------

def test_a_buyer_supplied_price_is_ignored_in_favour_of_the_catalog_price():
    """The whole point. A buyer asking to pay 1 paise for a 4999-rupee shoe
    gets quoted 499900 anyway."""
    [resolved] = resolve_lines([{"sku": "NW-SHOE-001", "qty": 1, "unit_paise": 1}])
    assert resolved.unit_paise == 499900


def test_a_buyer_supplied_name_is_ignored_too():
    [resolved] = resolve_lines([{"sku": "NW-SHOE-001", "qty": 1, "name": "Free Shoe"}])
    assert resolved.name == get_product("NW-SHOE-001")["name"]


def test_duplicate_skus_in_one_request_are_merged_into_a_single_line():
    """Two lines for the same sku would make the cart hash depend on how the
    buyer chose to split them."""
    lines = resolve_lines(
        [{"sku": "NW-SOCK-001", "qty": 2}, {"sku": "NW-SOCK-001", "qty": 3}]
    )
    assert len(lines) == 1
    assert lines[0].qty == 5


def test_lines_come_back_sorted_by_sku_regardless_of_request_order():
    """Stable ordering so the cart hash does not depend on request order."""
    forward = resolve_lines([{"sku": "NW-CAP-001", "qty": 1}, {"sku": "NW-BELT-001", "qty": 1}])
    reverse = resolve_lines([{"sku": "NW-BELT-001", "qty": 1}, {"sku": "NW-CAP-001", "qty": 1}])
    assert [l.sku for l in forward] == [l.sku for l in reverse] == ["NW-BELT-001", "NW-CAP-001"]


# --- stock ------------------------------------------------------------------

def test_an_out_of_stock_item_is_refused():
    with pytest.raises(OutOfStock):
        resolve_lines([{"sku": "NW-SHOE-004", "qty": 1}])


def test_ordering_more_than_stock_is_refused():
    stock = get_product("NW-SHOE-008")["stock"]
    with pytest.raises(OutOfStock):
        resolve_lines([{"sku": "NW-SHOE-008", "qty": stock + 1}])


def test_ordering_exactly_the_remaining_stock_is_allowed():
    stock = get_product("NW-SHOE-008")["stock"]
    [resolved] = resolve_lines([{"sku": "NW-SHOE-008", "qty": stock}])
    assert resolved.qty == stock


def test_merged_duplicates_are_stock_checked_on_the_merged_quantity():
    """Splitting an over-stock order across two lines must not sneak past."""
    stock = get_product("NW-SHOE-008")["stock"]
    with pytest.raises(OutOfStock):
        resolve_lines(
            [{"sku": "NW-SHOE-008", "qty": stock}, {"sku": "NW-SHOE-008", "qty": 1}]
        )


def test_known_limitation_concurrent_quotes_for_the_last_units_all_pass():
    """This is documentation of an accepted gap, not a desirable behaviour.

    resolve_lines checks stock against the static catalog file; it does not
    reserve or decrement anything. Two independent quote calls for the same
    last units of stock both read the same catalog and both succeed, because
    nothing at quote time claims the stock for either caller. A real system
    would reserve stock at checkout, inside the Gate's transaction, where a
    second reservation for already-claimed stock would be rejected. Here,
    checkout-time reservation does not exist, so both callers below "win":
    """
    stock = get_product("NW-SHOE-008")["stock"]

    first_buyer = resolve_lines([{"sku": "NW-SHOE-008", "qty": stock}])
    second_buyer = resolve_lines([{"sku": "NW-SHOE-008", "qty": stock}])

    assert first_buyer[0].qty == stock
    assert second_buyer[0].qty == stock  # oversold in reality; not caught here


# --- the poisoned listing ---------------------------------------------------

def test_the_poisoned_product_is_served_exactly_as_written():
    """The defence against prompt injection is the merchant-side gate, not
    scrubbing the catalog. If this ever gets filtered, the Phase 6 demo stops
    proving anything."""
    p = get_product("NW-GIFT-001")
    assert "SYSTEM NOTICE" in p["description"]
    assert p in all_products()


def test_the_catalog_merchant_id_matches_config():
    """A drift here would let a quote be issued by one merchant id and a cart
    mandate be bound to another, with both sides looking internally consistent."""
    import config

    assert load_catalog()["merchant_id"] == config.MERCHANT_ID
