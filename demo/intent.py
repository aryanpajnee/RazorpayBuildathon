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

The model behind this step is GroqCloud (`config.INTENT_PROVIDER`), wired here
ONLY for reading the prompt — it never touches the buyer's tool-calling loop or
anything numeric.

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

# Where the product name stops and a purpose/qualifier clause starts: "a coffee
# machine FOR my kitchen", "shoes UNDER 3000", "an espresso machine, EXACT
# PRODUCT ONLY". Punctuation counts too — a comma or dash almost always opens an
# aside rather than continuing the product name.
#
# Bare "to" is deliberately NOT in this list even though "up to" is. Hyphens are
# word boundaries, so `\bto\b` fires inside "bean-to-cup espresso machine" and
# amputates the scope to "bean-" — a label that matches no product on earth and
# turns every candidate into a scope refusal. "up to" is matched as a unit
# instead, which is the only form that actually introduces a price clause.
_PURPOSE_RE = re.compile(
    r"[,;:]|\s[-–—]\s|"
    r"\b(?:for|with|without|under|below|above|over|around|about|upto|up\s+to|"
    r"that|which|so)\b",
    re.IGNORECASE,
)


def _fallback_category(request: str) -> str:
    """Deterministic label from the request text, no LLM. Drops price tokens and
    filler words and keeps the last few remaining words — the product noun tends
    to sit at the end of an English request ("buy me wireless HEADPHONES").

    The "last few words" rule alone mis-reads a request that ends in a purpose
    phrase: "a coffee machine for my kitchen" came out as "coffee machine
    kitchen", because "for"/"my" are filler and get dropped rather than being
    read as the boundary they actually are. The user then sees that phrase as
    the scope on the consent screen and signs it. So the purpose clause is cut
    FIRST, on the preposition, and only the head is labelled.

    Rejected alternative: asking the LLM to do this. `consent_category` runs on
    the request path that prepares the bytes a human is about to sign, and it
    must be instant, offline and identical on every call — a model that names
    the scope differently on a retry would change what the user is signing.
    `understand_request` is where the model gets to name the scope; this stays
    dumb on purpose.
    """
    text = _PRICE_RE.sub(" ", request or "")
    # Cut at the first purpose/qualifier preposition: everything after it
    # describes why or how much, not what.
    head = _PURPOSE_RE.split(text, maxsplit=1)[0]
    words = [w for w in re.findall(r"[a-zA-Z][a-zA-Z-]*", head.lower()) if w not in _STOP]
    if not words:
        # The request was nothing but qualifiers — fall back to the whole text
        # rather than returning an empty scope nothing could ever match.
        words = [w for w in re.findall(r"[a-zA-Z][a-zA-Z-]*", text.lower()) if w not in _STOP]
    if not words:
        return normalize_category(request) or "general"
    return normalize_category(" ".join(words[-3:]))


def consent_category(request: str) -> str:
    """Derive the scope shown and signed before a run, without model or network I/O."""
    return _fallback_category(request)


_intent_invoke = None


def _provider_invoke():
    """Lazily build the invoke entry point for the configured prompt-understanding
    provider (`config.INTENT_PROVIDER`, GroqCloud by default), wrapped in an
    LLMGateway for the shared rate-guard + retry. Raises MissingAPIKeyError if the
    provider's key is absent — the caller catches that and degrades to the
    deterministic fallback, so the prompt step is never silently rerouted to a
    different model."""
    global _intent_invoke
    if _intent_invoke is None:
        from buyer.llm import LLMGateway, get_chat_model

        model = get_chat_model(provider=config.INTENT_PROVIDER)
        _intent_invoke = LLMGateway(model).invoke
    return _intent_invoke


def understand_request(request: str, *, invoke=None) -> str:
    """Return an open, normalised product category for a free-text request.

    Uses GroqCloud (`config.INTENT_PROVIDER`) to READ the prompt and extract what
    the user wants to buy. `invoke` is the LLM entry point; leave it None for the
    real Groq call, or inject a fake in tests to exercise the logic without a
    network call. Any failure — missing key, bad output, exception — falls through
    to `_fallback_category`, so this always returns a usable non-empty label and a
    run never dies on the model.
    """
    request = (request or "").strip()
    if not request:
        return "general"

    if invoke is None:
        try:
            invoke = _provider_invoke()
        except Exception:  # noqa: BLE001 — no/invalid key: degrade to deterministic
            return _fallback_category(request)

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
