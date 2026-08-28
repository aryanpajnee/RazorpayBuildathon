"""The interactive engine behind the Authority Bench.

The React console lets a person compose a Cart Mandate — a signed spending
ceiling, a cart of real catalogue products, and optionally an attempt to cheat
— and submit it to the REAL merchant Gate. This module is what actually runs
it: it registers a real Intent Mandate, resolves real catalogue lines, creates
and stores a real Quote, signs a real Ed25519 Cart Mandate, and calls the REAL
`merchant.gate.check`. No money logic is re-implemented here; the Gate's own
`GateResult` and the hash-chained `core.ledger` are the source of truth.

Everything runs against an isolated demo database directory so the bench never
touches real operational data, and the hash chain accumulates across submits so
a person can watch it grow as they experiment. `reset()` starts a fresh chain.

The "attacks" a person can toggle each map to a real Gate refusal — the Gate
decides, this module only constructs the (adversarial) envelope:

    forge_key     sign with a stranger's key, not the one the intent binds -> SIG_INVALID
    tamper_total  raise total_paise AFTER signing (breaks the signature)   -> SIG_INVALID
    wrong_category slip a non-footwear item into a footwear-scoped intent  -> CATEGORY_MISMATCH
    (a low ceiling vs a big cart needs no toggle)                          -> OVER_LIMIT
    replay        resubmit the previous mandate verbatim (reused nonce)    -> NONCE_REUSED
"""

from __future__ import annotations

import copy
import shutil
import threading
import time
from dataclasses import dataclass

import config
from core.mandate import generate_keypair, make_cart_mandate, make_intent_mandate, sign

# The seven checks, and which one a given refusal code belongs to. The Gate
# returns only the FIRST failure; everything before it passed and everything
# after was never reached, so the per-check view is a faithful derivation.
CHECKS: list[tuple[str, str]] = [
    ("a", "Signature & authority"),
    ("b", "Intent not expired"),
    ("c", "Within signed ceiling"),
    ("d", "Cart matches quote"),
    ("e", "Quote still fresh"),
    ("f", "Nonce unused"),
    ("g", "Price unchanged"),
]
_CODE_TO_CHECK = {
    "SIG_INVALID": "a", "INTENT_NOT_FOUND": "a", "AGENT_MISMATCH": "a", "WRONG_MERCHANT": "a",
    "INTENT_EXPIRED": "b",
    "QUOTE_NOT_FOUND": "c", "CURRENCY_MISMATCH": "c", "OVER_LIMIT": "c",
    "CATEGORY_MISMATCH": "c", "PURCHASES_EXHAUSTED": "c",
    "CART_HASH_MISMATCH": "d", "QUOTE_EXPIRED": "e", "NONCE_REUSED": "f", "PRICE_DRIFT": "g",
}

_DEMO_DIR = config.DATA_DIR / "ui_demo"
_WRONG_CATEGORY_SKU = "NW-SOCK-001"  # a real product, but socks, not footwear


def rupees(paise: int) -> str:
    return f"₹{paise // 100:,}.{paise % 100:02d}"


def _point_config_at_demo_dbs(*, wipe: bool) -> None:
    if wipe and _DEMO_DIR.exists():
        shutil.rmtree(_DEMO_DIR)
    _DEMO_DIR.mkdir(parents=True, exist_ok=True)
    config.LEDGER_DB = _DEMO_DIR / "ledger.db"
    config.QUOTES_DB = _DEMO_DIR / "quotes.db"
    config.INTENTS_DB = _DEMO_DIR / "intents.db"
    config.GATE_NONCES_DB = _DEMO_DIR / "gate_nonces.db"
    config.ORDERS_DB = _DEMO_DIR / "orders.db"


@dataclass
class _Bench:
    lock: threading.Lock
    sk: object
    vk: object
    agent_id: str
    emitted: int
    last_envelope: dict | None
    last_cart_label: str | None


_bench: _Bench | None = None


def _engine() -> _Bench:
    global _bench
    if _bench is None:
        _point_config_at_demo_dbs(wipe=True)
        sk, vk = generate_keypair()
        _bench = _Bench(
            lock=threading.Lock(), sk=sk, vk=vk,
            agent_id=f"agt_bench_{vk.encode().hex()[:8]}",
            emitted=0, last_envelope=None, last_cart_label=None,
        )
    return _bench


def catalog() -> list[dict]:
    """The footwear a person can add to a cart, cheapest first."""
    _engine()
    from merchant.catalog import all_products  # noqa: PLC0415

    items = [
        {"sku": p["sku"], "name": p["name"], "price_paise": p["price_paise"],
         "price_rupees": rupees(p["price_paise"]), "in_stock": p.get("stock", 0) > 0}
        for p in all_products() if p.get("category") == "footwear"
    ]
    return sorted(items, key=lambda p: p["price_paise"])


def _check_states(passed: bool, reason_code: str | None) -> list[dict]:
    if passed:
        return [{"id": cid, "label": label, "state": "pass"} for cid, label in CHECKS]
    failing = _CODE_TO_CHECK.get(reason_code or "", "a")
    order = [cid for cid, _ in CHECKS]
    fail_idx = order.index(failing)
    out = []
    for idx, (cid, label) in enumerate(CHECKS):
        out.append({"id": cid, "label": label,
                    "state": "pass" if idx < fail_idx else "refuse" if idx == fail_idx else "skip"})
    return out


