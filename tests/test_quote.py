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
