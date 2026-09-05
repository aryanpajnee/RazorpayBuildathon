"""The nine canonical adversarial attacks for Track 01, Phase 6 red team.

Each attack below is the deterministic, offline reproduction of one entry in
the adversarial test suite promised by the design spec: replay, quote
expiry, price drift, cart tamper, over-limit, forged signature, expired
intent, payment-failure double-order, and ledger tamper. Seven of the nine
are caught by `merchant.gate.check()` (a `GateResult` with a specific
closed-set `reason_code`, never an exception -- the Gate never trusts buyer
input enough to let it raise); one (payment failure never doubling an order)
is caught by `merchant.gateway.create_order()`'s reservation-first
idempotency on `quote_id`; one (ledger tamper) is caught by
`core.ledger.verify_chain()` walking the hash chain from genesis.

Which defense catches which attack:

    1. Replay                -> Gate: NONCE_REUSED
    2. Quote expiry (91s)    -> Gate: QUOTE_EXPIRED
    3. Price drift           -> Gate: PRICE_DRIFT
    4. Cart tamper           -> Gate: CART_HASH_MISMATCH
    5. Over-limit            -> Gate: OVER_LIMIT
    6. Forged signature      -> Gate: SIG_INVALID
    7. Expired intent        -> Gate: INTENT_EXPIRED
    8. Payment failure       -> merchant.gateway idempotency (same quote_id
       never doubles an order          -> same order_id, gateway called once)
    9. Ledger tamper         -> core.ledger.verify_chain(): ok=False,
                                 first_broken_seq points at the edited row

For every Gate-refusal attack (1-7), the test also confirms a `gate.refused`
row landed in the ledger carrying that exact reason_code -- the refusal
itself must be auditable, not just returned to the caller.

This file borrows `make_valid_case`, `build_cart_envelope`, `flip_hex_char`,
`patch_price` and `assert_refused` from `tests/test_gate.py` rather than
reinventing them (same fixtures, same hand-verified totals). Fixtures do not
cross pytest modules, so the `isolate_dbs` autouse fixture is copied here
verbatim -- this file needs its own isolated, tmp_path-scoped DBs exactly
like test_gate.py does.

Fully offline and deterministic throughout: no network, no real Razorpay
(FakeGateway only), no LLM, no time.sleep -- every expiry is simulated via
the Gate's injected `now=` clock.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import config
from core.ledger import all_entries, append, verify_chain
from merchant.gate import check
from merchant.gateway import FakeGateway, create_order

from tests.test_gate import (
    FOOTWEAR_SKU,
    assert_passed,
    assert_refused,
    build_cart_envelope,
    flip_hex_char,
    make_valid_case,
    patch_price,
)


# ---------------------------------------------------------------------------
# Isolation (copied verbatim from tests/test_gate.py -- fixtures don't cross
# modules, and every attack here must run against its own tmp_path DBs).
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolate_dbs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Every DB the Gate touches lives under tmp_path for this test only.

    `check()` has no db_path parameter -- it resolves paths from `config`
    itself -- so seed data below must go through the stores' default path
    (no db_path=) to land in these same monkeypatched files.
    """
    monkeypatch.setattr(config, "QUOTES_DB", tmp_path / "quotes.db")
    monkeypatch.setattr(config, "INTENTS_DB", tmp_path / "intents.db")
    monkeypatch.setattr(config, "GATE_NONCES_DB", tmp_path / "gate_nonces.db")
    monkeypatch.setattr(config, "LEDGER_DB", tmp_path / "ledger.db")
    yield


def _assert_last_ledger_refusal(reason_code: str) -> None:
    """The most recent ledger row must be exactly the `gate.refused` entry
    for this attack, carrying the same reason_code the Gate returned to the
    caller -- proving the refusal is auditable, not just returned."""
    entries = all_entries()
    assert entries, "expected at least one ledger entry after a Gate refusal"
    last = entries[-1]
    assert last.event_type == "gate.refused"
    assert last.payload.get("reason_code") == reason_code


# ---------------------------------------------------------------------------
# Attack 1: Replay -- the same signed Cart Mandate submitted twice.
# ---------------------------------------------------------------------------


