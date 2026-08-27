# RazorpayBuildathon

A merchant that an AI buyer agent can transact with autonomously, under signed and
bounded authority.

**Razorpay AI Buildathon — Track 01, AI Growth & Agentic Commerce.**

> **Status: in development.** The mandate layer, quote engine, enforcement **Gate**,
> hash-chained **audit ledger**, quote/intent persistence, the merchant **API**, and
> the full money path — order creation, a checkout page, and webhook receipt — are
> built and tested (251 tests, offline and deterministic). The live path is proven
> against test-mode Razorpay: a real order created through the Orders API, and real
> webhooks received and HMAC-verified through a public tunnel. Next: a successful
> capture (the test card was blocked as international; UPI test payments clear it)
> and the buyer/merchant agent layer. Demo and video to follow.

## The idea

Payment rails assume a human clicks "Pay". When an AI agent does the buying, the
merchant has no way to know whether that agent is actually authorised to spend that
money, on those items, up to that limit.

This puts the authority in a cryptographic object the agent must present, and
enforces it **on the merchant side** — not in the agent's good behaviour. The
merchant re-verifies every signature, re-derives the cart hash, checks the quote
hasn't expired, and refuses any payment its mandate doesn't cover.

## Two rules the code follows

1. **All money is integer paise.** No float ever touches a monetary value.
2. **The LLM never touches the money path.** Quoting, mandate verification,
   authorisation, payment execution and idempotency are a deterministic state
   machine. The model does goal decomposition and product selection only.

## On UAP

NPCI's Unified Agentic Protocol is **not live** — it is pending RBI approval. This
implements an **AP2-style** mandate layer and treats UAP as the slot it plugs into
when it ships. Nothing here claims to implement UAP.

## What is built so far

| Component | What it does |
|---|---|
| `core/mandate.py` | Intent + Cart Mandates. Canonical JSON, Ed25519 sign/verify, cart hashing |
| `core/ledger.py` | Hash-chained, append-only audit log. `verify_chain()` names the first tampered row |
| `merchant/quote.py` | Deterministic quote engine. Integer paise, GST in basis points, 90-second TTL |
| `merchant/quote_store.py` | Quote persistence, looked up by `quote_id`, full line items retained for price re-derivation |
| `merchant/intent_store.py` | Trusted on-file intents + purchase counter. Immutable once granted |
| `merchant/gate.py` | The enforcement **Gate**: seven checks in fixed order, fourteen refusal codes, one caller path |
| `merchant/catalog.py` | Product lookup. The buyer names a sku; the merchant names the price |
| `merchant/gateway.py` | Razorpay order creation, idempotent on `quote_id`. Reservation-first: claims the `quote_id` before calling Razorpay, so a cross-process race can't create two orders |
| `merchant/webhooks.py` | Webhook receipt. HMAC over raw bytes, replay-safe |
| `merchant/checkout_page.py` | The one human step: a Razorpay Standard Checkout page to pay a created order with a test card/UPI |
| `merchant/api.py` | FastAPI surface: catalog search, quote, checkout (runs the Gate, then creates the order), `/pay/{order_id}`, `/webhook`, ledger |
| `buyer/llm.py` | LLM wiring behind a rate guard. Gemini for anything numeric; an NVIDIA 8B fast lane for prose-only surfaces. Structurally off the money path |

Specified in `docs/specs/`, not yet implemented: the buyer **agent state machine**
and the seventeen agent surfaces that drive the merchant.

## The part worth reading

`verify()` proves a mandate was signed by the holder of the key it carries and
has not changed since. It does **not** prove that key belongs to anyone entitled
to spend — an attacker can generate their own keypair and produce a perfectly
valid signature.

So a passing signature check is not authorisation. The Gate has to check the
embedded public key against one it already trusts, re-derive the cart hash from
the merchant's own records, and re-compute the total itself.

**A valid signature proves origin, not permission.** Most of the design follows
from taking that seriously.

## Honest limitations

Kept current rather than reconstructed at submission time. `FAILURES.md` has the
full account; the short version:

- Order idempotency is reservation-first: a caller claims the `quote_id` in the
  database (an atomic `BEGIN IMMEDIATE` insert) **before** calling Razorpay, so two
  racing callers can't both create an order — the loser never reaches the gateway.
  One narrow window remains (a crash between the gateway confirming an order and
  the row being finalized); it needs a crash, not ordinary concurrency, and is
  documented in `FAILURES.md`.
- Stock is checked at quote time but never reserved, so concurrent quotes for
  the last unit all succeed.
- `gemini-3.6-flash` ignores the `temperature` parameter, so no determinism is
  claimed on the model side. Determinism is claimed only where it is enforced:
  the quote engine, the mandate layer, and the Gate.

## Running it

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/), and Razorpay test-mode keys.

```bash
uv sync --extra dev
cp .env.example .env   # then fill in your keys
uv run pytest -q       # 251 tests, offline and deterministic
```

Note `--extra dev`: a bare `uv sync` omits pytest, and `uv run pytest` will then
silently fall through to a system pytest outside the venv.

### The live money path (Razorpay test mode)

Needs Razorpay test keys in `.env`, a `RAZORPAY_WEBHOOK_SECRET`, and a public
tunnel so Razorpay can reach `/webhook`.

```bash
uv run uvicorn merchant.api:app --port 8000      # terminal 1: the merchant
cloudflared tunnel --url http://localhost:8000   # terminal 2: a public URL
```

Point a webhook at `https://<tunnel>/webhook` in the Razorpay dashboard
(events: `payment.captured`, `payment.failed`, `order.paid`) with your secret,
then drive a scripted end-to-end purchase:

```bash
uv run python scripts/happy_path.py --max-rupees 10000
```

It grants an intent, quotes, signs a Cart Mandate, passes the Gate, and creates
a **real** test-mode order, then prints a pay URL. Open it and pay with UPI
`success@razorpay` (the generic `4111…` card is often blocked as international on
test accounts). Razorpay's webhook then completes the ledger.
