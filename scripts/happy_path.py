"""Scripted end-to-end money path — no LLM, no buyer agent yet.

This plays the buyer by hand, exactly along the canonical flow, so the whole
Phase 1–3 pipeline can be exercised end to end before any agent exists:

    grant intent -> search -> quote -> sign Cart Mandate -> checkout (GATE ->
    Razorpay order) -> [human pays the order] -> webhook -> ledger

Everything up to the checkout response is fully autonomous. The single human
step is opening the printed pay URL and paying with a Razorpay **test card** —
that is the one thing today's rails still require a person for, and it is the
gap the mandate layer exists to make safe, not to remove. Once paid, Razorpay
calls the merchant's /webhook through the tunnel and the ledger fills in.

Prerequisites:
  * the merchant API is running, e.g.  uv run uvicorn merchant.api:app --port 8000
  * for a REAL payment: a cloudflared tunnel -> /webhook in the Razorpay
    dashboard -> RAZORPAY_WEBHOOK_SECRET in .env  (see the run checklist)

This script and the server must share the same data/ databases — run both from
the repo root on one machine, which is the default.

Run:
    uv run python scripts/happy_path.py                 # against localhost:8000
    uv run python scripts/happy_path.py --no-wait       # skip ledger polling
    uv run python scripts/happy_path.py --base-url http://localhost:8000

Nothing here signs authority the merchant does not re-check: the intent is
granted (registered) locally to stand in for the Phase 4 user-consent step,
but the Gate still re-verifies the Cart Mandate signature, re-derives the
total from its own catalog, and refuses anything the intent does not cover.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# Allow `uv run python scripts/happy_path.py` to run from anywhere: Python only
# puts this file's own scripts/ dir on sys.path, so add the repo root (its
# parent) so `config`, `core`, and `merchant` resolve.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

import config  # noqa: E402
from core.mandate import (  # noqa: E402
    generate_keypair,
    make_cart_mandate,
    make_intent_mandate,
    sign,
)
from merchant import intent_store  # noqa: E402

# Terminal events that end the wait loop — the payment has resolved one way or
# the other, so there is nothing further to poll for.
_TERMINAL_LEDGER_EVENTS = {"payment.succeeded", "payment.failed"}


def _rupees(paise: int) -> str:
    """Integer-only paise -> '₹4,767.20'. No float touches a money value."""
    return f"₹{paise // 100:,}.{paise % 100:02d}"


def _section(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m")


def _fail(message: str) -> "NoReturn":  # type: ignore[valid-type]
    print(f"\n\033[31m✗ {message}\033[0m")
    sys.exit(1)


def run(base_url: str, *, max_paise: int, wait: bool, poll_seconds: int) -> None:
    base_url = base_url.rstrip("/")
    client = httpx.Client(base_url=base_url, timeout=30.0)

    mode = "FAKE gateway (dry run — no real order/payment)" if config.USE_FAKE_GATEWAY else "REAL Razorpay test mode"
    print(f"Merchant: {base_url}   Mode: {mode}")

    # --- 1. Grant the intent (stands in for the Phase 4 user-consent step) ---
    _section("1. Grant intent")
    sk, vk = generate_keypair()
    agent_id = f"agent_happy_{vk.encode().hex()[:8]}"
    intent_payload = make_intent_mandate(
        user_id="user_happy_path",
        agent_id=agent_id,
        category="footwear",
        max_paise=max_paise,
        max_purchases=5,
        ttl_seconds=3600,
    )
    intent_store.register_intent(intent_payload)
    print(f"  agent      {agent_id}")
    print(f"  category   footwear   ceiling {_rupees(max_paise)}   mandate {intent_payload['mandate_id']}")

    # --- 2. Search the catalog ---
    _section("2. Search catalog")
    try:
        resp = client.get("/catalog/search", params={"q": "footwear"})
    except httpx.ConnectError:
        _fail(f"could not reach {base_url} — start the server first:\n"
              f"    uv run uvicorn merchant.api:app --port 8000")
    resp.raise_for_status()
    products = resp.json()["products"]
    if not products:
        _fail("catalog search returned no footwear products")
    product = products[0]
    sku = product["sku"]
    print(f"  picked     {sku}  —  {product.get('name', '')}")

    # --- 3. Quote ---
    _section("3. Quote")
    resp = client.post("/quote", json={"items": [{"sku": sku, "qty": 1}]})
    if resp.status_code != 200:
        _fail(f"/quote failed ({resp.status_code}): {resp.text}")
    quote = resp.json()
    print(f"  quote_id   {quote['quote_id']}")
    print(f"  total      {_rupees(quote['total_paise'])}   (cart_hash {quote['cart_hash'][:12]}…, TTL to {quote['expires_at']})")
    if quote["total_paise"] > max_paise:
        _fail(f"quoted total {_rupees(quote['total_paise'])} exceeds the intent ceiling "
              f"{_rupees(max_paise)} — pick a cheaper sku or raise --max-rupees")

    # --- 4. Sign the Cart Mandate (the agent's key, never the model) ---
    _section("4. Sign Cart Mandate")
    cart_payload = make_cart_mandate(
        intent_mandate_id=intent_payload["mandate_id"],
        agent_id=agent_id,
        merchant_id=config.MERCHANT_ID,
        quote_id=quote["quote_id"],
        cart_hash=quote["cart_hash"],
        total_paise=quote["total_paise"],
    )
    envelope = sign(cart_payload, sk)
    print(f"  signed     cart_mandate {cart_payload['mandate_id']}   nonce {cart_payload['nonce']}")

    # --- 5. Checkout: the GATE, then a real order ---
    _section("5. Checkout (Gate → order)")
    resp = client.post("/checkout", json={"cart_envelope": envelope})
    if resp.status_code != 200:
        _fail(f"/checkout failed ({resp.status_code}): {resp.text}")
    result = resp.json()
    if not result["passed"]:
        _fail(f"Gate REFUSED: {result['reason_code']} — {result['message']}")
    if result.get("order_error"):
        _fail(f"Gate passed but order creation failed: {result['order_error']}")
    order_id = result["order_id"]
    print(f"  \033[32mGATE PASSED\033[0m   re-derived total {_rupees(result['total_paise'])}")
    print(f"  order      {order_id}")

    # --- 6. The one human step: pay the order ---
    _section("6. Pay the order  ← the one human step")
    pay_url = f"{base_url}{result['pay_url']}"
    print(f"  Open this and pay with test card 4111 1111 1111 1111 (any future expiry, any CVV):")
    print(f"    \033[36m{pay_url}\033[0m")
    if config.USE_FAKE_GATEWAY:
        print("  (FAKE mode: this order id is not real at Razorpay, so the page can't actually pay —"
              " the flow above is what the dry run proves.)")

    # --- 7. Watch the ledger fill in from the webhook ---
    _section("7. Ledger")
    if wait and not config.USE_FAKE_GATEWAY:
        print(f"  waiting up to {poll_seconds}s for the payment webhook…")
        deadline = time.monotonic() + poll_seconds
        seen = 0
        while time.monotonic() < deadline:
            entries = client.get("/ledger").json()["entries"]
            for entry in entries[seen:]:
                print(f"    #{entry['seq']:<3} {entry['event_type']}")
            seen = len(entries)
            if any(e["event_type"] in _TERMINAL_LEDGER_EVENTS for e in entries):
                break
            time.sleep(2.0)
        else:
            print("    (no terminal payment event yet — pay the order above, it will land when you do)")
    else:
        entries = client.get("/ledger").json()["entries"]
        for entry in entries:
            print(f"    #{entry['seq']:<3} {entry['event_type']}")

    print("\n\033[32m✓ happy path complete\033[0m")


def main() -> None:
    parser = argparse.ArgumentParser(description="Scripted end-to-end merchant money path.")
    parser.add_argument("--base-url", default="http://localhost:8000", help="merchant API base URL")
    parser.add_argument("--max-rupees", type=int, default=5000,
                        help="intent spending ceiling in whole rupees (default 5000)")
    parser.add_argument("--no-wait", dest="wait", action="store_false",
                        help="don't poll the ledger for the payment webhook")
    parser.add_argument("--poll-seconds", type=int, default=120,
                        help="how long to wait for the payment webhook (default 120)")
    args = parser.parse_args()

    run(args.base_url, max_paise=args.max_rupees * 100, wait=args.wait, poll_seconds=args.poll_seconds)


if __name__ == "__main__":
    main()
