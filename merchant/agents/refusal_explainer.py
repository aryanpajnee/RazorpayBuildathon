"""Agent #6 — Refusal Explainer.

Turns one `gate.GateResult` refusal — `reason_code` + `message` + `detail`
(see `merchant/gate.py`'s seven checks and fourteen refusal codes) — into
plain English a buyer agent or a human can act on: an `explanation` of what
went wrong and a `fix` of what to do next.

Money discipline: the Gate's `detail` dict already carries the authoritative
paise figures (e.g. `OVER_LIMIT`'s `limit_paise`/`over_by_paise`,
`PRICE_DRIFT`'s `quoted_total_paise`/`current_total_paise`) — those numbers
were computed by `merchant.quote.compute_total`, never by this module. This
file's own Python (`_format_detail`) converts every `*_paise` integer to a
rupee string via `paise // 100`/`% 100` BEFORE anything is shown to a model.
The model only narrates numbers it is handed; it is never asked to add,
subtract, or round one, and never sees a raw paise value to potentially
mis-scale (the same trap `config.py` documents for the NVIDIA fast lane).

Availability discipline (spec S9): a refusal is the one moment a buyer most
needs an explanation, so the explanation's existence can never depend on an
LLM call succeeding. `_TEMPLATES` is a closed, deterministic map from every
one of the fourteen `gate.py` reason codes (plus a generic fallback for an
unrecognised code) to an explanation+fix built only from the formatted
`detail` dict — no model involved. The normal path asks the model to
rephrase that deterministic pair into friendlier prose, explicitly told to
preserve every number verbatim rather than recompute it; if the call raises,
times out, or returns something unparsable/incomplete, `explain` catches it
and returns the deterministic pair unchanged. `purpose="refusal_explainer"`
is in `config.FAST_LLM_SURFACES`, so this runs on the NVIDIA fast lane — the
right place for a surface that only rephrases already-computed numbers and
never performs arithmetic itself, exactly the property that makes the fast
lane's rupee-scaling bug irrelevant here (see `config.py`'s comment on why
that lane is prose-only).
"""

from __future__ import annotations

import config
from buyer import llm
from buyer.nodes_common import extract_json, message_text

# --- the closed set of Gate refusal codes ------------------------------------
# Mirrors merchant/gate.py's constants verbatim rather than importing them,
# so this module has no import-time dependency on the Gate — an explainer
# describing a refusal should not need the refusing code to be importable.

SIG_INVALID = "SIG_INVALID"
INTENT_NOT_FOUND = "INTENT_NOT_FOUND"
AGENT_MISMATCH = "AGENT_MISMATCH"
WRONG_MERCHANT = "WRONG_MERCHANT"
INTENT_EXPIRED = "INTENT_EXPIRED"
OVER_LIMIT = "OVER_LIMIT"
CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
CATEGORY_MISMATCH = "CATEGORY_MISMATCH"
PURCHASES_EXHAUSTED = "PURCHASES_EXHAUSTED"
QUOTE_NOT_FOUND = "QUOTE_NOT_FOUND"
CART_HASH_MISMATCH = "CART_HASH_MISMATCH"
QUOTE_EXPIRED = "QUOTE_EXPIRED"
NONCE_REUSED = "NONCE_REUSED"
PRICE_DRIFT = "PRICE_DRIFT"

_PAISE_SUFFIX = "_paise"


def _rupees(paise: int) -> str:
    """Integer paise -> '₹5,000.00'. The only place this module does money
    arithmetic; the model never sees a raw paise value or does this division."""
    rupees, remainder = divmod(paise, config.PAISE_PER_RUPEE)
    return f"₹{rupees:,}.{remainder:02d}"


def _format_detail(detail: dict) -> dict:
    """Copy `detail`, adding a `<name>_rupees` string beside every
    `<name>_paise` integer. Both keys are kept (the paise key stays, in case
    a caller wants it) so template functions can read whichever they need."""
    formatted: dict = {}
    for key, value in (detail or {}).items():
        formatted[key] = value
        if key.endswith(_PAISE_SUFFIX) and isinstance(value, int):
            rupee_key = key[: -len(_PAISE_SUFFIX)] + "_rupees"
            formatted[rupee_key] = _rupees(value)
    return formatted


# --- deterministic per-code templates ----------------------------------------
# Each function takes the *formatted* detail dict (rupee strings already
# computed) and returns (explanation, fix). `.get(..., fallback)` throughout
# so a detail dict missing an expected key still produces a sane sentence
# rather than a KeyError — the Gate's detail shapes are documented but this
# module must not crash if one ever drifts.


