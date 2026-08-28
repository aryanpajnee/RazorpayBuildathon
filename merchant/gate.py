"""The Gate — the single chokepoint between a signed Cart Mandate and money.

Every path that ends in a Razorpay charge — the buyer agent's normal
checkout, the merchant's own sales/upsell agent trying to push a bigger
cart, a recovery agent retrying after a refusal — passes through
`gate.check()`. There is no second door, and `check()` takes no
caller-identity parameter and no bypass flag: the merchant's own upsell
agent is refused exactly the same way an attacker is.

Seven checks, fixed order, no exceptions carved out for a caller who says
it's fine:

    a. Ed25519 signature valid, and the chain of authority it names is real
    b. Intent mandate not expired
    c. Total <= the intent's max_paise (plus currency, category, purchase count)
    d. cart_hash matches the quote the merchant issued
    e. Quote within its TTL
    f. Nonce unseen (replay defence, SQLite-backed, atomic insert)
    g. Price unchanged since the quote was issued

`check()` never raises on buyer input, however malformed or adversarial —
it always returns a `GateResult`. An exception escaping `check()` can only
mean the merchant's own storage broke, never that the buyer sent something
bad.

No LLM import, call, or client anywhere in this file. Given the same
mandate, quote, and merchant state, `check()` returns the identical result
every time it is run.

Spec: docs/specs/gate-spec.md
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

import config
from core.ledger import append as ledger_append
from core.mandate import MandateVerificationError, canonical, verify
from merchant import catalog, intent_store, quote_store
from merchant.quote import LineItem, compute_total

# --- the closed set of refusal codes ----------------------------------------
# No code outside this set may ever be returned as reason_code. Extending
# this set is a spec change (docs/specs/gate-spec.md §4), not something a
# future edit here should do on its own.

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

_VALID_CODES = frozenset(
    {
        SIG_INVALID,
        INTENT_NOT_FOUND,
        AGENT_MISMATCH,
        WRONG_MERCHANT,
        INTENT_EXPIRED,
        OVER_LIMIT,
        CURRENCY_MISMATCH,
        CATEGORY_MISMATCH,
        PURCHASES_EXHAUSTED,
        QUOTE_NOT_FOUND,
        CART_HASH_MISMATCH,
        QUOTE_EXPIRED,
        NONCE_REUSED,
        PRICE_DRIFT,
    }
)

_NONCE_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS gate_nonces (
    nonce           TEXT PRIMARY KEY,
    cart_mandate_id TEXT NOT NULL,
    agent_id        TEXT NOT NULL,
    quote_id        TEXT NOT NULL,
    recorded_at     INTEGER NOT NULL
)
"""


@dataclass(frozen=True, slots=True)
class GateResult:
    """The outcome of one `check()` call — pass or refuse, never an exception.

    `total_paise` is always the Gate's OWN re-derivation via
    `merchant.quote.compute_total` over the merchant's stored line items —
    never the number the cart mandate merely asserts. `message` is for the
    merchant's own logs and the ledger, not for the buyer; a separate
    Refusal Explainer agent turns `reason_code` + `detail` into buyer-facing
    language.
    """

    passed: bool
    reason_code: str | None
    message: str
    detail: dict
    total_paise: int | None
    quote_id: str | None
    cart_mandate_id: str | None
    checked_at: int


# --- internal accumulator ----------------------------------------------------
# Carries whatever the Gate has managed to extract so far, so the single
# terminal ledger-append call (see _refuse / _pass below) always has as much
# context as was available at the point of refusal — never less than what a
# prior check already resolved.


@dataclass
class _Context:
    checked_at: int
    envelope: dict
    payload: dict | None = None
    cart_mandate_id: str | None = None
    quote_id: str | None = None
    agent_id: str | None = None
    intent: dict | None = None
    gate_total_paise: int | None = None
    nonce: str | None = None
    detail_extra: dict = field(default_factory=dict)


def _nonce_connect(db_path: Path | None) -> sqlite3.Connection:
    path = db_path or config.GATE_NONCES_DB
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(_NONCE_CREATE_TABLE_SQL)
    return conn


def _refuse(ctx: _Context, reason_code: str, message: str, detail: dict) -> GateResult:
    """Build a refusal GateResult and make the one ledger.append call for it.

    Every refusal path in `check()` routes through here — there is no path
    that returns a refusal without also appending `gate.refused`.
    """
    assert reason_code in _VALID_CODES, f"unknown reason_code {reason_code!r}"

    safe_detail = dict(detail)  # keep JSON-safe: ints/strings only, no objects

    ledger_payload = {
        "reason_code": reason_code,
        "message": message,
        "detail": safe_detail,
        "cart_mandate_id": ctx.cart_mandate_id,
        "quote_id": ctx.quote_id,
        "agent_id": ctx.agent_id,
    }
    ledger_append("gate.refused", ledger_payload)

    return GateResult(
        passed=False,
        reason_code=reason_code,
        message=message,
        detail=safe_detail,
        total_paise=ctx.gate_total_paise,
        quote_id=ctx.quote_id,
        cart_mandate_id=ctx.cart_mandate_id,
        checked_at=ctx.checked_at,
    )


