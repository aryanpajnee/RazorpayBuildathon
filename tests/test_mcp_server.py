"""Tests for merchant/mcp_server.py — the MCP transport adapter.

Proves the invariant `merchant/mcp_server.py`'s own docstring states: MCP is
a transport adapter, never a second door to the money path. Every test
drives the tool functions directly -- the `@mcp.tool()` decorator (from the
installed `mcp.server.mcpserver.MCPServer`, aliased `FastMCP` in that module)
only registers the function as a side effect and returns it unchanged, so
`search_catalog`, `get_quote`, `checkout`, and `buy` are plain, directly
callable Python functions here, exactly as an MCP client would invoke them
over the protocol.

Fully offline: `merchant.mcp_server._client()` is monkeypatched to return a
`fastapi.testclient.TestClient` bound to the SAME `merchant.api` FastAPI app
the HTTP-layer tests (tests/test_api.py, tests/test_money_path_api.py) use,
instead of a real socket at `config.MERCHANT_BASE_URL`. TestClient exposes
the identical sync `.get()`/`.post()`/context-manager interface `_client()`
promises (it IS an httpx.Client subclass, bridged onto the ASGI app), so
`merchant/mcp_server.py` needs no test-only branch -- no live server, no
real Razorpay, no network. Store isolation follows
tests/test_money_path_api.py's fixture exactly (QUOTES_DB / INTENTS_DB /
GATE_NONCES_DB / LEDGER_DB / the fake gateway / gateway.ORDERS_DB), plus
config.KEY_DIR isolated to tmp_path so `buy()`'s local signing key never
touches data/keys.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import config
from core.mandate import (
    generate_keypair,
    make_cart_mandate,
    make_intent_mandate,
    save_keypair,
    sign,
)
from merchant import gateway, intent_store
from merchant.api import app
from merchant.mcp_server import buy, checkout, get_quote, search_catalog

FOOTWEAR_SKU = "NW-SHOE-007"  # Northwind Drift Recovery Slide, Rs 1499, stock 30


@pytest.fixture
def merchant(tmp_path, monkeypatch):
    """Isolate every store the app touches, force the fake gateway, isolate
    the key directory, and route merchant.mcp_server's httpx client at the
    in-process ASGI app instead of a real socket -- so these tests are fully
    offline and never share state with a real run."""
    monkeypatch.setattr(config, "QUOTES_DB", tmp_path / "quotes.db")
    monkeypatch.setattr(config, "INTENTS_DB", tmp_path / "intents.db")
    monkeypatch.setattr(config, "GATE_NONCES_DB", tmp_path / "gate_nonces.db")
    monkeypatch.setattr(config, "LEDGER_DB", tmp_path / "ledger.db")
    monkeypatch.setattr(config, "USE_FAKE_GATEWAY", True)
    monkeypatch.setattr(config, "KEY_DIR", tmp_path / "keys")
    # gateway.py binds ORDERS_DB at import time, same as test_money_path_api.py.
    monkeypatch.setattr(gateway, "ORDERS_DB", tmp_path / "orders.db")

    import merchant.mcp_server as mcp_server

    # mcp_server calls `with _client() as client:` — a SYNC context manager.
    # FastAPI's TestClient is a sync httpx.Client bound to the in-process ASGI
    # app (ASGITransport is async-only and would fail that `with`), so it is
    # the drop-in that keeps these tests offline with zero source changes.
    def _test_client() -> TestClient:
        return TestClient(app)

    monkeypatch.setattr(mcp_server, "_client", _test_client)
    return mcp_server


def _grant_intent(*, max_paise: int, agent_pubkey_hex: str | None = None) -> tuple[dict, object, object]:
    """Register an intent for footwear. Returns (intent_payload, sk, vk).

    Pass agent_pubkey_hex to bind the intent to a specific key instead of a
    freshly generated one -- used to set up (or deliberately violate) buy()'s
    demo agent-key-binding precondition.
    """
    sk, vk = generate_keypair()
    agent_id = f"agent_mcp_test_{vk.encode().hex()[:8]}"
    intent_payload = make_intent_mandate(
        user_id="user_mcp_test",
        agent_id=agent_id,
        agent_pubkey=agent_pubkey_hex or vk.encode().hex(),
        category="footwear",
        max_paise=max_paise,
        max_purchases=5,
        ttl_seconds=3600,
    )
    intent_store.register_intent(intent_payload)
    return intent_payload, sk, vk


def _sign_cart_for(intent_payload: dict, quote: dict, sk) -> dict:
    cart_payload = make_cart_mandate(
        intent_mandate_id=intent_payload["mandate_id"],
        agent_id=intent_payload["agent_id"],
        merchant_id=config.MERCHANT_ID,
        quote_id=quote["quote_id"],
        cart_hash=quote["cart_hash"],
        total_paise=quote["total_paise"],
    )
    return sign(cart_payload, sk)


# --- search_catalog -----------------------------------------------------------


def test_search_catalog_returns_products(merchant):
    products = search_catalog("running")
    assert isinstance(products, list)
    assert len(products) > 0
    for product in products:
        assert "sku" in product
        assert isinstance(product["price_paise"], int)
        assert not isinstance(product["price_paise"], float)


def test_search_catalog_empty_query_returns_everything(merchant):
    products = search_catalog("")
    assert len(products) > 0


def test_search_catalog_no_match_returns_empty_list(merchant):
    assert search_catalog("zzzz-nonsense-term-nobody-sells") == []


# --- get_quote ------------------------------------------------------------


def test_get_quote_is_well_formed(merchant):
    quote = get_quote([{"sku": FOOTWEAR_SKU, "qty": 1}])
    assert "quote_id" in quote
    assert "cart_hash" in quote
    assert "expires_at" in quote
    assert isinstance(quote["total_paise"], int)
    assert not isinstance(quote["total_paise"], bool)
    assert not isinstance(quote["total_paise"], float)


def test_get_quote_unknown_sku_returns_error_not_a_quote(merchant):
    result = get_quote([{"sku": "NOT-A-REAL-SKU", "qty": 1}])
    assert "error" in result
    assert "quote_id" not in result


def test_get_quote_out_of_stock_returns_error(merchant):
    result = get_quote([{"sku": FOOTWEAR_SKU, "qty": 10_000}])
    assert "error" in result
    assert "quote_id" not in result


# --- checkout: the Gate is enforced through MCP, not re-implemented -----------


def test_checkout_valid_envelope_passes_and_creates_real_order(merchant):
    quote = get_quote([{"sku": FOOTWEAR_SKU, "qty": 1}])
    intent_payload, sk, _vk = _grant_intent(max_paise=10_000_00)
    envelope = _sign_cart_for(intent_payload, quote, sk)

    result = checkout(envelope)

    assert result["passed"] is True
    assert result["reason_code"] is None
    assert "order_id" in result
    assert result["pay_url"] == f"/pay/{result['order_id']}"
    assert isinstance(result["total_paise"], int)
    assert result["total_paise"] == quote["total_paise"]


def test_checkout_over_limit_cart_is_refused_with_correct_code(merchant):
    quote = get_quote([{"sku": FOOTWEAR_SKU, "qty": 1}])
    intent_payload, sk, _vk = _grant_intent(max_paise=1000)  # far below the quoted total
    envelope = _sign_cart_for(intent_payload, quote, sk)

    result = checkout(envelope)

    assert result["passed"] is False
    assert result["reason_code"] == "OVER_LIMIT"
    assert "order_id" not in result


def test_checkout_tampered_signing_key_is_refused_sig_invalid(merchant):
    """A cart mandate carrying the right intent_mandate_id and agent_id, but
    signed by a key OTHER than the one the intent bound -- the exact
    agent-key-impersonation gap merchant/gate.py's fix closes (see
    CLAUDE.md, 'Phase 6 already surfaced one real finding'). MCP must not
    let this through any more leniently than the HTTP API does: same
    gate.check() call, same refusal."""
    quote = get_quote([{"sku": FOOTWEAR_SKU, "qty": 1}])
    intent_payload, _sk, _vk = _grant_intent(max_paise=10_000_00)
    forged_sk, _forged_vk = generate_keypair()  # NOT the key bound in the intent

    envelope = _sign_cart_for(intent_payload, quote, forged_sk)
    result = checkout(envelope)

    assert result["passed"] is False
    assert result["reason_code"] == "SIG_INVALID"
    assert "order_id" not in result


def test_checkout_unknown_quote_id_is_refused(merchant):
    intent_payload, sk, _vk = _grant_intent(max_paise=10_000_00)
    cart_payload = make_cart_mandate(
        intent_mandate_id=intent_payload["mandate_id"],
        agent_id=intent_payload["agent_id"],
        merchant_id=config.MERCHANT_ID,
        quote_id="qt_never_issued",
        cart_hash="0" * 64,
        total_paise=100,
    )
    envelope = sign(cart_payload, sk)

    result = checkout(envelope)

    assert result["passed"] is False
    assert result["reason_code"] == "QUOTE_NOT_FOUND"


# --- buy: the demo end-to-end tool --------------------------------------------


def test_buy_end_to_end_creates_a_real_order(merchant):
    demo_sk, demo_vk = generate_keypair()
    save_keypair(demo_sk, config.BUYER_AGENT_KEY_NAME)
    intent_payload, _sk, _vk = _grant_intent(max_paise=10_000_00, agent_pubkey_hex=demo_vk.encode().hex())

    result = buy(FOOTWEAR_SKU, 1, intent_payload["mandate_id"])

    assert result["passed"] is True
    assert "order_id" in result
    assert isinstance(result["total_paise"], int)


def test_buy_unknown_intent_returns_demo_error_not_a_gate_refusal(merchant):
    result = buy(FOOTWEAR_SKU, 1, "man_int_does_not_exist")

    assert result["passed"] is False
    assert result["reason_code"] == "DEMO_INTENT_NOT_FOUND"


def test_buy_missing_signing_key_returns_demo_error(merchant):
    # No save_keypair call -- config.BUYER_AGENT_KEY_NAME has no key on disk
    # under the isolated tmp_path KEY_DIR.
    intent_payload, _sk, _vk = _grant_intent(max_paise=10_000_00)

    result = buy(FOOTWEAR_SKU, 1, intent_payload["mandate_id"])

    assert result["passed"] is False
    assert result["reason_code"] == "DEMO_SIGNING_KEY_MISSING"


def test_buy_intent_bound_to_a_different_key_is_refused_by_the_gate(merchant):
    """buy() always signs with config.BUYER_AGENT_KEY_NAME. If the intent it
    is told to use was bound to some OTHER key, the Gate -- not buy() itself
    -- is what refuses it. Proves buy() doesn't quietly self-authorize."""
    demo_sk, _demo_vk = generate_keypair()
    save_keypair(demo_sk, config.BUYER_AGENT_KEY_NAME)

    other_sk, other_vk = generate_keypair()
    intent_payload, _sk, _vk = _grant_intent(max_paise=10_000_00, agent_pubkey_hex=other_vk.encode().hex())

    result = buy(FOOTWEAR_SKU, 1, intent_payload["mandate_id"])

    assert result["passed"] is False
    assert result["reason_code"] == "SIG_INVALID"