def _t_sig_invalid(d: dict) -> tuple[str, str]:
    return (
        "The cart mandate's digital signature could not be verified, so the "
        "merchant cannot confirm this cart genuinely came from an authorised "
        "buyer agent.",
        "Re-sign the cart mandate with the correct Ed25519 agent key and resubmit.",
    )


def _t_intent_not_found(d: dict) -> tuple[str, str]:
    mid = d.get("intent_mandate_id", "the referenced intent mandate")
    return (
        f"The merchant has no record of intent mandate {mid!r} on file, so it "
        "cannot check what this buyer is authorised to spend.",
        "Have the human re-issue and sign a fresh Intent Mandate, then retry.",
    )


def _t_agent_mismatch(d: dict) -> tuple[str, str]:
    cart_agent = d.get("cart_agent_id", "the cart's signing agent")
    intent_agent = d.get("intent_agent_id", "the intent's authorised agent")
    return (
        f"This cart was signed by agent {cart_agent!r}, but the intent mandate "
        f"only authorises agent {intent_agent!r} to spend against it.",
        "Submit the cart using the same agent identity the intent mandate names.",
    )


def _t_wrong_merchant(d: dict) -> tuple[str, str]:
    got = d.get("cart_merchant_id") or d.get("intent_merchant_id") or "a different merchant"
    expected = d.get("expected", "this merchant")
    return (
        f"This mandate is scoped to merchant {got!r}, not {expected!r}, so it "
        "cannot be used here.",
        "Issue a new mandate addressed to this merchant, or take the cart to the merchant it names.",
    )


def _t_intent_expired(d: dict) -> tuple[str, str]:
    return (
        "The intent mandate's authorization window has already closed, so it "
        "can no longer be used to approve a purchase.",
        "Ask the human to sign a new Intent Mandate with a fresh expiry.",
    )


def _t_over_limit(d: dict) -> tuple[str, str]:
    limit = d.get("limit_rupees", "the authorised spending ceiling")
    over = d.get("over_by_rupees", "an amount")
    return (
        f"This cart totals more than the {limit} spending ceiling the human "
        f"authorised — it is over by {over}.",
        f"Remove or swap items to bring the cart at or under {limit}, then request a new quote.",
    )


def _t_currency_mismatch(d: dict) -> tuple[str, str]:
    cart_currency = d.get("cart_currency", "an unexpected currency")
    return (
        f"The cart is priced in {cart_currency}, which does not match the "
        "currency the intent mandate or the merchant expects.",
        "Rebuild the cart in the merchant's own currency and request a new quote.",
    )


def _t_category_mismatch(d: dict) -> tuple[str, str]:
    sku = d.get("sku", "an item in the cart")
    product_category = d.get("product_category")
    intent_category = d.get("intent_category")
    if product_category and intent_category:
        return (
            f"Item {sku!r} is in the {product_category!r} category, but the "
            f"intent mandate only authorises purchases in {intent_category!r}.",
            f"Remove {sku!r} from the cart, or get a new intent mandate covering {product_category!r}.",
        )
    return (
        f"Item {sku!r} in the cart is no longer valid for the category the "
        "intent mandate authorises.",
        "Remove the offending item and rebuild the cart from the authorised category.",
    )


def _t_purchases_exhausted(d: dict) -> tuple[str, str]:
    used = d.get("purchases_used", "the authorised number of")
    max_purchases = d.get("max_purchases", "its cap")
    return (
        f"This intent mandate has already been used for {used} purchase(s), "
        f"reaching its cap of {max_purchases}.",
        "Ask the human to sign a new Intent Mandate authorising further purchases.",
    )


def _t_quote_not_found(d: dict) -> tuple[str, str]:
    quote_id = d.get("quote_id", "the referenced quote")
    return (
        f"The merchant has no record of quote {quote_id!r} — it may never "
        "have been issued, or it has since been purged.",
        "Request a fresh quote for this cart and resubmit with the new quote_id.",
    )


def _t_cart_hash_mismatch(d: dict) -> tuple[str, str]:
    return (
        "The cart's contents do not match what the merchant quoted — the cart "
        "hash on the mandate disagrees with the stored quote.",
        "Request a new quote for exactly the cart you intend to buy, then sign against that quote.",
    )


