"""The quote engine is on the money path, so every number here is checked by
hand rather than against the implementation's own output.

Expected values are computed independently in the test docstrings. If a test
and the code disagree, the test is right until proven otherwise.
"""

import pytest

import config
from merchant.quote import LineItem, Totals, compute_total


def line(sku: str, unit_paise: int, qty: int = 1) -> LineItem:
    return LineItem(sku=sku, name=sku, unit_paise=unit_paise, qty=qty)


# --- the arithmetic ---------------------------------------------------------

def test_single_item_above_free_shipping_threshold():
    """499900 >= 99900 so shipping is waived.
    taxable = 499900; gst = 499900 * 0.18 = 89982 exactly; total = 589882.
    """
    t = compute_total([line("NW-SHOE-001", 499900)])
    assert t.subtotal_paise == 499900
    assert t.shipping_paise == 0
    assert t.taxable_paise == 499900
    assert t.gst_paise == 89982
    assert t.total_paise == 589882


def test_shipping_charged_below_threshold():
    """99899 < 99900 so 4900 shipping applies.
    taxable = 104799; gst = 104799 * 0.18 = 18863.82 -> 18864; total = 123663.
    """
    t = compute_total([line("NW-SOCK-003", 99899)])
    assert t.shipping_paise == config.SHIPPING_FLAT_PAISE
    assert t.taxable_paise == 104799
    assert t.gst_paise == 18864
    assert t.total_paise == 123663


def test_threshold_is_inclusive_at_exactly_999_rupees():
    """The rule is 'free at or above 99900', so 99900 itself ships free.
    taxable = 99900; gst = 17982; total = 117882.
    """
    t = compute_total([line("NW-SOCK-002", 99900)])
    assert t.shipping_paise == 0
    assert t.total_paise == 117882


def test_one_paise_either_side_of_the_threshold_differs_by_shipping_plus_its_gst():
    below = compute_total([line("X", config.FREE_SHIPPING_ABOVE_PAISE - 1)])
    at = compute_total([line("X", config.FREE_SHIPPING_ABOVE_PAISE)])
    # Crossing the threshold costs 1 paise more in goods and saves 4900 in
    # shipping, so taxable drops by 4899; GST drops 18864 -> 17982 = 882.
    assert below.total_paise - at.total_paise == 4899 + 882


def test_gst_applies_to_goods_plus_shipping_not_goods_alone():
    """If GST were charged on the subtotal only, total would be 24925 + 3604
    = 28529 rather than 29412."""
    t = compute_total([line("NW-ELEC-001", 20025)])
    assert t.taxable_paise == 20025 + config.SHIPPING_FLAT_PAISE
    assert t.total_paise == 29412


def test_gst_rounds_half_up_not_down():
    """taxable 24925 * 0.18 = 4486.5 exactly. Floor gives 4486; half-up gives
    4487. This test is the whole reason the rounding rule is written down."""
    t = compute_total([line("NW-ELEC-001", 20025)])
    assert t.gst_paise == 4487, "GST must round half up, not truncate"


def test_quantity_multiplies_the_unit_price():
    """3 x 20025 = 60075; + 4900 shipping = 64975; 64975 * 0.18 = 11695.5
    -> 11696; total = 76671."""
    t = compute_total([line("NW-ELEC-001", 20025, qty=3)])
    assert t.subtotal_paise == 60075
    assert t.gst_paise == 11696
    assert t.total_paise == 76671


def test_multiple_lines_sum_into_the_subtotal():
    t = compute_total([line("A", 159900), line("B", 79900, qty=2)])
    assert t.subtotal_paise == 159900 + 159800
    assert t.shipping_paise == 0


# --- no floats, ever --------------------------------------------------------

def test_every_returned_amount_is_a_real_int():
    t = compute_total([line("NW-ELEC-001", 20025, qty=3)])
    for field, value in t.as_dict().items():
        assert type(value) is int, f"{field} is {type(value).__name__}, not int"


def test_a_float_unit_price_is_rejected_rather_than_coerced():
    with pytest.raises(TypeError):
        compute_total([LineItem(sku="A", name="A", unit_paise=499900.0, qty=1)])


def test_a_float_quantity_is_rejected():
    with pytest.raises(TypeError):
        compute_total([LineItem(sku="A", name="A", unit_paise=499900, qty=1.0)])


def test_a_bool_is_not_an_acceptable_int():
    """bool subclasses int in Python. True would silently mean qty=1."""
    with pytest.raises(TypeError):
        compute_total([LineItem(sku="A", name="A", unit_paise=499900, qty=True)])


# --- rejected carts ---------------------------------------------------------

def test_empty_cart_is_refused():
    with pytest.raises(ValueError):
        compute_total([])


def test_zero_quantity_is_refused():
    with pytest.raises(ValueError):
        compute_total([line("A", 499900, qty=0)])


def test_negative_quantity_is_refused():
    with pytest.raises(ValueError):
        compute_total([line("A", 499900, qty=-1)])


def test_negative_price_is_refused():
    """A negative line would let a cart subtract its way under a mandate limit."""
    with pytest.raises(ValueError):
        compute_total([line("A", -100)])


# --- determinism ------------------------------------------------------------

def test_same_cart_produces_an_identical_total_one_hundred_times():
    cart = [line("A", 499900), line("B", 20025, qty=3), line("C", 99899)]
    first = compute_total(cart)
    for _ in range(100):
        assert compute_total(cart) == first


def test_line_order_does_not_change_the_total():
    a, b, c = line("A", 499900), line("B", 20025, qty=3), line("C", 99899)
    assert compute_total([a, b, c]) == compute_total([c, a, b])


