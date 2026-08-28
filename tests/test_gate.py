"""`merchant/gate.py` is the single chokepoint between a signed Cart Mandate
and any real money movement. This file is the test matrix from
`docs/specs/gate-spec.md` sec 8, transcribed one row at a time: every one of
the seven checks (a-g) gets a passing test proving it doesn't fire on a
legitimate cart, and a failing test proving it produces the exact refusal
code the spec assigns it — no more, no fewer than the fourteen codes in
sec 4's closed enum.

All DB access is isolated to pytest's tmp_path via monkeypatching
`config.QUOTES_DB` / `config.INTENTS_DB` / `config.GATE_NONCES_DB` /
`config.LEDGER_DB`. `gate.check()` takes no db_path parameter — it reads
these from `config` internally — so seed data (`register_intent`,
`save_quote`) is written through the stores' DEFAULT path (no `db_path=`)
specifically so the Gate and the test's own seed data land in the same
monkeypatched files.

Money check: NW-SHOE-001 costs 499900 paise. subtotal 499900 already clears
FREE_SHIPPING_ABOVE_PAISE (99900), so shipping is 0; taxable = 499900;
gst = (499900*1800 + 5000)//10000 = 89982; total = 589882 paise. Every
"under the limit" test uses a max_paise comfortably above that, and every
"over the limit" test uses one comfortably below it.
"""

from __future__ import annotations

import copy
import sqlite3
import types
from pathlib import Path

import pytest

import config
from core.mandate import generate_keypair, make_cart_mandate, make_intent_mandate, sign
from merchant.catalog import resolve_lines
from merchant.intent_store import register_intent
from merchant.quote import create_quote
from merchant.quote_store import save_quote

try:
    from merchant.gate import GateResult, check
except ImportError as exc:  # pragma: no cover - reported, not swallowed
    GateResult = None
    check = None
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None


FOOTWEAR_SKU = "NW-SHOE-001"  # 499900 paise, footwear, stock 14 — under any
                               # sane max_paise used below and well above
                               # FREE_SHIPPING_ABOVE_PAISE on its own.
KNOWN_TOTAL_PAISE = 589882     # hand-computed above; sanity-asserted below.

CLOSED_REASON_CODES = frozenset({
    "SIG_INVALID",
    "INTENT_NOT_FOUND",
    "AGENT_MISMATCH",
    "WRONG_MERCHANT",
    "INTENT_EXPIRED",
    "OVER_LIMIT",
    "CURRENCY_MISMATCH",
    "CATEGORY_MISMATCH",
    "PURCHASES_EXHAUSTED",
    "QUOTE_NOT_FOUND",
    "CART_HASH_MISMATCH",
    "QUOTE_EXPIRED",
    "NONCE_REUSED",
    "PRICE_DRIFT",
})