def _t_quote_expired(d: dict) -> tuple[str, str]:
    return (
        "The quote this cart was built against has passed its validity "
        "window, so its prices may no longer be current.",
        "Request a fresh quote and submit the cart before it expires.",
    )


def _t_nonce_reused(d: dict) -> tuple[str, str]:
    return (
        "This exact cart mandate has already been used once — the merchant "
        "will not process the same signed cart twice.",
        "Build and sign a new cart mandate, with a fresh nonce, for this purchase.",
    )


def _t_price_drift(d: dict) -> tuple[str, str]:
    quoted = d.get("quoted_total_rupees", "the quoted price")
    current = d.get("current_total_rupees", "the current catalog price")
    return (
        f"The catalog price changed since the quote was issued — it was "
        f"{quoted} at quote time and is now {current}.",
        "Request a new quote at the current price and confirm the buyer still wants to proceed.",
    )


def _t_unknown(d: dict) -> tuple[str, str]:
    return (
        "The merchant refused this cart mandate for a reason this explainer "
        "does not recognise.",
        "Check the merchant's refusal message and detail for specifics, or contact support.",
    )


_TEMPLATES = {
    SIG_INVALID: _t_sig_invalid,
    INTENT_NOT_FOUND: _t_intent_not_found,
    AGENT_MISMATCH: _t_agent_mismatch,
    WRONG_MERCHANT: _t_wrong_merchant,
    INTENT_EXPIRED: _t_intent_expired,
    OVER_LIMIT: _t_over_limit,
    CURRENCY_MISMATCH: _t_currency_mismatch,
    CATEGORY_MISMATCH: _t_category_mismatch,
    PURCHASES_EXHAUSTED: _t_purchases_exhausted,
    QUOTE_NOT_FOUND: _t_quote_not_found,
    CART_HASH_MISMATCH: _t_cart_hash_mismatch,
    QUOTE_EXPIRED: _t_quote_expired,
    NONCE_REUSED: _t_nonce_reused,
    PRICE_DRIFT: _t_price_drift,
}

_SYSTEM_PROMPT = """You rewrite a merchant's refusal explanation for an AI
buyer agent (and, through it, the human it acts for), in clear, friendly
plain English.

You are given a deterministic explanation and fix that were already computed
from exact figures. Your only job is to rephrase them more naturally.

CRITICAL: preserve every number, amount, id, and fact exactly as given. Do
NOT invent, compute, round, or change any number, and do NOT add a figure
that was not already in the text you were given - you are rephrasing, not
calculating.

Respond with exactly one JSON object, and nothing else - no markdown fence,
no commentary before or after it:
{"explanation": "<rephrased explanation>", "fix": "<rephrased fix>"}"""


def explain(*, reason_code: str, message: str, detail: dict) -> dict:
    """Turn one Gate refusal into `{"explanation": str, "fix": str}`.

    Always returns a usable result and never raises for an LLM failure — see
    the module docstring's availability discipline. `detail`'s `*_paise`
    figures are formatted to rupees by this module's own Python
    (`_format_detail`) before either the deterministic template or the model
    prompt ever sees them; the model is only asked to rephrase, never to
    compute.
    """
    formatted = _format_detail(detail if isinstance(detail, dict) else {})
    template_fn = _TEMPLATES.get(reason_code, _t_unknown)
    fallback_explanation, fallback_fix = template_fn(formatted)

    human_prompt = (
        f"reason_code: {reason_code}\n"
        f"merchant log message: {message}\n"
        f"deterministic explanation: {fallback_explanation}\n"
        f"deterministic fix: {fallback_fix}\n\n"
        "Rewrite both the explanation and the fix in clearer, friendlier "
        "plain English. Keep every number and fact exactly as given."
    )

    try:
        response = llm.invoke(
            [("system", _SYSTEM_PROMPT), ("human", human_prompt)],
            purpose="refusal_explainer",
        )
        parsed = extract_json(message_text(response))
        if not isinstance(parsed, dict):
            raise ValueError(f"expected a JSON object, got {type(parsed).__name__}")

        explanation = parsed.get("explanation")
        fix = parsed.get("fix")
        if not isinstance(explanation, str) or not explanation.strip():
            raise ValueError("model response missing a non-empty 'explanation'")
        if not isinstance(fix, str) or not fix.strip():
            raise ValueError("model response missing a non-empty 'fix'")

        return {"explanation": explanation.strip(), "fix": fix.strip()}
    except Exception:  # noqa: BLE001 - deliberately broad, see module docstring
        return {"explanation": fallback_explanation, "fix": fallback_fix}
