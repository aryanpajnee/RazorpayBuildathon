"""render_checkout_page is pure -- no I/O, no network -- so these tests just
inspect the returned HTML string.
"""

import pytest

from merchant.checkout_page import render_checkout_page


KEY_ID = "rzp_test_abc123"
ORDER_ID = "order_TU5yNYnOtVabYD"


def test_contains_order_id_and_key_id():
    out = render_checkout_page(key_id=KEY_ID, order_id=ORDER_ID, amount_paise=476720)
    assert ORDER_ID in out
    assert KEY_ID in out


def test_contains_checkout_js_script_src():
    out = render_checkout_page(key_id=KEY_ID, order_id=ORDER_ID, amount_paise=476720)
    assert 'src="https://checkout.razorpay.com/v1/checkout.js"' in out


def test_rupee_formatting_with_paise_remainder():
    """476720 paise -> 4767 rupees, 20 paise -> "4,767.20"."""
    out = render_checkout_page(key_id=KEY_ID, order_id=ORDER_ID, amount_paise=476720)
    assert "4,767.20" in out


def test_rupee_formatting_exact_rupees():
    """500000 paise -> 5000 rupees, 0 paise -> "5,000.00"."""
    out = render_checkout_page(key_id=KEY_ID, order_id=ORDER_ID, amount_paise=500000)
    assert "5,000.00" in out


def test_never_embeds_key_secret_name_or_value():
    secret_value = "super_secret_key_value_xyz"
    out = render_checkout_page(key_id=KEY_ID, order_id=ORDER_ID, amount_paise=476720)
    assert "RAZORPAY_KEY_SECRET" not in out
    assert secret_value not in out


@pytest.mark.parametrize(
    "bad_amount",
    [0, -1, 4767.2, True, False, "476720", None],
)
def test_rejects_non_positive_or_non_int_amount(bad_amount):
    with pytest.raises((TypeError, ValueError)):
        render_checkout_page(key_id=KEY_ID, order_id=ORDER_ID, amount_paise=bad_amount)


def test_handler_message_and_webhook_notice_present():
    out = render_checkout_page(key_id=KEY_ID, order_id=ORDER_ID, amount_paise=476720)
    assert "razorpay_payment_id" in out
    assert "webhook" in out.lower()


def test_custom_currency_and_merchant_name():
    out = render_checkout_page(
        key_id=KEY_ID,
        order_id=ORDER_ID,
        amount_paise=100,
        currency="USD",
        merchant_name="Test Co",
    )
    assert "USD" in out
    assert "Test Co" in out


def test_returns_full_html_document():
    out = render_checkout_page(key_id=KEY_ID, order_id=ORDER_ID, amount_paise=476720)
    assert out.strip().startswith("<!DOCTYPE html>")
    assert "</html>" in out