def test_buy_over_limit_is_refused_with_correct_code(merchant):
    demo_sk, demo_vk = generate_keypair()
    save_keypair(demo_sk, config.BUYER_AGENT_KEY_NAME)
    intent_payload, _sk, _vk = _grant_intent(max_paise=1000, agent_pubkey_hex=demo_vk.encode().hex())

    result = buy(FOOTWEAR_SKU, 1, intent_payload["mandate_id"])

    assert result["passed"] is False
    assert result["reason_code"] == "OVER_LIMIT"


# --- money discipline: no float ever touches a monetary field -----------------


def test_no_float_anywhere_in_quote_or_checkout_output(merchant):
    quote = get_quote([{"sku": FOOTWEAR_SKU, "qty": 1}])
    assert isinstance(quote["total_paise"], int)
    assert not isinstance(quote["total_paise"], float)

    intent_payload, sk, _vk = _grant_intent(max_paise=10_000_00)
    envelope = _sign_cart_for(intent_payload, quote, sk)
    result = checkout(envelope)

    assert isinstance(result["total_paise"], int)
    assert not isinstance(result["total_paise"], float)


def test_no_float_anywhere_in_buy_output(merchant):
    demo_sk, demo_vk = generate_keypair()
    save_keypair(demo_sk, config.BUYER_AGENT_KEY_NAME)
    intent_payload, _sk, _vk = _grant_intent(max_paise=10_000_00, agent_pubkey_hex=demo_vk.encode().hex())

    result = buy(FOOTWEAR_SKU, 1, intent_payload["mandate_id"])

    assert isinstance(result["total_paise"], int)
    assert not isinstance(result["total_paise"], float)
