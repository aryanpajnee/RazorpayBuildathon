# Spec — `buyer/agent.py`

**Phase 4. You write this file.** This spec settles the state machine's shape,
every transition, every bounded counter and the signing boundary in advance —
writing the file is transcription and wiring, not design-while-typing.

Read the whole thing once before opening an editor. The signing boundary (§4)
and the termination proof (§7) are the two sections you will be asked to
defend in an interview; everything else exists to make those two sections
true in code.

Plan.md targets LangGraph for the surrounding scaffolding (`buyer/llm.py`
wires Gemini, a `StateGraph` binds the nodes). This spec is written
independently of that: the `Phase` enum below is a natural set of LangGraph
node names and the transition table is a natural set of conditional edges,
but nothing here requires LangGraph. A hand-rolled `while` loop that
implements the same table is equally correct. Build against whichever is
already wired when you get to this file.

---

## 1. What this file is

The deterministic executor that drives a shopping goal from a signed Intent
Mandate to a completed, ledgered payment — or to an honest stop. It owns two
things and only two things: **state transitions** and **signing**.

### In scope

- The `Phase` enum and the transition table in §2 — the only legal paths
  through a checkout
- The `AgentState` object — what gets read and written at each phase
- Calling the LLM agent nodes (Planner, Discovery, Evaluator, Buyer
  Negotiator, Recovery) and validating what they return
- Loading the agent's signing key, building the Cart Mandate payload via
  `core.mandate.make_cart_mandate`, and signing it via `core.mandate.sign`
- Calling the merchant's `/checkout` endpoint and interpreting its response
- Every bounded counter that proves the machine halts (§7)
- Rate-budgeting its own model calls against the shared Gemini free tier (§8)

### Explicitly out of scope

- **Prompt engineering.** What a node says to the model, few-shot examples,
  output-format instructions — that lives in the node modules
  (`buyer/planner.py`, `buyer/discovery.py`, `buyer/evaluator.py`,
  `buyer/negotiator.py`, `buyer/recovery.py`, `buyer/intent_compiler.py`).
  `agent.py` calls these as functions; it does not know or care how they
  talk to Gemini.
- **Pricing.** `agent.py` never computes a total. `merchant/quote.py` does
  that; `agent.py` only reads the number back off the quote response.
- **Authorisation.** `agent.py` never decides whether a payment is allowed.
  It builds and signs a Cart Mandate, submits it, and reads back whatever the
  Gate decided. The Gate's decision is final and is never second-guessed,
  retried around, or overridden here.
- **Mandate authenticity mechanics.** `core.mandate.verify` does the
  cryptography. `agent.py` calls it and reacts to the result; it does not
  reimplement or approximate what "authentic" means.
- **The refusal reason-code taxonomy.** That is `docs/specs/gate-spec.md`'s
  contract. This file treats a refusal as an opaque structured object —
  `{code, recoverable, detail}` — and does not invent its own competing set
  of codes. §6 says explicitly which parts of RECOVER are provisional on
  that spec landing.

The one-sentence version: **`agent.py` is orchestration and signing. If a
decision requires judgment, language, or strategy, it belongs in a node
module and `agent.py` only consumes the result.**

---

## 2. The state enum and transition table

```python
class Phase(Enum):
    PLAN = "plan"
    DISCOVER = "discover"
    EVALUATE = "evaluate"
    COMMIT = "commit"
    RECOVER = "recover"
    COMPLETED = "completed"     # terminal
    ABANDONED = "abandoned"     # terminal
    FAILED = "failed"           # terminal
```

Three terminal states, not one, because they mean different things to
whoever reads the log afterwards:

- **COMPLETED** — a payment was executed, the webhook confirmed it, and the
  ledger has the entry. The goal was met.
- **ABANDONED** — the machine made a deliberate, correct decision to stop.
  Nothing is broken. The intent was infeasible, or every recoverable option
  was tried and none worked, or the attempt cap was reached. This is a
  successful shutdown, not a bug.
