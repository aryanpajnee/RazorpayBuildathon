"""Understand a free-text purchase request → an open product category label.

This is the "the AI figures out what you want" step. The user types anything —
"wireless noise-cancelling headphones", "a foam roller for my back", "cheap
running shoes" — and this returns a short, normalised category label
("headphones", "recovery", "footwear", …) that becomes the SIGNED SCOPE of the
Intent Mandate. It is the Intent Compiler surface (#7) doing its job: turn a
sentence into the structured field the mandate carries.

WHERE THIS SITS RELATIVE TO THE MONEY PATH. The label this produces is written
into the mandate the user signs and is then enforced by the Gate as an exact
string match against the relisted offer's category. So an LLM NAMES the scope —
but it never decides the price, the budget, the signature, or whether a payment
clears. The hard money bound is the budget (max_paise), which is set by the user
and re-checked by the deterministic Gate; this module cannot widen it. If the
model misreads the request, the worst case is the wrong *kind* of product is
searched for under the user's own budget — never an over-budget or unsigned
purchase.

Degrade, never hard-block: if the model is unavailable (no key, quota, outage)
or returns nothing usable, a deterministic keyword-free fallback derives a label
straight from the request text. A run must never die because the LLM hiccuped —
same rule the web-search lane follows.
"""

from __future__ import annotations

import re

import config
from merchant.offers import normalize_category

_SYSTEM = (
    "You turn a shopping request into a SHORT product category label. "
    "Reply with ONLY the label: 1-3 lowercase words naming the kind of product "
    "the user wants to buy, singular, no punctuation, no price, no brand. "
    "Examples: 'wireless noise cancelling headphones' -> headphones; "
    "'a foam roller for recovery' -> foam roller; 'cheap running shoes' -> "
    "running shoes; 'protein bars' -> protein bar."
)

# Words that describe budget/quantity/filler, not the product — stripped by the
# deterministic fallback so it lands on the actual noun(s).
_STOP = {
    "buy", "get", "me", "a", "an", "the", "some", "please", "want", "need", "i",
    "for", "to", "of", "my", "with", "under", "below", "up", "upto", "around",
    "about", "cheap", "cheapest", "best", "good", "new", "budget", "rs", "inr",
    "rupees", "rupee", "and", "or", "any", "something", "looking", "would", "like",
}
_PRICE_RE = re.compile(r"(?:₹|rs\.?|inr)?\s*\d[\d,]*(?:\.\d+)?k?", re.IGNORECASE)


def _fallback_category(request: str) -> str:
    """Deterministic label from the request text, no LLM. Drops price tokens and
    filler words and keeps the last few remaining words — the product noun tends
    to sit at the end of an English request ("buy me wireless HEADPHONES")."""
    text = _PRICE_RE.sub(" ", request or "")
    words = [w for w in re.findall(r"[a-zA-Z][a-zA-Z-]*", text.lower()) if w not in _STOP]
    if not words:
        return normalize_category(request) or "general"
    return normalize_category(" ".join(words[-3:]))


def understand_request(request: str, *, invoke=None) -> str:
    """Return an open, normalised product category for a free-text request.

    `invoke` is the LLM entry point (defaults to `buyer.llm.invoke`); inject a
    fake in tests to exercise the model path without a network call. Any failure
    — missing key, bad output, exception — falls through to `_fallback_category`,
    so this function always returns a usable non-empty label.
    """
    request = (request or "").strip()
    if not request:
        return "general"

    if invoke is None:
        from buyer.llm import invoke as _invoke  # lazy: importing never needs a key
        invoke = _invoke

    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        reply = invoke(
            [SystemMessage(content=_SYSTEM), HumanMessage(content=request)],
            purpose=config.AGENT_CATEGORY_PURPOSE,
        )
        content = getattr(reply, "content", reply)
        if isinstance(content, list):  # Gemini may return content blocks
            content = " ".join(
                b.get("text", "") if isinstance(b, dict) else str(b) for b in content
            )
        label = normalize_category(str(content))
        # Guard against a chatty or empty reply — keep only the first line, and
        # if the model ignored the instruction and wrote a sentence, fall back.
        label = label.splitlines()[0].strip() if label else ""
        if label and len(label.split()) <= 5:
            return label
    except Exception:  # noqa: BLE001 — any model failure degrades to the fallback
        pass

    return _fallback_category(request)
