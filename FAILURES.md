# What broke, and how I got out

Kept live from day one. Razorpay's form asks for this and says it's the answer
they read first, so it's written as things break — not reconstructed afterwards.

---

## 2026-08-25 — Razorpay's docs say test mode needs no KYC. The dashboard disagrees.

**What broke.** Razorpay's documentation is explicit that Test Mode requires no
KYC: *"You can generate API keys in Test Mode without adding a website."* In
practice, signing up redirects to `easy.razorpay.com`, which hard-gates on a
**Business PAN** — a single field, no skip link, no way back to the dashboard.
`dashboard.razorpay.com/app/keys` and every other direct URL just bounce to login.
I burned close to an hour trying documented routes that assumed a dashboard I
could not reach.

**How I got out.** Submitted a personal PAN (the form itself says to, if you're
unregistered), then chose business type **Individual** rather than Sole
Proprietorship — Sole Proprietorship demands GST/Udyam documents I don't have.
That unlocked a **"Get test keys"** button in the top nav, which issues test keys
*without completing onboarding at all*. No bank account, no activation, no waiting
on verification.

**What I'd tell the next person.** The button exists, but only after business-type
selection, and it's in the nav bar rather than the flow — so if you're following
the docs step by step you never see it. Don't fill in bank details; you don't need
them.

**Cost:** ~1 hour. **Verified:** `HTTP 200` from `api.razorpay.com`, live
`order.create` returning `amount: 9900` as `int` paise.

---

## 2026-08-25 — The Gemini model the API advertises 404s when you call it.

**What broke.** Anthropic has no free API tier, so the buyer agent moved to Google
Gemini's free tier. `GET /v1beta/models` listed `models/gemini-2.5-flash`, so I
wired that into `config.py`. The first real `generateContent` call returned **404**:
*"This model is no longer available to new users."*

**How I got out.** Caught it because I tested tool-calling with a real request
instead of trusting the model list — listing models only proves the key
authenticates, not that a model is callable on this key. Switched to
`gemini-3.6-flash`, which the error message named. Re-ran the same tool-calling
test: correct function call, and it converted "under 5000 rupees" to `500000`
paise correctly.

**What I'd tell the next person.** A model appearing in the list endpoint does not
mean your key can call it. Availability is per-account, and the list endpoint
doesn't reflect that. Test the actual call.

**Cost:** ~10 minutes, because it was caught at setup rather than mid-agent-loop.

---

## 2026-08-26 — A green test run that wasn't running my tests.

**What broke.** `uv run pytest tests/test_quote.py` failed with
`ModuleNotFoundError: No module named 'dotenv'` — a module that was definitely
installed, in a venv `uv run python -c "import dotenv"` was perfectly happy with.
The dev extras had never been synced, so `pytest` wasn't in the project venv at
all. `uv run` fell through to a **system** pytest on `PATH`, which ran under a
different interpreter that couldn't see the project's dependencies.

**How I got out.** `uv sync --extra dev`. Ten seconds, once I stopped reading the
import error as an import problem and started reading it as a *which interpreter
is this* problem.

**What I'd tell the next person.** `uv run <tool>` does not guarantee the tool
came from your venv. If it isn't installed there, uv will happily run whatever
is on `PATH` and the failure looks like a broken dependency rather than a
missing one. The tell is that the error names a package you know you have.
Worse than the ten minutes lost: a system pytest that happens to *work* gives
you a green run against the wrong environment, and you trust it.

**Cost:** ~10 minutes.

---

## 2026-08-26 — `TEMPERATURE = 0.0` is doing nothing.

**What broke.** Nothing, visibly — which is why it is worth writing down.
`config.py` sets `TEMPERATURE = 0.0` on the reasoning that deterministic
selection matters more than creative phrasing. While verifying tool-calling
against a real Gemini call, the API returned a warning: `gemini-3.6-flash` uses
fixed sampling and **ignores the `temperature` parameter entirely**.

