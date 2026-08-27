"""A single-purpose, human-operated Razorpay Standard Checkout page.

The buyer in this system is an autonomous agent with no browser, and
Razorpay does not offer a freely-available headless server-side "pay this
order" API -- S2S order payment needs Razorpay to enable it per-account. So
the one moment in the whole demo that needs a human is: someone opens this
page once, in a browser, and completes a real test-mode card payment against
an order the merchant already created. That payment fires a genuine
`payment.captured` webhook back at the merchant, which is what the rest of
the system (webhooks.py, the ledger) actually reacts to.

This module renders that page. It does no I/O and holds no state -- one pure
function in, one HTML string out. Whoever serves it (a route, a script, a
`python -m http.server` on a temp file) decides how; that is deliberately
not this module's problem.

Money on this page follows the same rule as the rest of the codebase: no
float ever touches a paise amount. Rupees and paise-of-a-rupee are split
with `//` and `%` and rendered as text, not with `/100` or an f-string
`:.2f` format code.
"""

from __future__ import annotations

import html
import json

DEFAULT_CURRENCY = "INR"
DEFAULT_MERCHANT_NAME = "Northwind"


def _format_rupees(amount_paise: int) -> str:
    """Integer-only paise -> "₹4,767.20" style rendering.

    rupees = amount_paise // 100, paise-remainder = amount_paise % 100.
    `f"{rupees:,}"` inserts thousands separators on the int directly -- no
    division by a float divisor, no `:.2f`, nothing that could round a
    monetary value.
    """
    rupees = amount_paise // 100
    remainder_paise = amount_paise % 100
    return f"₹{rupees:,}.{remainder_paise:02d}"


def render_checkout_page(
    *,
    key_id: str,
    order_id: str,
    amount_paise: int,
    currency: str = DEFAULT_CURRENCY,
    merchant_name: str = DEFAULT_MERCHANT_NAME,
) -> str:
    """Return a complete, self-contained HTML document that opens Razorpay
    Standard Checkout for `order_id` on page load.

    `key_id` must be the Razorpay **public** key id (starts `rzp_test_` /
    `rzp_live_`). Never pass a key secret here -- this string is served to a
    browser. The one external script this page loads,
    `https://checkout.razorpay.com/v1/checkout.js`, is Razorpay's own
    checkout widget and is required for Standard Checkout to work at all.

    Raises TypeError/ValueError on a malformed amount, same discipline as
    `merchant/quote.py` -- a bad amount here would open a real payment
    dialog for the wrong sum, which is worse than refusing to render.
    """
    if isinstance(amount_paise, bool) or not isinstance(amount_paise, int):
        raise TypeError(f"amount_paise must be an int, got {type(amount_paise).__name__}")
    if amount_paise <= 0:
        raise ValueError(f"amount_paise must be positive, got {amount_paise}")
    if not key_id:
        raise ValueError("key_id is required")
    if not order_id:
        raise ValueError("order_id is required")

    amount_display = _format_rupees(amount_paise)
    order_id_safe = html.escape(order_id)
    merchant_name_safe = html.escape(merchant_name)
    amount_display_safe = html.escape(amount_display)
    currency_safe = html.escape(currency)

    # json.dumps, not an f-string, for every value that goes inside the
    # <script> block -- this correctly escapes quotes/backslashes so an
    # order_id or currency string can never break out of the JS literal.
    options = {
        "key": key_id,
        "order_id": order_id,
        "amount": amount_paise,
        "currency": currency,
        "name": merchant_name,
        "description": f"Order {order_id}",
    }
    options_json = json.dumps(options)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Complete payment - {merchant_name_safe}</title>
<script src="https://checkout.razorpay.com/v1/checkout.js"></script>
<style>
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    max-width: 480px;
    margin: 48px auto;
    padding: 0 16px;
    color: #1a1a1a;
  }}
  h1 {{ font-size: 20px; }}
  .summary {{
    border: 1px solid #ddd;
    border-radius: 8px;
    padding: 16px;
    margin: 16px 0;
  }}
  .summary dt {{ color: #666; font-size: 13px; }}
  .summary dd {{ margin: 0 0 12px 0; font-size: 16px; font-weight: 600; }}
  .notice {{
    background: #fff8e1;
    border: 1px solid #f0d878;
    border-radius: 8px;
    padding: 12px 16px;
    font-size: 14px;
  }}
  #result {{
    margin-top: 16px;
    padding: 12px 16px;
    border-radius: 8px;
    background: #e8f5e9;
    border: 1px solid #a5d6a7;
    display: none;
  }}
</style>
</head>
<body>
<h1>{merchant_name_safe} - test-mode checkout</h1>

<dl class="summary">
  <dt>Order ID</dt>
  <dd>{order_id_safe}</dd>
  <dt>Amount</dt>
  <dd>{amount_display_safe} {currency_safe}</dd>
</dl>

<p class="notice">
  This is a Razorpay <strong>test-mode</strong> payment. The Razorpay
  checkout window opens automatically. The message shown on this page after
  you pay is just a client-side acknowledgement -- the
  <strong>real confirmation comes from the server webhook</strong>
  (<code>payment.captured</code>), not from this browser page. Watch the
  ledger for the authoritative result.
</p>

<div id="result"></div>

<script>
  var options = {options_json};
  options.handler = function (response) {{
    var box = document.getElementById("result");
    box.style.display = "block";
    box.textContent = "Payment submitted -- razorpay_payment_id: " +
      response.razorpay_payment_id +
      ". Confirmation arrives via the webhook -- watch the ledger.";
  }};
  options.modal = {{
    ondismiss: function () {{
      var box = document.getElementById("result");
      box.style.display = "block";
      box.textContent = "Checkout was closed before payment completed. No payment_id was issued.";
    }}
  }};
  var rzp = new Razorpay(options);
  rzp.open();
</script>
</body>
</html>
"""
