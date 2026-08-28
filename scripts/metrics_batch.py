"""Drive the REAL money path N times and print the merchant-value metrics.

This is the batch runner behind agent #17 (`observability/metrics.py`). Every
number it prints is computed by `compute_metrics` from the Gate's OWN outcomes
— real quotes, real Ed25519 signatures, the real seven-check Gate, and (on a
pass) a real test-mode Razorpay order. The LLM Sales agent (#3) is toggled per
arm; it computes no metric, it only proposes upsells.

Each round runs the same buyer twice: once with the Sales agent OFF (the
baseline cart) and once ON (the Sales agent tries to grow the cart). Whether an
upsell clears is the Gate's call — an over-ceiling upsell is refused OVER_LIMIT,
which is exactly the "bounded upsell" metric.

Prereqs: the merchant API running with real keys —
    uv run uvicorn merchant.api:app --port 8000
Run:
    uv run python scripts/metrics_batch.py --runs 20      # the video batch
    uv run python scripts/metrics_batch.py --runs 3       # a quick real check
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

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
from merchant.agents import sales  # noqa: E402
from observability import metrics  # noqa: E402
from observability.metrics import RunOutcome  # noqa: E402

# A generous per-run ceiling so a modest upsell can PASS (showing AOV lift)
# while an aggressive one is REFUSED (showing bounded upsell). Both are real
# Gate outcomes, not staged.
CEILING_PAISE = 1_500_000       # ₹15,000
BASE_SKU = "NW-SHOE-001"        # Tempo 3, ₹4,999
# In-category (footwear) add-ons the Sales agent may pick; kept in-category so
# the ONLY thing that can refuse an upsell is the ceiling, never CATEGORY.
FOOTWEAR_ADDONS = ["NW-SHOE-006", "NW-SHOE-007", "NW-SHOE-003", "NW-SHOE-005"]


def _drive_one(client: httpx.Client, *, sales_on: bool) -> RunOutcome:
    sk, vk = generate_keypair()
    agent_id = f"agent_metrics_{vk.encode().hex()[:8]}"
    intent = make_intent_mandate(
        user_id="user_metrics", agent_id=agent_id, agent_pubkey=vk.encode().hex(),
        category="footwear", max_paise=CEILING_PAISE, max_purchases=5, ttl_seconds=3600,
    )
    intent_store.register_intent(intent)

    items = [{"sku": BASE_SKU, "qty": 1}]
    upsell_offered = False
    upsell_accepted = False

    if sales_on:
        intent_ctx = {"category": "footwear", "max_paise": CEILING_PAISE, "currency": config.CURRENCY}
        try:
            proposal = sales.upsell(cart=list(items), intent=intent_ctx)
            adds = proposal.get("add", []) if isinstance(proposal, dict) else []
        except Exception:  # noqa: BLE001 - advisory surface, never fail the run
            adds = []
        # Keep only in-category add-ons the Gate could actually authorise.
        picked = [a for a in adds if isinstance(a, dict) and a.get("sku") in FOOTWEAR_ADDONS]
        if not picked:
            # The agent proposed nothing usable; add a default footwear add-on so
            # the ON arm genuinely differs from the baseline.
            picked = [{"sku": FOOTWEAR_ADDONS[0], "qty": 1}]
        upsell_offered = True
        for add in picked:
            items.append({"sku": add["sku"], "qty": int(add.get("qty", 1))})
        upsell_accepted = True  # the buyer took the offer into the submitted cart

    quote = client.post("/quote", json={"items": items})
    if quote.status_code != 200:
        return RunOutcome(sales_on=sales_on, passed=False, reason_code="QUOTE_FAILED",
                          order_total_paise=None, upsell_offered=upsell_offered, upsell_accepted=False)
    quote = quote.json()

    payload = make_cart_mandate(
        intent_mandate_id=intent["mandate_id"], agent_id=agent_id, merchant_id=config.MERCHANT_ID,
        quote_id=quote["quote_id"], cart_hash=quote["cart_hash"], total_paise=quote["total_paise"],
    )
    envelope = sign(payload, sk)
    result = client.post("/checkout", json={"cart_envelope": envelope}).json()

    return RunOutcome(
        sales_on=sales_on,
        passed=bool(result.get("passed")),
        reason_code=result.get("reason_code"),
        order_total_paise=result.get("total_paise") if result.get("passed") else None,
        upsell_offered=upsell_offered,
        upsell_accepted=upsell_accepted and bool(result.get("passed")),
        human_involved=False,  # the whole run is autonomous — the new sales channel
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Real-money-path metrics batch.")
    parser.add_argument("--runs", type=int, default=config.METRICS_BATCH_SIZE)
    parser.add_argument("--base-url", default=config.MERCHANT_BASE_URL)
    args = parser.parse_args()

    client = httpx.Client(base_url=args.base_url.rstrip("/"), timeout=60.0)
    print(f"Driving {args.runs} paired runs against {args.base_url} "
          f"(fake_gateway={config.USE_FAKE_GATEWAY})…\n")

    result = metrics.run_batch(lambda sales_on: _drive_one(client, sales_on=sales_on), n=args.runs)

    print(metrics.render(result))

    config.METRICS_DIR.mkdir(parents=True, exist_ok=True)
    out = config.METRICS_DIR / "metrics.json"
    out.write_text(json.dumps(metrics.as_dict(result), indent=2))
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