**How I got out.** Nothing to fix — but the belief behind the setting was wrong,
and an unexamined wrong belief on a demo path is a bug waiting for a bad moment.
The setting stays for the other two providers; what changed is the claim. I can
no longer say "the model is deterministic because temperature is zero." Agent
output on Gemini is *not* reproducible run to run.

**What I'd tell the next person.** This matters more here than in most projects,
because the whole architecture rests on a line between deterministic code and
non-deterministic models. If you claim determinism, claim it where it is
actually true — the vault, the quote engine, the gate — and not for a model
whose provider quietly ignores the knob you set.

**Cost:** zero, because it surfaced in a warning I read rather than a demo I lost.

---

## 2026-08-26 — Two agents built the same vocabulary twice, and disagreed.

**What broke.** I ran five subagents in parallel on file-disjoint work. Two of
them wrote specs that had to interoperate: the ledger spec defined a **closed**
list of audit event types (`gate.passed`, `gate.refused`, dotted lowercase), and
the gate spec — written simultaneously, unable to see the other — independently
invented `GATE_PASS` and `GATE_REFUSAL` for the same two events. Both documents
were internally consistent and confidently written. Left alone I would have
implemented `ledger.py` against one vocabulary and `gate.py` against the other
and found out at integration, on a day with no slack in it.

**How I got out.** Diffed the event names across the two specs before accepting
either — `grep -oE` for the event-shaped tokens in each file, sorted, compared.
The ledger owns that vocabulary because its list is the closed one, so the gate
spec was the one that got rewritten, along with two references to test files
that don't exist.

**What I'd tell the next person.** File-disjoint is not the same as
conflict-free. Two agents can respect every file boundary you set and still
produce work that cannot be integrated, because the thing they collided on was a
shared *concept*, not a shared file. If parallel work has to interoperate, one
side has to own the shared vocabulary and the other has to be told to consume
it — and you have to check, because both will sound certain.

**Cost:** ~5 minutes to catch, because I looked. Would have been an afternoon in
Phase 2 if I hadn't.

---

## 2026-08-26 — Idempotency that stops a second row, not a second order.

**What broke.** Found in code review rather than at runtime, which is the only
reason it is cheap. `merchant/gateway.py` made order creation idempotent on
`quote_id` with two layers: an in-process lock, and a `UNIQUE` constraint as the
fallback. The constraint reliably guaranteed **one row per quote_id**. It did
not guarantee **one order per quote_id**.

Two processes (two uvicorn workers, say) racing the same `quote_id` both passed
the "not on file yet" check, both called Razorpay, and both got a real order
back. Only one INSERT then won; the loser returned the winner's row and
discarded its own. The discarded order still existed at Razorpay — orphaned,
unreferenced, and invisible to the merchant's own records.

**How I got out (2026-08-27, actually fixed).** Rewrote `create_order()` around
reservation-first ordering instead of call-then-record. The `orders` table's
`order_id` column is now nullable and rows move through `pending` (claimed,
gateway not yet called) → `created` (gateway confirmed, order_id on file), with
a third `reclaiming` state for taking over an abandoned reservation. A caller
first tries to INSERT a `pending` row for `quote_id` inside a `BEGIN IMMEDIATE`
transaction — SQLite's IMMEDIATE lock, not the old Python-level
`threading.Lock`, is what actually excludes a second writer, and it does so
across processes as well as threads, because it is enforced by the database
file itself. Only the caller whose INSERT lands may call the gateway. A caller
that loses the claim never calls the gateway: if the row is already `created`
it returns that order (`from_cache=True`); if it is still `pending` it polls,
without holding any lock while waiting, for the owner to finish. A gateway
failure (exception, an unconfirmed amount, or a garbage order id) deletes the
reservation so a retry claims a clean slate — preserving the original
`OrderCreationError` promise that "a failed attempt leaves no row behind to
collide with."

**What's actually closed.** The exact scenario above — two live callers racing
the same `quote_id`, both reaching Razorpay — is now excluded by construction,
not just observed and reported. Proven with a real `threading` regression test
(`test_reservation_first_closes_the_cross_process_double_call_race` in
`tests/test_gateway.py`): two threads released together via a `Barrier` against
a gateway that sleeps before answering, asserting the gateway is called exactly
once and both callers get the same `order_id`.

