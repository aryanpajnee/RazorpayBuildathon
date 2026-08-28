"""Agent #1 — Storefront.

The merchant's conversational front door: greets an AI buyer, answers plain
prose questions ("what do you sell?", "do you have running shoes?"), and
points them at the merchant's real category vocabulary
(`config.CATALOG_CATEGORIES`). PROSE ONLY — `purpose="storefront"` is in
`config.FAST_LLM_SURFACES`, so this runs on the NVIDIA fast lane, which is
exactly right here: the fast lane mis-scales rupees to paise and must never
see a numeric task (see `config.py`'s `FAST_LLM_SURFACES` comment), and
storefront never emits a number anything downstream trusts.

Money discipline: this module never quotes an authoritative price, never
builds or proposes a cart (`list[{sku, qty}]` — that's Discovery/Evaluator's
job), never signs anything, and never calls `/quote`, `/checkout`, the Gate,
or Razorpay. It only returns a string. If it mentions a price at all, that is
flavour text a buyer must not rely on; the merchant re-derives every real
price at quote time regardless of what storefront said.

Availability discipline: a storefront that goes silent because an LLM call
failed is a bad front door, and unlike the buyer-side nodes there is no
downstream money decision riding on this text — so, unlike `buyer/nodes_common`
modules (which raise `NodeError` and let the caller own retries), `reply`
swallows any LLM failure and returns a deterministic fallback string instead
of propagating. This is a deliberate, narrow exception to the "raise, never
guess" rule the buyer nodes follow: it is safe here only because nothing
reads storefront's return value as data — it is prose shown to a buyer, not a
value the merchant acts on.
"""

from __future__ import annotations

import config
from buyer import llm
from buyer.nodes_common import message_text

_CATEGORY_LIST = ", ".join(config.CATALOG_CATEGORIES)

_SYSTEM_PROMPT = f"""You are the storefront agent for Northwind, an online
merchant selling running and athletic gear. You are talking to an AI buyer
agent, not a human browsing casually - keep replies short, concise, and
helpful.

Northwind's real product categories are exactly these - mention only these,
never invent a category that isn't in this list: {_CATEGORY_LIST}.

You are a greeter and guide only. You NEVER state an exact price, availability
count, or stock level as authoritative - if asked about pricing or stock,
say the buyer should search or request a quote for the current numbers. You
NEVER propose or describe a specific cart or order. Keep every reply to two
or three sentences."""

# Deterministic welcome, used both as `greeting()`'s return value and as the
# fallback `reply()` gives when the LLM call fails. Kept in sync in content
# (not verbatim) with the system prompt's category list so a buyer reading
# either still sees Northwind's real vocabulary.
_FALLBACK_REPLY = (
    "Welcome to Northwind. We carry footwear, apparel, socks, accessories, "
    "nutrition, and recovery gear, plus the occasional bundle. Search our "
    "catalog or ask me about a category and I'll point you in the right "
    "direction."
)


def greeting() -> str:
    """Deterministic, zero-arg opening line. No LLM call.

    A storefront's very first line is the one a buyer sees before it has said
    anything to react to, so there is nothing for a model to reason about yet
    - a fixed string is exactly as good as an LLM call here and costs nothing
    and cannot fail. It is also reused as `reply`'s fallback so a mid-
    conversation LLM outage degrades to the same known-good text rather than
    a different, untested one.
    """
    return _FALLBACK_REPLY


def reply(*, buyer_message: str, context: str = "") -> str:
    """Answer `buyer_message` in plain prose. Never raises.

    `context` is optional extra text the caller may pass (e.g. a short
    catalog summary) to ground the reply; callers are not required to supply
    it. On any failure - malformed/unparseable output, a transient LLM error,
    anything `llm.invoke` or `message_text` might raise - this catches it and
    returns the deterministic `_FALLBACK_REPLY` rather than propagating: a
    storefront's availability must not depend on the LLM, since nothing here
    feeds the money path and a friendly canned line beats a stack trace shown
    to a buyer agent.
    """
    if not isinstance(buyer_message, str) or not buyer_message.strip():
        return _FALLBACK_REPLY

    human_prompt = buyer_message.strip()
    if isinstance(context, str) and context.strip():
        human_prompt = f"{human_prompt}\n\n(Context: {context.strip()})"

    try:
        response = llm.invoke(
            [("system", _SYSTEM_PROMPT), ("human", human_prompt)],
            purpose="storefront",
        )
        text = message_text(response).strip()
    except Exception:  # noqa: BLE001 - deliberately broad, see module docstring
        return _FALLBACK_REPLY

    return text if text else _FALLBACK_REPLY