def test_totals_is_immutable():
    t = compute_total([line("A", 499900)])
    with pytest.raises(Exception):
        t.total_paise = 1


# --- issuing a quote --------------------------------------------------------
#
# create_quote takes ALREADY-RESOLVED lines. merchant/catalog.py imports
# LineItem from this module, so importing resolve_lines back would be a
# circular import. The layering is one-way on purpose: catalog resolves a
# buyer's request into priced lines, quote turns priced lines into a total.

import time

from merchant.catalog import resolve_lines
from merchant.quote import Quote, create_quote


def shoes_and_socks():
    return resolve_lines([{"sku": "NW-SHOE-001", "qty": 1}, {"sku": "NW-SOCK-001", "qty": 2}])


def test_a_quote_carries_an_id_prefixed_so_it_is_recognisable_in_a_ledger():
    q = create_quote(shoes_and_socks())
    assert q.quote_id.startswith("qt_")


def test_every_quote_gets_a_distinct_id():
    """quote_id is the idempotency key for the whole payment path. Two quotes
    sharing one would let a second cart settle against the first one's order."""
    ids = {create_quote(shoes_and_socks()).quote_id for _ in range(100)}
    assert len(ids) == 100


def test_a_quote_expires_exactly_ttl_seconds_after_it_was_issued():
    q = create_quote(shoes_and_socks())
    assert q.expires_at - q.issued_at == config.QUOTE_TTL_SECONDS


def test_a_fresh_quote_is_not_expired_but_is_one_second_past_its_ttl():
    q = create_quote(shoes_and_socks())
    assert not q.is_expired(now=q.issued_at)
    assert not q.is_expired(now=q.expires_at)          # the boundary is still valid
    assert q.is_expired(now=q.expires_at + 1)


def test_a_quotes_totals_match_computing_them_directly():
    lines = shoes_and_socks()
    assert create_quote(lines).totals == compute_total(lines)


def test_a_quote_carries_a_64_character_cart_hash():
    q = create_quote(shoes_and_socks())
    assert len(q.cart_hash) == 64
    int(q.cart_hash, 16)


def test_the_same_cart_produces_the_same_cart_hash_one_hundred_times():
    """The determinism the Gate depends on: it re-derives this hash from its
    own records and compares it against the one the buyer signed."""
    lines = shoes_and_socks()
    first = create_quote(lines).cart_hash
    for _ in range(100):
        assert create_quote(lines).cart_hash == first


def test_request_order_does_not_change_the_cart_hash():
    forward = resolve_lines([{"sku": "NW-SHOE-001", "qty": 1}, {"sku": "NW-SOCK-001", "qty": 2}])
    reverse = resolve_lines([{"sku": "NW-SOCK-001", "qty": 2}, {"sku": "NW-SHOE-001", "qty": 1}])
    assert create_quote(forward).cart_hash == create_quote(reverse).cart_hash


def test_splitting_one_sku_across_two_lines_hashes_the_same_as_one_merged_line():
    split = resolve_lines([{"sku": "NW-SOCK-001", "qty": 1}, {"sku": "NW-SOCK-001", "qty": 2}])
    merged = resolve_lines([{"sku": "NW-SOCK-001", "qty": 3}])
    assert create_quote(split).cart_hash == create_quote(merged).cart_hash


def test_a_different_quantity_produces_a_different_cart_hash():
    one = resolve_lines([{"sku": "NW-SOCK-001", "qty": 1}])
    two = resolve_lines([{"sku": "NW-SOCK-001", "qty": 2}])
    assert create_quote(one).cart_hash != create_quote(two).cart_hash


def test_the_cart_hash_ignores_the_product_name():
    """A marketing edit to a product's name must not invalidate a signed cart
    mandate. Only sku, quantity and unit price bind the economics."""
    a = LineItem(sku="X", name="Original Name", unit_paise=100, qty=1)
    b = LineItem(sku="X", name="Renamed In Marketing", unit_paise=100, qty=1)
    assert create_quote([a]).cart_hash == create_quote([b]).cart_hash


def test_the_cart_hash_changes_when_the_unit_price_changes():
    """Check (g) of the Gate depends on this: price drift after a quote was
    issued has to be detectable."""
    a = LineItem(sku="X", name="X", unit_paise=100, qty=1)
    b = LineItem(sku="X", name="X", unit_paise=101, qty=1)
    assert create_quote([a]).cart_hash != create_quote([b]).cart_hash


def test_a_quote_names_this_merchant_and_currency():
    q = create_quote(shoes_and_socks())
    assert q.merchant_id == config.MERCHANT_ID
    assert q.currency == config.CURRENCY


def test_an_empty_cart_cannot_be_quoted():
    with pytest.raises(ValueError):
        create_quote([])


def test_a_quote_is_immutable():
    q = create_quote(shoes_and_socks())
    with pytest.raises(Exception):
        q.total_paise = 1


def test_every_money_value_in_a_serialised_quote_is_an_int():
    q = create_quote(shoes_and_socks())
    d = q.as_dict()
    for field in ("subtotal_paise", "shipping_paise", "taxable_paise", "gst_paise", "total_paise"):
        assert type(d[field]) is int, f"{field} is not an int"
    assert type(d["issued_at"]) is int and type(d["expires_at"]) is int


def test_total_paise_is_reachable_directly_on_the_quote():
    """The Gate compares this against the signed mandate's total_paise."""
    lines = shoes_and_socks()
    assert create_quote(lines).total_paise == compute_total(lines).total_paise