**What's still open — narrower, but honest.** If a caller's process dies
*after* the gateway confirms an order but *before* the finalizing `UPDATE`
commits, its reservation is left `pending` (or `reclaiming`) forever. A later
caller for the same `quote_id`, after waiting out a timeout, will reclaim that
abandoned reservation and call the gateway again, creating a second real order.
This needs two independent failures — a crash landing in that exact
few-millisecond gap between "gateway answered" and "row written" — rather than
ordinary concurrency, and a single-process demo cannot hit it. It is not
claimed to be closed; a real fix needs either a durable outbox/reconciliation
step against Razorpay's own order-by-receipt lookup, or idempotency support on
the gateway's side, and that is out of scope for this pass.

**What I'd tell the next person.** A `UNIQUE` constraint makes your *database*
idempotent. It does nothing about the side effect you already performed before
reaching it. If the expensive, irreversible action happens before the
ownership of the attempt is settled, the constraint is deduplicating your
records, not your actions — and with a payment gateway those are very
different things. Settling ownership *before* the side effect (reserve, then
call) is what actually closes the race; the constraint is only ever a backstop
for whatever the reservation step itself failed to prevent.

**Cost:** none. Caught in code review before the original fallback shipped to
a demo, and closed before the live-money-path work in Phase 3.

---

## 2026-08-26 — `INSERT OR REPLACE` reset a spend counter that a mandate depends on.

**What broke.** Phase 2 (the ledger, the Gate, the merchant API) was built by
six subagents running in parallel on file-disjoint slices. `merchant/intent_store.py`
was one of those slices, and it used `INSERT OR REPLACE` to register an Intent
Mandate by `mandate_id`. That statement doesn't merge a row, it overwrites the
whole thing. The Gate enforces the user's `max_purchases` cap by reading a
`purchases_used` counter stored on that same row, so re-registering an existing
`mandate_id` silently reset the counter back to 0 — restoring a spent purchase
cap out from under the Gate. The same overwrite would let a repeat `mandate_id`
swap in a different stored payload too, including a raised `max_paise`. Nothing
in Phase 2 exposes intent re-registration yet, so this wasn't reachable at
runtime — but the buyer-side intent-grant path is exactly what Phase 4 adds, and
I found this in a manager review pass over the six slices, not by hitting it.

**How I got out.** Switched the statement to
`INSERT ... ON CONFLICT(mandate_id) DO NOTHING`. An Intent Mandate is now
immutable once granted: a repeat `mandate_id` is ignored outright, so both the
original payload and the purchase counter survive untouched. This is correct,
not lossy, because a genuine new grant always carries a fresh uuid `mandate_id`
— there's no legitimate case where the same id should ever mean a different
intent. Added a regression test that registers, spends twice, re-registers with
the same `mandate_id`, and asserts the counter is still 2.

**What I'd tell the next person.** `INSERT OR REPLACE` is a full-row overwrite
dressed up as an upsert — fine for a cache where any field can go stale, unsafe
the moment the row also holds security-relevant state like a spend counter,
because "replace" doesn't know which columns are cache and which are ledger.
And the parallel-agent lesson repeats: file-disjoint slices don't get you
cross-cutting correctness for free. Each of the six files was locally
reasonable on its own; this only surfaced because someone reviewed all six
against each other afterward.

**Cost:** caught in review, minutes to fix. Would have been a real
authorization bypass if it had reached the Phase 4 intent-grant endpoint
unnoticed.

---

## 2026-08-26 — Benchmarking NVIDIA's free tier against Gemini: fast or correct, not both.

