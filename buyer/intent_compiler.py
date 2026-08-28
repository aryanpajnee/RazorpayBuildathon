"""Agent #7 — Intent Compiler.

The human-consent front door: turns a sentence like "get me running shoes
under ₹5000" into an unsigned Intent Mandate payload, renders it back to the
human in plain language for approval, recognises their confirmation, and — on
confirmation only — signs it with the USER's key (`config.USER_KEY_NAME`),
distinct from the agent's own Cart-Mandate-signing key
(`config.BUYER_AGENT_KEY_NAME`).

Money discipline is the whole point of this file. `draft_intent` is the only
node in this deployment where the model's output feeds directly into a
signed-money field (`max_paise`), so the model is instructed to return WHOLE
RUPEES ONLY — never paise — and this module's own Python does the
rupees→paise multiplication (`config.PAISE_PER_RUPEE`). `readback` is
deliberately NOT an LLM call: the one artifact a human approves before
signing money away must render the exact number that will be signed, with no
model free to paraphrase, round, or hallucinate it.

Signing happens only after `is_confirmation` returns True for the human's
reply — this module never auto-signs. Driving that confirm-then-sign sequence
is `buyer/agent.py`'s job, not this module's.
"""

from __future__ import annotations

from nacl.signing import SigningKey

import config
from buyer import llm
from buyer.nodes_common import NodeError, extract_json, message_text
from core.mandate import make_intent_mandate, sign

_CATEGORY_CHOICES = ", ".join(config.CATALOG_CATEGORIES)

_SYSTEM_PROMPT = f"""You extract a shopping intent from a user's sentence.
Read the sentence and identify: the product category, the maximum they are
willing to spend, how many purchases they authorize, and how long the
authorization should last.

CRITICAL - category: the category MUST be EXACTLY one of these merchant
categories, lowercase, nothing else: {_CATEGORY_CHOICES}. Map the user's words
to the closest one - e.g. "running shoes", "sneakers", "trainers" -> "footwear";
"t-shirt", "shorts", "jacket" -> "apparel"; "cap", "belt" -> "accessories". Do
NOT invent a category outside that list.

CRITICAL - spend: the maximum spend must be returned in WHOLE RUPEES ONLY - an
integer, never paise, never a decimal, never a currency symbol. If the
sentence gives no explicit spending limit, make a reasonable one for the
category. If the sentence gives no explicit purchase count, default to 1. If
the sentence gives no explicit time limit, default to 24 hours.

Respond with exactly one JSON object, and nothing else - no markdown fence, no
commentary before or after it:
{{"category": "<one of: {_CATEGORY_CHOICES}>", "max_rupees": <integer>, "max_purchases": <integer>, "ttl_hours": <integer>}}"""

# Lower-cased lookup so a model that returns "Footwear" still maps to the
# canonical config spelling the Gate compares against.
_CANONICAL_CATEGORY = {c.casefold(): c for c in config.CATALOG_CATEGORIES}

# The fixed, deterministic confirmation vocabulary. Anything not in this set
# is treated as "not yet confirmed" - a human typing "yes I think so" should
# not sign money away on a fuzzy match.
_CONFIRMATION_TOKENS = frozenset({
    "confirm", "yes", "y", "sign", "i confirm", "confirmed",
})


