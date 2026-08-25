# RazorpayBuildathon

A merchant that an AI buyer agent can transact with autonomously, under signed and
bounded authority.

**Razorpay AI Buildathon — Track 01, AI Growth & Agentic Commerce.**

> **Status: in development.** The mandate layer, quote engine and Razorpay money
> path are built and tested (156 tests). The enforcement Gate, audit ledger and
> agent layer are specified and land next. Demo and video to follow.

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
| `merchant/quote.py` | Deterministic quote engine. Integer paise, GST in basis points, 90-second TTL |
| `merchant/catalog.py` | Product lookup. The buyer names a sku; the merchant names the price |
| `merchant/gateway.py` | Razorpay order creation, idempotent on `quote_id` |
| `merchant/webhooks.py` | Webhook receipt. HMAC over raw bytes, replay-safe |
| `buyer/llm.py` | LLM wiring behind a rate guard. Structurally off the money path |

Specified in `docs/specs/`, not yet implemented: the enforcement **Gate** (seven
checks, fourteen machine-readable refusal codes), the hash-chained **ledger**,
and the buyer **agent state machine**.

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

- Order idempotency uses a `UNIQUE` constraint, which deduplicates **rows**, not
  **actions**. A cross-process race can still create a second real order at
  Razorpay.
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
uv run pytest -q       # 156 tests, offline and deterministic
```

Note `--extra dev`: a bare `uv sync` omits pytest, and `uv run pytest` will then
silently fall through to a system pytest outside the venv.