**What broke.** Nothing in the shipped code — this was a deliberate check
before committing to Gemini as the only model, to see whether NVIDIA's free API
tier was a better fit for the 17 agent surfaces. The axis that actually matters
for this project is tool-calling plus correct number handling, because the
intent-compiler drafts the `max_paise` value the user signs — a wrong number
there is not a UX bug, it's a wrong mandate. I ran the same prompts against
three models. Gemini `gemini-3.6-flash` went 3/3, median ~3s, correct tool
call, and converted "at most ₹5000" to `500000` paise correctly. NVIDIA's
high-quality model, `meta/llama-3.3-70b-instruct`, was also correct when it
answered — but free-tier latency was a median of **37.7s**, with one of three
calls eating the full 90s timeout. Unusable for a live demo firing many agent
calls. NVIDIA's fast model, `meta/llama-3.1-8b-instruct`, was the opposite
problem: **~0.4s** median, passed the tool call, but on the mandate-drafting
task it returned `max_paise: 5000` where `500000` was required — it never
scaled rupees to paise. A 100x money error, in the one task that feeds a signed
mandate.

**How I got out.** There wasn't a fix to make, just a decision to take: on
NVIDIA's free tier the model fast enough to demo with is the one that miscounts
money, and the model that gets the money right is too slow to demo with. Kept
Gemini 3.6 Flash as the default for everything numeric or judgment-critical.
Wired the NVIDIA 8B in only as a fast lane for surfaces that emit no number
anyone relies on — storefront chat, refusal-explainer wording, substitution
suggestions, the ledger auditor's narration, red-team injection copy — so its
paise weakness is structurally out of reach rather than merely unlikely to
trigger.

**What I'd tell the next person.** Speed is worthless if the model is wrong on
the one value the whole system exists to protect — test the number, not just
whether the call comes back. And "free tier vs. free tier" isn't a vendor
comparison, it's a which-hosted-model comparison; NVIDIA's own two free models
disagreed with each other by two orders of magnitude and forty seconds.

**Cost:** ~an hour of benchmarking. Net gain — the fast lane speeds up prose
surfaces without ever touching the money path.

---

## 2026-08-27 — The live run works; the test card is "international" and blocked.

**What broke.** First real end-to-end run against test-mode Razorpay. The
autonomous half was flawless: intent granted, quoted (₹5,898.82), Cart Mandate
signed, Gate passed, and a real order (`order_TUZLFFTIF2U0Th`) created via the
Orders API. Opened the checkout page, entered the canonical Razorpay test card
`4111 1111 1111 1111` — and got *"Payment could not be completed. International
cards are not supported."* Retried three times: three real `payment.failed`
webhooks, no capture.

**How I got out.** Two facts. First, this was never a bug in our code — the
order, the checkout page and the webhook receiver all worked; the three failed
attempts each produced a real, HMAC-verified `payment.failed` in the ledger, so
the webhook round-trip proved itself on live Razorpay events *before* a single
success. Second, the block is account config, not the card: Razorpay classifies
a test card's country by its BIN, and an Indian test account has international
payments off by default, so `4111…` (which Razorpay's own docs also list as
"domestic") gets rejected on this account. Switched to the **UPI test VPA
`success@razorpay`**, which is unambiguously domestic and is Razorpay's canonical
test-mode success path; a domestic Mastercard `5104 0155 5555 5558` also clears it.

**What I'd tell the next person.** Don't reach for the famous `4111` Visa on a
Razorpay test account — its country classification is per-account and will
sometimes trip the international block with no warning until checkout. Use
`success@razorpay` UPI for a reliable domestic test success and `failure@razorpay`
to exercise the failure path on purpose. And a failed payment is not a failed
integration: ours logged three real `payment.failed` events with genuine
`event_id`s, which is exactly the webhook round-trip the money path needed to prove.

**Cost:** minutes, once the error was read as account config rather than code.

---

## 2026-08-27 — The reservation-first rewrite needs a fresh orders.db.

**What broke.** After rewriting `create_order()` to reserve `quote_id` *before*
calling the gateway, `order_id` became nullable (a `pending` reservation has no
order id yet). But a `data/orders.db` left over from before the rewrite still
had the old `order_id TEXT NOT NULL` schema, and `CREATE TABLE IF NOT EXISTS`
does not alter an existing table — so the first `pending` INSERT (order_id NULL)
would hit a NOT NULL violation and surface as `UnexpectedIntegrityError`.