def _pass(ctx: _Context) -> GateResult:
    """Build a passing GateResult and make the one ledger.append call for it."""
    ledger_payload = {
        "cart_mandate_id": ctx.cart_mandate_id,
        "intent_mandate_id": ctx.payload["intent_mandate_id"],
        "quote_id": ctx.quote_id,
        "agent_id": ctx.agent_id,
        "nonce": ctx.nonce,
        "total_paise": ctx.gate_total_paise,
        "checked_at": ctx.checked_at,
    }
    ledger_append("gate.passed", ledger_payload)

    return GateResult(
        passed=True,
        reason_code=None,
        message="cart mandate authorised",
        detail={},
        total_paise=ctx.gate_total_paise,
        quote_id=ctx.quote_id,
        cart_mandate_id=ctx.cart_mandate_id,
        checked_at=ctx.checked_at,
    )


def check(cart_envelope: dict, *, now: int | None = None) -> GateResult:
    """Does this authentic, on-file Intent Mandate authorise this exact cart,
    at this exact price, right now, for the first time?

    Seven checks in fixed order a-g; the FIRST failing one refuses. No
    caller-identity parameter and no bypass flag — every caller, buyer
    checkout or the merchant's own upsell agent, gets the same seven checks.
    """
    checked_at = now if now is not None else int(time.time())
    ctx = _Context(checked_at=checked_at, envelope=cart_envelope)

    # --- (a) signature -------------------------------------------------
    try:
        payload = verify(cart_envelope)
    except MandateVerificationError as exc:
        # Best-effort extraction for logging only — nothing here is trusted,
        # since the signature over it hasn't been established as authentic.
        raw_payload = (
            cart_envelope.get("payload", {}) if isinstance(cart_envelope, dict) else {}
        )
        if isinstance(raw_payload, dict):
            ctx.cart_mandate_id = raw_payload.get("mandate_id")
            ctx.quote_id = raw_payload.get("quote_id")
            ctx.agent_id = raw_payload.get("agent_id")

        try:
            envelope_hash = hashlib.sha256(canonical(cart_envelope)).hexdigest()
        except Exception:
            envelope_hash = None

        return _refuse(
            ctx,
            SIG_INVALID,
            f"cart mandate signature/envelope invalid: {exc}",
            {"envelope_sha256": envelope_hash},
        )

    ctx.payload = payload
    ctx.cart_mandate_id = payload["mandate_id"]
    ctx.quote_id = payload.get("quote_id")
    ctx.agent_id = payload.get("agent_id")

    # --- (a) intent resolution ------------------------------------------
    intent = intent_store.get_intent(payload["intent_mandate_id"])
    if intent is None:
        return _refuse(
            ctx,
            INTENT_NOT_FOUND,
            f"no intent mandate on file for {payload['intent_mandate_id']!r}",
            {"intent_mandate_id": payload["intent_mandate_id"]},
        )
    ctx.intent = intent

    # --- (a) agent binding ------------------------------------------------
    if payload["agent_id"] != intent["agent_id"]:
        return _refuse(
            ctx,
            AGENT_MISMATCH,
            "cart mandate agent_id does not match the intent's agent_id",
            {"cart_agent_id": payload["agent_id"], "intent_agent_id": intent["agent_id"]},
        )

    # --- (a) agent KEY binding --------------------------------------------
    # The agent_id above is a label; this is the proof. `verify()` established
    # the signature is valid over the envelope's OWN embedded public_key — NOT
    # that the key belongs to anyone entitled to spend (see core/mandate.py's
    # module docstring). The user bound the agent's trusted key into the intent
    # they signed; a cart signed by any other key, however well it name-drops
    # the right agent_id, is impersonation. Refused SIG_INVALID — the document's
    # signing key cannot be trusted (gate-spec check (a); the closed §4 code set
    # is unchanged). `.get` fails safe: a legacy intent with no bound key yields
    # None, and a 64-hex envelope key never equals None, so it is refused too.
    if cart_envelope["public_key"] != intent.get("agent_pubkey"):
        return _refuse(
            ctx,
            SIG_INVALID,
            "cart mandate signed by a key not bound to this agent in the intent",
            {"cart_public_key": cart_envelope["public_key"]},
        )

    # --- (a) merchant binding ----------------------------------------------
    if payload["merchant_id"] != config.MERCHANT_ID:
        return _refuse(
            ctx,
            WRONG_MERCHANT,
            "cart mandate is not addressed to this merchant",
            {"cart_merchant_id": payload["merchant_id"], "expected": config.MERCHANT_ID},
        )
    if intent.get("merchant_id") is not None and intent["merchant_id"] != config.MERCHANT_ID:
        return _refuse(
            ctx,
            WRONG_MERCHANT,
            "intent mandate is scoped to a different merchant",
            {"intent_merchant_id": intent["merchant_id"], "expected": config.MERCHANT_ID},
        )

    # --- (b) intent expiry ---------------------------------------------
    if checked_at >= intent["expires_at"]:
        return _refuse(
            ctx,
            INTENT_EXPIRED,
            "intent mandate has expired",
            {"expires_at": intent["expires_at"], "checked_at": checked_at},
        )

    # --- resolve quote (feeds both (c) and (d)/(e)/(g)) ------------------
    quote = quote_store.get_quote(payload["quote_id"])
    if quote is None:
        return _refuse(
            ctx,
            QUOTE_NOT_FOUND,
            f"no quote on file for {payload['quote_id']!r}",
            {"quote_id": payload["quote_id"]},
        )

    gate_total = compute_total(list(quote.lines)).total_paise
    ctx.gate_total_paise = gate_total

    # --- (c) currency, over-limit, category, purchase count --------------
    if payload["currency"] != intent["currency"] or payload["currency"] != config.CURRENCY:
        return _refuse(
            ctx,
            CURRENCY_MISMATCH,
            "cart currency does not match the intent's currency or the merchant's currency",
            {
                "cart_currency": payload["currency"],
                "intent_currency": intent["currency"],
                "merchant_currency": config.CURRENCY,
            },
        )

    if gate_total > intent["max_paise"]:
        return _refuse(
            ctx,
            OVER_LIMIT,
            "cart total exceeds the intent's max_paise",
            {
                "limit_paise": intent["max_paise"],
                "over_by_paise": gate_total - intent["max_paise"],
            },
        )

    for line in quote.lines:
        try:
            product = catalog.get_product(line.sku)
        except catalog.ProductNotFound:
            return _refuse(
                ctx,
                CATEGORY_MISMATCH,
                f"quoted sku {line.sku!r} no longer exists in the catalog",
                {"sku": line.sku},
            )
        if product["category"] != intent["category"]:
            return _refuse(
                ctx,
                CATEGORY_MISMATCH,
                f"sku {line.sku!r} is category {product['category']!r}, "
                f"intent only authorises {intent['category']!r}",
                {"sku": line.sku, "product_category": product["category"], "intent_category": intent["category"]},
            )

    used = intent_store.purchases_used(intent["mandate_id"])
    if used >= intent["max_purchases"]:
        return _refuse(
            ctx,
            PURCHASES_EXHAUSTED,
            "intent mandate's purchase count is exhausted",
            {"purchases_used": used, "max_purchases": intent["max_purchases"]},
        )

    # --- (d) cart hash matches the merchant's own quote --------------------
    if payload["cart_hash"] != quote.cart_hash:
        return _refuse(
            ctx,
            CART_HASH_MISMATCH,
            "cart hash does not match the merchant's stored quote",
            {"cart_hash": payload["cart_hash"], "quote_cart_hash": quote.cart_hash},
        )

    # --- (e) quote TTL -------------------------------------------------
    if checked_at - quote.issued_at > config.QUOTE_TTL_SECONDS:
        return _refuse(
            ctx,
            QUOTE_EXPIRED,
            "quote has exceeded its TTL",
            {
                "issued_at": quote.issued_at,
                "checked_at": checked_at,
                "ttl_seconds": config.QUOTE_TTL_SECONDS,
            },
        )

    # --- (f) nonce replay defence — atomic insert IS the check ------------
    nonce = payload["nonce"]
    ctx.nonce = nonce
    conn = _nonce_connect(None)
    try:
        try:
            conn.execute(
                """
                INSERT INTO gate_nonces (nonce, cart_mandate_id, agent_id, quote_id, recorded_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (nonce, ctx.cart_mandate_id, ctx.agent_id, ctx.quote_id, checked_at),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            return _refuse(
                ctx,
                NONCE_REUSED,
                "this cart mandate's nonce has already been used",
                {"nonce": nonce},
            )
    finally:
        conn.close()

    # --- (g) price drift since the quote was issued -----------------------
    current_lines = [
        LineItem(
            sku=line.sku,
            name=line.name,
            unit_paise=catalog.get_product(line.sku)["price_paise"],
            qty=line.qty,
        )
        for line in quote.lines
    ]
    current_total = compute_total(current_lines).total_paise
    if current_total != quote.total_paise:
        return _refuse(
            ctx,
            PRICE_DRIFT,
            "catalog price has changed since the quote was issued",
            {"quoted_total_paise": quote.total_paise, "current_total_paise": current_total},
        )

    # --- pass --------------------------------------------------------------
    intent_store.record_purchase(intent["mandate_id"])
    return _pass(ctx)
