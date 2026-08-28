# Spec — `merchant/gate.py`

**Not written this session.** This is the spec, not the implementation. It
exists so that whoever writes `gate.py`, now or in three
weeks, is transcribing a decision that has already been made, not making it
under deadline pressure. Read the whole thing before typing a line.

Read `docs/specs/mandate-spec.md` first if you haven't. This spec assumes you
know the Intent Mandate and Cart Mandate schemas, `canonical()`, `cart_hash()`,
and that `core.mandate.verify()` raises `MandateVerificationError` rather than
returning `False`.

---

## 1. What this file is

The Gate is the single chokepoint between a buyer's signed Cart Mandate and
any real money movement. Every path that ends in a Razorpay charge — the
buyer agent's normal checkout, the merchant's own sales/upsell agent trying to
push a bigger cart, a recovery agent retrying after a refusal — passes
through `gate.check()`. There is no second door.

This is the centrepiece of the project. Everything else — the catalog agent,
the negotiation agent, the sales agent's upsell copy — is allowed to be
wrong, persuasive, or manipulated, because none of it can move money on its
own. The Gate is what makes that true.

### In scope

- Verifying the Cart Mandate is authentic and binds to a real Intent Mandate
- Verifying the transaction the mandate describes is one the intent actually
  permits (limit, currency, category, purchase count, merchant)
- Verifying the cart matches a quote the merchant itself issued, at a price
  the merchant still stands behind, within the quote's lifetime
- Defending against replay
- Producing a machine-readable result — pass, with a re-derived total, or
  refuse, with a reason a downstream agent can act on
- Writing every decision, pass or refuse, to the audit ledger

### Out of scope — do NOT put these here

- **Pricing.** The Gate never computes a total from scratch off buyer input;
  it only re-derives the total of a quote the merchant already issued, using
  `merchant/quote.compute_total`, to check it hasn't drifted. Quoting itself
  lives in `merchant/quote.py` and `merchant/catalog.py`.
- **Talking to Razorpay.** The Gate returns pass/refuse. Whether a pass
  becomes an actual order and charge is the caller's job, downstream.
- **Deciding what to buy, what to upsell, or how to explain a refusal in
  words.** Those are agent surfaces (sales agent, refusal-explainer agent,
  recovery agent) that call the Gate or read its output. The Gate has no
  opinion about product fit and generates no prose.
- **Any LLM call, of any kind, anywhere in this file.** The Gate is exactly
  as deterministic as `core/mandate.py` and `merchant/quote.py`, and for the
  same reason: it sits on the money path. Given the same mandate, quote, and
  merchant state, `gate.check()` must return the identical result every time
  it is run, including in a year, on a different machine, replayed from a
  ledger entry.
- **Deciding whether to retry.** That's the recovery agent's job, using the
  refusal code this file defines. The Gate never retries itself and never
  mutates a cart to try to make it pass.

**This file answers exactly one question: "does this authentic, on-file
Intent Mandate actually authorise this exact cart, at this exact price, right
now, for the first time?"** Seven checks, fixed order, no exceptions carved
out for a caller who says it's fine.

---

## 2. The seven checks, in order

```
a. Ed25519 signature valid                       (core.mandate.verify)
b. Intent mandate not expired
c. Total <= the intent's max_paise
d. cart_hash matches the quote the merchant issued
e. Quote within its 90-second TTL
f. Nonce unseen (replay defence, SQLite-backed)
g. Price unchanged since the quote was issued
```

The order is not incidental. Two rules govern it, and both are worth
defending on their own:

**Rule 1 — authenticity before anything else.** Check (a) runs first because
every check after it reads a field out of the cart or intent payload and
trusts what it finds. `cart.total_paise`, `cart.cart_hash`,
`cart.intent_mandate_id` — none of these mean anything until the signature
over them is known-good. Running check (c) before check (a) would mean
comparing an attacker-chosen number against a limit and calling the
comparison meaningful. It isn't. A valid signature proves origin, not
permission — but permission checks are meaningless without it, so it has to
come first even though it settles nothing about permission itself.

**Rule 2 — cheap, local, document-only checks before anything that touches
mutable state.** Checks (a) through (e) only ever read: the cart envelope
itself, the intent record the merchant already has on file (resolved once,
verified once, at intent-grant time — not re-verified against Ed25519 on
every cart), and the quote record the merchant itself wrote when it quoted.
Nothing in (a)–(e) can have changed since the quote was issued, because none
of it depends on anything outside those two records. Check (f) is the first
one that touches something that changes on every call — the nonce table —
and check (g) is the only one that re-reads a value that can genuinely drift
between quote and settlement: the catalog price. Ordering the checks this
way means the Gate never pays for a catalog re-fetch, or writes to the nonce
table, on a cart that was going to fail on a free, local, static comparison
anyway. It also means the one check with a real side effect (f, which writes)
runs as early as it safely can — see the nonce-timing discussion below — and
the one check with the least deterministic answer (g, which depends on
whatever the catalog says *right now*) runs dead last, after everything else
has already established the cart is worth spending an extra read on.