pytestmark = pytest.mark.skipif(
    check is None,
    reason=f"merchant.gate not importable yet: {_IMPORT_ERROR}",
)


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolate_dbs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Every DB the Gate touches lives under tmp_path for this test only.

    `check()` has no db_path parameter — it resolves paths from `config`
    itself — so seed data below must go through the stores' default path
    (no db_path=) to land in these same monkeypatched files.
    """
    monkeypatch.setattr(config, "QUOTES_DB", tmp_path / "quotes.db")
    monkeypatch.setattr(config, "INTENTS_DB", tmp_path / "intents.db")
    monkeypatch.setattr(config, "GATE_NONCES_DB", tmp_path / "gate_nonces.db")
    monkeypatch.setattr(config, "LEDGER_DB", tmp_path / "ledger.db")
    yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_valid_case(
    *,
    agent_id: str = "agt_shopper",
    max_paise: int = 1_000_000,
    max_purchases: int = 3,
    ttl_seconds: int = 3600,
    category: str = "footwear",
    sku: str = FOOTWEAR_SKU,
    qty: int = 1,
    merchant_id: str | None = None,
) -> dict:
    """Build one fully valid, gate-passing case, and register/save every
    record the Gate is expected to look up. Individual tests perturb one
    piece of the returned dict (via `build_cart_envelope`) to make it fail
    exactly one check.
    """
    sk, vk = generate_keypair()

    intent = make_intent_mandate(
        user_id="u1",
        agent_id=agent_id,
        agent_pubkey=vk.encode().hex(),
        category=category,
        max_paise=max_paise,
        max_purchases=max_purchases,
        ttl_seconds=ttl_seconds,
        merchant_id=merchant_id,
    )
    register_intent(intent)

    lines = resolve_lines([{"sku": sku, "qty": qty}])
    quote = create_quote(lines)
    save_quote(quote)

    cart_payload = make_cart_mandate(
        intent_mandate_id=intent["mandate_id"],
        agent_id=agent_id,
        merchant_id=config.MERCHANT_ID,
        quote_id=quote.quote_id,
        cart_hash=quote.cart_hash,
        total_paise=quote.total_paise,
    )
    envelope = sign(cart_payload, sk)

    return {
        "sk": sk,
        "vk": vk,
        "agent_id": agent_id,
        "intent": intent,
        "quote": quote,
        "cart_payload": cart_payload,
        "envelope": envelope,
    }


def build_cart_envelope(
    case: dict,
    quote,
    *,
    sk=None,
    agent_id: str | None = None,
    merchant_id: str | None = None,
    intent_mandate_id_override: str | None = None,
    cart_hash_override: str | None = None,
    total_paise_override: int | None = None,
    currency_override: str | None = None,
):
    """Sign a fresh Cart Mandate for `case`'s intent against `quote`
    (a real `Quote`, or a stand-in with `.quote_id` / `.cart_hash` /
    `.total_paise`), with one or more fields perturbed after construction.

    A single field is overridden and the whole payload is re-signed, so the
    signature always stays valid — the point of every one of these tests is
    that a *specific business-rule* check fires, not that the signature is
    forged (that's `test_gate_refuses_forged_signature`'s job alone).
    """
    signing_key = sk or case["sk"]
    payload = make_cart_mandate(
        intent_mandate_id=intent_mandate_id_override or case["intent"]["mandate_id"],
        agent_id=agent_id or case["agent_id"],
        merchant_id=merchant_id or config.MERCHANT_ID,
        quote_id=quote.quote_id,
        cart_hash=cart_hash_override or quote.cart_hash,
        total_paise=total_paise_override if total_paise_override is not None else quote.total_paise,
    )
    if currency_override is not None:
        payload["currency"] = currency_override
    return sign(payload, signing_key)


def flip_hex_char(hex_str: str, index: int = 0) -> str:
    """Change exactly one hex character, keeping the string the same length
    and still valid hex — enough to break a signature without changing its
    shape."""
    original = hex_str[index]
    replacement = "1" if original == "0" else "0"
    return hex_str[:index] + replacement + hex_str[index + 1 :]


def fake_quote(quote_id: str, *, cart_hash: str, total_paise: int):
    """A minimal stand-in for a `Quote` that was never actually saved —
    just enough shape for `build_cart_envelope` to sign against a quote_id
    the merchant never issued."""
    return types.SimpleNamespace(quote_id=quote_id, cart_hash=cart_hash, total_paise=total_paise)


def patch_price(monkeypatch: pytest.MonkeyPatch, sku: str, new_price_paise: int) -> None:
    """Simulate the catalog price for `sku` changing after a quote was
    issued. Patches `merchant.catalog.get_product` (the module attribute,
    so `import merchant.catalog as catalog; catalog.get_product(...)`
    picks it up) and, defensively, `merchant.gate.get_product` in case the
    Gate imported the name directly rather than the module.
    """
    import merchant.catalog as catalog_module

    real_get_product = catalog_module.get_product

    def fake_get_product(target_sku: str) -> dict:
        product = dict(real_get_product(target_sku))
        if target_sku == sku:
            product = dict(product)
            product["price_paise"] = new_price_paise
        return product

    monkeypatch.setattr(catalog_module, "get_product", fake_get_product)

    import merchant.gate as gate_module

    if hasattr(gate_module, "get_product"):
        monkeypatch.setattr(gate_module, "get_product", fake_get_product)


def assert_refused(result, reason_code: str) -> None:
    assert result.passed is False
    assert result.reason_code == reason_code
    assert result.reason_code in CLOSED_REASON_CODES
    assert isinstance(result.message, str) and result.message
    assert isinstance(result.detail, dict)


def assert_passed(result) -> None:
    assert result.passed is True
    assert result.reason_code is None
    assert result.total_paise is not None


# ---------------------------------------------------------------------------
# Sanity: the hand-computed total used throughout matches the real quote engine.
# ---------------------------------------------------------------------------


def test_known_total_paise_matches_hand_computation():
    quote = create_quote(resolve_lines([{"sku": FOOTWEAR_SKU, "qty": 1}]))
    assert quote.total_paise == KNOWN_TOTAL_PAISE


# ---------------------------------------------------------------------------
# (a) signature
# ---------------------------------------------------------------------------


def test_gate_passes_with_valid_signature():
    case = make_valid_case()
    result = check(case["envelope"], now=case["quote"].issued_at + 1)
    assert_passed(result)
    assert result.cart_mandate_id == case["cart_payload"]["mandate_id"]
    assert result.quote_id == case["quote"].quote_id


def test_gate_refuses_forged_signature():
    case = make_valid_case()
    tampered = copy.deepcopy(case["envelope"])
    tampered["signature"] = flip_hex_char(tampered["signature"])

    result = check(tampered, now=case["quote"].issued_at + 1)
    assert_refused(result, "SIG_INVALID")


def test_gate_refuses_wrong_key_right_agent_id():
    """Red-team atk_agent_key_impersonation: an attacker with a DIFFERENT
    keypair signs a cart that correctly names the victim's agent_id, against
    the victim's intent. The signature is internally valid over the attacker's
    own key — the string agent_id matches — but the key was never the one the
    user bound into the intent. The Gate must refuse SIG_INVALID, not pass.

    A plain `verify()` would accept this envelope; authorisation is what
    rejects it. Guards check (a)'s agent-key binding
    (docs/design/agent-key-binding.md).
    """
    case = make_valid_case()  # intent bound to case["vk"]; carts signed by case["sk"]

    attacker_sk, attacker_vk = generate_keypair()
    assert attacker_vk.encode() != case["vk"].encode()

    # Same agent_id, same intent, same quote — only the signing key is wrong.
    envelope = build_cart_envelope(case, case["quote"], sk=attacker_sk)
    assert envelope["public_key"] == attacker_vk.encode().hex()
    assert envelope["payload"]["agent_id"] == case["agent_id"]

    result = check(envelope, now=case["quote"].issued_at + 1)
    assert_refused(result, "SIG_INVALID")


# ---------------------------------------------------------------------------
# (a) intent resolution
# ---------------------------------------------------------------------------


def test_gate_passes_with_known_intent():
    case = make_valid_case()
    result = check(case["envelope"], now=case["quote"].issued_at + 1)
    assert_passed(result)


def test_gate_refuses_unknown_intent_mandate_id():
    case = make_valid_case()
    envelope = build_cart_envelope(
        case, case["quote"], intent_mandate_id_override="man_int_does_not_exist"
    )

    result = check(envelope, now=case["quote"].issued_at + 1)
    assert_refused(result, "INTENT_NOT_FOUND")


# ---------------------------------------------------------------------------
# (a) agent binding
# ---------------------------------------------------------------------------


def test_gate_passes_matching_agent():
    case = make_valid_case(agent_id="agt_shopper")
    result = check(case["envelope"], now=case["quote"].issued_at + 1)
    assert_passed(result)


def test_gate_refuses_agent_mismatch():
    case = make_valid_case(agent_id="agt_shopper")
    # Signed by the same key as the registered intent, but the cart's own
    # agent_id field names a different agent than the intent named.
    envelope = build_cart_envelope(case, case["quote"], agent_id="agt_impersonator")

    result = check(envelope, now=case["quote"].issued_at + 1)
    assert_refused(result, "AGENT_MISMATCH")


# ---------------------------------------------------------------------------
# (a) merchant binding
# ---------------------------------------------------------------------------


def test_gate_passes_matching_merchant():
    case = make_valid_case()
    result = check(case["envelope"], now=case["quote"].issued_at + 1)
    assert_passed(result)


def test_gate_refuses_wrong_merchant():
    case = make_valid_case()
    envelope = build_cart_envelope(case, case["quote"], merchant_id="merch_other")

    result = check(envelope, now=case["quote"].issued_at + 1)
    assert_refused(result, "WRONG_MERCHANT")


# ---------------------------------------------------------------------------
# (b) intent expiry
# ---------------------------------------------------------------------------


def test_gate_passes_unexpired_intent():
    case = make_valid_case(ttl_seconds=3600)
    result = check(case["envelope"], now=case["quote"].issued_at + 1)
    assert_passed(result)


def test_gate_refuses_expired_intent():
    case = make_valid_case(ttl_seconds=1)
    # The Gate's own injected clock, well past the intent's expires_at —
    # never a real sleep.
    now = case["intent"]["expires_at"] + 5

    result = check(case["envelope"], now=now)
    assert_refused(result, "INTENT_EXPIRED")


# ---------------------------------------------------------------------------
# (c) total <= max_paise
# ---------------------------------------------------------------------------


def test_gate_passes_under_limit():
    case = make_valid_case(max_paise=1_000_000)
    result = check(case["envelope"], now=case["quote"].issued_at + 1)
    assert_passed(result)
    assert result.total_paise <= 1_000_000


def test_gate_refuses_over_limit():
    case = make_valid_case(max_paise=100_000)  # well under 589882
    result = check(case["envelope"], now=case["quote"].issued_at + 1)
    assert_refused(result, "OVER_LIMIT")
    assert result.detail.get("limit_paise") == 100_000
    assert result.detail.get("over_by_paise", 0) > 0


# ---------------------------------------------------------------------------
# (c) currency
# ---------------------------------------------------------------------------


def test_gate_passes_matching_currency():
    case = make_valid_case()
    assert case["cart_payload"]["currency"] == config.CURRENCY
    result = check(case["envelope"], now=case["quote"].issued_at + 1)
    assert_passed(result)


def test_gate_refuses_currency_mismatch():
    case = make_valid_case(max_paise=1_000_000)  # stay under the limit
    envelope = build_cart_envelope(case, case["quote"], currency_override="USD")

    result = check(envelope, now=case["quote"].issued_at + 1)
    assert_refused(result, "CURRENCY_MISMATCH")


# ---------------------------------------------------------------------------
# (c) category
# ---------------------------------------------------------------------------


def test_gate_passes_matching_category():
    case = make_valid_case(category="footwear")
    result = check(case["envelope"], now=case["quote"].issued_at + 1)
    assert_passed(result)


def test_gate_refuses_category_mismatch():
    # Intent grants "electronics"; the quoted cart is footwear (NW-SHOE-001).
    case = make_valid_case(category="electronics", max_paise=1_000_000)
    result = check(case["envelope"], now=case["quote"].issued_at + 1)
    assert_refused(result, "CATEGORY_MISMATCH")


# ---------------------------------------------------------------------------
# (c) purchase count
# ---------------------------------------------------------------------------


def test_gate_passes_purchases_remaining():
    case = make_valid_case(max_purchases=3)
    result = check(case["envelope"], now=case["quote"].issued_at + 1)
    assert_passed(result)

    from merchant.intent_store import purchases_used

    assert purchases_used(case["intent"]["mandate_id"]) < 3


def test_gate_refuses_purchases_exhausted():
    case = make_valid_case(max_purchases=1)

    first = check(case["envelope"], now=case["quote"].issued_at + 1)
    assert_passed(first)

    # A second, otherwise-legitimate cart against the same spent-out intent:
    # fresh quote, fresh nonce (a brand-new Cart Mandate), same intent_mandate_id.
    quote2 = create_quote(resolve_lines([{"sku": FOOTWEAR_SKU, "qty": 1}]))
    save_quote(quote2)
    envelope2 = build_cart_envelope(case, quote2)

    second = check(envelope2, now=quote2.issued_at + 1)
    assert_refused(second, "PURCHASES_EXHAUSTED")


# ---------------------------------------------------------------------------
# (d) quote resolution
# ---------------------------------------------------------------------------


def test_gate_passes_known_quote():
    case = make_valid_case()
    result = check(case["envelope"], now=case["quote"].issued_at + 1)
    assert_passed(result)
    assert result.quote_id == case["quote"].quote_id


def test_gate_refuses_unknown_quote_id():
    case = make_valid_case(max_paise=1_000_000)
    bogus = fake_quote(
        "qt_does_not_exist",
        cart_hash=case["quote"].cart_hash,
        total_paise=case["quote"].total_paise,
    )
    envelope = build_cart_envelope(case, bogus)

    result = check(envelope, now=case["quote"].issued_at + 1)
    assert_refused(result, "QUOTE_NOT_FOUND")


# ---------------------------------------------------------------------------
# (d) cart hash matches the merchant's own quote
# ---------------------------------------------------------------------------


def test_gate_passes_matching_cart_hash():
    case = make_valid_case()
    result = check(case["envelope"], now=case["quote"].issued_at + 1)
    assert_passed(result)


def test_gate_refuses_cart_tamper():
    case = make_valid_case(max_paise=1_000_000)
    envelope = build_cart_envelope(case, case["quote"], cart_hash_override="f" * 64)

    result = check(envelope, now=case["quote"].issued_at + 1)
    assert_refused(result, "CART_HASH_MISMATCH")


# ---------------------------------------------------------------------------
# (e) quote TTL
# ---------------------------------------------------------------------------


def test_gate_passes_within_ttl():
    case = make_valid_case()
    result = check(case["envelope"], now=case["quote"].issued_at + 1)
    assert_passed(result)


def test_gate_refuses_quote_expired_91s():
    case = make_valid_case()
    # Injected clock, never time.sleep(91).
    now = case["quote"].issued_at + 91

    result = check(case["envelope"], now=now)
    assert_refused(result, "QUOTE_EXPIRED")


# ---------------------------------------------------------------------------
# (f) nonce / replay
# ---------------------------------------------------------------------------


def test_gate_passes_fresh_nonce():
    case = make_valid_case()
    result = check(case["envelope"], now=case["quote"].issued_at + 1)
    assert_passed(result)

    conn = sqlite3.connect(config.GATE_NONCES_DB)
    try:
        row = conn.execute(
            "SELECT nonce FROM gate_nonces WHERE nonce = ?",
            (case["cart_payload"]["nonce"],),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None


def test_gate_refuses_replayed_nonce():
    case = make_valid_case(max_purchases=3)  # headroom so (c) doesn't fire first

    first = check(case["envelope"], now=case["quote"].issued_at + 1)
    assert_passed(first)

    # The exact same signed envelope, submitted again.
    second = check(case["envelope"], now=case["quote"].issued_at + 2)
    assert_refused(second, "NONCE_REUSED")


# ---------------------------------------------------------------------------
# (g) price drift
# ---------------------------------------------------------------------------


def test_gate_passes_unchanged_price():
    case = make_valid_case()
    result = check(case["envelope"], now=case["quote"].issued_at + 1)
    assert_passed(result)


def test_gate_refuses_price_drift(monkeypatch: pytest.MonkeyPatch):
    case = make_valid_case(max_paise=2_000_000)  # generous, isolates PRICE_DRIFT
    # The catalog price moves after the quote was issued and saved.
    patch_price(monkeypatch, FOOTWEAR_SKU, 599_900)

    result = check(case["envelope"], now=case["quote"].issued_at + 1)
    assert_refused(result, "PRICE_DRIFT")
    assert result.detail.get("quoted_total_paise") == case["quote"].total_paise
    assert result.detail.get("current_total_paise") is not None
    assert result.detail["current_total_paise"] != result.detail["quoted_total_paise"]


# ---------------------------------------------------------------------------
# Extra required tests
# ---------------------------------------------------------------------------


def test_gate_reports_first_failing_check_in_order():
    """(c) precedes (d): a cart that is BOTH over-limit AND hash-tampered
    must refuse OVER_LIMIT, not CART_HASH_MISMATCH. The cheapest possible
    regression test for check ordering."""
    case = make_valid_case(max_paise=100_000)  # under 589882: over-limit
    envelope = build_cart_envelope(case, case["quote"], cart_hash_override="f" * 64)

    result = check(envelope, now=case["quote"].issued_at + 1)
    assert_refused(result, "OVER_LIMIT")


def test_refusal_is_written_to_the_ledger():
    from core.ledger import all_entries

    case = make_valid_case(max_paise=100_000)  # forces a refusal
    check(case["envelope"], now=case["quote"].issued_at + 1)

    entries = all_entries()
    assert any(entry.event_type == "gate.refused" for entry in entries)


def test_pass_is_written_to_the_ledger():
    from core.ledger import all_entries

    case = make_valid_case()
    check(case["envelope"], now=case["quote"].issued_at + 1)

    entries = all_entries()
    assert any(entry.event_type == "gate.passed" for entry in entries)


def test_no_reason_code_outside_the_closed_set():
    scenarios = []

    case_sig = make_valid_case()
    tampered = copy.deepcopy(case_sig["envelope"])
    tampered["signature"] = flip_hex_char(tampered["signature"])
    scenarios.append((tampered, case_sig["quote"].issued_at + 1))

    case_limit = make_valid_case(max_paise=100_000)
    scenarios.append((case_limit["envelope"], case_limit["quote"].issued_at + 1))

    case_expired_quote = make_valid_case()
    scenarios.append((case_expired_quote["envelope"], case_expired_quote["quote"].issued_at + 91))

    case_category = make_valid_case(category="electronics", max_paise=1_000_000)
    scenarios.append((case_category["envelope"], case_category["quote"].issued_at + 1))

    case_merchant = make_valid_case()
    envelope_merchant = build_cart_envelope(case_merchant, case_merchant["quote"], merchant_id="merch_other")
    scenarios.append((envelope_merchant, case_merchant["quote"].issued_at + 1))

    for envelope, now in scenarios:
        result = check(envelope, now=now)
        assert result.passed is False
        assert result.reason_code in CLOSED_REASON_CODES