def _new_ledger_rows(bench: _Bench) -> list[dict]:
    from core.ledger import all_entries  # noqa: PLC0415

    entries = all_entries()
    rows = [
        {"seq": e.seq, "event_type": e.event_type, "entry_hash": e.entry_hash,
         "prev_hash": e.prev_hash, "payload": e.payload}
        for e in entries[bench.emitted :]
    ]
    bench.emitted = len(entries)
    return rows


def _chain_status() -> dict:
    from core.ledger import verify_chain  # noqa: PLC0415

    s = verify_chain()
    return {"ok": s.ok, "entries_checked": s.entries_checked, "detail": s.detail,
            "first_broken_seq": s.first_broken_seq}


def reset() -> dict:
    """Start a fresh hash chain and a fresh buyer identity."""
    global _bench
    _bench = None
    _engine()
    return {"ledger": [], "chain": _chain_status()}


def submit(*, ceiling_paise: int, items: list[dict], attacks: list[str] | None = None,
           replay: bool = False) -> dict:
    """Run one composed mandate through the real Gate and return the outcome."""
    bench = _engine()
    attacks = attacks or []
    with bench.lock:
        from core.ledger import append  # noqa: PLC0415
        from merchant import intent_store, quote_store  # noqa: PLC0415
        from merchant.catalog import ProductNotFound, OutOfStock, resolve_lines  # noqa: PLC0415
        from merchant.gate import check  # noqa: PLC0415
        from merchant.quote import create_quote  # noqa: PLC0415

        # --- a replay resubmits the previous, already-spent envelope verbatim -
        if replay:
            if bench.last_envelope is None:
                return {"error": "Nothing to replay yet — submit a mandate first."}
            result = check(copy.deepcopy(bench.last_envelope))
            return _outcome(bench, result, cart_label=f"{bench.last_cart_label} (replayed)",
                            ceiling_paise=ceiling_paise, replayed=True)

        if not items:
            return {"error": "Add at least one item to the cart before submitting."}

        # --- the cart, plus any wrong-category smuggling ---------------------
        cart_items = [{"sku": it["sku"], "qty": int(it.get("qty", 1))} for it in items]
        if "wrong_category" in attacks:
            cart_items.append({"sku": _WRONG_CATEGORY_SKU, "qty": 1})

        # --- a fresh intent bound to the buyer key (ceiling can change each run)
        intent = make_intent_mandate(
            user_id="user_bench", agent_id=bench.agent_id,
            agent_pubkey=bench.vk.encode().hex(), category="footwear",
            max_paise=ceiling_paise, max_purchases=50, ttl_seconds=3600,
            merchant_id=config.MERCHANT_ID,
        )
        intent_store.register_intent(intent)

        try:
            quote = create_quote(resolve_lines(cart_items))
        except ProductNotFound as exc:
            return {"error": f"Unknown product: {exc}"}
        except OutOfStock as exc:
            return {"error": f"Out of stock: {exc}"}
        quote_store.save_quote(quote)
        append("quote.issued", {"quote_id": quote.quote_id, "cart_hash": quote.cart_hash,
                                "total_paise": quote.total_paise, "expires_at": quote.expires_at})

        payload = make_cart_mandate(
            intent_mandate_id=intent["mandate_id"], agent_id=bench.agent_id,
            merchant_id=config.MERCHANT_ID, quote_id=quote.quote_id,
            cart_hash=quote.cart_hash, total_paise=quote.total_paise,
        )
        # --- forge the key: sign with a stranger's, not the one intent binds --
        signing_key = generate_keypair()[0] if "forge_key" in attacks else bench.sk
        envelope = sign(payload, signing_key)
        # --- tamper the total AFTER signing: breaks the signature ------------
        if "tamper_total" in attacks:
            envelope = copy.deepcopy(envelope)
            envelope["payload"]["total_paise"] = quote.total_paise + 100_000

        cart_label = ", ".join(
            f"{it['sku'].replace('NW-SHOE-', 'Shoe ')}×{it.get('qty', 1)}" for it in items
        )
        result = check(envelope)
        # Only a mandate that actually PASSED consumed its nonce, so only a
        # passed mandate is worth replaying — resubmitting it then trips the
        # nonce defence (NONCE_REUSED). A refused cart never reached the nonce
        # check, so replaying it would just refuse the same way again.
        if result.passed:
            bench.last_envelope = envelope
            bench.last_cart_label = cart_label
        return _outcome(bench, result, cart_label=cart_label,
                        ceiling_paise=ceiling_paise, replayed=False)


def _outcome(bench: _Bench, result, *, cart_label: str, ceiling_paise: int, replayed: bool) -> dict:
    return {
        "cart_label": cart_label,
        "replayed": replayed,
        "passed": result.passed,
        "reason_code": result.reason_code,
        "message": result.message,
        "detail": result.detail,
        "total_paise": result.total_paise,
        "total_rupees": rupees(result.total_paise) if result.total_paise is not None else None,
        "ceiling_paise": ceiling_paise,
        "ceiling_rupees": rupees(ceiling_paise),
        "agent_id": bench.agent_id,
        "agent_pubkey": bench.vk.encode().hex(),
        "checks": _check_states(result.passed, result.reason_code),
        "ledger": _new_ledger_rows(bench),
        "chain": _chain_status(),
    }
