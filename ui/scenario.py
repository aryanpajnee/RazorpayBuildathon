"""The live demo arc, driven through the REAL money path.

Nothing here re-implements money logic. Each act registers a real Intent
Mandate, resolves real catalog lines, creates and stores a real Quote, signs a
real Cart Mandate with a real Ed25519 key, and submits it to the REAL
`merchant.gate.check`. The Gate's own `GateResult` and the hash-chained
`core.ledger` are the source of truth; this module only narrates what actually
happened and yields it as a stream of events for the React console.

The four acts, in order, exercise a genuine pass and three genuine refusals'
worth of range:

    1. Authorized purchase           -> Gate PASSES (all seven checks green)
    2. The merchant's OWN sales agent -> Gate REFUSES OVER_LIMIT
       upsells past the signed ceiling   (the headline: no caller-identity bypass)
    3. Recovery trims under the ceiling -> Gate PASSES
    4. An attacker replays a used nonce -> Gate REFUSES NONCE_REUSED

The whole run writes to an isolated demo database directory that is wiped at
the start of every run, so the hash chain grows from genesis on camera and the
demo is perfectly repeatable. Real Razorpay is intentionally NOT called here:
the Gate decision and the tamper-evident ledger are what this console shows;
the live Razorpay order + webhook path is proven by the money-path tests and
the MCP server, not re-driven on every page load.
"""

from __future__ import annotations

import shutil
import time
from collections.abc import Iterator
from pathlib import Path

import config
from core.mandate import (
    generate_keypair,
    make_cart_mandate,
    make_intent_mandate,
    sign,
)

# --- the seven checks, and how a refusal code maps to the one that failed ----
# The Gate returns only the FIRST failing check (as a reason_code). Everything
# before it necessarily passed; everything after it was never reached. So the
# per-check panel is a faithful *derivation* from the single GateResult — the
# Gate is not modified to expose its internals.

CHECKS: list[tuple[str, str]] = [
    ("a", "Signature & authority"),
    ("b", "Intent not expired"),
    ("c", "Within signed limit"),
    ("d", "Cart matches quote"),
    ("e", "Quote still fresh"),
    ("f", "Nonce unused (no replay)"),
    ("g", "Price unchanged"),
]

# reason_code -> the check letter it belongs to.
_CODE_TO_CHECK: dict[str, str] = {
    "SIG_INVALID": "a",
    "INTENT_NOT_FOUND": "a",
    "AGENT_MISMATCH": "a",
    "WRONG_MERCHANT": "a",
    "INTENT_EXPIRED": "b",
    "QUOTE_NOT_FOUND": "c",
    "CURRENCY_MISMATCH": "c",
    "OVER_LIMIT": "c",
    "CATEGORY_MISMATCH": "c",
    "PURCHASES_EXHAUSTED": "c",
    "CART_HASH_MISMATCH": "d",
    "QUOTE_EXPIRED": "e",
    "NONCE_REUSED": "f",
    "PRICE_DRIFT": "g",
}

_DEMO_DIR = config.DATA_DIR / "ui_demo"


def _rupees(paise: int) -> str:
    """Integer-only paise -> '₹5,898.82'. No float touches a money value."""
    return f"₹{paise // 100:,}.{paise % 100:02d}"


def _point_config_at_demo_dbs() -> None:
    """Redirect every store the Gate reads to a fresh demo directory.

    The stores read `config.*_DB` at call time, so reassigning these attributes
    is exactly how the test-suite isolates the Gate under tmp_path. Wiping the
    directory first means the ledger starts empty and the chain grows from
    genesis every run.
    """
    if _DEMO_DIR.exists():
        shutil.rmtree(_DEMO_DIR)
    _DEMO_DIR.mkdir(parents=True, exist_ok=True)
    config.LEDGER_DB = _DEMO_DIR / "ledger.db"
    config.QUOTES_DB = _DEMO_DIR / "quotes.db"
    config.INTENTS_DB = _DEMO_DIR / "intents.db"
    config.GATE_NONCES_DB = _DEMO_DIR / "gate_nonces.db"
    config.ORDERS_DB = _DEMO_DIR / "orders.db"


def _check_states(passed: bool, reason_code: str | None) -> list[dict]:
    """Derive the seven per-check states from a single GateResult."""
    if passed:
        return [{"id": cid, "label": label, "state": "pass"} for cid, label in CHECKS]

    failing = _CODE_TO_CHECK.get(reason_code or "", "a")
    order = [cid for cid, _ in CHECKS]
    fail_idx = order.index(failing)
    states = []
    for idx, (cid, label) in enumerate(CHECKS):
        if idx < fail_idx:
            state = "pass"
        elif idx == fail_idx:
            state = "refuse"
        else:
            state = "skip"
        states.append({"id": cid, "label": label, "state": state})
    return states