- **FAILED** — something the machine did not expect happened: a malformed
  model output it couldn't recover from locally, a rate-limit budget
  exhausted before the goal could be reasonably attempted, or a Gate refusal
  whose code implies a security or integrity problem (forged signature,
  replayed nonce, tampered cart hash) rather than an ordinary business
  outcome. FAILED is the state that means "look at this."

### Preconditions, before `Phase.PLAN`

`agent.py` does not construct an `AgentState` from a raw sentence. Its entry
point is a function that accepts an **already-signed Intent Mandate
envelope**. Producing that envelope — Intent Compiler, readback, human
confirmation, user signature — happens upstream and is the subject of §5.

On entry, before anything else, `agent.py`:

1. Calls `core.mandate.verify(intent_envelope)`. If this raises
   `MandateVerificationError`, the run never starts — return/raise
   immediately. Note precisely what this check is: it establishes that the
   envelope is authentic and unmodified. It is **not** an authorisation
   decision (whether this mandate permits some particular cart is the
   Gate's question, asked later, on the merchant side) — it is the same
   "no client-side check is ever trusted, including on its own upstream
   steps" discipline applied to its own input.
2. Deterministically checks `expires_at > now()` and
   `purchases_used < max_purchases` on the verified payload. Either
   violated → straight to `ABANDONED`, reason recorded, no model called.
   There is nothing a Planner call could usefully contribute to a mandate
   that is already spent or expired.

Only after both checks pass does the machine enter `Phase.PLAN`.

### Transition table

Every row is `(from, trigger) -> to`. Every trigger is either a deterministic
check (marked **[det]**) or a validated model-node result (marked
**[node]**). An unlisted `(from, trigger)` pair is a bug: if you find
yourself needing a transition not in this table, that's a sign the table is
wrong, not that the code should improvise one.

| From | Trigger | To |
|---|---|---|
| *(entry)* | intent envelope fails `verify` **[det]** | — (raise before any state) |
| *(entry)* | intent expired or exhausted **[det]** | `ABANDONED` |
| `PLAN` | Planner returns a strategy **[node]** | `DISCOVER` |
| `PLAN` | Planner reports the intent infeasible **[node]** | `ABANDONED` |
| `PLAN` | Planner call fails past its local retry, or model budget for `PLAN` exhausted **[det]** | `FAILED` |
| `DISCOVER` | Discovery returns ≥1 candidate **[node]** | `EVALUATE` |
| `DISCOVER` | Discovery returns zero candidates **[node]** | `RECOVER` (reason `NO_CANDIDATES`) |
| `DISCOVER` | call fails past local retry / budget exhausted **[det]** | `FAILED` |
| `EVALUATE` | Evaluator selects a cart (`[{sku, qty}]`) **[node]** | `COMMIT` |
| `EVALUATE` | Evaluator finds no candidate genuinely fits the intent **[node]** | `RECOVER` (reason `NO_FIT`) |
| `EVALUATE` | call fails past local retry / budget exhausted **[det]** | `FAILED` |
| `COMMIT` | quote request to the merchant errors at the network/HTTP level **[det]** | `FAILED` |
| `COMMIT` | merchant declines to quote this cart (e.g. went out of stock) **[det]** | `RECOVER` (reason `QUOTE_UNAVAILABLE`) |
| `COMMIT` | quote's `expires_at` passes before the cart mandate is submitted **[det]** | `RECOVER` (reason `QUOTE_EXPIRED`) |
| `COMMIT` | negotiation runs out its turn cap without an acceptable price **[node]/[det]** | `RECOVER` (reason `NEGOTIATION_STALEMATE`) |
| `COMMIT` | Gate accepts; payment executes; webhook confirms **[det]** | `COMPLETED` |
| `COMMIT` | Gate refuses, `recoverable: true` **[det]** | `RECOVER` (reason `GATE_REFUSAL`, carries the Gate's code) |
| `COMMIT` | Gate refuses, `recoverable: false`, security/integrity code family **[det]** | `FAILED` |
| `COMMIT` | Gate refuses, `recoverable: false`, permanent business code family **[det]** | `ABANDONED` |
| `COMMIT` | payment gateway declines after Gate approval **[det]** | `RECOVER` (reason `PAYMENT_FAILED`) |
| `RECOVER` | `attempt_count >= ATTEMPT_CAP` (see §6) **[det]** | `ABANDONED` |
| `RECOVER` | failure class is non-recoverable in principle (§6) **[det]** | `FAILED` |
| `RECOVER` | Recovery node proposes an adjustment **[node]** | `DISCOVER`, `EVALUATE`, or `COMMIT` (whichever the adjustment targets — see §6), `attempt_count += 1` |
| `RECOVER` | Recovery node declines to propose anything, or its call fails past local retry / budget exhausted **[det]** | `ABANDONED` |

`RECOVER` is the only state with an edge back to an earlier state. That is
deliberate and is the load-bearing fact behind the termination proof in §7:
every loop in this graph passes through `RECOVER`, and `RECOVER` cannot loop
without spending one unit of `attempt_count`.

---

## 3. The `AgentState` object

One mutable object threaded through every phase. Field types are exact —
money is `int` paise throughout, matching `core.mandate` and
`merchant/quote.py`.

```python
@dataclass
class AgentState:
    run_id: str
    phase: Phase

    # authority — set once at entry, never mutated
    intent_envelope: dict            # verified signed envelope, as received
    user_id: str                     # denormalised from intent payload
    agent_id: str
    max_paise: int
    max_purchases: int
    purchases_used: int              # from the intent payload at entry

    # working state — written by node outputs, read by the next phase
    plan: dict | None = None                 # Planner's strategy
    candidates: list[dict] = field(default_factory=list)   # Discovery's output
    selected: list[dict] = field(default_factory=list)     # [{sku, qty}] ONLY — see §4
    quote: dict | None = None                # merchant's quote response
    cart_mandate_envelope: dict | None = None
    checkout_result: dict | None = None      # Gate's response, success or refusal

    # bounded counters — see §7
    attempt_count: int = 0
    negotiation_turns: int = 0
    model_calls_used: dict[Phase, int] = field(default_factory=dict)

    # diagnostics
    last_failure: dict | None = None         # {reason, code, recoverable, detail}
    log: list[dict] = field(default_factory=list)   # append-only, for the UI
    terminal_reason: str | None = None       # set on entering a terminal state
```

Notes on specific fields:

- `intent_envelope` is stored as received and never rewritten. If any code
  needs the payload, it re-derives it via `core.mandate.verify`, never by
  trusting a cached, unverified copy — verifying once at entry proves
  authenticity for this run; it does not license treating later reads as
  pre-verified forever if the object were ever serialised and reloaded.
- `selected` is the single most sensitive field in this object. Its shape is
  fixed and enforced — see §4.
- `quote` holds exactly what the merchant returned: `quote_id`,
  `total_paise`, `expires_at`, and the priced line items. `agent.py` never
  edits any of these; it only reads `total_paise` and `cart_hash`-relevant
  line data straight into `make_cart_mandate`.
- `model_calls_used` is per-run accounting for §8's budget checks. It is not
  the actual rate limiter — that is a token bucket in `buyer/llm.py`, shared
  across all 17 agent surfaces. This dict is `agent.py`'s own ledger so it
  can refuse to call a node *before* asking `buyer/llm.py` to do something
  that would 429.

---

## 4. The signing boundary — the most important section

Draw this line once, in code, in one place, and never let a model cross it.

### What the executor does itself

Everything below is plain deterministic code in `agent.py` (or a helper it
calls directly). No model is in the loop for any of it.

1. **Loads the signing key once**, via `core.mandate.load_signing_key(name)`,
   and holds it as a `SigningKey` object for the run's lifetime. It is never
   serialised into `AgentState`, never placed in a dict that gets logged,
   and never passed as an argument to anything that constructs a prompt.
2. **Requests the quote** — an HTTP call to the merchant, not a decision.
3. **Checks quote validity** — `quote["expires_at"] > now()` — a
   comparison, not a judgment call.
4. **Computes the cart hash** — `core.mandate.cart_hash(items)` over the
   line items the *merchant* returned in the quote response, never over
   anything a model wrote.
5. **Builds the Cart Mandate payload** —
   `core.mandate.make_cart_mandate(intent_mandate_id=..., agent_id=...,
   merchant_id=..., quote_id=..., cart_hash=..., total_paise=...)` — every
   argument sourced from the verified intent envelope or the merchant's own
   quote response.
6. **Signs it** — `core.mandate.sign(payload, sk)`.
7. **Submits it** — `POST /checkout` with the resulting envelope.
8. **Interprets the result and drives the transition table** in §2.

### What a model node is permitted to return

A model node proposing a cart — Evaluator, Buyer Negotiator, or Recovery
when it adjusts a cart — may return exactly one shape and nothing else:

```python
list[{"sku": str, "qty": int}]
```

No price. No total. No `cart_hash`. No mandate field of any kind, signed or
otherwise. No key material. That is the entire contract. Planner and
Discovery return their own shapes (a strategy dict; a candidate list) but
neither of those ever reaches `make_cart_mandate` — only `selected` does,
and `selected` is validated against the schema above before it is used for
anything.

### What happens if a node returns something outside that shape

**Reject outright. Do not sanitise.** Validate with a strict schema —
`extra="forbid"`, `qty` a positive int, `sku` a non-empty string — and if
validation fails, the entire proposal is discarded as a node-level failure.
It counts against that phase's local retry (§7); if it keeps happening, the
run goes to `FAILED`, not to a version of the cart with the bad field
quietly dropped.

This matters for a reason beyond hygiene. Silently stripping an unexpected
`total_paise` a model hallucinated is indistinguishable, in the logs, from
the model never having produced it — but a model that starts emitting price
fields is very plausibly a model whose context has been primed to do that
by something it read, such as a poisoned catalog description (§10). That
behaviour needs to be *visible*, because the red team is specifically
testing for it. Sanitising it away would hide the exact signal the
adversarial suite exists to surface.

### Why the key never enters a prompt or a tool result

Prompt content — the system prompt, the conversation, every tool result fed
back to the model — is attacker-reachable surface in this project by design:
the catalog descriptions the model reads are literally written by an
Injector agent (#14) to manipulate what the model does next. Anything that
ever appears in that context is not secret anymore, whether or not the
model echoes it back, because a sufficiently well-crafted injection could
induce the model to try to exfiltrate it through a tool call, and because
the request/response traffic to a third-party API is not a trust boundary
this project controls. The only way to guarantee the key is never at risk
from a prompt injection is for no code path that a model's output can
influence to ever read it. The key lives in step 1 above and nowhere a
model-derived value flows into it.

---

## 5. The human consent point

The Intent Mandate is signed by the **user**, not the agent — and this is
the one manual step in an otherwise fully autonomous flow. It is what makes
everything downstream legitimate: the agent can shop, negotiate, and pay
with zero further human interaction precisely because a human already
looked at, in plain English, exactly what they were authorising.

This happens **before** `agent.py`'s entry point is ever called, driven by
the Intent Compiler (#7):

1. The user types a sentence — *"get me running shoes under ₹5,000."*
2. Intent Compiler turns it into a draft payload via
   `core.mandate.make_intent_mandate(...)` — **unsigned**.
3. Intent Compiler renders a plain-English readback of every field that
   matters: category, `max_paise` formatted as rupees, `max_purchases`,
   `expires_at` in a human timeframe, `merchant_id` (or "any merchant"),
   `agent_id`. The readback is shown verbatim, not summarised further.
4. The user gives an explicit affirmative response to that specific
   readback — a literal confirmation ("confirm" typed, or a UI button
   labelled to sign, not silence, not a default, not inferred from the
   conversation continuing). What counts as confirmation is a fixed, small
   set of unambiguous tokens; anything else is treated as "not confirmed"
   and the flow stops there.
5. Only on confirmation is `core.mandate.sign(payload, user_signing_key)`
   called — with the **user's** key, distinct from the agent's signing key
   used in §4.
6. The resulting signed envelope is what gets passed into `agent.py`'s entry
   point, where §2's precondition checks run.

`agent.py` itself has no readback UI and makes no confirmation decision —
that is Intent Compiler's job. What `agent.py` is responsible for is
refusing to start on anything it cannot verify (§2's precondition), which is
the downstream half of taking this consent step seriously: a readback the
user approved is worthless as an authority document if the code that
consumes it doesn't check the signature is still the one the user actually
produced.

---

## 6. `RECOVER` in detail

### Attempt cap: **3**, hard, non-negotiable

`ATTEMPT_CAP = 3`. `attempt_count` starts at 0 and increments only inside
`RECOVER`, only on the branch where it decides to loop back (§2's table).
Reaching `attempt_count >= ATTEMPT_CAP` forces `ABANDONED` regardless of
whether the most recent failure was, in isolation, recoverable.

Reasoning: three total checkout attempts (one initial pass plus two
recovery-adjusted retries) is enough to demonstrate genuinely adaptive
behaviour — dropping an item, substituting a cheaper option, re-quoting
after expiry — without turning the demo into an unbounded negotiation
against the merchant. It also keeps the worst-case model-call count (§7)
comfortably inside the shared 15-request-per-minute Gemini budget even when
other agent surfaces are calling the same API in the same window. Raise this
number and both of those guarantees weaken; this is the number, not a
placeholder.

### The invariant: a retry never produces a second order

This follows the project rule directly: idempotency keys on `quote_id`, and
the Gate enforces it merchant-side regardless of what the buyer does. What
`agent.py` must get right on its side:

- **If the quote is still valid** (`expires_at` in the future), a retry
  reuses the **same `quote_id`**. It builds a new Cart Mandate against that
  same quote (a new `nonce`, a new `mandate_id`, same `quote_id`, same
  `total_paise`) and resubmits. If the earlier attempt never reached the
  payment gateway (e.g. it was a Gate refusal), there is nothing to
  duplicate. If it did reach the gateway and failed there (`PAYMENT_FAILED`),
  the same `quote_id` idempotency key ensures the gateway/Gate side cannot
  create a second order even if the retry succeeds where the first attempt
  didn't.
- **If the quote has expired**, `agent.py` must **not** resubmit against the
  stale `quote_id` — a Cart Mandate built over an expired quote is not
  something the Gate should ever be asked to approve, and retrying it wastes
  an attempt on a guaranteed refusal. Instead: request a **fresh quote**
  (fresh HTTP call, fresh `quote_id`), build a **new** Cart Mandate bound to
  that new `quote_id`, and treat this as a new attempt
  (`attempt_count += 1`, same as any other RECOVER-driven retry). This is
  not "resuming" the earlier attempt — it is a new checkout attempt that
  happens to want the same cart. Nothing was ever charged against the
  expired `quote_id`, so there is no duplication risk; the invariant holds
  because the two `quote_id`s are simply two different attempts, each
  individually idempotent.

`QUOTE_EXPIRED` is a deliberate failure case the project wants to
demonstrate (§8 of `config.py`'s comments: 90-second TTL, short on purpose).
Expect this to fire in the demo when negotiation or a slow model call eats
into the 90 seconds. It is not an error state to suppress; it is a
transition to exercise on camera.

### Recoverable vs. terminal failure classes

| Reason | Recoverable? | RECOVER's response |
|---|---|---|
| `NO_CANDIDATES` | yes | Discovery widens the search (looser category/tags), loop to `DISCOVER` |
| `NO_FIT` | yes | Evaluator reconsiders with relaxed fit criteria, loop to `EVALUATE` |
| `QUOTE_UNAVAILABLE` | yes | drop or substitute the unavailable line, loop to `EVALUATE` |
| `QUOTE_EXPIRED` | yes | re-quote the same cart, loop to `COMMIT` (see invariant above) |
| `NEGOTIATION_STALEMATE` | yes | accept current price if under budget, else drop an item, loop to `COMMIT` |
| `PAYMENT_FAILED` | yes | retry against the same `quote_id` if still valid, loop to `COMMIT` |
| `GATE_REFUSAL`, `recoverable: true` | yes | adjust per the Gate's `detail` (e.g. drop the item that pushed the cart over `max_paise`), loop to `EVALUATE` or `COMMIT` depending on whether cart composition changes |
| `GATE_REFUSAL`, `recoverable: false`, security/integrity family (forged signature, tampered `cart_hash`, replayed nonce, mandate/agent mismatch) | **no** | straight to `FAILED` — this implies a bug in this code or an active attack, and the agent must never attempt to "fix" a forged signature by trying again |
| `GATE_REFUSAL`, `recoverable: false`, permanent business family (category never covered by this intent, `merchant_id` mismatch when the intent pins one merchant) | **no** | straight to `ABANDONED` — no adjustment makes this cart legal under this mandate |

The exact code taxonomy behind the two `recoverable: false` rows is
`gate-spec.md`'s contract, not this file's. What is fixed here is the
*shape* of the response — a structured refusal always carries `code`,
`recoverable`, and `detail`, and RECOVER's classification logic is the one
place in this codebase that maps codes to a recover/fail/abandon decision.
When `gate-spec.md` lands with its actual code list, only that mapping
table needs updating — the state machine around it does not change.

---

## 7. Termination guarantees

The claim: **this machine cannot run forever.** The proof rests on one
structural fact plus three bounded counters.

**Structural fact.** `RECOVER` is the only state in the transition table
with an edge back to an earlier phase. Every other phase either advances
forward or terminates. So any infinite run would have to pass through
`RECOVER` infinitely many times.

**Bound 1 — `attempt_count`, cap 3.** `RECOVER` increments this on its only
loop-back branch and forces `ABANDONED` once it hits the cap (§6). So
`RECOVER` can loop back at most 3 times. Combined with the structural fact:
the whole graph can be entered at most 1 (initial) + 3 (recoveries) = **4
full passes**, ever.

**Bound 2 — `negotiation_turns`, cap 4 per `COMMIT` attempt
(`NEGOTIATION_TURN_CAP = 4`).** Within a single pass through `COMMIT`, the
Buyer Negotiator ↔ Merchant Negotiator exchange cannot run indefinitely
either; it forces `NEGOTIATION_STALEMATE` at the cap. This bounds the work
done inside one `COMMIT` visit, not just the number of visits.

**Bound 3 — local retry, cap 1 per model call
(`LOCAL_RETRY_CAP = 1`).** Within any single phase, if a node's raw output
fails schema validation (§4) or the call raises, `agent.py` retries that one
call once with a corrective note, then treats it as a phase-level failure
per the transition table. No phase can spin on a malformed response.

**Putting a number on it.** One pass's model-call budget, worst case with
every local retry firing: PLAN (1×2) + DISCOVER (2×2) + EVALUATE (1×2) +
COMMIT (1 quote-side call + up to 4 negotiation turns, ×2) = 2+4+2+10 = 18.
Four passes: 72. Add RECOVER's own diagnose call, up to 3 times, with its
own local retry: 6. **Ceiling: ~78 model calls for any single run, in the
absolute worst case.** A normal successful run — no refusals, no expired
quote, negotiation settling in one or two turns — uses on the order of 10.
The ceiling exists to prove the machine halts and stays inside budget even
in the worst case; it is not what a demo run is expected to look like, and
if it starts looking like the ceiling every run, that's a signal something
upstream (the merchant's own limits, the catalog, the negotiator's
concession strategy) needs tuning, not that the cap should be raised.

This is also the argument for why `ATTEMPT_CAP` and `NEGOTIATION_TURN_CAP`
are set where they are: raising either grows the ceiling multiplicatively,
and the ceiling has to stay well inside 15 requests/minute shared across
every other agent surface that might be calling Gemini in the same window.

---

## 8. Rate-limit awareness

Gemini free tier: **15 requests/minute, 1,500/day** (`config.GEMINI_RPM_LIMIT`,
`config.GEMINI_DAILY_LIMIT`), shared across all 17 agent surfaces in the
project — not just this state machine. `buyer/llm.py` owns the actual token
bucket; `agent.py`'s job is to never hand it a call it already knows it
shouldn't make.

Per-phase model-call budget (the number this file checks
`model_calls_used[phase]` against **before** calling a node):

| Phase | Budget | What it's spent on |
|---|---|---|
| `PLAN` | 1 | Planner strategy |
| `DISCOVER` | 2 | merchant lookup + catalog search read |
| `EVALUATE` | 1 | fit judgment over candidates |
| `COMMIT` | 1 + up to `NEGOTIATION_TURN_CAP` (4) | one call to review the quote, up to 4 negotiation turns |
| `RECOVER` | 1 per attempt | diagnose + propose an adjustment |

**Before** calling a node, `agent.py` checks the phase's remaining budget.
Exhausted → straight to `FAILED` (reason `RATE_LIMIT_EXHAUSTED`), never a
retry-and-hope. This is a **proactive** check, not a `try/except` around the
429. The reason that distinction matters: catching a 429 after the fact
still burns the request that triggered it, still costs the wall-clock delay
of making the call, and still risks leaving `AgentState` in a half-updated
condition if the exception surfaces mid-write. A budget check that runs
before the call costs nothing and fails cleanly. A demo that dies on a 429
partway through is a design failure — the budget was knowable in advance —
not bad luck, and this is the section that makes it knowable.

---

## 9. Failure and observability

Every phase transition appends one entry to `AgentState.log`:

```python
{"ts": <unix seconds>, "phase": <Phase>, "event": <str>, "detail": <dict>}
```

This is what the `rich` terminal UI renders live — it does not reach into
`AgentState`'s working fields directly, it reads the log stream. Log
`event` strings are the vocabulary the UI's "middle panel" (per plan.md's
demo layout) is built around, so keep them stable and few: node calls
(`node_call`, `node_result`), transitions (`transition`), refusals
(`gate_refusal`), and terminal entry (`terminal`).

On entering any terminal state, `terminal_reason` is set — a single
human-readable sentence, always, unconditionally. It must not depend on a
model call succeeding: even if the Refusal Explainer (#6) or Recovery (#12)
would normally produce a nicer sentence, `agent.py` sets a deterministic
fallback templated from `last_failure` (e.g. *"Stopped after 3 attempts:
the merchant refused the cart for exceeding the ₹5,000 mandate ceiling, and
no cheaper substitute was in stock."*) before or instead of anything a model
might add. Availability of an explanation for why the agent stopped must
never itself depend on the thing that just failed (a model call, an API,
the network).

---

## 10. The adversarial case

The red team's core demo moment: a poisoned product description (Injector,
#14) tries to talk the Evaluator or Buyer Negotiator into overspending —
"this is a limited restock, act now," a description that nudges the model
toward a pricier substitute, or similar.

**The honest position for this file: the buyer agent may be fooled. That is
the point of the demo.** This spec does not add prompt-hardening,
description-sanitisation, or any other defence against injected content
inside `agent.py` or its node modules, and it should not pretend to. Trying
to defend against injection in the layer that reads untrusted natural
language is a losing game, and pretending otherwise here would undercut the
actual architectural claim of the project.

The real defence is entirely merchant-side: the Gate re-derives the total
from its own catalog prices, checks it against the mandate's `max_paise`,
and refuses regardless of what the model was convinced of. From `agent.py`'s
point of view, a buyer talked into overspending by a poisoned description is
indistinguishable from any other `GATE_REFUSAL` — it is not a special case
in the transition table, it is exactly the `GATE_REFUSAL, recoverable: true`
row in §6's table (assuming the refusal reason is an ordinary over-limit
code), handled by `RECOVER` the same way an honest price miscalculation
would be. That the enforcement doesn't need to know the refusal was
triggered by an attack, rather than an ordinary mistake, is the point: the
Gate refuses on the numbers, not on intent.

---

## 11. Check yourself in this order

Each step assumes the previous one passes. Don't skip ahead.

1. **Construct + verify only.** Build an `AgentState` from a hand-built
   signed intent envelope (use the mandate-spec test vectors). Confirm
   `agent.py` raises before entering `PLAN` when the signature is tampered,
   and proceeds normally when it isn't.
2. **Happy path with fake nodes.** Stub every LLM node as a fixed-return
   fake — no network, no Gemini calls. Drive `PLAN → DISCOVER → EVALUATE →
   COMMIT → COMPLETED` against a real `merchant/quote.py` and a real (or
   locally running) Gate. This proves the transition table's forward path
   and the signing calls, independent of any model behaviour.
3. **Prove the signing boundary.** Make the fake Evaluator return
   `{"sku": "X", "qty": 1, "unit_paise": 1}` — an extra price field.
   Confirm `agent.py` rejects the whole proposal and does not let
   `unit_paise` anywhere near `make_cart_mandate`.
4. **Force one refusal.** Mock the Gate to return a `recoverable: true`
   refusal. Confirm `RECOVER` receives it, `attempt_count` increments
   exactly once, and the machine loops back to the correct phase.
5. **Force three refusals in a row.** Confirm `ABANDONED` fires at exactly
   the third, never a fourth pass through `PLAN`/`DISCOVER`/`EVALUATE`/`COMMIT`.
6. **Force an expired quote mid-`COMMIT`.** Fake the clock past
   `config.QUOTE_TTL_SECONDS`. Confirm the retry requests a fresh
   `quote_id` rather than resubmitting the stale one, and that
   `attempt_count` still increments correctly.
7. **Swap in real nodes one at a time**, watching `model_calls_used` against
   §8's per-phase budget as you go — Planner first, then Discovery, and so
   on, so a budget miscalculation shows up against one node at a time
   instead of all at once.
8. **Full run, real everything.** One typed sentence in, through Intent
   Compiler's readback and your own confirmation, to a `terminal_reason` out
   and a ledger entry present — against the real merchant and Razorpay test
   mode.

---

## 12. If you get stuck

**The agent loops between `EVALUATE` and `COMMIT` seemingly forever** —
almost certainly a transition is being taken that isn't in §2's table, most
likely `RECOVER` looping back without incrementing `attempt_count`.
`attempt_count += 1` must live inside `RECOVER`'s own code path, not
wherever it jumps to.

**A 429 kills the demo mid-run** — a phase called a node without checking
`model_calls_used` against its §8 budget first. The check has to happen
*before* the call, as a guard, not wrapped around the call as exception
handling.

**The same order shows up twice in the ledger** — a retry resubmitted a
cart mandate built from a stale `quote_id` instead of fetching a fresh one.
Check the `QUOTE_EXPIRED` branch specifically; this is the one place a
retry is allowed to use a *different* idempotency key, and it's easy to
accidentally reuse the old one out of habit.

**A price sneaks into a signed cart mandate** — the schema check on a
node's return value isn't using `extra="forbid"`, or the field got read
before validation ran instead of after. Validate first, use the value
second, always in that order.

**Everything works except the very first real run** — the intent envelope
handed to `agent.py`'s entry point was never actually run through
`core.mandate.verify` before being trusted as state; it was just assumed
good because it came from Intent Compiler. `agent.py` verifies its own
input regardless of who produced it.

**Stuck past 30 minutes on one thing** — stop and ask. Same rule as the
mandate spec: a design bug found while building costs minutes, the same bug
found on day 6 costs an afternoon.

---

## 13. Done when

- [ ] `Phase` enum matches §2 exactly; every transition in the table is
      implemented and no transition outside the table exists
- [ ] Entry point verifies the intent envelope and checks expiry/exhaustion
      before entering `PLAN`
- [ ] `AgentState` matches §3; every money field is `int` paise
- [ ] The signing boundary in §4 holds: `agent.py` loads the key, builds the
      cart mandate, and signs it; every model node's return is validated
      against `list[{"sku": str, "qty": int}]` with `extra="forbid"` before
      use, and a violation rejects the whole proposal
- [ ] `ATTEMPT_CAP = 3`, enforced inside `RECOVER` only
- [ ] Retrying never resubmits a stale `quote_id`; expired quotes get a
      fresh one and count as a new attempt
- [ ] The recoverable/terminal classification in §6's table is implemented,
      with the two `recoverable: false` rows going to `FAILED` and
      `ABANDONED` respectively, not both to the same place
- [ ] `NEGOTIATION_TURN_CAP = 4` enforced inside `COMMIT`
- [ ] Per-phase model-call budgets from §8 are checked before each node
      call, not after
- [ ] Every terminal state sets a deterministic, non-model-dependent
      `terminal_reason`
- [ ] All eight steps in §11 pass, in order