def draft_intent(
    sentence: str, *, agent_id: str = "agent_buyer", agent_pubkey: str
) -> dict:
    """Extract an Intent Mandate payload from `sentence`. Does NOT sign.

    One `llm.invoke` call extracts category/rupees/purchases/ttl from the
    sentence; this function's own Python then converts whole rupees to paise
    (`config.PAISE_PER_RUPEE`) and builds the unsigned payload via
    `core.mandate.make_intent_mandate`. The model never sees or returns a
    paise value.

    `agent_pubkey` is the public half of the agent's Cart-Mandate-signing key
    (the caller holds the private half). It is bound into the grant the user
    signs so the Gate can later reject a cart signed by any other key — the
    model is nowhere near it.
    """
    response = llm.invoke(
        [("system", _SYSTEM_PROMPT), ("human", sentence)],
        purpose="intent_compiler",
    )
    parsed = extract_json(message_text(response))

    if not isinstance(parsed, dict):
        raise NodeError(f"intent_compiler expected a JSON object, got {type(parsed).__name__}: {parsed!r}")

    category = parsed.get("category")
    max_rupees = parsed.get("max_rupees")
    max_purchases = parsed.get("max_purchases")
    ttl_hours = parsed.get("ttl_hours")

    if not isinstance(category, str) or not category.strip():
        raise NodeError(f"intent_compiler response missing/invalid 'category': {parsed!r}")
    # Constrain to the merchant's controlled vocabulary: an intent category the
    # Gate won't recognise is a guaranteed CATEGORY_MISMATCH refusal downstream,
    # so reject it here rather than sign an unusable mandate. Map to the
    # canonical config spelling the Gate compares against.
    canonical = _CANONICAL_CATEGORY.get(category.strip().casefold())
    if canonical is None:
        raise NodeError(
            f"intent_compiler category {category!r} is not one of the merchant "
            f"categories {config.CATALOG_CATEGORIES}: {parsed!r}"
        )
    category = canonical
    if type(max_rupees) is not int or max_rupees < 1:
        raise NodeError(f"intent_compiler response missing/invalid 'max_rupees' (must be a positive int): {parsed!r}")
    if type(max_purchases) is not int or max_purchases < 1:
        raise NodeError(f"intent_compiler response missing/invalid 'max_purchases' (must be a positive int): {parsed!r}")
    if type(ttl_hours) is not int or ttl_hours < 1:
        raise NodeError(f"intent_compiler response missing/invalid 'ttl_hours' (must be a positive int): {parsed!r}")

    return make_intent_mandate(
        user_id="user_local",
        agent_id=agent_id,
        agent_pubkey=agent_pubkey,
        category=category,
        max_paise=max_rupees * config.PAISE_PER_RUPEE,
        max_purchases=max_purchases,
        ttl_seconds=ttl_hours * 3600,
        merchant_id=None,
    )


def _format_paise_as_rupees(paise: int) -> str:
    """Integer paise -> '₹5,000.00'. No float touches this — see the module
    docstring on why readback in particular must never round-trip through one."""
    rupees, remainder = divmod(paise, config.PAISE_PER_RUPEE)
    return f"₹{rupees:,}.{remainder:02d}"


def _format_ttl(issued_at: int, expires_at: int) -> str:
    """A human timeframe from the two unix-int timestamps on the payload."""
    total_seconds = max(0, expires_at - issued_at)
    hours, remainder_seconds = divmod(total_seconds, 3600)
    if hours >= 24 and remainder_seconds == 0 and hours % 24 == 0:
        days = hours // 24
        return f"{days} day{'s' if days != 1 else ''}"
    if hours >= 1:
        return f"{hours} hour{'s' if hours != 1 else ''}"
    minutes = max(1, total_seconds // 60)
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


def readback(payload: dict) -> str:
    """Render every field a human needs to approve, deterministically.

    No LLM call. Money must be exact, not paraphrased — this renders directly
    from the integer paise on the payload via `_format_paise_as_rupees`.
    """
    ceiling = _format_paise_as_rupees(payload["max_paise"])
    ttl = _format_ttl(payload["issued_at"], payload["expires_at"])
    merchant = payload.get("merchant_id") or "any merchant"
    max_purchases = payload["max_purchases"]

    return (
        f"Category: {payload['category']}\n"
        f"Spending ceiling: {ceiling}\n"
        f"Max purchases: {max_purchases}\n"
        f"Expires in: {ttl}\n"
        f"Merchant: {merchant}\n"
        f"Agent: {payload['agent_id']}\n"
        f"Agent key: {payload['agent_pubkey']}\n"
        "Reply 'confirm' to sign this authorization, or anything else to cancel."
    )


def is_confirmation(user_input: str) -> bool:
    """True only for a fixed small token set (case-insensitive, stripped).

    Deliberately not fuzzy: a human's confirmation to sign an Intent Mandate
    is the one moment in this whole flow money changes hands on a person's
    word rather than a signature check, so it should not accept anything
    ambiguous.
    """
    if not isinstance(user_input, str):
        return False
    return user_input.strip().casefold() in _CONFIRMATION_TOKENS


def sign_intent(payload: dict, user_sk: SigningKey) -> dict:
    """Thin wrapper: sign `payload` with the USER's key and return the
    envelope. Only ever called after `is_confirmation` has returned True —
    this module does not enforce that sequencing itself, `agent.py` does."""
    return sign(payload, user_sk)