class _Run:
    """One scenario run against the real, demo-isolated money path."""

    def __init__(self) -> None:
        _point_config_at_demo_dbs()
        # Import merchant modules AFTER config is redirected — they read the
        # config paths at call time, so import order does not matter, but this
        # keeps the isolation obvious.
        from merchant import intent_store, quote_store  # noqa: PLC0415
        from merchant.catalog import get_product, resolve_lines  # noqa: PLC0415
        from merchant.gate import check  # noqa: PLC0415
        from merchant.quote import create_quote  # noqa: PLC0415
        from core.ledger import all_entries, append, verify_chain  # noqa: PLC0415

        self._intent_store = intent_store
        self._quote_store = quote_store
        self._resolve_lines = resolve_lines
        self._get_product = get_product
        self._create_quote = create_quote
        self._gate_check = check
        self._all_entries = all_entries
        self._append = append
        self._verify_chain = verify_chain

        self._sk, self._vk = generate_keypair()
        self._agent_id = f"agt_console_{self._vk.encode().hex()[:8]}"
        self._ceiling_paise = 600_000  # ₹6,000 signed footwear ceiling
        self._intent: dict | None = None
        self._emitted_seq = 0

    # -- helpers --------------------------------------------------------------

    def _quote_for(self, items: list[dict]) -> object:
        lines = self._resolve_lines(items)
        quote = self._create_quote(lines)
        self._quote_store.save_quote(quote)
        self._append(
            "quote.issued",
            {
                "quote_id": quote.quote_id,
                "cart_hash": quote.cart_hash,
                "total_paise": quote.total_paise,
                "expires_at": quote.expires_at,
            },
        )
        return quote

    def _signed_envelope(self, quote: object) -> dict:
        payload = make_cart_mandate(
            intent_mandate_id=self._intent["mandate_id"],
            agent_id=self._agent_id,
            merchant_id=config.MERCHANT_ID,
            quote_id=quote.quote_id,  # type: ignore[attr-defined]
            cart_hash=quote.cart_hash,  # type: ignore[attr-defined]
            total_paise=quote.total_paise,  # type: ignore[attr-defined]
        )
        return sign(payload, self._sk)

    def _new_ledger_events(self) -> Iterator[dict]:
        """Yield ledger rows appended since the last time we looked."""
        entries = self._all_entries()
        for entry in entries[self._emitted_seq :]:
            yield {
                "type": "ledger",
                "seq": entry.seq,
                "event_type": entry.event_type,
                "entry_hash": entry.entry_hash,
                "prev_hash": entry.prev_hash,
                "ts": entry.ts,
                "payload": entry.payload,
            }
        self._emitted_seq = len(entries)

    def _submit(self, envelope: dict, *, cart_label: str, limit_paise: int) -> Iterator[dict]:
        """Run the real Gate on an envelope and yield the derived UI events."""
        # Flush the quote.issued row(s) first.
        yield from self._new_ledger_events()

        result = self._gate_check(envelope)
        yield {
            "type": "gate_begin",
            "cart_label": cart_label,
            "total_paise": result.total_paise,
            "total_rupees": _rupees(result.total_paise) if result.total_paise is not None else None,
            "limit_paise": limit_paise,
            "limit_rupees": _rupees(limit_paise),
        }
        for state in _check_states(result.passed, result.reason_code):
            yield {"type": "gate_check", **state}

        yield {
            "type": "gate_result",
            "passed": result.passed,
            "reason_code": result.reason_code,
            "message": result.message,
            "detail": result.detail,
            "total_paise": result.total_paise,
            "total_rupees": _rupees(result.total_paise) if result.total_paise is not None else None,
        }

        # On a pass, the money would now move: record the order the way the real
        # API does (order-first), with a clearly-demo order id — real Razorpay
        # order creation is proven by the money-path test, not re-driven here.
        if result.passed:
            order_id = f"order_uidemo_{result.quote_id[-8:]}"
            self._append(
                "order.created",
                {"order_id": order_id, "quote_id": result.quote_id, "total_paise": result.total_paise},
            )
            self._append(
                "payment.attempted",
                {"quote_id": result.quote_id, "razorpay_order_id": order_id},
            )

        yield from self._new_ledger_events()
        status = self._verify_chain()
        yield {
            "type": "chain",
            "ok": status.ok,
            "entries_checked": status.entries_checked,
            "detail": status.detail,
        }

    # -- the arc --------------------------------------------------------------

    def events(self) -> Iterator[dict]:
        yield {"type": "run_begin", "agent_id": self._agent_id, "ceiling_rupees": _rupees(self._ceiling_paise)}

        # The user grants authority once: a signed Intent Mandate. The agent's
        # public key is bound INTO the intent, so a cart signed by any other
        # key is refused (SIG_INVALID) — verify() proves origin, not permission.
        self._intent = make_intent_mandate(
            user_id="user_console",
            agent_id=self._agent_id,
            agent_pubkey=self._vk.encode().hex(),
            category="footwear",
            max_paise=self._ceiling_paise,
            max_purchases=5,
            ttl_seconds=3600,
            merchant_id=config.MERCHANT_ID,
        )
        self._intent_store.register_intent(self._intent)
        yield {
            "type": "conversation",
            "role": "system",
            "text": (
                f"User signs one Intent Mandate: footwear, ceiling "
                f"{_rupees(self._ceiling_paise)}, key bound to agent "
                f"{self._agent_id[:16]}…"
            ),
        }

        # --- Act 1: an authorized purchase -----------------------------------
        yield {"type": "act", "n": 1, "title": "Authorized purchase"}
        yield {"type": "conversation", "role": "buyer",
               "text": "I need a road running shoe under the ceiling. Quoting the Tempo 3."}
        quote1 = self._quote_for([{"sku": "NW-SHOE-001", "qty": 1}])
        yield {"type": "conversation", "role": "merchant",
               "text": f"Tempo 3 quoted at {_rupees(quote1.total_paise)}, GST-inclusive. Cart hash bound."}
        yield {"type": "conversation", "role": "buyer",
               "text": "Signing the Cart Mandate with my bound key and submitting to the Gate."}
        env1 = self._signed_envelope(quote1)
        yield from self._submit(env1, cart_label="Tempo 3 ×1", limit_paise=self._ceiling_paise)
        yield {"type": "conversation", "role": "gate",
               "text": "All seven checks pass. Authorized. Order created."}

        # --- Act 2: the merchant's OWN sales agent oversteps -----------------
        yield {"type": "act", "n": 2, "title": "The merchant's own sales agent upsells past the ceiling"}
        yield {"type": "conversation", "role": "merchant",
               "text": "Sales agent: add the Ridge Trail Shoe too — great pairing for off-road long runs."}
        quote2 = self._quote_for([{"sku": "NW-SHOE-001", "qty": 1}, {"sku": "NW-SHOE-003", "qty": 1}])
        yield {"type": "conversation", "role": "buyer",
               "text": f"Upsold cart re-derives to {_rupees(quote2.total_paise)} — over the signed ceiling. Submitting anyway."}
        env2 = self._signed_envelope(quote2)
        yield from self._submit(env2, cart_label="Tempo 3 + Ridge Trail", limit_paise=self._ceiling_paise)
        yield {"type": "conversation", "role": "gate",
               "text": "Refused OVER_LIMIT. The Gate takes no caller identity — it refuses the merchant's own sales agent exactly as it would an attacker."}

        # --- Act 3: recovery trims back under the ceiling --------------------
        yield {"type": "act", "n": 3, "title": "Recovery trims back under the ceiling"}
        yield {"type": "conversation", "role": "buyer",
               "text": "Dropping the upsell, re-quoting the single shoe, and re-signing (fresh nonce)."}
        quote3 = self._quote_for([{"sku": "NW-SHOE-001", "qty": 1}])
        env3 = self._signed_envelope(quote3)
        yield from self._submit(env3, cart_label="Tempo 3 ×1 (recovered)", limit_paise=self._ceiling_paise)
        yield {"type": "conversation", "role": "gate",
               "text": "Back inside the signed authority. Authorized. Growth stayed inside consent."}

        # --- Act 4: an attacker replays a used nonce -------------------------
        yield {"type": "act", "n": 4, "title": "Replay attack"}
        yield {"type": "conversation", "role": "system",
               "text": "A third party captures Act 1's already-authorized cart and replays it verbatim."}
        yield from self._submit(env1, cart_label="Act 1 cart, replayed", limit_paise=self._ceiling_paise)
        yield {"type": "conversation", "role": "gate",
               "text": "Refused NONCE_REUSED. A signature proves origin, never a second spend."}

        # --- close: the whole chain is still intact --------------------------
        status = self._verify_chain()
        yield {
            "type": "run_end",
            "chain_ok": status.ok,
            "entries_checked": status.entries_checked,
            "detail": status.detail,
        }


def run_events() -> Iterator[dict]:
    """Public entry point: yield the full scenario as a stream of UI events."""
    yield from _Run().events()


if __name__ == "__main__":  # pragma: no cover - manual smoke check
    for event in run_events():
        print(event)
        time.sleep(0.02)
