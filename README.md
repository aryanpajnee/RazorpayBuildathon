# RazorpayBuildathon

A merchant that an AI buyer agent can transact with autonomously, under signed and
bounded authority.

**Razorpay AI Buildathon — Track 01, AI Growth & Agentic Commerce.**

> Status: in development. Architecture notes and demo to follow.

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

## Running it

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/), and Razorpay test-mode keys.

```bash
uv sync
cp .env.example .env   # then fill in your keys
```
