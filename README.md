# RazorpayBuildathon

A merchant that an AI buyer agent can transact with autonomously, under signed and
bounded authority.

**Razorpay AI Buildathon — Track 01, AI Growth & Agentic Commerce.**

> **Status: feature-complete, pre-submission.** The mandate layer, quote engine,
> enforcement **Gate**, hash-chained **audit ledger**, quote/intent persistence, the
> merchant **API**, the full money path — order creation, a checkout page, and
> webhook receipt — the **autonomous buyer agent**, the **merchant agent org**
> (storefront, semantic search, sales/upsell, negotiation, substitution, refusal
> explainer), the **red team** (autonomous attacker, prompt-injection injector,
> deterministic attack judge), the **observability agents** (ledger auditor,
> merchant-value metrics), an **MCP server** that lets any MCP client shop the
> merchant, and a **React live console** are all built and tested (**580 tests,
> offline and deterministic**). **All 17 agent surfaces are built.** The live path
> is proven against test-mode Razorpay: the merchant's own **sales agent upsells a
> cart past the user's signed ceiling and the merchant's own Gate refuses it**
> (OVER_LIMIT), the recovery agent adjusts the cart back under the limit and a
> **real** test-mode order is created end to end (`order_TVEeDvW8Kk8kFC`), and a
> purchase completes **through the MCP server** as well (`order_TVEeqIzxMnqoKU`).
> The one remaining human step is the successful **capture** (paying a created
> order — UPI `success@razorpay` clears it; the generic `4111…` card reads as
> international on test accounts).
>
> **In active build (1–4 Sep): the autonomous web buyer + a live mission-control
> console.** The buyer becomes a real LLM tool-calling agent that takes a typed
> request and a budget cap, **searches the live web** for matching products at
> real prices, and settles the chosen purchase through this merchant + Gate +
> Razorpay — the merchant-of-record pattern, so every rupee still clears a signed,
> enforced mandate. A new dashboard streams every agent step, tool call, web
> result, Gate check, and ledger row as it happens. The frozen money path above is
> unchanged; this work is additive.
>
> **Day 1 (1 Sep) landed:** the web-discovery search chain (Serper→Tavily→DuckDuckGo) and the merchant offer layer (`POST /offer`) are in, and a live web find is proven to pass the real **Gate** end to end — web find → merchant offer → quote → signed mandate → Gate PASS (`scripts/day1_offer_proof.py`).

## The idea

Payment rails assume a human clicks "Pay". When an AI agent does the buying, the
merchant has no way to know whether that agent is actually authorised to spend that
money, on those items, up to that limit.

This puts the authority in a cryptographic object the agent must present, and
enforces it **on the merchant side** — not in the agent's good behaviour. The
merchant re-verifies every signature, re-derives the cart hash, checks the quote
hasn't expired, and refuses any payment its mandate doesn't cover.