### (a) Signature and authority chain

Two things happen here, and both are authenticity, not permission:

1. `core.mandate.verify(cart_envelope)` — the cart's own signature is valid,
   the envelope is well-formed, `alg == "Ed25519"`. Anything
   `MandateVerificationError` catches (bad signature, malformed envelope,
   wrong `alg`, a float in a money field) is caught here and mapped to one
   refusal code.
2. `cart.intent_mandate_id` resolves to an Intent Mandate the merchant
   already holds and already verified. Intent Mandates are verified once,
   with `core.mandate.verify`, at the moment the user grants them — not
   re-verified cryptographically on every cart that cites them. The Gate's
   job here is a lookup against that trusted local store, not a second
   Ed25519 check. If `intent_mandate_id` doesn't resolve to anything on
   file, the chain of authority is broken and nothing past this point can be
   evaluated meaningfully — refuse.
3. `cart.agent_id == intent.agent_id`. The cart is validly signed, but by
   whom the intent named? This compares the **label** only — a cart naming a
   different agent than the intent authorises fails here with `AGENT_MISMATCH`.
3b. `cart_envelope.public_key == intent.agent_pubkey`. This is the **proof**
   behind the label. `verify()` (step 1) established the signature is valid
   over the envelope's OWN embedded key — never that the key is entitled to
   spend. The user bound the agent's trusted key into the intent they signed;
   a cart whose `agent_id` matches but is signed by any *other* key is
   impersonation, and it belongs with authenticity, not the business-rule
   checks in (c). Refused **`SIG_INVALID`** — the document's signing key cannot
   be trusted. (`intent.agent_pubkey` absent, e.g. a legacy pre-binding intent,
   is treated as untrusted: nothing can match `None`, so the cart is refused.)
   Without this step a valid signature over an attacker-generated key passes as
   authorisation — the exact gap the `atk_agent_key_impersonation` red-team
   finding proved, `verify()` alone not being authorisation (see
   `core/mandate.py`).
4. `cart.merchant_id == config.MERCHANT_ID`. Purely local, no lookup: is
   this cart even addressed to us? A cart naming a different merchant fails
   here regardless of anything else about it — there's no reason to spend a
   nonce-table write or a catalog read on a document that was never meant
   for this merchant. (If `intent.merchant_id` is set — not `None` — it must
   also equal `config.MERCHANT_ID`, checked the same place, same reasoning.)

**What this defeats:** a forged cart mandate (wrong key, tampered payload);
a cart that name-drops a plausible-looking `intent_mandate_id` that was
never actually granted; a cart signed by an agent key the user never
authorised, even if it's otherwise well-formed; a cart genuinely signed by a
real agent but intended for a different merchant, replayed here by mistake
or by an attacker hoping cross-merchant checks are sloppy.

**Refusal codes:** `SIG_INVALID`, `INTENT_NOT_FOUND`, `AGENT_MISMATCH`,
`WRONG_MERCHANT`.

### (b) Intent not expired

`now >= intent.expires_at` → refuse. Compared against the merchant's own
clock, never a timestamp the buyer supplies. There is no field on the cart
for "current time" and if there were, the Gate would ignore it.

**What this defeats:** an agent continuing to spend against a standing
grant the user meant to lapse — the entire point of putting an expiry on
the Intent Mandate in the first place.

**Refusal code:** `INTENT_EXPIRED`.

### (c) Total within what the intent grants

This is stated in the brief as "total <= max_paise," but max_paise is one
dimension of a broader question — *does the intent, as the user actually
signed it, cover this transaction at all?* Four checks live here together
because they're the same kind of check: static comparisons between the
cart/quote and the resolved intent record, none requiring any I/O beyond
records already in hand from (a) and (b).

- `total_paise <= intent.max_paise`, where `total_paise` is the Gate's own
  re-derived total (see §5), never the number the cart merely claims.
- `cart.currency == intent.currency == config.CURRENCY`. Without this, a
  paise ceiling and a paise total aren't comparable in the first place —
  currency match is what makes the max_paise comparison mean something,
  which is why it lives next to it rather than off on its own.
