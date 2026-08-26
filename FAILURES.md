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
reason it is cheap. `merchant/gateway.py` makes order creation idempotent on
`quote_id` with two layers: an in-process lock, and a `UNIQUE` constraint as the
fallback. The constraint reliably guarantees **one row per quote_id**. It does
not guarantee **one order per quote_id**.

Two processes (two uvicorn workers, say) racing the same `quote_id` both pass
the "not on file yet" check, both call Razorpay, and both get a real order back.
Only one INSERT then wins; the loser returns the winner's row and discards its
own. The discarded order still exists at Razorpay — orphaned, unreferenced, and
invisible to the merchant's own records.

**How I got out.** Not fixed, and deliberately so: closing it properly needs an
INSERT-then-call ordering with a reservation row, which is a real change to the
money path and not something to do the day before a demo. What I did instead was
stop the code from lying about it — the comment used to defer to `FAILURES.md`,
where no such entry existed. Now it does.

**What I'd tell the next person.** A `UNIQUE` constraint makes your *database*
idempotent. It does nothing about the side effect you already performed before
reaching it. If the expensive, irreversible action happens before the constraint
is tested, the constraint is deduplicating your records, not your actions —
and with a payment gateway those are very different things.

**Cost:** none yet. Single-process demo runs cannot hit it. Written down so it
is a known limitation rather than a surprise.

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
