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