- Category match: `intent.category` against the product category of every
  line in the resolved quote (looked up via the catalog, since the Cart
  Mandate schema carries no category field of its own — a cart can't assert
  its own category, the merchant's catalog says what a sku actually is).
- `intent.max_purchases` not exhausted — the merchant's own count of how
  many times this intent has already produced a `gate.passed`, read from the
  ledger or a local counter, never a count the buyer reports.

**What this defeats:** an over-budget cart; a cart billed in a currency the
user didn't authorise; a cart buying something outside the category the user
named (footwear intent, electronics cart); an agent using a "buy sneakers
once" grant a second time.

**Refusal codes:** `OVER_LIMIT`, `CURRENCY_MISMATCH`, `CATEGORY_MISMATCH`,
`PURCHASES_EXHAUSTED`.

### (d) Cart hash matches the merchant's own quote

`cart.quote_id` must resolve to a quote record the merchant actually issued
(if it doesn't, refuse — same "can't evaluate a nonexistent record"
reasoning as intent resolution in (a); code `QUOTE_NOT_FOUND`). Given that
record, `cart.cart_hash` must equal the `cart_hash` the merchant computed
and stored *at quote time*, over the merchant's own resolved line items —
never recomputed from anything the cart itself sends. The cart mandate
carries a hash; the Gate never trusts it as a hash of "the buyer's cart," it
trusts it only as a claim to be checked against the merchant's own record of
what it quoted.

**What this defeats:** cart tampering after quoting — swapping a sku,
bumping a quantity, adding a line — where an attacker (or a bug in the
merchant's own sales agent) tries to get a mandate's signature to cover a
cart it never actually described. The signature in (a) proves the *cart
mandate document* is authentic; it says nothing about whether the cart
inside it matches what the merchant offered. This check is what ties those
two together.

**Refusal codes:** `QUOTE_NOT_FOUND`, `CART_HASH_MISMATCH`.

### (e) Quote within its TTL

`now - quote.issued_at <= config.QUOTE_TTL_SECONDS` (90s), against the
merchant's own quote record and the merchant's own clock — never a
timestamp the cart supplies.

**What this defeats:** an agent (or an attacker holding a stale, previously
valid cart mandate) settling a quote long after the price it locked in
stopped being current. 90 seconds is short by design — short enough that
"quote expired mid-flow" is a routine, demoable failure mode, not an edge
case nobody will ever see live.

**Refusal code:** `QUOTE_EXPIRED`.

### (f) Nonce unseen — replay defence

`cart.nonce` must not already exist in the Gate's nonce store. See §6 for
the schema and §2.1 below for the timing question, which is the subtle part
of this whole file.

**What this defeats:** replay — resubmitting a cart mandate (or one
intercepted in transit) that has already been used to authorise a payment,
to try to trigger a second one. A valid signature and an unexpired quote are
not enough on their own; they'd both still be true the second time the exact
same bytes are submitted.

**Refusal code:** `NONCE_REUSED`.

### (g) Price unchanged since the quote was issued

This is **not** the same check as (d). Check (d) proves the cart the buyer
signed matches the cart the merchant quoted — it defeats tampering by the
buyer side. Check (g) proves the price the merchant quoted is still the
price the merchant is offering *right now* — it defeats drift on the
merchant's own side, in the gap between issuing the quote and this cart
mandate arriving. The Gate re-fetches the current catalog price for every
sku in the quote's stored line items, re-runs `merchant.quote.compute_total`
over those current prices, and compares the result to the `total_paise`
stored on the quote record at issuance. Any difference — up or down —
refuses. (Not just "went up": a merchant that silently pays out at a lower
price than it now lists is still charging one customer differently from the
one who orders a second later, and if the demo's poisoned-catalog scenario
ever tries to bait the merchant into *under*-charging by a manufactured
price drop, this check has to catch that too. Direction doesn't matter,
mismatch does.)

**What this defeats:** the demo's headline attack — a catalog description
(or a compromised catalog-update path) changes a listed price in the window
between quote and settlement, and either the buyer ends up paying a price
the merchant no longer honours, or the merchant ends up selling at a price
it no longer offers. Either way, the transaction was priced against a moment
in time that's no longer real, and the Gate is the thing that notices.

**Refusal code:** `PRICE_DRIFT`.

### 2.1 The nonce-timing question — check-and-record must be one atomic step

The brief asks this directly, so here is a direct answer with the reasoning,
not just the conclusion.

**Decision: checking that a nonce is unseen and recording it as seen happen
in the same atomic operation, at step (f), regardless of whether check (g)
later refuses the same cart.** Concretely: a single `INSERT` into the nonce
table with `nonce` as a `PRIMARY KEY`; if the insert succeeds, the nonce was
unseen and is now recorded, in one indivisible step; if it raises
`sqlite3.IntegrityError`, the nonce was already present and the Gate refuses
with `NONCE_REUSED`. There is no separate "peek" followed by a later
"commit."

Two things had to be weighed against each other to get here.

**Why they can't be two separate steps.** If checking and recording were
split — read "is it there," decide, write "now it's there" as a later step —
there's a window between the read and the write. Two concurrent submissions
of the identical cart mandate could both read "not present" before either
writes, and both proceed. That's exactly the race the nonce exists to close.
A `PRIMARY KEY` insert is atomic at the database layer for free; splitting
the operation only to re-join it later buys nothing and reopens the race.

**Why recording immediately, even though check (g) might still refuse
afterwards, is the right call — not an oversight.** The concern the brief
raises is real: if a cart mandate gets its nonce burned at (f) and then
fails at (g) for price drift, that exact mandate can now never be replayed
even if drift was transient. Two things make this the correct trade-off
rather than a self-inflicted bug:

1. A Cart Mandate is bound, by its own signed `total_paise` and `cart_hash`,
   to one specific quote at one specific price. If check (g) refuses it,
   that mandate is not "temporarily blocked" — it is describing a price
   that no longer exists. There is no legitimate future in which resubmitting
   those exact signed bytes should succeed; the only correct recovery is a
   fresh quote and a fresh Cart Mandate, which — per the mandate spec —
   carries a fresh `nonce` by construction. Burning the old nonce costs a
   well-behaved buyer nothing, because it was never going to be reused.
2. The alternative — deferring the nonce write until after check (g), or
   until after the downstream Razorpay call succeeds — widens the window an
   attacker has to race two submissions of the same mandate through the
   pipeline concurrently, and does so specifically to protect a case (retry
   the identical envelope after a *content* failure) that a correctly-built
   buyer agent should never do. The mandate spec's own reasoning about
   `verify` raising rather than returning applies by extension here: don't
   design the money path around a caller doing the wrong thing, design it so
   the right thing is what naturally happens.

The one caller behaviour this deliberately does **not** support is "blindly
resubmit the exact same signed cart mandate hoping a transient failure
clears up." That is intentional. If a submission is genuinely interrupted
before the buyer ever sees a response (a dropped connection, not a refusal),
the correct fix lives at the payment/idempotency layer keyed on `quote_id`
— per the project's own rule that a failed transaction never produces a
second payment — not by treating the mandate's nonce as something safe to
resubmit. The nonce is a replay defence for a *specific, once-signed
authorisation*, not a request-idempotency key; conflating the two is exactly
the mistake this section exists to head off. `quote_id` idempotency and
`nonce` replay-defence are deliberately two different mechanisms guarding
two different things, and neither should be asked to do the other's job.

---

## 3. Binding checks — where each one lives

Restated as a single table, since the brief asks for it called out
separately. Nothing here is a new, eighth check; every row is folded into
one of the seven above, with the reasoning already given in §2.

| Binding check | Lives in | Why here, not elsewhere |
|---|---|---|
| `cart.intent_mandate_id` resolves to a real, verified intent | (a) | Nothing downstream can be evaluated against an intent that doesn't exist; this is authenticity-of-the-chain, not a business rule. |
| `cart.agent_id == intent.agent_id` | (a) | A validly-signed cart from the wrong agent is impersonation, not an over-limit case. |
| `cart.merchant_id == config.MERCHANT_ID` | (a) | Purely local, no lookup, cheapest possible early exit — a cart addressed elsewhere is refused before any record is even fetched. |
| `cart.currency == intent.currency` | (c) | Currency is what makes the `max_paise` comparison in (c) meaningful; they're one question, not two. |
| Category match (via catalog lookup, not a cart field) | (c) | Same family as (c): does the intent's grant, as signed, actually cover this cart. |
| `intent.max_purchases` not exhausted | (c) | Same family again — a spent-out grant is a scope failure, not a signature or hash failure. |

---

## 4. Refusal reason codes — closed enum

Two other agent surfaces read these directly: a **Refusal Explainer** that
turns a code into plain language for the buyer, and a **Recovery agent**
that decides whether to adjust the cart and retry, mint a fresh mandate and
retry, or stop. Both need more than a string to act on, so every refusal
carries `detail: dict` alongside the code — see §5.

| Code | Check | Recoverable? | What a well-behaved recovery agent does |
|---|---|---|---|
| `SIG_INVALID` | (a) | Never | Stop. Alert. Do not retry in any form — the document itself cannot be trusted. |
| `INTENT_NOT_FOUND` | (a) | Never (automatically) | Stop, surface to a human. The referenced intent doesn't exist on this merchant — a bug or an attack, not something to paper over. |
| `AGENT_MISMATCH` | (a) | Never | Stop. Likely spoofing or key misconfiguration. |
| `WRONG_MERCHANT` | (a) | Never (here) | Stop against this merchant. If the agent genuinely meant a different merchant, that's a routing bug in the caller, not something this Gate should work around. |
| `INTENT_EXPIRED` | (b) | Never automatically | Stop, surface to the user. Only a human can grant a new Intent Mandate. |
| `OVER_LIMIT` | (c) | **Yes** | `detail` carries `limit_paise` and `over_by_paise`. Drop or downgrade line items to bring the cart under the ceiling, request a fresh quote, sign a fresh cart mandate, retry. |
| `CURRENCY_MISMATCH` | (c) | Never automatically | Stop, surface to the user — needs a new intent in the right currency. |
| `CATEGORY_MISMATCH` | (c) | Limited | If some lines are in-category and some aren't, drop the out-of-category lines and retry with what's left; if none qualify, stop. |
| `PURCHASES_EXHAUSTED` | (c) | Never | Stop. The grant's purchase count is spent; only a new intent restores it. |
| `QUOTE_NOT_FOUND` | (d) | Yes | Request a fresh quote for the same cart and retry — almost certainly a stale or mistyped `quote_id`, not an attack. |
| `CART_HASH_MISMATCH` | (d) | Yes, but never by "fixing" the hash | Discard the tampered cart entirely, request a brand-new quote from the merchant's own catalog/quote engine, sign a brand-new cart mandate against *that*, retry. Never attempt to patch the mismatched fields and resubmit the same mandate. |
| `QUOTE_EXPIRED` | (e) | Yes | Request a fresh quote (new 90-second window), sign a fresh cart mandate, retry. |
| `NONCE_REUSED` | (f) | Only via a brand-new mandate | This exact signed document is spent or compromised. If the recovery agent has no record of ever legitimately submitting it, treat as suspicious and stop; otherwise mint an entirely new Cart Mandate (fresh nonce) if the purchase is still wanted. |
| `PRICE_DRIFT` | (g) | Yes | `detail` carries `quoted_total_paise` and `current_total_paise`. Request a fresh quote at current prices, sign a fresh cart mandate, retry — subject to (c) still holding at the new price. |

No code outside this table may be returned. If `gate.check()` is about to
return a refusal that doesn't fit one of these fourteen, that's a bug in
the Gate, not a fifteenth code invented on the spot — extending this table
is a spec change, not an implementation decision.

---

## 5. The result type

**Decision: `gate.check()` returns an object, `GateResult`, on both pass and
refuse. It never raises for anything an adversarial or merely malformed
buyer input can trigger.**

This is a deliberate departure from `core.mandate.verify`'s "raise, don't
return `False`" stance, and the mandate spec's own reasoning is the right
lens to apply here — it just points the other way. `verify()` is called by
code that has no sensible branch for "this document is fake"; the only safe
response to a forged signature is to stop, loudly, and an exception is the
thing that can't be silently ignored. `gate.check()` is different: refusal
is not exceptional, it is one of the two expected outcomes of every single
call, and it is consumed by callers — the FastAPI route, the recovery agent,
the ledger writer — that all need to keep running afterwards and branch on
*which* of fourteen reasons it was. Modelling fourteen refusal reasons as
fourteen exception subclasses, all of which the caller must catch anyway to
do anything useful, buys nothing over a plain object with a `reason_code`
field, and it would make "refuse" and "an actual bug in the Gate" look the
same shape from the caller's side — which is exactly the ambiguity the
mandate spec's exception design was trying to avoid in the first place.

The one thing that must never happen either way: a genuinely malformed
envelope (not just an unauthentic one — literally the wrong shape, missing
keys) reaching `gate.check()` and crashing the caller. `core.mandate.verify`
already raises `MandateVerificationError` for that; the Gate catches it at
step (a) and turns it into a `GateResult` with `SIG_INVALID`, same as any
other authenticity failure. An exception escaping `gate.check()` should mean
"the merchant's own code broke" — a bug in the Gate's own storage, a
corrupt local quote record — never "the buyer sent something bad." Buyer
input, however adversarial, always terminates in a `GateResult`.

```python
@dataclass(frozen=True, slots=True)
class GateResult:
    passed: bool
    reason_code: str | None       # None on pass; one of §4's codes on refuse
    message: str                  # human-readable, for logs — not for the buyer
    detail: dict                  # structured, for the recovery agent — see §4
    total_paise: int | None       # the Gate's own re-derived total; present on
                                   # pass, and on any refusal reached after (d)
    quote_id: str | None
    cart_mandate_id: str | None
    checked_at: int                # unix seconds, the Gate's own clock
```

`message` is not what the Refusal Explainer shows the buyer — that agent's
whole job is turning `reason_code` and `detail` into something a person or
another agent should read. `message` is for the merchant's own logs and the
ledger.

`total_paise` is always the Gate's **own** re-derivation, computed via
`merchant.quote.compute_total` over the merchant's stored line items —
never the number the cart mandate merely asserts. This is what makes the
Gate's pass meaningful: the total that gets charged is the total the
merchant itself just calculated, not a number the buyer supplied and the
Gate merely rubber-stamped.

---

## 6. The nonce store

SQLite, its own file: `config.GATE_NONCES_DB` (`data/gate_nonces.db`). Not
`config.LEDGER_DB` — that database belongs to the owner's ledger module and
this file must not write to it directly. (`data/*.db` is already gitignored,
so a second db file next to `ledger.db` needs no new gitignore entry.)

```sql
CREATE TABLE IF NOT EXISTS gate_nonces (
    nonce            TEXT PRIMARY KEY,
    cart_mandate_id  TEXT NOT NULL,
    agent_id         TEXT NOT NULL,
    quote_id         TEXT NOT NULL,
    recorded_at      INTEGER NOT NULL   -- unix seconds, Gate's own clock
);
```

- **Scope: global to the merchant**, not scoped per-agent or per-intent. The
  mandate spec defines `nonce` as "unique per cart mandate" with no stated
  namespace, so the safe, simple reading is global uniqueness — scoping it
  more narrowly would only be meaningful if two different agents were
  allowed to reuse an identical nonce string, and nothing about the mandate
  format assumes that. `PRIMARY KEY` on the bare `nonce` column enforces
  this for free.
- **Never expires.** A used nonce must never become valid again — that's
  the entire point of it. No TTL, no cleanup job, no row ever deleted.
  Storage growth is one row per Gate call that reaches step (f); at this
  project's scale that is not a real constraint, and "delete old nonces to
  save space" is exactly the kind of shortcut that quietly reopens a replay
  window months later. Don't add it.
- **Check-and-record is the insert itself** (see §2.1): attempt
  `INSERT INTO gate_nonces (...) VALUES (...)`; a successful insert *is* the
  passing check; an `IntegrityError` (primary key collision) *is* the
  `NONCE_REUSED` refusal. There is no separate `SELECT` before it.

---

## 7. Ledger integration

`core/ledger.py` is the owner's file, written in parallel with a sibling
spec (`docs/specs/ledger-spec.md`); this section describes the integration
at the level of "append this event, with this payload," not an exact
function signature, per that spec's own scope.

**Every single call to `gate.check()` that reaches a final answer appends
exactly one event to the ledger — pass or refuse, no exceptions.** A silent
refusal is not auditable, and the entire pitch of the ledger is that nothing
touching money happens off the record.

- **On pass:** append a `gate.passed` event. Payload: `cart_mandate_id`,
  `intent_mandate_id`, `quote_id`, `agent_id`, `nonce`, the Gate's own
  re-derived `total_paise`, `checked_at`. This is the record that a specific
  cart, at a specific price, was authorised to proceed to Razorpay — it is
  not itself proof that a charge happened; that's a separate event the
  payment-execution code appends later, keyed on the same `quote_id`.
- **On refuse:** append a `gate.refused` event for every one of the fourteen
  codes in §4. Payload: `reason_code`, `message`, `detail`, and whatever of
  `cart_mandate_id` / `quote_id` / `agent_id` the Gate actually managed to
  extract before refusing. For an early refusal like `SIG_INVALID`, most of
  those fields may be unavailable or untrusted — record what can be
  extracted (at minimum a hash of the raw envelope bytes and a timestamp)
  rather than skipping the ledger write. An attack attempt that fails
  loudly is exactly what the ledger exists to prove happened; refusing to
  log it because the input wasn't trustworthy defeats the purpose.
- The Gate's ledger responsibility ends at `gate.passed` / `gate.refused`.
  Downstream events — `order.created`, `payment.attempted`,
  `webhook.received`, `payment.succeeded`, `payment.failed` — are appended by
  the payment-execution and webhook code, not by this file. In particular the
  Gate never emits `payment.attempted` or `order.created`: it does not talk to
  Razorpay (§1), so it is never the code that attempts a payment or creates an
  order, and `payment.attempted` carrying a `razorpay_order_id` can only be
  written *after* `order.created`, downstream of a `gate.passed`. These events
  are chained onto the same ledger by whatever hash-chaining mechanism
  `core/ledger.py` defines. The Gate calls the append once per decision and
  moves on; it does not read the chain back or verify it.

---

## 8. Test matrix

Every row names the `tests/test_gate.py` function expected and the exact
refusal code the failing case must produce. "Pass" cases are the control —
each check needs at least one test proving it doesn't fire on a legitimate
cart, not just one proving it fires on a bad one.

| Check | Passing test | Failing test | Refusal code |
|---|---|---|---|
| (a) signature | `test_gate_passes_with_valid_signature` | `test_gate_refuses_forged_signature` | `SIG_INVALID` |
| (a) intent resolution | `test_gate_passes_with_known_intent` | `test_gate_refuses_unknown_intent_mandate_id` | `INTENT_NOT_FOUND` |
| (a) agent binding | `test_gate_passes_matching_agent` | `test_gate_refuses_agent_mismatch` | `AGENT_MISMATCH` |
| (a) agent-key binding | `test_gate_passes_with_valid_signature` | `test_gate_refuses_wrong_key_right_agent_id` | `SIG_INVALID` |
| (a) merchant binding | `test_gate_passes_matching_merchant` | `test_gate_refuses_wrong_merchant` | `WRONG_MERCHANT` |
| (b) expiry | `test_gate_passes_unexpired_intent` | `test_gate_refuses_expired_intent` | `INTENT_EXPIRED` |
| (c) max_paise | `test_gate_passes_under_limit` | `test_gate_refuses_over_limit` | `OVER_LIMIT` |
| (c) currency | `test_gate_passes_matching_currency` | `test_gate_refuses_currency_mismatch` | `CURRENCY_MISMATCH` |
| (c) category | `test_gate_passes_matching_category` | `test_gate_refuses_category_mismatch` | `CATEGORY_MISMATCH` |
| (c) purchase count | `test_gate_passes_purchases_remaining` | `test_gate_refuses_purchases_exhausted` | `PURCHASES_EXHAUSTED` |
| (d) quote resolution | `test_gate_passes_known_quote` | `test_gate_refuses_unknown_quote_id` | `QUOTE_NOT_FOUND` |
| (d) cart hash | `test_gate_passes_matching_cart_hash` | `test_gate_refuses_cart_tamper` | `CART_HASH_MISMATCH` |
| (e) TTL | `test_gate_passes_within_ttl` | `test_gate_refuses_quote_expired_91s` | `QUOTE_EXPIRED` |
| (f) nonce | `test_gate_passes_fresh_nonce` | `test_gate_refuses_replayed_nonce` | `NONCE_REUSED` |
| (g) price | `test_gate_passes_unchanged_price` | `test_gate_refuses_price_drift` | `PRICE_DRIFT` |

### The ten red-team attacks

| Attack | Caught by | Test |
|---|---|---|
| Replay | Check (f) | `test_gate_refuses_replayed_nonce` — submit the same cart mandate twice; first call passes, second refuses `NONCE_REUSED`. |
| Quote expiry (91s) | Check (e) | `test_gate_refuses_quote_expired_91s` — inject a clock (never `time.sleep(91)`) so the quote's `issued_at` is 91s in the past. |
| Price drift | Check (g) | `test_gate_refuses_price_drift` — mutate the catalog's price for a sku between quoting and gating. |
| Cart tamper | Check (d) | `test_gate_refuses_cart_tamper` — sign a cart mandate, then flip a qty or sku in the stored quote's line items before gating, so `cart_hash` no longer matches. |
| Over-limit | Check (c) | `test_gate_refuses_over_limit` — quote a cart above `intent.max_paise`. |
| Forged signature | Check (a) | `test_gate_refuses_forged_signature` — flip a byte in the signature so it no longer verifies over its own key. |
| Agent-key impersonation | Check (a).3b | `test_gate_refuses_wrong_key_right_agent_id` — attacker signs a cart that correctly names the victim's `agent_id`, against the victim's intent, with a *different* keypair. The signature is internally valid, the `agent_id` matches; the signing key was never the one the user bound. Refused `SIG_INVALID`. (Red-team finding `atk_agent_key_impersonation`.) |
| Expired intent | Check (b) | `test_gate_refuses_expired_intent` — `intent.expires_at` in the past. |
| Payment failure | **Out of scope.** Happens downstream of a `gate.passed`, at the Razorpay call / webhook layer. The Gate's only obligation here is to have passed correctly beforehand; idempotency on a failed-and-retried payment is enforced by `quote_id`, tested in `tests/test_gateway.py` and `tests/test_webhooks.py`, not here. |
| Ledger tamper | **Out of scope.** Chain-integrity verification is `core/ledger.py`'s job, tested in `tests/test_ledger.py`. The Gate only calls append; it never reads the chain back and has no way to detect a tampered *past* entry. |

**One ordering test worth calling out explicitly:** a cart that is
simultaneously over-limit *and* has a tampered hash must refuse
`OVER_LIMIT`, not `CART_HASH_MISMATCH` — because (c) runs before (d). Add
`test_gate_reports_first_failing_check_in_order` constructing exactly that
cart and asserting the code. This is the cheapest possible regression test
for "did someone reorder the checks" and it's worth having on its own.

---

## 9. The demo moment

The pitch video's spine runs through this file twice.

**Poisoned catalog description → buyer overspends → Gate refuses anyway.**
A product description in the catalog is written to manipulate the buyer
agent's LLM into wanting to add an item that blows past the user's Intent
Mandate ceiling (or steps outside its category). The buyer agent, its
reasoning genuinely swayed, builds a cart, gets it quoted, and signs a Cart
Mandate for it. None of that is stopped anywhere upstream — it isn't
supposed to be; the LLM is allowed to be persuaded, that's the whole point
of using a real model for that layer. The Gate is what actually stops it,
at check (c), refusing `OVER_LIMIT` (or `CATEGORY_MISMATCH`, depending on
which the demo cart trips), and it must be legible on screen doing it: the
terminal prints a clearly-marked refusal — check name, code, the Gate's own
re-derived total against `intent.max_paise`, the exact `over_by_paise` — so
a viewer with no context can see in one glance that the merchant, not the
buyer's good behaviour, is what stopped the overspend. The `rich` UI should
render this as a visually distinct (red) panel, not a line buried in
scrollback.

**The merchant's own sales agent, upselling past the ceiling, refused by
the merchant's own gate.** The second beat is the sharper one: this time
it isn't an external attacker or an adversarial catalog entry, it's the
merchant's *own* upsell agent — code on the merchant's own side — trying to
push a bigger cart through. It calls `gate.check()` exactly the same way
the buyer's checkout path does, with no special-casing, and gets refused
exactly the same way. Nothing about being "on the merchant's own team"
grants a bypass. That's the sentence the whole project is trying to prove,
so the spec's obligation is narrow but firm: `gate.check()` must have
**no caller-identity parameter, no internal flag, no privileged code path**
of any kind. One function, one set of seven checks, every caller.

---

## 10. If you get stuck

| Symptom | Real cause |
|---|---|
| `CART_HASH_MISMATCH` fires on a cart nobody tampered with | The Gate recomputed `cart_hash` over a differently-shaped item list than the one hashed at quote time — e.g. including `line_paise` in one place and not the other. `canonical()` is sensitive to the exact key set; compare the raw bytes each side hashed, not the dicts, when debugging. |
| Nonce check passes the first call but the second call also passes, when it shouldn't | The SQLite connection isn't committing, or a fresh connection/in-memory DB is opened per call instead of one persistent file — the insert from call one never actually lands before call two runs. Test both calls within the same test using the same store instance. |
| A legitimate-looking retry of the same request gets permanently refused `NONCE_REUSED` | This is very likely correct behaviour, not a bug — see §2.1. If the test harness is resubmitting the byte-identical signed envelope hoping for idempotent success, that's testing the wrong mechanism; idempotency belongs to the `quote_id` layer downstream, not the mandate nonce. |
| `PRICE_DRIFT` fires on every test even when nothing changed | The re-derived total in (g) was computed from a `LineItem` list built in a different order, or with a different rounding path, than `merchant.quote.compute_total` used at quote time — go through `merchant/catalog.resolve_lines` both times so sku ordering and price lookup are identical, don't hand-build the line list in the test. |
| The 91-second TTL test is slow or flaky | Something is literally sleeping 91 seconds. Inject the clock — a `now` parameter or a monkeypatched `time.time` — never a real `sleep` in a check that fixed-order tests will run dozens of times. |
| An over-limit-and-tampered cart reports the wrong code | Checks are running out of the fixed a→g order. See the explicit ordering test in §8; if it's missing, add it before debugging further — it'll usually point straight at the misordered check. |
| A refusal never shows up in the ledger | Almost certainly a refusal path that returns early (e.g. inside a `try/except MandateVerificationError`) before reaching the single ledger-append call at the end of `check()`. Structure `check()` so every return path — pass or any of the fourteen refusals — flows through one final append, not one append per check. |

---

## 11. Done when

- [ ] The seven checks run in the fixed order a→g, provably (§8's ordering
      test passes)
- [ ] Every value compared in every check is re-derived from the merchant's
      own records — nothing from the cart or the buyer is trusted as-is,
      anywhere, including values the buyer signed
- [ ] All six binding checks from §3 are implemented and land in the check
      each is assigned to, not a bolted-on eighth check
- [ ] All fourteen codes in §4 are reachable by a dedicated failing test, and
      no other string is ever returned as `reason_code`
- [ ] `gate.check()` returns a `GateResult` on every buyer-triggerable input,
      including malformed envelopes — nothing from adversarial input ever
      raises out of `check()`
- [ ] Nonce check-and-record is one atomic SQLite insert, not a
      check-then-write pair
- [ ] Every call to `check()` appends exactly one ledger event, pass or
      refuse, with no early-return path that skips it
- [ ] No LLM import, call, or client anywhere in the file
- [ ] All ten red-team attacks in §8 are each caught by a named test, or
      explicitly out of scope with the reason stated
- [ ] The upsell-agent demo path calls the exact same `gate.check()` as the
      buyer checkout path — no identity parameter, no bypass