def test_attack_replay_same_envelope_twice_second_refused_nonce_reused():
    case = make_valid_case(max_purchases=3)  # headroom so (c) doesn't fire first

    first = check(case["envelope"], now=case["quote"].issued_at + 1)
    assert_passed(first)

    # The literal same signed envelope, submitted again by a replaying
    # attacker who captured it off the wire.
    second = check(case["envelope"], now=case["quote"].issued_at + 2)
    assert_refused(second, "NONCE_REUSED")
    _assert_last_ledger_refusal("NONCE_REUSED")


# ---------------------------------------------------------------------------
# Attack 2: Quote expiry -- a cart submitted 91s after the quote was issued
# (TTL is config.QUOTE_TTL_SECONDS == 90).
# ---------------------------------------------------------------------------


def test_attack_quote_expiry_91s_refused_quote_expired():
    case = make_valid_case()
    assert config.QUOTE_TTL_SECONDS == 90

    issued_at = case["quote"].issued_at
    # Injected clock, never a real sleep.
    now = issued_at + 91

    result = check(case["envelope"], now=now)
    assert_refused(result, "QUOTE_EXPIRED")
    _assert_last_ledger_refusal("QUOTE_EXPIRED")


# ---------------------------------------------------------------------------
# Attack 3: Price drift -- the catalog price moves after the quote was
# issued and saved; the signed cart still names the old (now stale) total.
# ---------------------------------------------------------------------------


def test_attack_price_drift_after_quote_issued_refused_price_drift(monkeypatch: pytest.MonkeyPatch):
    case = make_valid_case(max_paise=2_000_000)  # generous, isolates PRICE_DRIFT
    patch_price(monkeypatch, FOOTWEAR_SKU, 599_900)

    result = check(case["envelope"], now=case["quote"].issued_at + 1)
    assert_refused(result, "PRICE_DRIFT")
    _assert_last_ledger_refusal("PRICE_DRIFT")


# ---------------------------------------------------------------------------
# Attack 4: Cart tamper -- the signature is valid (the whole payload,
# tampered cart_hash included, was re-signed), but the cart_hash disagrees
# with the merchant's own stored quote.
# ---------------------------------------------------------------------------


def test_attack_cart_tamper_signed_but_wrong_cart_hash_refused_cart_hash_mismatch():
    case = make_valid_case(max_paise=1_000_000)
    tampered_hash = flip_hex_char(case["quote"].cart_hash)
    envelope = build_cart_envelope(case, case["quote"], cart_hash_override=tampered_hash)

    result = check(envelope, now=case["quote"].issued_at + 1)
    assert_refused(result, "CART_HASH_MISMATCH")
    _assert_last_ledger_refusal("CART_HASH_MISMATCH")


# ---------------------------------------------------------------------------
# Attack 5: Over-limit -- the buyer's own signed intent ceiling is below the
# quoted total. NW-SHOE-001's known total is 589882 paise (see
# tests/test_gate.py's module docstring); 500000 is comfortably under it.
# ---------------------------------------------------------------------------


def test_attack_over_limit_cart_exceeds_signed_ceiling_refused_over_limit():
    case = make_valid_case(max_paise=500_000)  # below the known 589882 total

    result = check(case["envelope"], now=case["quote"].issued_at + 1)
    assert_refused(result, "OVER_LIMIT")
    assert result.detail.get("limit_paise") == 500_000
    assert result.detail.get("over_by_paise", 0) > 0
    _assert_last_ledger_refusal("OVER_LIMIT")


# ---------------------------------------------------------------------------
# Attack 6: Forged signature -- a valid envelope with one hex character of
# its signature flipped. Everything else (cart_hash, total, nonce) is
# untouched -- only the signature itself is broken.
# ---------------------------------------------------------------------------


def test_attack_forged_signature_one_hex_char_flipped_refused_sig_invalid():
    case = make_valid_case()
    import copy

    tampered = copy.deepcopy(case["envelope"])
    tampered["signature"] = flip_hex_char(tampered["signature"])

    result = check(tampered, now=case["quote"].issued_at + 1)
    assert_refused(result, "SIG_INVALID")
    _assert_last_ledger_refusal("SIG_INVALID")