**How I got out.** `data/*.db` is gitignored, regenerable operational state, so
the fix is to move the stale db aside and let the new schema be created fresh.
No migration script — this store holds no history worth preserving (the audit
history lives in the ledger, which is a separate append-only store this never
touches), so recreating it is correct, not lossy.

**What I'd tell the next person.** `CREATE TABLE IF NOT EXISTS` silently keeps an
old schema. When a column's constraints change, that guard becomes a trap: new
code runs against an old shape and fails at the first write the new schema would
have allowed. For a throwaway operational store, delete and recreate; for
anything with history you'd need a real migration — which is exactly why the
ledger is kept separate, append-only, and out of this path.

**Cost:** caught on the first dry run, before the live run.

---

## 2026-08-27 — The first real capture, and one payment written to the ledger twice.

**What broke.** Two things, on the run that finally produced a successful
capture. First, the planned success path (UPI `success@razorpay`) didn't exist:
the account's checkout preferences endpoint returns `upi: False` — UPI is
disabled on this test account and, with the account unactivated, isn't a toggle
we could flip. The prior entry's "switched to UPI" resolution never actually
happened; **netbanking** was the real success path (any test bank → "Success"
on the simulated page → auto-captured). Second, once the capture landed, the
ledger held **two** `payment.succeeded` rows for the single payment
(`pay_TUaYZKpLh3vEIg`, seq 12 and seq 14). Razorpay announces one successful
payment with two distinct events — `payment.captured` *and* `order.paid`. Both
carry different bytes, so `webhooks.py` (which dedupes on the SHA-256 of the raw
body) correctly treats each as a genuine first delivery, and `api.py`'s
money-outcome branch — guarded only by that byte-level `replay` flag — appended a
success row for each. No double *charge* (one order, one payment id, order
marked `paid` once), but the audit log double-counted the win, which for a
project whose whole thesis is "a failed transaction never produces a second
payment or order" is exactly the wrong place to be loose.

**How I got out.** Added a second dedupe layer at the point that matters: before
appending `payment.succeeded`, scan the ledger for an existing success with the
same `razorpay_payment_id` and skip if present (`_success_already_logged` in
`api.py`). The dedupe key is the payment's money-truth identity, not the bytes
of whichever event announced it — so one captured payment maps to exactly one
success row no matter how many events describe it. `payment.failed` needs no
such guard: only one event type maps to "failed" and each failed attempt has its
own payment id. Pinned with a test (`test_captured_then_order_paid_logs_one_success`)
that fires both events for one payment and asserts a single success row; suite
252 green. Also made auto-capture explicit (`payment_capture: 1` in
`gateway.create_order`) so the terminal event is always `payment.captured` and
never a silent `payment.authorized` at the mercy of the account default —
though note the capture that exposed all this happened on an order created
*before* that change, which means the account default was already auto-capture;
the flag is correctness insurance, not the thing that fixed this run.

**What I'd tell the next person.** "Not a byte-identical replay" is a weaker
guarantee than "not the same event." Idempotency keyed on the raw-body hash
stops redeliveries but not two *different* provider events about one underlying
fact — and payment providers routinely fire several (`payment.captured`,
`order.paid`, sometimes more) for a single success. Dedupe money outcomes on the
provider's stable payment identifier, not on the delivery. The current guard is
a read-then-write ledger scan that's only safe because `/webhook` is
single-writer (no `await` between the scan and the append, one uvicorn worker);
a multi-process deployment would have to enforce this in the store, not in
application code.

**Cost:** ~an hour, most of it understanding that captured and order.paid are
two events for one payment rather than a bug in the dedupe that already existed.

## 2026-08-27 — The buyer agent's mocked tests all passed. The real model returned a list.