The buyer is free to shop the whole internet: it takes a plain request and a
budget cap, searches the live web for real products at real prices, and picks the
best fit. But it cannot pay anywhere it likes — the purchase settles through this
merchant, which relists the chosen item and holds the transaction to the signed,
budget-bounded mandate. Discovery is open; **authority is enforced at one door.**

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
| `buyer/llm.py` | The shared LLM gateway for every agent surface, behind one rate guard. Gemini for anything numeric; an NVIDIA fast lane (`gpt-oss-20b`) for prose-only surfaces, which degrades to Gemini if it fails. Structurally off the money path |
| `buyer/agent.py` | The buyer's deterministic executor: PLAN → DISCOVER → EVALUATE → COMMIT → RECOVER state machine. Loads the signing key, builds and signs the Cart Mandate, submits it, and drives every transition. A model node may propose only `[{sku, qty}]`; nothing model-derived reaches the signing step |
| `buyer/intent_compiler.py` | Turns a human sentence into a bounded Intent Mandate draft, renders a plain-English readback for the human to sign. The model returns whole rupees; Python converts to paise, so the model never emits a money value |
| `buyer/planner.py` · `discovery.py` · `evaluator.py` | The buyer's judgment surfaces: feasibility/strategy, catalog search + candidate selection, and the final cart choice. Language and selection only — never a price, never a signature |
| `buyer/negotiator.py` · `recovery.py` · `negotiation.py` | The buyer's negotiator (haggles for a cheaper cart) and recovery node (diagnoses a refusal and adjusts the cart), plus the bounded, turn-capped loop that runs the negotiator against the merchant's. Both return `[{sku, qty}]` only |
| `merchant/agents/*.py` | The merchant agent org (#1–#6): a conversational storefront, semantic catalog search, a sales/upsell agent, a negotiator that concedes only genuinely cheaper real-catalog carts, a substitution agent for out-of-stock items, and a refusal explainer. All advisory and off the money path — they propose or explain, and the Gate still re-derives and enforces every price |
| `redteam/*.py` | The red team (#13–#15): an autonomous **attacker** that forms hypotheses and mutates cart mandates on a bounded loop, an **injector** that writes poisoned product copy, and an **attack judge**. The judge's verdict (defended/breach) is pure deterministic Python — the LLM only narrates it, never decides it. The attacker never moves money and never decides pass/fail |
| `observability/auditor.py` | The **Auditor** (#16): reads the ledger and writes a plain-English incident report — what happened, under whose authority, why refused. Prose-only with a deterministic fallback; it copies every figure verbatim from a ledger payload and computes no number |
| `observability/metrics.py` | The **Metrics** agent (#17): the four merchant-value numbers (AOV lift, attach rate, autonomous-revenue channel, bounded-upsell refusals) plus the attack table. Every number is deterministic integer arithmetic — the LLM computes none. `scripts/metrics_batch.py` drives it over the real money path |
| `merchant/mcp_server.py` | An **MCP server** exposing the merchant to any MCP client (Claude, another agent) as `search_catalog` / `get_quote` / `checkout` / `buy`. A transport adapter over the real API, never a second door — `checkout` reaches the same Gate and real Razorpay order path |
| `ui/` | A **React live console** (Vite + TypeScript) over a FastAPI SSE backend: three panels — the agent conversation, the Gate's seven checks flipping PASS/REFUSE, and the hash chain growing — driven by the real Gate, quote store and ledger. Built for the demo video |

Implemented and driven end to end. All 17 agent surfaces plus the money-path
vault, the MCP server, and the live console are built.

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
uv run pytest -q       # 580 tests, offline and deterministic
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

### The autonomous buyer agent (needs a Gemini key)

With the merchant running (above) and `GEMINI_API_KEY` set, the buyer agent
takes it from a single sentence — no scripted steps. It compiles the sentence
into an Intent Mandate, shows a readback to sign, then plans, searches the
catalog, selects a cart, signs the Cart Mandate, and submits it to the Gate,
stopping at the one human step (paying the created order). An over-budget
request is refused by the merchant Gate on the real GST-inclusive total, and the
agent reports why — enforcement stays on the merchant side, never in the
agent's good behaviour.

### The merchant agent org, live (needs a Gemini key)

With the merchant running and `GEMINI_API_KEY` set, one script walks the whole
Phase 5 story end to end against real models and test-mode Razorpay:

```bash
uv run python scripts/phase5_demo.py
```

The storefront greets, semantic search ranks the catalog, the **sales agent
upsells a cart past the signed ceiling and the Gate refuses the merchant's own
sales agent**, the refusal explainer puts it in plain English, the recovery agent
adjusts the cart back under the limit and a **real** order is created, the buyer
and merchant negotiators settle a cheaper cart in a bounded loop, and the
substitution agent offers in-stock alternatives for an out-of-stock item — with
every step landing in the hash-chained ledger.

### The MCP server — any AI client can shop the merchant

With the merchant running, expose it over the Model Context Protocol:

```bash
uv run python -m merchant.mcp_server                    # stdio, for Claude Desktop/Code
uv run python -m merchant.mcp_server --transport streamable-http   # or over HTTP
```

`search_catalog`, `get_quote`, and `checkout` are thin adapters over the running
API, so an MCP client gets the exact same seven-check Gate enforcement as the
buyer agent — MCP is a transport, never a bypass. A demo-only `buy` tool signs a
Cart Mandate locally and completes a **real** test-mode order end to end.

### The merchant-value metrics (needs a Gemini key)

With the merchant running, drive a batch over the real money path and print the
numbers for the pitch:

```bash
uv run python scripts/metrics_batch.py --runs 20
```

AOV lift with the sales agent on vs off, attach rate, autonomous-revenue channel,
and how often the Gate refused the merchant's own sales agent — every figure is
deterministic Python, never a model output.

### The live console (the demo UI)

A React three-panel console — the agent conversation, the Gate's seven checks
flipping PASS/REFUSE, and the hash chain growing — driven by the real Gate:

```bash
cd ui/web && npm install && npm run build && cd ../..
uv run uvicorn ui.server:app --port 8100     # then open http://127.0.0.1:8100
```