# ---------------------------------------------------------------------------
# Attack 7: Expired intent -- the on-file Intent Mandate itself has expired
# by the time the cart is submitted (short ttl_seconds, clock pushed past
# expires_at).
# ---------------------------------------------------------------------------


def test_attack_expired_intent_mandate_refused_intent_expired():
    case = make_valid_case(ttl_seconds=1)
    now = case["intent"]["expires_at"] + 5  # injected clock, no real sleep

    result = check(case["envelope"], now=now)
    assert_refused(result, "INTENT_EXPIRED")
    _assert_last_ledger_refusal("INTENT_EXPIRED")


# ---------------------------------------------------------------------------
# Attack 8: Payment failure must never produce a second payment or order.
# Not a Gate check -- this is standing rule 4: retries, recovery-agent
# decisions and webhook replays all resolve through the same idempotency
# key, quote_id. Exercised directly against merchant.gateway.create_order()
# with a FakeGateway, fully offline, isolating orders.db under tmp_path.
# ---------------------------------------------------------------------------


def test_attack_retry_after_failure_never_doubles_the_order(tmp_path: Path):
    """Simulates the adversarial sequence: a caller (retry logic, a recovery
    agent, or a replayed webhook) submits the SAME quote_id for order
    creation more than once -- as would happen if a client never saw the
    first response (e.g. because the payment attempt appeared to fail) and
    retried. The merchant must return the exact same order_id both times and
    must never call the gateway a second time for that quote_id, so a
    "failed" transaction from the caller's point of view can never silently
    become two real orders."""
    db = tmp_path / "orders.db"
    gw = FakeGateway()
    quote_id = "quote_adversarial_retry"

    first = create_order(quote_id, 589_882, gateway=gw, db_path=db)
    assert first.from_cache is False
    assert gw.calls == 1

    # The adversarial retry: same quote_id, same amount, as if the buyer's
    # client (or a recovery/negotiation node, or a replayed webhook) resent
    # the same request believing the first attempt had failed.
    second = create_order(quote_id, 589_882, gateway=gw, db_path=db)
    assert second.order_id == first.order_id
    assert second.from_cache is True
    assert gw.calls == 1, "a retry on the same quote_id must never reach the gateway again"

    # A third retry, for good measure -- idempotency must hold under
    # repeated replay, not just a single duplicate.
    third = create_order(quote_id, 589_882, gateway=gw, db_path=db)
    assert third.order_id == first.order_id
    assert gw.calls == 1

    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            "SELECT order_id FROM orders WHERE quote_id = ?", (quote_id,)
        ).fetchall()
    finally:
        conn.close()
    assert rows == [(first.order_id,)], "exactly one order row must ever exist for this quote_id"


# ---------------------------------------------------------------------------
# Attack 9: Ledger tamper -- a mid-chain row is edited directly with raw
# SQL (bypassing the append-only API entirely, as an attacker with
# filesystem/DB access would). verify_chain() must detect it and name the
# exact seq where the chain first breaks.
# ---------------------------------------------------------------------------


def test_attack_ledger_tamper_mid_chain_row_detected_at_exact_seq():
    append("quote.issued", {"quote_id": "qt_adversarial_0001"})
    append("mandate.verified", {"mandate_id": "man_adversarial_0001"})
    append("gate.passed", {"quote_id": "qt_adversarial_0001"})

    pre_tamper_status = verify_chain()
    assert pre_tamper_status.ok is True
    assert pre_tamper_status.entries_checked == 3
    assert pre_tamper_status.first_broken_seq is None

    # Bypass the append-only API entirely: an attacker with raw DB access
    # edits seq=2's payload directly, exactly as docs/specs/ledger-spec.md
    # sec 10's tamper demo does.
    conn = sqlite3.connect(config.LEDGER_DB)
    try:
        conn.execute(
            "UPDATE ledger SET payload = ? WHERE seq = 2",
            ('{"mandate_id":"man_adversarial_FORGED"}',),
        )
        conn.commit()
    finally:
        conn.close()

    status = verify_chain()
    assert status.ok is False
    assert status.first_broken_seq == 2