**What broke.** Phase 4's four LLM node modules (planner, discovery, evaluator,
intent_compiler) and the state machine (`buyer/agent.py`) were built with
hermetic tests — every `llm.invoke` mocked, `.content` a plain JSON string. 60+
tests green, a full suite of 310. Then the first live run against real
`gemini-3.6-flash` died on the very first call: `intent_compiler.draft_intent`
raised `NodeError: expected a string model response, got list`. A real Gemini
response returns `.content` as a **list of content blocks**
(`[{"type": "text", "text": "..."}]`), not a bare string. The mocks returned a
string because that is the obvious shape to fake — so they hid the one thing
only a live call could show. Every node would have failed identically in
production; the test suite could not see it.

**How I got out.** Added `nodes_common.message_text()` to coalesce a message's
`.content` — bare string, list of strings, or list of `{"text": ...}` blocks —
into plain text, and routed all four nodes through it before `extract_json`.
Pinned it with a `FakeBlockMsg` test that carries the real list-of-blocks shape,
and an end-to-end node test that a list-content response parses rather than
raising. The live run got further.

**Then two more the mocks couldn't surface, in the same session:**

- **The evaluator's "nothing fits" was a dead branch.** `agent.py`'s
  `_validate_selected` rejected an empty list as a *malformed shape*, so an
  evaluator returning `[]` (its documented no-fit signal) became a
  `NodeError → FAILED` instead of `RECOVER(NO_FIT)`. The entire NO_FIT recovery
  path in spec §2/§6 was unreachable and untested. A new test for the
  rate-limit path tripped over it. Fix: emptiness is a presence judgment the
  caller makes, not a shape violation — `_validate_selected` now returns `[]`,
  and EVALUATE turns it into NO_FIT. (The signing side still can never be handed
  an empty cart: EVALUATE only advances to COMMIT on a non-empty selection.)

- **The intent category didn't speak the merchant's language.** With the
  list-content bug fixed, a real "running shoes under ₹5000" run got a clean
  intent whose category was the natural phrase `"running shoes"`. The Gate
  compares `product["category"] != intent["category"]` **exactly**, and the
  catalog's category is `"footwear"`, so every shoe request was refused
  `CATEGORY_MISMATCH` — the LLM happy path could never complete. The agent
  *handled* it correctly (clean ABANDONED with a precise reason), which is why
  nothing crashed; it just could never succeed. Fix: a controlled vocabulary in
  `config.CATALOG_CATEGORIES`, and `intent_compiler` now constrains the model to
  emit one of those and maps it to the canonical spelling the Gate compares
  against ("running shoes" → "footwear") before anything is signed.

- **A payment retry could read a stale failure as the verdict.**
  `default_confirm_payment` early-returned on the first `payment.succeeded/failed`
  it found for a `quote_id`. Since a PAYMENT_FAILED retry reuses the same
  `quote_id`, an earlier attempt's `payment.failed` row would resolve a later
  success as "failed". The order idempotency key guarantees ≤1 capture per
  `quote_id`, so a `payment.succeeded` is terminal truth: the scan now reads the
  whole ledger and lets succeeded win over failed.

**What proved it all works.** With those four fixed, one typed sentence
("buy me one pair of running shoes, up to ₹9000") ran fully autonomously through
real Gemini (intent draft → plan → discover → evaluate) to a real Gate PASS and
a **real Razorpay test-mode order** (`order_TUdVWPai5hFMta`, `payment.attempted`
in the ledger) — the only remaining human step being the payment itself. And the
thesis got its cleanest live demo for free: a "budget ₹5000" run picked a ₹4,999
shoe the buyer thought fit, but the merchant's real total with 18% GST was
₹5,898, so the **Gate refused OVER_LIMIT** and the agent abandoned. The buyer
never computed the total; the merchant did, and said no.

**What I'd tell the next person.** A mock encodes what you *think* the boundary
looks like; a live call tells you what it *is*. Three of these four bugs were
invisible to 310 green tests and took one real request each to expose — the
response content shape, the category taxonomy coupling, and (via a new test) a
whole dead recovery branch. Run the real thing early, once, before you trust the
suite. The GST-over-budget refusal wasn't a bug at all — it's the whole point,
and it only showed up because the run was real.

**Cost:** ~40 minutes across the four, most of it the two-line `message_text`
normaliser and realising the category field needed the merchant's vocabulary,
not the user's words.
